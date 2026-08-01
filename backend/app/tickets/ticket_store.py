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


def allowed_transitions(status: str) -> list[str]:
    """从某个状态能流转到哪些状态，按 VALID_STATUSES 的顺序。界面按它出按钮。"""
    targets = _TRANSITIONS.get(status, set())
    return [s for s in VALID_STATUSES if s in targets]

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

-- 待发送记录（outbox）。工单落库和"通知出去了"是两件事，原先第二件没有任何
-- 记录：`_notify_ticket_webhook` 发一次、异常吞掉、连 HTTP 状态都不看。
-- 于是「客户被告知已转人工」和「值班的人真的收到了」之间没有任何东西连着。
--
-- `id` 同时是**幂等键**（随 Idempotency-Key 发出去），`ticket_id` 上的唯一索引
-- 保证同一个工单只入队一次——重试重发的是同一条记录，不是新造一条。
CREATE TABLE IF NOT EXISTS ticket_notifications (
    id              TEXT PRIMARY KEY,
    ticket_id       TEXT NOT NULL,
    url             TEXT NOT NULL,
    payload         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    last_error      TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_notif_ticket ON ticket_notifications (ticket_id);
CREATE INDEX IF NOT EXISTS idx_notif_due ON ticket_notifications (status, next_attempt_at);
"""

NOTIFY_STATUSES = ("pending", "delivered", "failed")


@dataclass
class Delivery:
    """一条待发送/已发送的工单通知。`id` 同时是发给接收方的幂等键。"""

    id:              str
    ticket_id:       str
    url:             str
    payload:         str
    status:          str = "pending"
    attempts:        int = 0
    next_attempt_at: float = 0.0
    last_error:      str = ""
    created_at:      float = field(default_factory=time.time)
    updated_at:      float = field(default_factory=time.time)


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

    # ── 通知 outbox ───────────────────────────────────────────────────────────

    async def notify_enqueue(self, ticket_id: str, url: str, payload: str) -> Delivery | None:
        """
        登记一条待发送通知。已经登记过就返回 `None`——**去重靠唯一索引，
        不靠调用方记得只调一次**。
        """
        return await asyncio.to_thread(self._notify_enqueue, ticket_id, url, payload)

    def _notify_enqueue(self, ticket_id: str, url: str, payload: str) -> Delivery | None:
        d = Delivery(id=f"nt_{uuid.uuid4().hex[:12]}", ticket_id=ticket_id,
                     url=url, payload=payload, next_attempt_at=time.time())
        with self._lock:
            try:
                self._conn().execute(
                    "INSERT INTO ticket_notifications (id, ticket_id, url, payload, status,"
                    " attempts, next_attempt_at, last_error, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (d.id, d.ticket_id, d.url, d.payload, d.status, d.attempts,
                     d.next_attempt_at, d.last_error, d.created_at, d.updated_at),
                )
                self._conn().commit()
            except sqlite3.IntegrityError:
                return None
        return d

    async def notify_due(self, now: float, limit: int = 20) -> list[Delivery]:
        """到期该发的那些。进程重启后靠它把积压捡回来。"""
        return await asyncio.to_thread(self._notify_due, now, limit)

    def _notify_due(self, now: float, limit: int) -> list[Delivery]:
        with self._lock:
            rows = self._conn().execute(
                "SELECT * FROM ticket_notifications WHERE status = 'pending'"
                " AND next_attempt_at <= ? ORDER BY next_attempt_at LIMIT ?",
                (now, limit),
            ).fetchall()
        return [Delivery(**{k: r[k] for k in r.keys()}) for r in rows]

    async def notify_mark(self, delivery_id: str, status: str, *, attempts: int,
                          next_attempt_at: float = 0.0, last_error: str = "") -> None:
        await asyncio.to_thread(self._notify_mark, delivery_id, status,
                                attempts, next_attempt_at, last_error)

    def _notify_mark(self, delivery_id: str, status: str, attempts: int,
                     next_attempt_at: float, last_error: str) -> None:
        with self._lock:
            self._conn().execute(
                "UPDATE ticket_notifications SET status = ?, attempts = ?,"
                " next_attempt_at = ?, last_error = ?, updated_at = ? WHERE id = ?",
                (status, attempts, next_attempt_at, last_error[:500], time.time(), delivery_id),
            )
            self._conn().commit()

    async def notify_for(self, ticket_ids: list[str]) -> dict[str, Delivery]:
        """工单 id → 它的通知记录。没有记录的工单不在返回里（通知没配，或没入队）。"""
        return await asyncio.to_thread(self._notify_for, ticket_ids) if ticket_ids else {}

    def _notify_for(self, ticket_ids: list[str]) -> dict[str, Delivery]:
        marks = ",".join("?" * len(ticket_ids))
        with self._lock:
            rows = self._conn().execute(
                f"SELECT * FROM ticket_notifications WHERE ticket_id IN ({marks})", ticket_ids
            ).fetchall()
        return {r["ticket_id"]: Delivery(**{k: r[k] for k in r.keys()}) for r in rows}

    async def notify_stats(self) -> dict[str, int]:
        return await asyncio.to_thread(self._notify_stats)

    def _notify_stats(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn().execute(
                "SELECT status, COUNT(*) AS n FROM ticket_notifications GROUP BY status"
            ).fetchall()
        result = {s: 0 for s in NOTIFY_STATUSES}
        for row in rows:
            result[row["status"]] = row["n"]
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

    # ── 通知 outbox（与 SQLite 版同构，说明见那边）──────────────────────────

    async def notify_enqueue(self, ticket_id: str, url: str, payload: str) -> Delivery | None:
        d = Delivery(id=f"nt_{uuid.uuid4().hex[:12]}", ticket_id=ticket_id,
                     url=url, payload=payload, next_attempt_at=time.time())
        # ON CONFLICT DO NOTHING + RETURNING：冲突时不返回行，等价于 SQLite 那边
        # 的 IntegrityError 分支。去重靠 ticket_id 的唯一索引，不靠调用方自觉。
        row = await self._db.fetchrow(
            "INSERT INTO ticket_notifications (id, ticket_id, url, payload, status,"
            " attempts, next_attempt_at, last_error, created_at, updated_at)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)"
            " ON CONFLICT (ticket_id) DO NOTHING RETURNING id",
            d.id, d.ticket_id, d.url, d.payload, d.status, d.attempts,
            d.next_attempt_at, d.last_error, d.created_at, d.updated_at,
        )
        return d if row else None

    async def notify_due(self, now: float, limit: int = 20) -> list[Delivery]:
        rows = await self._db.fetch(
            "SELECT * FROM ticket_notifications WHERE status = 'pending'"
            " AND next_attempt_at <= $1 ORDER BY next_attempt_at LIMIT $2",
            now, limit,
        )
        return [Delivery(**dict(r)) for r in rows]

    async def notify_mark(self, delivery_id: str, status: str, *, attempts: int,
                          next_attempt_at: float = 0.0, last_error: str = "") -> None:
        await self._db.execute(
            "UPDATE ticket_notifications SET status = $1, attempts = $2,"
            " next_attempt_at = $3, last_error = $4, updated_at = $5 WHERE id = $6",
            status, attempts, next_attempt_at, last_error[:500], time.time(), delivery_id,
        )

    async def notify_for(self, ticket_ids: list[str]) -> dict[str, Delivery]:
        if not ticket_ids:
            return {}
        rows = await self._db.fetch(
            "SELECT * FROM ticket_notifications WHERE ticket_id = ANY($1::text[])", ticket_ids
        )
        return {r["ticket_id"]: Delivery(**dict(r)) for r in rows}

    async def notify_stats(self) -> dict[str, int]:
        rows = await self._db.fetch(
            "SELECT status, COUNT(*) AS n FROM ticket_notifications GROUP BY status"
        )
        result = {s: 0 for s in NOTIFY_STATUSES}
        for row in rows:
            result[row["status"]] = row["n"]
        return result

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
