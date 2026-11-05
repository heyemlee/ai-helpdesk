from __future__ import annotations

import hashlib
import sqlite3
from typing import Iterable, Sequence, Tuple

# 与 postgres_loader 保持一致：AI SQL Agent 自身的基础设施表不算业务 Schema
from .postgres_loader import DEFAULT_EXCLUDED_TABLE_PREFIXES


def _digest(rows: Iterable[Tuple[str, ...]]) -> str:
    """把 (表, 字段, 类型) 三元组序列压成一个指纹。"""
    hasher = hashlib.sha256()
    for row in sorted(rows):
        hasher.update("\x1f".join(str(item) for item in row).encode("utf-8"))
        hasher.update(b"\x1e")
    return hasher.hexdigest()


def sqlite_schema_fingerprint(db_path: str) -> str:
    """
    SQLite 业务 Schema 指纹。

    只读 sqlite_master 与 PRAGMA，比重建索引便宜几个数量级，
    适合定时轮询。
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]

        rows = []
        for table in tables:
            for column in conn.execute(f'PRAGMA table_info("{table}")').fetchall():
                # (表名, 字段名, 类型, 是否非空, 是否主键)
                rows.append((table, column[1], column[2] or "", str(column[3]), str(column[5])))
        return _digest(rows)
    finally:
        conn.close()


def postgres_schema_fingerprint(
    dsn: str,
    schema: str = "public",
    excluded_prefixes: Sequence[str] = DEFAULT_EXCLUDED_TABLE_PREFIXES,
) -> str:
    """PostgreSQL 业务 Schema 指纹：一条 information_schema 查询。"""
    import psycopg

    conn = psycopg.connect(dsn, autocommit=True, connect_timeout=5)
    try:
        rows = conn.execute(
            """
            SELECT table_name, column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = %s
            ORDER BY table_name, ordinal_position
            """,
            (schema,),
        ).fetchall()
    finally:
        conn.close()

    filtered = [
        tuple(str(item) for item in row)
        for row in rows
        if not str(row[0]).startswith(tuple(excluded_prefixes))
    ]
    return _digest(filtered)


def database_fingerprint(dialect: str, db_path: str = "", dsn: str = "") -> str:
    """按方言取业务 Schema 指纹；取不到时返回空串（视为“未知，不触发重建”）。"""
    try:
        if dialect == "postgres":
            return postgres_schema_fingerprint(dsn)
        return sqlite_schema_fingerprint(str(db_path))
    except Exception:
        return ""
