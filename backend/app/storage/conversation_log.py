"""
对话审计日志（append-only）。

核心问题：工作记忆在 Redis 里 24h TTL 即焚，情景记忆只存摘要——
原始对话哪里都没有留档。客服场景下这过不了合规（服务记录留存），
也让坐席接到转人工工单时看不到完整上下文。

与 MemoryManager 的分工：
  - MemoryManager 面向**模型**：为下一轮回答组装上下文，允许压缩、摘要、遗忘；
  - ConversationLog 面向**人和审计**：原文 append-only，不改写不删除，
    工单里的 conv_id 在这里能查回完整对话。

实现与 TicketStore 同一套取舍：SQLite + 单连接 + 锁 + to_thread，
写入在 /chat 响应路径之外无额外 LLM 成本；换 Postgres 只改这一个模块。
保留策略通过 purge_before() 暴露给运维定时任务，不在服务内部悄悄删数据。
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_log (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    conv_id     TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    request_id  TEXT NOT NULL DEFAULT '',
    role        TEXT NOT NULL,             -- user / assistant
    content     TEXT NOT NULL,
    agent_type  TEXT NOT NULL DEFAULT '',  -- assistant 侧：实际作答的 Agent
    created_at  REAL NOT NULL,
    turn_id     TEXT NOT NULL DEFAULT '',  -- 指回那一轮的事件（app/events/）
    instance    TEXT NOT NULL DEFAULT ''   -- staff / customer；加列之前的老记录是空串
);
CREATE INDEX IF NOT EXISTS idx_convlog_conv ON conversation_log (conv_id, seq);
CREATE INDEX IF NOT EXISTS idx_convlog_time ON conversation_log (created_at);
"""

# 加列之前建的库：CREATE TABLE IF NOT EXISTS 不会改已有的表，SQLite 的 ADD COLUMN
# 又没有 IF NOT EXISTS，所以先看有没有再加
_ADDED_COLUMNS = {
    "turn_id": "TEXT NOT NULL DEFAULT ''",
    "instance": "TEXT NOT NULL DEFAULT ''",
}


# 读出来的列只取 LogEntry 有的：表上以后再加列，旧代码按 `**行` 构造对象就会报错，
# 这里不重演（两个实例共用一个库，总有一个先升级）
def _entry(row: Any) -> LogEntry:
    return LogEntry(**{k: row[k] for k in _ENTRY_FIELDS if k in row.keys()})


@dataclass
class LogEntry:
    seq:        int
    conv_id:    str
    user_id:    str
    request_id: str
    role:       str
    content:    str
    agent_type: str
    created_at: float
    turn_id:    str = ""
    instance:   str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "conv_id": self.conv_id,
            "user_id": self.user_id,
            "request_id": self.request_id,
            "role": self.role,
            "content": self.content,
            "agent_type": self.agent_type,
            "created_at": self.created_at,
            "turn_id": self.turn_id,
            "instance": self.instance,
        }


_ENTRY_FIELDS = tuple(f.name for f in fields(LogEntry))


class ConversationLog:
    """SQLite 对话审计日志。公开方法均为 async，内部走线程池。"""

    def __init__(self, db_path: str = "./data/conversations.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, timeout=10.0, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)
            existing = {row["name"] for row in self._db.execute("PRAGMA table_info(conversation_log)")}
            for column, ddl in _ADDED_COLUMNS.items():
                if column not in existing:
                    self._db.execute(f"ALTER TABLE conversation_log ADD COLUMN {column} {ddl}")
            self._db.commit()

    async def append(
        self,
        conv_id: str,
        user_id: str,
        role: str,
        content: str,
        request_id: str = "",
        agent_type: str = "",
        turn_id: str = "",
        instance: str = "",
    ) -> None:
        """追加一条记录。审计写入失败不能影响主链路，只记错误。"""
        try:
            await asyncio.to_thread(
                self._append, conv_id, user_id, role, content, request_id, agent_type,
                turn_id, instance,
            )
        except Exception:
            logger.exception("对话审计日志写入失败")

    def _append(
        self, conv_id: str, user_id: str, role: str,
        content: str, request_id: str, agent_type: str,
        turn_id: str, instance: str,
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO conversation_log (conv_id, user_id, request_id, role, content,"
                " agent_type, created_at, turn_id, instance) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (conv_id, user_id, request_id, role, content, agent_type, time.time(),
                 turn_id, instance),
            )
            self._db.commit()

    async def get_conversation(
        self, conv_id: str, limit: int = 500, instance: str | None = None,
    ) -> list[LogEntry]:
        """
        按时间顺序取回一次对话的完整原文（坐席接工单时用）。

        给了 instance 时只返回这个实例的记录，加上加列之前的老记录（实例为空串，分不出是
        谁的，照旧返回）。
        """
        return await asyncio.to_thread(self._get_conversation, conv_id, limit, instance)

    def _get_conversation(self, conv_id: str, limit: int, instance: str | None) -> list[LogEntry]:
        with self._lock:
            if instance is None:
                rows = self._db.execute(
                    "SELECT * FROM conversation_log WHERE conv_id = ? ORDER BY seq LIMIT ?",
                    (conv_id, limit),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM conversation_log WHERE conv_id = ? AND instance IN (?, '')"
                    " ORDER BY seq LIMIT ?",
                    (conv_id, instance, limit),
                ).fetchall()
        return [_entry(r) for r in rows]

    async def stats(self) -> dict[str, int]:
        return await asyncio.to_thread(self._stats)

    def _stats(self) -> dict[str, int]:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS messages, COUNT(DISTINCT conv_id) AS conversations"
                " FROM conversation_log"
            ).fetchone()
        return {"messages": row["messages"], "conversations": row["conversations"]}

    async def purge_before(self, cutoff_ts: float) -> int:
        """
        删除 cutoff_ts 之前的记录，返回删除条数。

        保留期由运维决定（合规要求因行业而异），服务本身不自动删——
        审计数据的删除必须是显式操作。
        """
        return await asyncio.to_thread(self._purge_before, cutoff_ts)

    def _purge_before(self, cutoff_ts: float) -> int:
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM conversation_log WHERE created_at < ?", (cutoff_ts,)
            )
            self._db.commit()
        logger.info(f"审计日志清理: 删除 {cur.rowcount} 条早于 {cutoff_ts} 的记录")
        return cur.rowcount

    async def close(self) -> None:
        def _close() -> None:
            with self._lock:
                self._db.close()
        await asyncio.to_thread(_close)


