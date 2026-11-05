"""
run 记录与事件日志的存储。

结构照 `audit/store.py`：默认 SQLite（`runtime_data/runs.db`），配了 PostgreSQL DSN 就落到
同一个实例，`AI_SQL_RUNS_ENABLED=false` 时用空实现。这份数据丢了不影响查询——只是没有
历史事件可看，所以任何一次写入失败都不该把请求带崩（由调用方 `RunRecorder` 兜住）。

PostgreSQL 这一侧不学审计共用一条加锁连接：事件写入比审计频繁得多，用执行层同款的
有界连接池（`execution/pool.py`）。
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Protocol, Sequence

from ai_sql_agent.core import get_logger, get_settings, log_event

from .events import RunEvent, utc_now_iso

_logger = get_logger("runs")

RUNS_TABLE = "ai_sql_runs"
EVENTS_TABLE = "ai_sql_run_events"


def process_owner() -> str:
    """`主机:进程号`。孤儿判定靠它：同一台机器上进程已经不在了，它的 running 就不会再结束。"""
    return f"{socket.gethostname()}:{os.getpid()}"


def _owner_is_gone(owner: str) -> Optional[bool]:
    """
    同一台机器上的进程：True = 已经不在了，False = 还活着。
    别的机器上的进程看不见，返回 None，交给"多久没动静"来判。
    """
    host, _, pid = owner.rpartition(":")
    if host != socket.gethostname() or not pid.isdigit():
        return None
    if int(pid) == os.getpid():
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


class RunStore(Protocol):
    def create_run(self, row: Dict[str, Any]) -> None: ...

    def append_events(self, events: Sequence[RunEvent]) -> None: ...

    def finish_run(self, run_id: str, *, status: str, error: str = "",
                   cancel_reason: str = "", usage: Optional[dict] = None,
                   outcome: Optional[dict] = None) -> None: ...

    def set_cancel_reason(self, run_id: str, reason: str) -> None: ...

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]: ...

    def list_events(self, run_id: str, after_seq: int = 0, limit: int = 500) -> List[Dict[str, Any]]: ...

    def running_runs(self) -> List[Dict[str, Any]]: ...

    def mark_interrupted(self, run_ids: Sequence[str], reason: str) -> int: ...

    def purge_finished_before(self, cutoff_iso: str) -> int: ...

    def close(self) -> None: ...


def _row_to_run(row: Dict[str, Any]) -> Dict[str, Any]:
    run = dict(row)
    for key in ("usage_json", "outcome_json"):
        raw = run.pop(key, None)
        value = raw if isinstance(raw, dict) else json.loads(raw or "{}")
        run[key[: -len("_json")]] = value
    for key in ("created_at", "updated_at", "finished_at"):
        if isinstance(run.get(key), datetime):
            run[key] = run[key].astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return run


def _row_to_event(row: Dict[str, Any]) -> Dict[str, Any]:
    event = dict(row)
    raw = event.pop("attrs_json", None)
    event["attrs"] = raw if isinstance(raw, dict) else json.loads(raw or "{}")
    return event


def orphans(runs: Sequence[Dict[str, Any]], *, stale_seconds: float) -> List[str]:
    """
    哪些 running 的 run 已经不会再结束了。

    同一台机器：进程不在了就是孤儿（启动时正是这种情况）。别的机器：超过
    `stale_seconds` 没有任何更新才算——单进程部署（Dockerfile `--workers 1`）下用不到这条，
    留着是为了多实例共用一个 PostgreSQL 时不误伤别人在跑的 run。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)
    found = []
    for run in runs:
        gone = _owner_is_gone(str(run.get("owner") or ""))
        if gone is None:
            updated = str(run.get("updated_at") or "")
            try:
                stamp = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            except ValueError:
                stamp = None
            gone = stamp is None or stamp < cutoff
        if gone:
            found.append(str(run["run_id"]))
    return found


