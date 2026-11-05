from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional, Protocol, Tuple

from ai_sql_agent.core import get_logger, get_settings, log_event

from .objects import AuditRecord, DailyUsage

_logger = get_logger("audit")

TABLE_NAME = "ai_sql_audit_log"


class AuditStore(Protocol):
    def record(self, record: AuditRecord) -> None: ...

    def daily_usage(self, user_id: str) -> DailyUsage: ...

    def recent(self, limit: int = 50, user_id: str = "") -> List[dict]: ...

    def last_successful_sqls(
        self, user_id: str, query: str
    ) -> List[Tuple[str, str]]: ...

    def get_by_trace(self, trace_id: str) -> Optional[dict]: ...


def _day_start_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


class SQLiteAuditStore:
    """SQLite 审计存储：单实例内部部署的默认选择。"""

    def __init__(self, db_path: str | Path = "runtime_data/audit.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT NOT NULL,
                    run_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    caller TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '',
                    query TEXT NOT NULL,
                    route TEXT NOT NULL DEFAULT '',
                    databases TEXT NOT NULL DEFAULT '[]',
                    tables TEXT NOT NULL DEFAULT '[]',
                    sqls TEXT NOT NULL DEFAULT '[]',
                    denied_tables TEXT NOT NULL DEFAULT '[]',
                    success INTEGER NOT NULL DEFAULT 1,
                    status_code INTEGER NOT NULL DEFAULT 200,
                    failed_steps INTEGER NOT NULL DEFAULT 0,
                    repaired_steps INTEGER NOT NULL DEFAULT 0,
                    duration_ms REAL NOT NULL DEFAULT 0,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    degraded INTEGER NOT NULL DEFAULT 0,
                    has_risk INTEGER NOT NULL DEFAULT 0,
                    execution_status TEXT NOT NULL DEFAULT '',
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_audit_user_time
                    ON {TABLE_NAME}(user_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_audit_trace
                    ON {TABLE_NAME}(trace_id);
                CREATE INDEX IF NOT EXISTS idx_audit_replay
                    ON {TABLE_NAME}(user_id, query, id);
                """
            )
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """轻量迁移：老库缺的列补上，避免升级后写入报错。"""
        existing = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({TABLE_NAME})").fetchall()
        }
        for column, ddl in (
            ("degraded", "INTEGER NOT NULL DEFAULT 0"),
            ("has_risk", "INTEGER NOT NULL DEFAULT 0"),
            ("execution_status", "TEXT NOT NULL DEFAULT ''"),
            ("run_id", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in existing:
                conn.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN {column} {ddl}")

    def record(self, record: AuditRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO {TABLE_NAME} (
                    trace_id, created_at, caller, user_id, session_id, query,
                    route, databases, tables, sqls, denied_tables,
                    success, status_code, failed_steps, repaired_steps,
                    duration_ms, prompt_tokens, completion_tokens, degraded,
                    has_risk, execution_status, error, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.trace_id,
                    record.created_at,
                    record.caller,
                    record.user_id,
                    record.session_id,
                    record.query,
                    record.route,
                    json.dumps(record.databases, ensure_ascii=False),
                    json.dumps(record.tables, ensure_ascii=False),
                    json.dumps(record.sqls, ensure_ascii=False),
                    json.dumps(record.denied_tables, ensure_ascii=False),
                    int(record.success),
                    record.status_code,
                    record.failed_steps,
                    record.repaired_steps,
                    record.duration_ms,
                    record.prompt_tokens,
                    record.completion_tokens,
                    int(record.degraded),
                    int(record.has_risk),
                    record.execution_status,
                    record.error,
                    record.run_id,
                ),
            )

    def last_successful_sqls(self, user_id: str, query: str) -> List[Tuple[str, str]]:
        """取该用户同一问题上次成功执行的 SQL 序列，供模型不可用时重放。"""
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT databases, sqls FROM {TABLE_NAME}
                WHERE user_id = ? AND query = ? AND success = 1
                  AND degraded = 0 AND sqls != '[]'
                ORDER BY id DESC LIMIT 1
                """,
                (user_id, query),
            ).fetchone()

        if not row:
            return []
        return _parse_steps(json.loads(row["databases"]), json.loads(row["sqls"]))

    def daily_usage(self, user_id: str) -> DailyUsage:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS queries,
                       COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens
                FROM {TABLE_NAME}
                WHERE user_id = ? AND created_at >= ?
                """,
                (user_id, _day_start_iso()),
            ).fetchone()
        return DailyUsage(
            user_id=user_id, queries=int(row["queries"]), tokens=int(row["tokens"])
        )

    def recent(self, limit: int = 50, user_id: str = "") -> List[dict]:
        query = f"SELECT * FROM {TABLE_NAME}"
        params: list = []
        if user_id:
            query += " WHERE user_id = ?"
            params.append(user_id)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [_row_to_dict(dict(row)) for row in rows]

    def get_by_trace(self, trace_id: str) -> Optional[dict]:
        """按 trace_id 取一条审计记录。

        用户反馈只报 trace_id，问题原文与实际执行的 SQL 一律以审计记录为准——
        让客户端把 SQL 一并传上来的话，「这条 SQL 是对的」这句话就等于
        客户端说了算，认证 SQL 复用会变成一个可以往 Prompt 里塞任意内容的入口。
        """
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {TABLE_NAME} WHERE trace_id = ? ORDER BY id DESC LIMIT 1",
                (trace_id,),
            ).fetchone()
        return _row_to_dict(dict(row)) if row else None


class PostgresAuditStore:
    """PostgreSQL 审计存储：已经在用 pgvector 时顺带落到同一实例。"""

    def __init__(self, dsn: str):
        import psycopg

        self.dsn = dsn
        self.conn = psycopg.connect(dsn, autocommit=True)
        self._lock = threading.Lock()
        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                id BIGSERIAL PRIMARY KEY,
                trace_id TEXT NOT NULL,
                run_id TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                caller TEXT NOT NULL,
                user_id TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                query TEXT NOT NULL,
                route TEXT NOT NULL DEFAULT '',
                databases JSONB NOT NULL DEFAULT '[]',
                tables JSONB NOT NULL DEFAULT '[]',
                sqls JSONB NOT NULL DEFAULT '[]',
                denied_tables JSONB NOT NULL DEFAULT '[]',
                success BOOLEAN NOT NULL DEFAULT TRUE,
                status_code INTEGER NOT NULL DEFAULT 200,
                failed_steps INTEGER NOT NULL DEFAULT 0,
                repaired_steps INTEGER NOT NULL DEFAULT 0,
                duration_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                degraded BOOLEAN NOT NULL DEFAULT FALSE,
                has_risk BOOLEAN NOT NULL DEFAULT FALSE,
                execution_status TEXT NOT NULL DEFAULT '',
                error TEXT
            )
            """
        )
        # 老库补列，升级不用手工迁移
        self.conn.execute(
            f"ALTER TABLE {TABLE_NAME} "
            f"ADD COLUMN IF NOT EXISTS degraded BOOLEAN NOT NULL DEFAULT FALSE"
        )
        self.conn.execute(
            f"ALTER TABLE {TABLE_NAME} "
            f"ADD COLUMN IF NOT EXISTS has_risk BOOLEAN NOT NULL DEFAULT FALSE"
        )
        self.conn.execute(
            f"ALTER TABLE {TABLE_NAME} "
            f"ADD COLUMN IF NOT EXISTS execution_status TEXT NOT NULL DEFAULT ''"
        )
        self.conn.execute(
            f"ALTER TABLE {TABLE_NAME} "
            f"ADD COLUMN IF NOT EXISTS run_id TEXT NOT NULL DEFAULT ''"
        )
        self.conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_audit_user_time "
            f"ON {TABLE_NAME} (user_id, created_at)"
        )
        self.conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_audit_trace ON {TABLE_NAME} (trace_id)"
        )
        self.conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_audit_replay "
            f"ON {TABLE_NAME} (user_id, query, id)"
        )

    def record(self, record: AuditRecord) -> None:
        with self._lock:
            self.conn.execute(
                f"""
                INSERT INTO {TABLE_NAME} (
                    trace_id, created_at, caller, user_id, session_id, query,
                    route, databases, tables, sqls, denied_tables,
                    success, status_code, failed_steps, repaired_steps,
                    duration_ms, prompt_tokens, completion_tokens, degraded,
                    has_risk, execution_status, error, run_id
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    record.trace_id,
                    record.created_at,
                    record.caller,
                    record.user_id,
                    record.session_id,
                    record.query,
                    record.route,
                    json.dumps(record.databases, ensure_ascii=False),
                    json.dumps(record.tables, ensure_ascii=False),
                    json.dumps(record.sqls, ensure_ascii=False),
                    json.dumps(record.denied_tables, ensure_ascii=False),
                    record.success,
                    record.status_code,
                    record.failed_steps,
                    record.repaired_steps,
                    record.duration_ms,
                    record.prompt_tokens,
                    record.completion_tokens,
                    record.degraded,
                    record.has_risk,
                    record.execution_status,
                    record.error,
                    record.run_id,
                ),
            )

    def last_successful_sqls(self, user_id: str, query: str) -> List[Tuple[str, str]]:
        """取该用户同一问题上次成功执行的 SQL 序列，供模型不可用时重放。"""
        with self._lock:
            row = self.conn.execute(
                f"""
                SELECT databases, sqls FROM {TABLE_NAME}
                WHERE user_id = %s AND query = %s AND success
                  AND NOT degraded AND sqls <> '[]'::jsonb
                ORDER BY id DESC LIMIT 1
                """,
                (user_id, query),
            ).fetchone()

        if not row:
            return []
        return _parse_steps(row[0], row[1])

    def daily_usage(self, user_id: str) -> DailyUsage:
        with self._lock:
            row = self.conn.execute(
                f"""
                SELECT COUNT(*) AS queries,
                       COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens
                FROM {TABLE_NAME}
                WHERE user_id = %s AND created_at >= date_trunc('day', now())
                """,
                (user_id,),
            ).fetchone()
        return DailyUsage(user_id=user_id, queries=int(row[0]), tokens=int(row[1]))

    def recent(self, limit: int = 50, user_id: str = "") -> List[dict]:
        sql = f"SELECT to_jsonb(t) FROM {TABLE_NAME} t"
        params: list = []
        if user_id:
            sql += " WHERE t.user_id = %s"
            params.append(user_id)
        sql += " ORDER BY t.id DESC LIMIT %s"
        params.append(limit)

        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [row[0] for row in rows]

    def get_by_trace(self, trace_id: str) -> Optional[dict]:
        """按 trace_id 取一条审计记录（见 SQLiteAuditStore.get_by_trace）。"""
        with self._lock:
            row = self.conn.execute(
                f"SELECT to_jsonb(t) FROM {TABLE_NAME} t "
                f"WHERE t.trace_id = %s ORDER BY t.id DESC LIMIT 1",
                (trace_id,),
            ).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        self.conn.close()


class NullAuditStore:
    """审计关闭时的空实现。"""

    def record(self, record: AuditRecord) -> None:
        return None

    def daily_usage(self, user_id: str) -> DailyUsage:
        return DailyUsage(user_id=user_id)

    def recent(self, limit: int = 50, user_id: str = "") -> List[dict]:
        return []

    def last_successful_sqls(self, user_id: str, query: str) -> List[Tuple[str, str]]:
        return []

    def get_by_trace(self, trace_id: str) -> Optional[dict]:
        return None


def _parse_steps(databases, sqls) -> List[Tuple[str, str]]:
    """
    把审计里的 sqls 解析成 [(database, sql)]。

    新格式是 [{"database": ..., "sql": ...}]；老记录是纯 SQL 字符串列表，
    此时只能退回用 databases 对齐，无法可靠还原跨库步骤，直接放弃重放。
    """
    sqls = list(sqls or [])
    if not sqls:
        return []

    if all(isinstance(item, dict) for item in sqls):
        steps = [
            (str(item.get("database", "")), str(item.get("sql", "")))
            for item in sqls
        ]
        return steps if all(db and sql for db, sql in steps) else []

    # 老格式：只有单库单步时才能确定库名
    databases = list(databases or [])
    if len(sqls) == 1 and len(databases) == 1 and isinstance(sqls[0], str):
        return [(databases[0], sqls[0])]
    return []


def _row_to_dict(row: dict) -> dict:
    for key in ("databases", "tables", "sqls", "denied_tables"):
        if isinstance(row.get(key), str):
            row[key] = json.loads(row[key])
    row["success"] = bool(row.get("success"))
    row["degraded"] = bool(row.get("degraded"))
    row["has_risk"] = bool(row.get("has_risk"))
    return row


def create_audit_store() -> AuditStore:
    """
    按配置创建审计存储。

    已经在用 PostgreSQL 就落到同一实例（运维只管一个库），
    否则落本地 SQLite；关闭审计时返回空实现。
    """
    settings = get_settings()

    if not settings.audit_enabled:
        log_event(_logger, logging.WARNING, "audit.disabled")
        return NullAuditStore()

    if settings.pgvector_dsn:
        try:
            store = PostgresAuditStore(settings.pgvector_dsn)
            log_event(_logger, logging.INFO, "audit.store", backend="postgres")
            return store
        except Exception as exc:
            log_event(
                _logger,
                logging.WARNING,
                "audit.fallback",
                backend="sqlite",
                reason=str(exc),
            )

    store = SQLiteAuditStore(settings.audit_db_path)
    log_event(
        _logger,
        logging.INFO,
        "audit.store",
        backend="sqlite",
        path=settings.audit_db_path,
    )
    return store
