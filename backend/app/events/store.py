"""
事件日志的存储和查询。两种：

- `PgEventStore`：`turn_events` 表（建表语句在 storage/pg.py 的 _SCHEMA 里）。写入由
  `EventWriter` 在后台攒批调 `insert`。
- `MemoryEventStore`：没有 Postgres 时（SQLite 演示模式、单测）存进程内最近 200 轮，
  重启就没了。它同时就是写入器：`put` 直接放进环形缓冲。

**实例隔离做在构造函数上**：`PgEventStore(db, instance="staff")`，所有查询和清理的 WHERE
条件都由它补上，调用方传不进别的实例。两个实例共用一个库、一张表，`conv_id` 又是客户端
带来的，可能撞上，所以每个查询都带 instance。
"""
from __future__ import annotations

import dataclasses
import json
import logging
from collections import OrderedDict

from app.events.recorder import EventLogStats
from app.events.types import COLUMNS, Event, event_from_row

logger = logging.getLogger(__name__)

# 保留任务每批处理的行数：一条语句锁的行不多，别的写入不用等它
PURGE_BATCH = 5000

_INSERT = (
    f"INSERT INTO turn_events ({', '.join(COLUMNS)})"
    " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13::jsonb, $14::jsonb)"
    # 主键是 (turn_id, seq)：重试同一批时，第一次其实已经写进去的那些不会写出重复行
    " ON CONFLICT (turn_id, seq) DO NOTHING"
)
_SELECT = f"SELECT {', '.join(COLUMNS)} FROM turn_events"


def _affected(status: str | None) -> int:
    """asyncpg 的 'UPDATE 5000' / 'DELETE 12' → 行数。"""
    try:
        return int((status or "").split()[-1])
    except (ValueError, IndexError):
        return 0


class PgEventStore:
    """`turn_events` 表上本实例的那一部分。"""

    backend = "postgres"

    def __init__(self, db, instance: str):
        self._db = db
        self.instance = instance

    async def insert(self, events: list[Event]) -> None:
        await self._db.executemany(_INSERT, [e.row() for e in events])

    async def events_of(self, turn_id: str) -> list[Event]:
        rows = await self._db.fetch(
            f"{_SELECT} WHERE instance = $1 AND turn_id = $2 ORDER BY seq",
            self.instance, turn_id,
        )
        return [event_from_row(r) for r in rows]

    async def events_of_turns(self, turn_ids: list[str]) -> dict[str, list[Event]]:
        """几轮的事件，一次查完。没有事件的轮次不在返回里。"""
        if not turn_ids:
            return {}
        rows = await self._db.fetch(
            f"{_SELECT} WHERE instance = $1 AND turn_id = ANY($2::text[]) ORDER BY turn_id, seq",
            self.instance, list(turn_ids),
        )
        out: dict[str, list[Event]] = {}
        for row in rows:
            out.setdefault(row["turn_id"], []).append(event_from_row(row))
        return out

    async def turn_ids_of(self, conv_id: str, limit: int = 50) -> list[str]:
        """一个会话最近的 limit 轮，新的在前。按每轮最早的一条算开始时间，开头那条丢了也找得到。"""
        rows = await self._db.fetch(
            "SELECT turn_id, MIN(ts) AS started FROM turn_events"
            " WHERE instance = $1 AND conv_id = $2"
            " GROUP BY turn_id ORDER BY started DESC LIMIT $3",
            self.instance, conv_id, limit,
        )
        return [r["turn_id"] for r in rows]

    async def turn_ids_between(self, since: float, until: float | None = None, limit: int = 1000) -> list[str]:
        """这段时间里开始的轮次，先开始的在前。运维检查和重放按时间段挑轮次用。"""
        rows = await self._db.fetch(
            "SELECT turn_id, MIN(ts) AS started FROM turn_events"
            " WHERE instance = $1 AND ts >= $2 AND ($3::double precision IS NULL OR ts < $3)"
            " GROUP BY turn_id ORDER BY started LIMIT $4",
            self.instance, since, until, limit,
        )
        return [r["turn_id"] for r in rows]

    async def conv_ids_of_user(self, user_id: str) -> list[str]:
        """这个用户的会话（按 turn_started 原文里的 user_id 找）。原文清空之后就找不到了。"""
        rows = await self._db.fetch(
            "SELECT DISTINCT conv_id FROM turn_events WHERE instance = $1 AND type = 'turn_started'"
            " AND body->>'user_id' = $2",
            self.instance, user_id,
        )
        return [r["conv_id"] for r in rows]

    async def delete_conversation(self, conv_id: str) -> int:
        """删掉本实例这个会话的全部事件（删除请求）。返回删了多少行。"""
        return _affected(await self._db.execute(
            "DELETE FROM turn_events WHERE instance = $1 AND conv_id = $2", self.instance, conv_id))

    async def purge_bodies(self, before_ts: float, batch: int = PURGE_BATCH) -> int:
        """把 before_ts 之前的原文清空成 NULL，结构留着。分批，返回清了多少行。"""
        total = 0
        while True:
            n = _affected(await self._db.execute(
                "UPDATE turn_events SET body = NULL WHERE (turn_id, seq) IN ("
                " SELECT turn_id, seq FROM turn_events"
                " WHERE instance = $1 AND ts < $2 AND body IS NOT NULL LIMIT $3)",
                self.instance, before_ts, batch,
            ))
            total += n
            if n < batch:
                return total

    async def delete_rows(self, before_ts: float, batch: int = PURGE_BATCH) -> int:
        """删掉 before_ts 之前的整行。分批，返回删了多少行。"""
        total = 0
        while True:
            n = _affected(await self._db.execute(
                "DELETE FROM turn_events WHERE (turn_id, seq) IN ("
                " SELECT turn_id, seq FROM turn_events"
                " WHERE instance = $1 AND ts < $2 LIMIT $3)",
                self.instance, before_ts, batch,
            ))
            total += n
            if n < batch:
                return total


