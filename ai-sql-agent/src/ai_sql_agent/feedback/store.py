from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Protocol

from ai_sql_agent.core import get_logger, get_settings, log_event

from .objects import CertifiedQuery, FeedbackRecord, FeedbackStats, Verdict

_logger = get_logger("feedback")

TABLE_NAME = "ai_sql_feedback"


class FeedbackStore(Protocol):
    def record(self, feedback: FeedbackRecord) -> None: ...

    def recent(
        self, limit: int = 50, user_id: str = "", verdict: str = ""
    ) -> List[dict]: ...

    def certified(self, user_id: str = "", limit: int = 200) -> List[CertifiedQuery]: ...

    def stats(self) -> FeedbackStats: ...

    def mark_resolved(self, trace_id: str) -> bool: ...


class SQLiteFeedbackStore:
    """
    SQLite 反馈存储，与审计库放在同一个文件里。

    一条 trace_id 只保留最新一次表态：用户改主意（先点对又改成错）是常事，
    留两条会让统计双计，也让认证范例里躺着一条已经被否掉的 SQL。
    """

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
                    trace_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    caller TEXT NOT NULL DEFAULT '',
                    verdict TEXT NOT NULL,
                    query TEXT NOT NULL DEFAULT '',
                    databases TEXT NOT NULL DEFAULT '[]',
                    tables TEXT NOT NULL DEFAULT '[]',
                    sqls TEXT NOT NULL DEFAULT '[]',
                    comment TEXT NOT NULL DEFAULT '',
                    route TEXT NOT NULL DEFAULT '',
                    had_risk INTEGER NOT NULL DEFAULT 0,
                    resolved INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_feedback_verdict
                    ON {TABLE_NAME}(verdict, id);
                CREATE INDEX IF NOT EXISTS idx_feedback_user
                    ON {TABLE_NAME}(user_id, id);
                """
            )

    def record(self, feedback: FeedbackRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO {TABLE_NAME} (
                    trace_id, created_at, user_id, caller, verdict, query,
                    databases, tables, sqls, comment, route, had_risk, resolved
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trace_id) DO UPDATE SET
                    created_at = excluded.created_at,
                    verdict = excluded.verdict,
                    comment = excluded.comment,
                    resolved = 0
                """,
                (
                    feedback.trace_id,
                    feedback.created_at,
                    feedback.user_id,
                    feedback.caller,
                    feedback.verdict.value,
                    feedback.query,
                    json.dumps(feedback.databases, ensure_ascii=False),
                    json.dumps(feedback.tables, ensure_ascii=False),
                    json.dumps(feedback.sqls, ensure_ascii=False),
                    feedback.comment,
                    feedback.route,
                    int(feedback.had_risk),
                    int(feedback.resolved),
                ),
            )

    def recent(
        self, limit: int = 50, user_id: str = "", verdict: str = ""
    ) -> List[dict]:
        sql = f"SELECT * FROM {TABLE_NAME}"
        clauses: List[str] = []
        params: list = []
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)
        if verdict:
            clauses.append("verdict = ?")
            params.append(verdict)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [_row_to_dict(dict(row)) for row in rows]

    def certified(self, user_id: str = "", limit: int = 200) -> List[CertifiedQuery]:
        sql = f"SELECT * FROM {TABLE_NAME} WHERE verdict = ? AND sqls != '[]'"
        params: list = [Verdict.CORRECT.value]
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return _to_certified([_row_to_dict(dict(row)) for row in rows])

    def stats(self) -> FeedbackStats:
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT verdict, had_risk, resolved, COUNT(*) AS n
                FROM {TABLE_NAME} GROUP BY verdict, had_risk, resolved
                """
            ).fetchall()
        return _aggregate(
            [
                (row["verdict"], bool(row["had_risk"]), bool(row["resolved"]), row["n"])
                for row in rows
            ]
        )

    def mark_resolved(self, trace_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE {TABLE_NAME} SET resolved = 1 WHERE trace_id = ?",
                (trace_id,),
            )
            return cursor.rowcount > 0


class PostgresFeedbackStore:
    """PostgreSQL 反馈存储：已经在用 pgvector 时与审计落同一实例。"""

    def __init__(self, dsn: str):
        import psycopg

        self.dsn = dsn
        self.conn = psycopg.connect(dsn, autocommit=True)
        self._lock = threading.Lock()
        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                id BIGSERIAL PRIMARY KEY,
                trace_id TEXT NOT NULL UNIQUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                user_id TEXT NOT NULL,
                caller TEXT NOT NULL DEFAULT '',
                verdict TEXT NOT NULL,
                query TEXT NOT NULL DEFAULT '',
                databases JSONB NOT NULL DEFAULT '[]',
                tables JSONB NOT NULL DEFAULT '[]',
                sqls JSONB NOT NULL DEFAULT '[]',
                comment TEXT NOT NULL DEFAULT '',
                route TEXT NOT NULL DEFAULT '',
                had_risk BOOLEAN NOT NULL DEFAULT FALSE,
                resolved BOOLEAN NOT NULL DEFAULT FALSE
            )
            """
        )
        self.conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_feedback_verdict "
            f"ON {TABLE_NAME} (verdict, id)"
        )
        self.conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_feedback_user ON {TABLE_NAME} (user_id, id)"
        )

    def record(self, feedback: FeedbackRecord) -> None:
        with self._lock:
            self.conn.execute(
                f"""
                INSERT INTO {TABLE_NAME} (
                    trace_id, created_at, user_id, caller, verdict, query,
                    databases, tables, sqls, comment, route, had_risk, resolved
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (trace_id) DO UPDATE SET
                    created_at = EXCLUDED.created_at,
                    verdict = EXCLUDED.verdict,
                    comment = EXCLUDED.comment,
                    resolved = FALSE
                """,
                (
                    feedback.trace_id,
                    feedback.created_at,
                    feedback.user_id,
                    feedback.caller,
                    feedback.verdict.value,
                    feedback.query,
                    json.dumps(feedback.databases, ensure_ascii=False),
                    json.dumps(feedback.tables, ensure_ascii=False),
                    json.dumps(feedback.sqls, ensure_ascii=False),
                    feedback.comment,
                    feedback.route,
                    feedback.had_risk,
                    feedback.resolved,
                ),
            )

    def recent(
        self, limit: int = 50, user_id: str = "", verdict: str = ""
    ) -> List[dict]:
        sql = f"SELECT to_jsonb(t) FROM {TABLE_NAME} t"
        clauses: List[str] = []
        params: list = []
        if user_id:
            clauses.append("t.user_id = %s")
            params.append(user_id)
        if verdict:
            clauses.append("t.verdict = %s")
            params.append(verdict)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY t.id DESC LIMIT %s"
        params.append(limit)

        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [row[0] for row in rows]

    def certified(self, user_id: str = "", limit: int = 200) -> List[CertifiedQuery]:
        sql = (
            f"SELECT to_jsonb(t) FROM {TABLE_NAME} t "
            f"WHERE t.verdict = %s AND t.sqls <> '[]'::jsonb"
        )
        params: list = [Verdict.CORRECT.value]
        if user_id:
            sql += " AND t.user_id = %s"
            params.append(user_id)
        sql += " ORDER BY t.id DESC LIMIT %s"
        params.append(limit)

        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return _to_certified([row[0] for row in rows])

    def stats(self) -> FeedbackStats:
        with self._lock:
            rows = self.conn.execute(
                f"SELECT verdict, had_risk, resolved, COUNT(*) FROM {TABLE_NAME} "
                f"GROUP BY verdict, had_risk, resolved"
            ).fetchall()
        return _aggregate([(row[0], bool(row[1]), bool(row[2]), row[3]) for row in rows])

    def mark_resolved(self, trace_id: str) -> bool:
        with self._lock:
            cursor = self.conn.execute(
                f"UPDATE {TABLE_NAME} SET resolved = TRUE WHERE trace_id = %s",
                (trace_id,),
            )
            return cursor.rowcount > 0

    def close(self) -> None:
        self.conn.close()


