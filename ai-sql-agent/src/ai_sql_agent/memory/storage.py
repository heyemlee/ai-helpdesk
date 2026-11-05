from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

import numpy as np

from .objects import ConversationSummary, LongTermMemory, MemoryKind, MemoryMessage, MessageRole


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteMemoryStore:
    """SQLite 持久化实现；连接按操作创建，可安全用于摘要后台线程。"""

    def __init__(self, db_path: str | Path = "runtime_data/memory.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_lock = threading.Lock()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._schema_lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    message_type TEXT NOT NULL DEFAULT 'text',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    token_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_messages_session
                    ON memory_messages(user_id, session_id, id);

                CREATE TABLE IF NOT EXISTS conversation_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    through_message_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, session_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_conversation_summary_latest
                    ON conversation_summaries(user_id, session_id, version DESC);

                CREATE TABLE IF NOT EXISTS long_term_memories (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    embedding BLOB NOT NULL,
                    embedding_dimensions INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_long_term_memory_user
                    ON long_term_memories(user_id, kind, created_at DESC);
                """
            )

    def add_message(
        self,
        *,
        session_id: str,
        user_id: str,
        role: MessageRole,
        content: str,
        message_type: str = "text",
        payload: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        token_count: int = 0,
    ) -> MemoryMessage:
        created_at = _utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO memory_messages(
                    session_id, user_id, role, content, message_type,
                    payload_json, metadata_json, token_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    user_id,
                    role.value,
                    content,
                    message_type,
                    json.dumps(payload or {}, ensure_ascii=False, default=str),
                    json.dumps(metadata or {}, ensure_ascii=False, default=str),
                    token_count,
                    created_at,
                ),
            )
            message_id = int(cursor.lastrowid)
        return MemoryMessage(
            id=message_id,
            session_id=session_id,
            user_id=user_id,
            role=role,
            content=content,
            message_type=message_type,
            payload=payload or {},
            metadata=metadata or {},
            token_count=token_count,
            created_at=created_at,
        )

    def list_messages(self, *, user_id: str, session_id: str) -> List[MemoryMessage]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM memory_messages
                   WHERE user_id = ? AND session_id = ? ORDER BY id ASC""",
                (user_id, session_id),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def get_latest_summary(
        self, *, user_id: str, session_id: str
    ) -> Optional[ConversationSummary]:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM conversation_summaries
                   WHERE user_id = ? AND session_id = ?
                   ORDER BY version DESC LIMIT 1""",
                (user_id, session_id),
            ).fetchone()
        if row is None:
            return None
        return ConversationSummary(
            id=int(row["id"]),
            session_id=row["session_id"],
            user_id=row["user_id"],
            version=int(row["version"]),
            content=row["content"],
            through_message_id=int(row["through_message_id"]),
            created_at=row["created_at"],
        )

    def save_summary(
        self,
        *,
        user_id: str,
        session_id: str,
        content: str,
        through_message_id: int,
    ) -> ConversationSummary:
        created_at = _utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT COALESCE(MAX(version), 0) AS version
                   FROM conversation_summaries
                   WHERE user_id = ? AND session_id = ?""",
                (user_id, session_id),
            ).fetchone()
            version = int(row["version"]) + 1
            cursor = conn.execute(
                """INSERT INTO conversation_summaries(
                       session_id, user_id, version, content,
                       through_message_id, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, user_id, version, content, through_message_id, created_at),
            )
            summary_id = int(cursor.lastrowid)
        return ConversationSummary(
            id=summary_id,
            session_id=session_id,
            user_id=user_id,
            version=version,
            content=content,
            through_message_id=through_message_id,
            created_at=created_at,
        )

    def save_long_term_memory(
        self,
        *,
        user_id: str,
        kind: MemoryKind,
        summary: str,
        content: Any,
        metadata: Dict[str, Any],
        embedding: np.ndarray,
        memory_id: Optional[str] = None,
    ) -> LongTermMemory:
        memory_id = memory_id or str(uuid.uuid4())
        created_at = _utc_now()
        vector = np.asarray(embedding, dtype=np.float32).reshape(-1)
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO long_term_memories(
                       id, user_id, kind, summary, content_json, metadata_json,
                       embedding, embedding_dimensions, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    memory_id,
                    user_id,
                    kind.value,
                    summary,
                    json.dumps(content, ensure_ascii=False, default=str),
                    json.dumps(metadata, ensure_ascii=False, default=str),
                    vector.tobytes(),
                    int(vector.size),
                    created_at,
                    created_at,
                ),
            )
        return LongTermMemory(
            id=memory_id,
            user_id=user_id,
            kind=kind,
            summary=summary,
            content=content,
            metadata=metadata,
            embedding=vector,
            created_at=created_at,
            updated_at=created_at,
        )

    def list_long_term_memories(
        self, *, user_id: str, kinds: Optional[Sequence[MemoryKind]] = None
    ) -> List[LongTermMemory]:
        params: List[Any] = [user_id]
        sql = "SELECT * FROM long_term_memories WHERE user_id = ?"
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            sql += f" AND kind IN ({placeholders})"
            params.extend(kind.value for kind in kinds)
        sql += " ORDER BY created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_long_term(row) for row in rows]

    def get_long_term_memory(self, *, user_id: str, memory_id: str) -> Optional[LongTermMemory]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM long_term_memories WHERE user_id = ? AND id = ?",
                (user_id, memory_id),
            ).fetchone()
        return self._row_to_long_term(row) if row else None

    def delete_long_term_memory(self, *, user_id: str, memory_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM long_term_memories WHERE user_id = ? AND id = ?",
                (user_id, memory_id),
            )
        return cursor.rowcount > 0

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> MemoryMessage:
        return MemoryMessage(
            id=int(row["id"]),
            session_id=row["session_id"],
            user_id=row["user_id"],
            role=MessageRole(row["role"]),
            content=row["content"],
            message_type=row["message_type"],
            payload=json.loads(row["payload_json"]),
            metadata=json.loads(row["metadata_json"]),
            token_count=int(row["token_count"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_long_term(row: sqlite3.Row) -> LongTermMemory:
        dimensions = int(row["embedding_dimensions"])
        embedding = np.frombuffer(row["embedding"], dtype=np.float32, count=dimensions).copy()
        return LongTermMemory(
            id=row["id"],
            user_id=row["user_id"],
            kind=MemoryKind(row["kind"]),
            summary=row["summary"],
            content=json.loads(row["content_json"]),
            metadata=json.loads(row["metadata_json"]),
            embedding=embedding,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