class MemoryEventStore:
    """进程内的环形缓冲：最近 max_turns 轮。接口和 PgEventStore 一致，另外它自己就是写入器。"""

    backend = "memory"

    def __init__(self, instance: str, max_turns: int = 200, stats: EventLogStats | None = None):
        self.instance = instance
        self._max = max_turns
        self._turns: OrderedDict[str, list[Event]] = OrderedDict()
        self._stats = stats if stats is not None else EventLogStats()

    @property
    def depth(self) -> int:
        return 0

    def put(self, event: Event) -> None:
        turn = self._turns.get(event.turn_id)
        if turn is None:
            turn = self._turns[event.turn_id] = []
            while len(self._turns) > self._max:
                self._turns.popitem(last=False)
        if any(e.seq == event.seq for e in turn):
            return              # 和 Postgres 的主键一样：同一轮同一个 seq 只留第一条（补事件会补两次）
        turn.append(event)
        self._stats.wrote(1)

    async def insert(self, events: list[Event]) -> None:
        for event in events:
            turn = self._turns.setdefault(event.turn_id, [])
            if all(e.seq != event.seq for e in turn):
                turn.append(event)

    async def events_of(self, turn_id: str) -> list[Event]:
        return sorted((e for e in self._turns.get(turn_id, []) if e.instance == self.instance),
                      key=lambda e: e.seq)

    async def events_of_turns(self, turn_ids: list[str]) -> dict[str, list[Event]]:
        out = {turn_id: await self.events_of(turn_id) for turn_id in turn_ids}
        return {turn_id: events for turn_id, events in out.items() if events}

    async def turn_ids_of(self, conv_id: str, limit: int = 50) -> list[str]:
        started = {
            turn_id: min(e.ts for e in turn)
            for turn_id, turn in self._turns.items()
            if turn and turn[0].instance == self.instance and turn[0].conv_id == conv_id
        }
        return sorted(started, key=started.__getitem__, reverse=True)[:limit]

    async def turn_ids_between(self, since: float, until: float | None = None, limit: int = 1000) -> list[str]:
        started = {
            turn_id: min(e.ts for e in turn)
            for turn_id, turn in self._turns.items() if turn and turn[0].instance == self.instance
        }
        hits = [t for t, ts in started.items() if ts >= since and (until is None or ts < until)]
        return sorted(hits, key=started.__getitem__)[:limit]

    async def conv_ids_of_user(self, user_id: str) -> list[str]:
        found = []
        for turn in self._turns.values():
            for e in turn:
                if (e.instance == self.instance and e.type == "turn_started" and e.body
                        and json.loads(e.body).get("user_id") == user_id and e.conv_id not in found):
                    found.append(e.conv_id)
        return found

    async def delete_conversation(self, conv_id: str) -> int:
        total = 0
        for turn_id in list(self._turns):
            turn = self._turns[turn_id]
            kept = [e for e in turn if not (e.instance == self.instance and e.conv_id == conv_id)]
            total += len(turn) - len(kept)
            if kept:
                self._turns[turn_id] = kept
            else:
                del self._turns[turn_id]
        return total

    async def purge_bodies(self, before_ts: float, batch: int = PURGE_BATCH) -> int:
        total = 0
        for turn in self._turns.values():
            for i, e in enumerate(turn):
                if e.instance == self.instance and e.ts < before_ts and e.body is not None:
                    turn[i] = dataclasses.replace(e, body=None)
                    total += 1
        return total

    async def delete_rows(self, before_ts: float, batch: int = PURGE_BATCH) -> int:
        total = 0
        for turn_id in list(self._turns):
            turn = self._turns[turn_id]
            kept = [e for e in turn if not (e.instance == self.instance and e.ts < before_ts)]
            total += len(turn) - len(kept)
            if kept:
                self._turns[turn_id] = kept
            else:
                del self._turns[turn_id]
        return total