class SQLiteRunStore:
    """SQLite：单实例部署的默认选择。"""

    def __init__(self, db_path: str | Path = "runtime_data/runs.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS {RUNS_TABLE} (
                    run_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL,
                    entry TEXT NOT NULL,
                    caller TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '',
                    query TEXT NOT NULL,
                    status TEXT NOT NULL,
                    cancel_reason TEXT,
                    error TEXT,
                    owner TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT,
                    last_seq INTEGER NOT NULL DEFAULT 0,
                    usage_json TEXT NOT NULL DEFAULT '{{}}',
                    outcome_json TEXT NOT NULL DEFAULT '{{}}'
                );
                CREATE INDEX IF NOT EXISTS idx_runs_user_time ON {RUNS_TABLE}(user_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_runs_trace ON {RUNS_TABLE}(trace_id);
                CREATE INDEX IF NOT EXISTS idx_runs_status ON {RUNS_TABLE}(status, updated_at);
                CREATE TABLE IF NOT EXISTS {EVENTS_TABLE} (
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    ts TEXT NOT NULL,
                    type TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT '',
                    step INTEGER,
                    attrs_json TEXT NOT NULL DEFAULT '{{}}',
                    PRIMARY KEY (run_id, seq)
                );
                """
            )

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

    def create_run(self, row: Dict[str, Any]) -> None:
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO {RUNS_TABLE} (
                    run_id, trace_id, entry, caller, user_id, session_id, query,
                    status, owner, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)
                """,
                (row["run_id"], row.get("trace_id", ""), row.get("entry", ""),
                 row.get("caller", ""), row.get("user_id", ""), row.get("session_id", ""),
                 row.get("query", ""), row.get("owner") or process_owner(), now, now),
            )

    def append_events(self, events: Sequence[RunEvent]) -> None:
        if not events:
            return
        with self._lock, self._connect() as conn:
            conn.executemany(
                f"""
                INSERT OR IGNORE INTO {EVENTS_TABLE} (run_id, seq, ts, type, stage, step, attrs_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (event.run_id, event.seq, event.ts, event.type, event.stage, event.step,
                     json.dumps(event.attrs, ensure_ascii=False, default=str))
                    for event in events
                ],
            )
            conn.execute(
                f"UPDATE {RUNS_TABLE} SET last_seq = MAX(last_seq, ?), updated_at = ? WHERE run_id = ?",
                (max(event.seq for event in events), utc_now_iso(), events[0].run_id),
            )

    def finish_run(self, run_id: str, *, status: str, error: str = "", cancel_reason: str = "",
                   usage: Optional[dict] = None, outcome: Optional[dict] = None) -> None:
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                f"""
                UPDATE {RUNS_TABLE}
                SET status = ?, error = ?, cancel_reason = COALESCE(NULLIF(?, ''), cancel_reason),
                    usage_json = ?, outcome_json = ?, finished_at = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (status, error or None, cancel_reason,
                 json.dumps(usage or {}, ensure_ascii=False, default=str),
                 json.dumps(outcome or {}, ensure_ascii=False, default=str),
                 now, now, run_id),
            )

    def set_cancel_reason(self, run_id: str, reason: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE {RUNS_TABLE} SET cancel_reason = ?, updated_at = ? WHERE run_id = ?",
                (reason, utc_now_iso(), run_id),
            )

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {RUNS_TABLE} WHERE run_id = ?", (run_id,)
            ).fetchone()
        return _row_to_run(dict(row)) if row else None

    def list_events(self, run_id: str, after_seq: int = 0, limit: int = 500) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT run_id, seq, ts, type, stage, step, attrs_json FROM {EVENTS_TABLE}
                WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?
                """,
                (run_id, int(after_seq), int(limit)),
            ).fetchall()
        return [_row_to_event(dict(row)) for row in rows]

    def running_runs(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT run_id, owner, updated_at FROM {RUNS_TABLE} WHERE status = 'running'"
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_interrupted(self, run_ids: Sequence[str], reason: str) -> int:
        if not run_ids:
            return 0
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            cursor = conn.executemany(
                f"""
                UPDATE {RUNS_TABLE} SET status = 'interrupted', error = ?, finished_at = ?, updated_at = ?
                WHERE run_id = ? AND status = 'running'
                """,
                [(reason, now, now, run_id) for run_id in run_ids],
            )
            return cursor.rowcount

    def purge_finished_before(self, cutoff_iso: str) -> int:
        with self._lock, self._connect() as conn:
            conn.execute(
                f"""
                DELETE FROM {EVENTS_TABLE} WHERE run_id IN (
                    SELECT run_id FROM {RUNS_TABLE}
                    WHERE finished_at IS NOT NULL AND finished_at < ?
                )
                """,
                (cutoff_iso,),
            )
            cursor = conn.execute(
                f"DELETE FROM {RUNS_TABLE} WHERE finished_at IS NOT NULL AND finished_at < ?",
                (cutoff_iso,),
            )
            return cursor.rowcount

    def close(self) -> None:
        return None


class PostgresRunStore:
    """PostgreSQL：已经在用 pgvector 时顺带落到同一实例（与审计同一个 DSN）。"""

    def __init__(self, dsn: str, pool_size: int = 4):
        import psycopg

        from ai_sql_agent.execution.pool import BoundedConnectionPool

        self.dsn = dsn
        self._pool = BoundedConnectionPool(
            lambda: psycopg.connect(dsn, autocommit=True), max_size=pool_size
        )
        with self._conn() as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {RUNS_TABLE} (
                    run_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL,
                    entry TEXT NOT NULL,
                    caller TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '',
                    query TEXT NOT NULL,
                    status TEXT NOT NULL,
                    cancel_reason TEXT,
                    error TEXT,
                    owner TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    finished_at TIMESTAMPTZ,
                    last_seq INTEGER NOT NULL DEFAULT 0,
                    usage_json JSONB NOT NULL DEFAULT '{{}}',
                    outcome_json JSONB NOT NULL DEFAULT '{{}}'
                )
                """
            )
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_runs_user_time ON {RUNS_TABLE} (user_id, created_at)")
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_runs_trace ON {RUNS_TABLE} (trace_id)")
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_runs_status ON {RUNS_TABLE} (status, updated_at)")
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {EVENTS_TABLE} (
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    ts TIMESTAMPTZ NOT NULL,
                    type TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT '',
                    step INTEGER,
                    attrs_json JSONB NOT NULL DEFAULT '{{}}',
                    PRIMARY KEY (run_id, seq)
                )
                """
            )

    @contextmanager
    def _conn(self):
        from psycopg.rows import dict_row

        conn, pooled = self._pool.acquire()
        healthy = True
        try:
            conn.row_factory = dict_row
            yield conn
        except Exception:
            healthy = False
            raise
        finally:
            self._pool.release(conn, pooled, reusable=healthy)

    def create_run(self, row: Dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                f"""
                INSERT INTO {RUNS_TABLE} (
                    run_id, trace_id, entry, caller, user_id, session_id, query, status, owner
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'running', %s)
                """,
                (row["run_id"], row.get("trace_id", ""), row.get("entry", ""),
                 row.get("caller", ""), row.get("user_id", ""), row.get("session_id", ""),
                 row.get("query", ""), row.get("owner") or process_owner()),
            )

    def append_events(self, events: Sequence[RunEvent]) -> None:
        if not events:
            return
        with self._conn() as conn:
            with conn.cursor() as cursor:
                cursor.executemany(
                    f"""
                    INSERT INTO {EVENTS_TABLE} (run_id, seq, ts, type, stage, step, attrs_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (run_id, seq) DO NOTHING
                    """,
                    [
                        (event.run_id, event.seq, event.ts, event.type, event.stage, event.step,
                         json.dumps(event.attrs, ensure_ascii=False, default=str))
                        for event in events
                    ],
                )
            conn.execute(
                f"UPDATE {RUNS_TABLE} SET last_seq = GREATEST(last_seq, %s), updated_at = now() WHERE run_id = %s",
                (max(event.seq for event in events), events[0].run_id),
            )

    def finish_run(self, run_id: str, *, status: str, error: str = "", cancel_reason: str = "",
                   usage: Optional[dict] = None, outcome: Optional[dict] = None) -> None:
        with self._conn() as conn:
            conn.execute(
                f"""
                UPDATE {RUNS_TABLE}
                SET status = %s, error = %s, cancel_reason = COALESCE(NULLIF(%s, ''), cancel_reason),
                    usage_json = %s, outcome_json = %s, finished_at = now(), updated_at = now()
                WHERE run_id = %s
                """,
                (status, error or None, cancel_reason,
                 json.dumps(usage or {}, ensure_ascii=False, default=str),
                 json.dumps(outcome or {}, ensure_ascii=False, default=str), run_id),
            )

    def set_cancel_reason(self, run_id: str, reason: str) -> None:
        with self._conn() as conn:
            conn.execute(
                f"UPDATE {RUNS_TABLE} SET cancel_reason = %s, updated_at = now() WHERE run_id = %s",
                (reason, run_id),
            )

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(f"SELECT * FROM {RUNS_TABLE} WHERE run_id = %s", (run_id,)).fetchone()
        return _row_to_run(row) if row else None

    def list_events(self, run_id: str, after_seq: int = 0, limit: int = 500) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT run_id, seq, ts, type, stage, step, attrs_json FROM {EVENTS_TABLE}
                WHERE run_id = %s AND seq > %s ORDER BY seq LIMIT %s
                """,
                (run_id, int(after_seq), int(limit)),
            ).fetchall()
        events = []
        for row in rows:
            event = _row_to_event(row)
            if isinstance(event.get("ts"), datetime):
                event["ts"] = event["ts"].astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            events.append(event)
        return events

    def running_runs(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT run_id, owner, updated_at FROM {RUNS_TABLE} WHERE status = 'running'"
            ).fetchall()
        return [
            {**row, "updated_at": row["updated_at"].astimezone(timezone.utc).isoformat()}
            if isinstance(row.get("updated_at"), datetime) else dict(row)
            for row in rows
        ]

    def mark_interrupted(self, run_ids: Sequence[str], reason: str) -> int:
        if not run_ids:
            return 0
        with self._conn() as conn:
            cursor = conn.execute(
                f"""
                UPDATE {RUNS_TABLE} SET status = 'interrupted', error = %s,
                    finished_at = now(), updated_at = now()
                WHERE run_id = ANY(%s) AND status = 'running'
                """,
                (reason, list(run_ids)),
            )
            return cursor.rowcount

    def purge_finished_before(self, cutoff_iso: str) -> int:
        with self._conn() as conn:
            conn.execute(
                f"""
                DELETE FROM {EVENTS_TABLE} WHERE run_id IN (
                    SELECT run_id FROM {RUNS_TABLE}
                    WHERE finished_at IS NOT NULL AND finished_at < %s
                )
                """,
                (cutoff_iso,),
            )
            cursor = conn.execute(
                f"DELETE FROM {RUNS_TABLE} WHERE finished_at IS NOT NULL AND finished_at < %s",
                (cutoff_iso,),
            )
            return cursor.rowcount

    def close(self) -> None:
        self._pool.close_all()