_PG_SELECT = (
    "SELECT c.seq, c.conv_id, c.user_id, c.request_id, c.role, c.content, c.agent_type, c.created_at,"
    " COALESCE(t.turn_id, '') AS turn_id, COALESCE(t.instance, '') AS instance"
    " FROM conversation_log c LEFT JOIN conversation_log_turns t ON t.seq = c.seq"
)


class PgConversationLog:
    """
    Postgres 版对话审计日志，接口与 ConversationLog 一致。

    turn_id 和 instance 存在旁边的 conversation_log_turns 里，不在 conversation_log 上加列：
    两个实例共用这个库，旧镜像按 `SELECT *` 读这张表（见 storage/pg.py）。
    """

    def __init__(self, db):
        self._db = db

    async def append(
        self,
        conv_id: str,
        user_id: str,
        role: str,
        content: str,
        request_id: str = "",
        agent_type: str = "",
        turn_id: str = "",
        instance: str = "",
    ) -> None:
        try:
            async with self._db.transaction() as conn:
                seq = await conn.fetchval(
                    "INSERT INTO conversation_log"
                    " (conv_id, user_id, request_id, role, content, agent_type, created_at)"
                    " VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING seq",
                    conv_id, user_id, request_id, role, content, agent_type, time.time(),
                )
                if turn_id or instance:
                    await conn.execute(
                        "INSERT INTO conversation_log_turns (seq, turn_id, instance) VALUES ($1, $2, $3)",
                        seq, turn_id, instance,
                    )
        except Exception:
            logger.exception("对话审计日志写入失败")

    async def get_conversation(
        self, conv_id: str, limit: int = 500, instance: str | None = None,
    ) -> list[LogEntry]:
        if instance is None:
            rows = await self._db.fetch(
                f"{_PG_SELECT} WHERE c.conv_id = $1 ORDER BY c.seq LIMIT $2", conv_id, limit,
            )
        else:
            rows = await self._db.fetch(
                f"{_PG_SELECT} WHERE c.conv_id = $1 AND COALESCE(t.instance, '') IN ($2, '')"
                " ORDER BY c.seq LIMIT $3",
                conv_id, instance, limit,
            )
        return [_entry(r) for r in rows]

    async def stats(self) -> dict[str, int]:
        row = await self._db.fetchrow(
            "SELECT COUNT(*) AS messages, COUNT(DISTINCT conv_id) AS conversations"
            " FROM conversation_log"
        )
        return {"messages": row["messages"], "conversations": row["conversations"]}

    async def purge_before(self, cutoff_ts: float) -> int:
        async with self._db.transaction() as conn:
            await conn.execute(
                "DELETE FROM conversation_log_turns WHERE seq IN"
                " (SELECT seq FROM conversation_log WHERE created_at < $1)", cutoff_ts
            )
            result = await conn.execute(
                "DELETE FROM conversation_log WHERE created_at < $1", cutoff_ts
            )
        removed = int(result.split()[-1]) if result else 0
        logger.info(f"审计日志清理: 删除 {removed} 条早于 {cutoff_ts} 的记录")
        return removed

    async def close(self) -> None:
        """连接池归 Database 管。"""
