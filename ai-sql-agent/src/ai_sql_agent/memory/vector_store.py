from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Optional, Protocol, Sequence

import numpy as np

from .objects import LongTermMemory, MemoryKind


class LongTermVectorIndex(Protocol):
    def upsert(self, memory: LongTermMemory) -> None: ...

    def search(
        self,
        *,
        user_id: str,
        query_embedding: np.ndarray,
        top_k: int,
        kinds: Optional[Sequence[MemoryKind]] = None,
    ) -> List[tuple[str, float]]: ...

    def delete(self, *, user_id: str, memory_id: str) -> None: ...


@dataclass
class PgVectorLongTermMemoryConfig:
    """长期记忆 pgvector 索引配置。"""

    dsn: str = ""
    """连接串，为空时取 Settings.pgvector_dsn（AI_SQL_PGVECTOR_DSN）。"""

    table_name: str = "ai_sql_long_term_memory"
    embedding_dimensions: int = 1024


def _validate_identifier(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"非法的表名：{name}")
    return name


class PgVectorLongTermVectorIndex:
    """
    PostgreSQL + pgvector 长期记忆向量索引。

    与 Schema 检索共用同一套向量栈（见 src/ai_sql_agent/retrieval/pgvector_index.py），
    整个项目只维护一个向量库。SQLite 继续作为原文与元信息的来源，
    这里只存 user_id / kind / summary / embedding，用于按用户过滤后的向量召回。
    """

    def __init__(self, config: Optional[PgVectorLongTermMemoryConfig] = None):
        self.config = config or PgVectorLongTermMemoryConfig()

        if not self.config.dsn:
            from ai_sql_agent.core import get_settings

            self.config.dsn = get_settings().pgvector_dsn

        if not self.config.dsn:
            raise ValueError(
                "缺少 pgvector 连接串。请设置 AI_SQL_PGVECTOR_DSN，"
                "或在 PgVectorLongTermMemoryConfig(dsn='...') 中传入。"
            )

        self.table = _validate_identifier(self.config.table_name)

        import psycopg
        from pgvector.psycopg import register_vector

        self.conn = psycopg.connect(self.config.dsn, autocommit=True)
        self.conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        register_vector(self.conn)

        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.table} (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                embedding vector({self.config.embedding_dimensions}) NOT NULL
            )
            """
        )
        self.conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS {self.table}_embedding_idx
            ON {self.table}
            USING hnsw (embedding vector_cosine_ops)
            """
        )
        self.conn.execute(
            f"CREATE INDEX IF NOT EXISTS {self.table}_user_idx ON {self.table} (user_id, kind)"
        )

        self._verify_dimensions()

    def _verify_dimensions(self) -> None:
        """
        校验既有表的向量维度与当前配置一致。

        CREATE TABLE IF NOT EXISTS 会静默沿用旧表，换了 Embedding 模型
        （维度随之变化）时会在写入阶段才报错，这里提前失败并说清原因。
        """
        row = self.conn.execute(
            """
            SELECT format_type(a.atttypid, a.atttypmod)
            FROM pg_class AS c
            JOIN pg_attribute AS a ON a.attrelid = c.oid
            WHERE c.relname = %s AND a.attname = 'embedding'
            """,
            (self.table,),
        ).fetchone()

        if not row:
            return

        actual = re.search(r"vector\((\d+)\)", row[0] or "")
        if actual and int(actual.group(1)) != self.config.embedding_dimensions:
            raise ValueError(
                f"表 {self.table} 的向量维度是 {actual.group(1)}，"
                f"当前配置为 {self.config.embedding_dimensions}。"
                "换 Embedding 模型后需要重建索引表或改用新表名。"
            )

    def upsert(self, memory: LongTermMemory) -> None:
        vector = np.asarray(memory.embedding, dtype=np.float32).reshape(-1)
        if vector.size != self.config.embedding_dimensions:
            raise ValueError(
                f"长期记忆向量维度应为 {self.config.embedding_dimensions}，实际为 {vector.size}"
            )

        self.conn.execute(
            f"""
            INSERT INTO {self.table} (id, user_id, kind, summary, embedding)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                user_id = EXCLUDED.user_id,
                kind = EXCLUDED.kind,
                summary = EXCLUDED.summary,
                embedding = EXCLUDED.embedding
            """,
            (memory.id, memory.user_id, memory.kind.value, memory.summary, vector),
        )

    def search(
        self,
        *,
        user_id: str,
        query_embedding: np.ndarray,
        top_k: int,
        kinds: Optional[Sequence[MemoryKind]] = None,
    ) -> List[tuple[str, float]]:
        vector = np.asarray(query_embedding, dtype=np.float32).reshape(-1)

        sql = f"""
            SELECT id, 1 - (embedding <=> %s) AS score
            FROM {self.table}
            WHERE user_id = %s
        """
        params: List[Any] = [vector, user_id]

        if kinds:
            sql += " AND kind = ANY(%s)"
            params.append([kind.value for kind in kinds])

        sql += " ORDER BY embedding <=> %s LIMIT %s"
        params.extend([vector, top_k])

        rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [(str(row[0]), float(row[1])) for row in rows]

    def delete(self, *, user_id: str, memory_id: str) -> None:
        self.conn.execute(
            f"DELETE FROM {self.table} WHERE id = %s AND user_id = %s",
            (memory_id, user_id),
        )

    def close(self) -> None:
        self.conn.close()