class NullFeedbackStore:
    """反馈关闭时的空实现。"""

    def record(self, feedback: FeedbackRecord) -> None:
        return None

    def recent(
        self, limit: int = 50, user_id: str = "", verdict: str = ""
    ) -> List[dict]:
        return []

    def certified(self, user_id: str = "", limit: int = 200) -> List[CertifiedQuery]:
        return []

    def stats(self) -> FeedbackStats:
        return FeedbackStats()

    def mark_resolved(self, trace_id: str) -> bool:
        return False


def _row_to_dict(row: dict) -> dict:
    for key in ("databases", "tables", "sqls"):
        if isinstance(row.get(key), str):
            row[key] = json.loads(row[key])
    row["had_risk"] = bool(row.get("had_risk"))
    row["resolved"] = bool(row.get("resolved"))
    return row


def _to_certified(rows: List[dict]) -> List[CertifiedQuery]:
    """把反馈行摊平成「一步一条」的认证查询。"""
    certified: List[CertifiedQuery] = []
    for row in rows:
        for step in row.get("sqls") or []:
            if not isinstance(step, dict):
                continue
            database = str(step.get("database") or "")
            sql = str(step.get("sql") or "")
            if not database or not sql:
                continue
            certified.append(
                CertifiedQuery(
                    query=str(row.get("query") or ""),
                    database=database,
                    sql=sql,
                    tables=list(row.get("tables") or []),
                    user_id=str(row.get("user_id") or ""),
                    created_at=str(row.get("created_at") or ""),
                )
            )
    return certified


def _aggregate(rows) -> FeedbackStats:
    stats = FeedbackStats()
    for verdict, had_risk, resolved, count in rows:
        count = int(count)
        stats.total += count
        if verdict == Verdict.CORRECT.value:
            stats.correct += count
        elif verdict == Verdict.WRONG.value:
            stats.wrong += count
            if had_risk:
                stats.wrong_with_risk += count
            else:
                stats.wrong_without_risk += count
            if not resolved:
                stats.unresolved_wrong += count
        else:
            stats.unsure += count
    return stats


def create_feedback_store() -> FeedbackStore:
    """按配置创建反馈存储；与审计存储共用同一个后端选择逻辑。"""
    settings = get_settings()

    if not settings.feedback_enabled:
        log_event(_logger, logging.WARNING, "feedback.disabled")
        return NullFeedbackStore()

    if settings.pgvector_dsn:
        try:
            store = PostgresFeedbackStore(settings.pgvector_dsn)
            log_event(_logger, logging.INFO, "feedback.store", backend="postgres")
            return store
        except Exception as exc:
            log_event(
                _logger,
                logging.WARNING,
                "feedback.fallback",
                backend="sqlite",
                reason=str(exc),
            )

    store = SQLiteFeedbackStore(settings.audit_db_path)
    log_event(
        _logger,
        logging.INFO,
        "feedback.store",
        backend="sqlite",
        path=settings.audit_db_path,
    )
    return store