class NullRunStore:
    """关闭持久化时的空实现：事件照样发给内存里的订阅者，只是不落库。"""

    def create_run(self, row: Dict[str, Any]) -> None:
        return None

    def append_events(self, events: Sequence[RunEvent]) -> None:
        return None

    def finish_run(self, run_id: str, **_kwargs: Any) -> None:
        return None

    def set_cancel_reason(self, run_id: str, reason: str) -> None:
        return None

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        return None

    def list_events(self, run_id: str, after_seq: int = 0, limit: int = 500) -> List[Dict[str, Any]]:
        return []

    def running_runs(self) -> List[Dict[str, Any]]:
        return []

    def mark_interrupted(self, run_ids: Sequence[str], reason: str) -> int:
        return 0

    def purge_finished_before(self, cutoff_iso: str) -> int:
        return 0

    def close(self) -> None:
        return None


def create_run_store() -> RunStore:
    """
    按配置创建 run 存储：关掉 → 空实现；配了 PostgreSQL DSN → 同一实例（连不上就退回 SQLite
    并告警，与审计的处理一致）；否则本地 SQLite。
    """
    settings = get_settings()
    if not settings.runs_enabled:
        log_event(_logger, logging.INFO, "runs.store", backend="none")
        return NullRunStore()
    if settings.pgvector_dsn:
        try:
            store = PostgresRunStore(settings.pgvector_dsn)
            log_event(_logger, logging.INFO, "runs.store", backend="postgres")
            return store
        except Exception as exc:
            log_event(_logger, logging.WARNING, "runs.fallback", backend="sqlite",
                      reason=f"{type(exc).__name__}: {exc}")
    store = SQLiteRunStore(settings.runs_db_path)
    log_event(_logger, logging.INFO, "runs.store", backend="sqlite", path=str(store.db_path))
    return store
