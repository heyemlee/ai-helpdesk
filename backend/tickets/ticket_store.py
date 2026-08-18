"""
转人工工单持久化。

核心问题：原来的转人工只有一句 logger.warning——用户被告知"已转交人工客服"，
但没有任何系统记录这件事，24h 后 Redis TTL 一过连对话都找不回来。
对客服产品来说 handoff 是核心业务动作，必须落库、可查询、可流转。

实现取舍：
  - SQLite + 标准库 sqlite3，不引入 ORM 和额外依赖。写入频率 = 转人工频率，
    远低于 SQLite 的写入能力；换 Postgres 是改这一个模块的事。
  - sqlite3 是同步库，所有操作放进线程池（asyncio.to_thread），
    与项目里其他同步库的处理方式一致，不阻塞事件循环。
  - 状态机只有 open → assigned → resolved / cancelled 四态，
    不做更细的流转——那是工单系统的事，这里只保证"转了就有记录"。
"""
from __future__ import annotations  # list 方法名会遮蔽内建 list 的注解求值

import asyncio
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

VALID_STATUSES = ("open", "assigned", "resolved", "cancelled")

# 合法的状态流转。open 可以直接 resolved（坐席秒处理）也可以 cancelled（用户撤回）。
_TRANSITIONS: dict[str, set[str]] = {
    "open":     {"assigned", "resolved", "cancelled"},
    "assigned": {"resolved", "cancelled", "open"},   # open：坐席退回队列
    "resolved": set(),
    "cancelled": set(),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id                TEXT PRIMARY KEY,
    conv_id           TEXT NOT NULL,
    user_id           TEXT NOT NULL,
    request_id        TEXT NOT NULL DEFAULT '',
    reason            TEXT NOT NULL,
    intent            TEXT NOT NULL DEFAULT '',
    urgency           TEXT NOT NULL DEFAULT '',
    answer_confidence REAL NOT NULL DEFAULT 0.0,
    last_message      TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'open',
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tickets_status  ON tickets (status, created_at);
CREATE INDEX IF NOT EXISTS idx_tickets_conv    ON tickets (conv_id);
"""


@dataclass
class Ticket:
    id:                str
    conv_id:           str
    user_id:           str
    reason:            str
    request_id:        str = ""
    intent:            str = ""
    urgency:           str = ""
    answer_confidence: float = 0.0
    last_message:      str = ""
    status:            str = "open"
    created_at:        float = field(default_factory=time.time)
    updated_at:        float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "conv_id": self.conv_id,
            "user_id": self.user_id,
            "request_id": self.request_id,
            "reason": self.reason,
            "intent": self.intent,
            "urgency": self.urgency,
            "answer_confidence": self.answer_confidence,
            "last_message": self.last_message,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class TicketStore:
    """SQLite 工单存储。所有公开方法均为 async，内部走线程池。"""

    def __init__(self, db_path: str = "./data/tickets.db"):
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # 单连接 + 锁：asyncio.to_thread 的线程池会在不同线程间轮转，
        # per-thread 连接会导致"创建线程"和"关闭线程"不一致而报错。
        # 工单写入频率 = 转人工频率，串行化完全无压力。
        self._db = sqlite3.connect(self._db_path, timeout=10.0, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def _conn(self) -> sqlite3.Connection:
        return self._db

    # ── 写入 ──────────────────────────────────────────────────────────────────

    async def create(
        self,
        conv_id: str,
        user_id: str,
        reason: str,
        request_id: str = "",
        intent: str = "",
        urgency: str = "",
        answer_confidence: float = 0.0,
        last_message: str = "",
    ) -> Ticket:
        ticket = Ticket(
            id=f"tk_{uuid.uuid4().hex[:12]}",
            conv_id=conv_id,
            user_id=user_id,
            reason=reason,
            request_id=request_id,
            intent=intent,
            urgency=urgency,
            answer_confidence=answer_confidence,
            last_message=last_message[:500],
        )
        await asyncio.to_thread(self._insert, ticket)
        logger.info(f"工单已创建: {ticket.id} conv={conv_id} reason={reason[:60]}")
        return ticket

    def _insert(self, t: Ticket) -> None:
        with self._lock:
            self._insert_locked(t)

    def _insert_locked(self, t: Ticket) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT INTO tickets (id, conv_id, user_id, request_id, reason, intent,"
            " urgency, answer_confidence, last_message, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (t.id, t.conv_id, t.user_id, t.request_id, t.reason, t.intent,
             t.urgency, t.answer_confidence, t.last_message, t.status,
             t.created_at, t.updated_at),
        )
        conn.commit()

    async def update_status(self, ticket_id: str, status: str) -> Ticket:
        """按状态机流转工单。非法流转抛 ValueError。"""
        if status not in VALID_STATUSES:
            raise ValueError(f"非法状态: {status}，可选 {VALID_STATUSES}")
        return await asyncio.to_thread(self._update_status, ticket_id, status)

    def _update_status(self, ticket_id: str, status: str) -> Ticket:
        with self._lock:
            return self._update_status_locked(ticket_id, status)

    def _update_status_locked(self, ticket_id: str, status: str) -> Ticket:
        conn = self._conn()
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None:
            raise KeyError(ticket_id)
        current = row["status"]
        if status != current and status not in _TRANSITIONS.get(current, set()):
            raise ValueError(f"工单 {ticket_id} 不允许从 {current} 流转到 {status}")
        conn.execute(
            "UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?",
            (status, time.time(), ticket_id),
        )
        conn.commit()
        return self._row_to_ticket(
            conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        )

    # ── 查询 ──────────────────────────────────────────────────────────────────

    async def get(self, ticket_id: str) -> Ticket | None:
        return await asyncio.to_thread(self._get, ticket_id)

    def _get(self, ticket_id: str) -> Ticket | None:
        with self._lock:
            row = self._conn().execute(
                "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
            ).fetchone()
        return self._row_to_ticket(row) if row else None

    async def list(self, status: str | None = None, limit: int = 50) -> list[Ticket]:
        return await asyncio.to_thread(self._list, status, limit)

    def _list(self, status: str | None, limit: int) -> list[Ticket]:
        with self._lock:
            return self._list_locked(status, limit)

    def _list_locked(self, status: str | None, limit: int) -> list[Ticket]:
        conn = self._conn()
        if status:
            rows = conn.execute(
                "SELECT * FROM tickets WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tickets ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_ticket(r) for r in rows]

    async def stats(self) -> dict[str, int]:
        """按状态统计，供 /health 和监控面板使用。"""
        return await asyncio.to_thread(self._stats)

    def _stats(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn().execute(
                "SELECT status, COUNT(*) AS n FROM tickets GROUP BY status"
            ).fetchall()
        result = {s: 0 for s in VALID_STATUSES}
        for row in rows:
            result[row["status"]] = row["n"]
        result["total"] = sum(result.values())
        return result

    @staticmethod
    def _row_to_ticket(row: sqlite3.Row) -> Ticket:
        return Ticket(**{k: row[k] for k in row.keys()})

    async def close(self) -> None:
        def _close() -> None:
            with self._lock:
                self._db.close()
        await asyncio.to_thread(_close)


class PgTicketStore:
    """Postgres 版工单存储，接口与 TicketStore 完全一致。建表由 storage/pg.py 完成。"""

    def __init__(self, db):
        self._db = db

    async def create(
        self,
        conv_id: str,
        user_id: str,
        reason: str,
        request_id: str = "",
        intent: str = "",
        urgency: str = "",
        answer_confidence: float = 0.0,
        last_message: str = "",
    ) -> Ticket:
        ticket = Ticket(
            id=f"tk_{uuid.uuid4().hex[:12]}",
            conv_id=conv_id,
            user_id=user_id,
            reason=reason,
            request_id=request_id,
            intent=intent,
            urgency=urgency,
            answer_confidence=answer_confidence,
            last_message=last_message[:500],
        )
        await self._db.execute(
            "INSERT INTO tickets (id, conv_id, user_id, request_id, reason, intent,"
            " urgency, answer_confidence, last_message, status, created_at, updated_at)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)",
            ticket.id, ticket.conv_id, ticket.user_id, ticket.request_id,
            ticket.reason, ticket.intent, ticket.urgency, ticket.answer_confidence,
            ticket.last_message, ticket.status, ticket.created_at, ticket.updated_at,
        )
        logger.info(f"工单已创建: {ticket.id} conv={conv_id} reason={reason[:60]}")
        return ticket

    async def update_status(self, ticket_id: str, status: str) -> Ticket:
        if status not in VALID_STATUSES:
            raise ValueError(f"非法状态: {status}，可选 {VALID_STATUSES}")
        row = await self._db.fetchrow("SELECT * FROM tickets WHERE id = $1", ticket_id)
        if row is None:
            raise KeyError(ticket_id)
        current = row["status"]
        if status != current and status not in _TRANSITIONS.get(current, set()):
            raise ValueError(f"工单 {ticket_id} 不允许从 {current} 流转到 {status}")
        updated = await self._db.fetchrow(
            "UPDATE tickets SET status = $1, updated_at = $2 WHERE id = $3 RETURNING *",
            status, time.time(), ticket_id,
        )
        return Ticket(**dict(updated))

    async def get(self, ticket_id: str) -> Ticket | None:
        row = await self._db.fetchrow("SELECT * FROM tickets WHERE id = $1", ticket_id)
        return Ticket(**dict(row)) if row else None

    async def list(self, status: str | None = None, limit: int = 50) -> list[Ticket]:
        if status:
            rows = await self._db.fetch(
                "SELECT * FROM tickets WHERE status = $1 ORDER BY created_at DESC LIMIT $2",
                status, limit,
            )
        else:
            rows = await self._db.fetch(
                "SELECT * FROM tickets ORDER BY created_at DESC LIMIT $1", limit
            )
        return [Ticket(**dict(r)) for r in rows]

    async def stats(self) -> dict[str, int]:
        rows = await self._db.fetch(
            "SELECT status, COUNT(*) AS n FROM tickets GROUP BY status"
        )
        result = {s: 0 for s in VALID_STATUSES}
        for row in rows:
            result[row["status"]] = row["n"]
        result["total"] = sum(result.values())
        return result

    async def close(self) -> None:
        """连接池归 Database 管。"""
