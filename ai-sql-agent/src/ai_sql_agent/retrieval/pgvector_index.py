from __future__ import annotations

import hashlib
import logging
import re
from typing import List, Tuple

from ai_sql_agent.core import get_logger, log_event

_logger = get_logger("pgvector")

# 记录各命名空间已建索引内容的指纹表
_META_TABLE = "ai_sql_vector_meta"


class PgVectorIndex:
    """
    PostgreSQL + pgvector 向量索引。

    与内存版 VectorIndex 同接口（build / search），用于生产部署：
    - 向量持久化在 Postgres，服务重启不用重建；
    - 使用 HNSW 索引 + 余弦距离检索。

    连接串通过参数或环境变量 AI_SQL_PGVECTOR_DSN 提供，例如：
        postgresql://ai_sql:ai_sql@localhost:5432/ai_sql
    """

    def __init__(
        self,
        embedding_client,
        dsn: str,
        namespace: str = "default",
        dimensions: int | None = None,
    ):
        # 延迟导入，保证未安装 psycopg/pgvector 时其余功能不受影响
        import psycopg
        from pgvector.psycopg import register_vector

        self.embedding_client = embedding_client
        self.dimensions = int(
            dimensions or getattr(embedding_client, "dimensions", 1024)
        )

        self.namespace = namespace
        safe_namespace = re.sub(r"[^a-z0-9_]", "_", namespace.lower())
        self.table = f"ai_sql_vectors_{safe_namespace}"

        self.conn = psycopg.connect(dsn, autocommit=True)
        self.conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        register_vector(self.conn)

        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.table} (
                doc_index INTEGER PRIMARY KEY,
                content TEXT NOT NULL,
                embedding vector({self.dimensions}) NOT NULL
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
            f"""
            CREATE TABLE IF NOT EXISTS {_META_TABLE} (
                namespace TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                document_count INTEGER NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )

        self._verify_dimensions()

    def _verify_dimensions(self) -> None:
        """
        校验既有表的向量维度与当前 Embedding 维度一致。

        CREATE TABLE IF NOT EXISTS 会静默沿用旧表，换 Embedding 模型后
        会在写入阶段才暴露，这里提前失败（工厂会据此回退内存索引并告警）。
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
        if actual and int(actual.group(1)) != self.dimensions:
            raise ValueError(
                f"表 {self.table} 的向量维度是 {actual.group(1)}，"
                f"当前 Embedding 维度为 {self.dimensions}。"
                "换 Embedding 模型后需要重建索引表或改用新命名空间。"
            )

    def build(self, documents: List[str]) -> None:
        """
        构建当前命名空间下的向量索引。

        Schema 基本不变，但服务每次重启都全量重跑一次 Embedding：既慢又
        按量计费。这里先比对指纹（索引文本 + 模型维度），没变就直接复用
        库里已有的向量，变了才重建。
        """
        fingerprint = self._fingerprint(documents)

        if self._is_up_to_date(fingerprint, len(documents)):
            log_event(
                _logger,
                logging.INFO,
                "pgvector.index_reused",
                namespace=self.namespace,
                documents=len(documents),
            )
            return

        vectors = self.embedding_client.embed_texts(documents)

        with self.conn.cursor() as cursor:
            cursor.execute(f"TRUNCATE {self.table}")
            for doc_index, (text, vector) in enumerate(zip(documents, vectors)):
                cursor.execute(
                    f"INSERT INTO {self.table} (doc_index, content, embedding) "
                    "VALUES (%s, %s, %s)",
                    (doc_index, text, vector.tolist()),
                )

        self.conn.execute(
            f"""
            INSERT INTO {_META_TABLE} (namespace, fingerprint, document_count, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (namespace) DO UPDATE SET
                fingerprint = EXCLUDED.fingerprint,
                document_count = EXCLUDED.document_count,
                updated_at = now()
            """,
            (self.namespace, fingerprint, len(documents)),
        )

        log_event(
            _logger,
            logging.INFO,
            "pgvector.index_rebuilt",
            namespace=self.namespace,
            documents=len(documents),
        )

    def _fingerprint(self, documents: List[str]) -> str:
        """索引内容 + 向量维度的指纹；任一变化都要重建。"""
        digest = hashlib.sha256()
        digest.update(str(self.dimensions).encode("utf-8"))
        model = getattr(
            getattr(self.embedding_client, "config", None), "model", ""
        )
        digest.update(str(model).encode("utf-8"))
        for document in documents:
            digest.update(b"\x00")
            digest.update(document.encode("utf-8"))
        return digest.hexdigest()

    def _is_up_to_date(self, fingerprint: str, document_count: int) -> bool:
        row = self.conn.execute(
            f"SELECT fingerprint, document_count FROM {_META_TABLE} WHERE namespace = %s",
            (self.namespace,),
        ).fetchone()

        if not row or row[0] != fingerprint or row[1] != document_count:
            return False

        # 指纹一致还要确认向量确实在（表可能被手工清过）
        actual = self.conn.execute(
            f"SELECT COUNT(*) FROM {self.table}"
        ).fetchone()
        return bool(actual and actual[0] == document_count)

    def search(self, query: str, top_k: int = 5) -> List[Tuple[int, float]]:
        """
        余弦相似度检索，返回 (doc_index, score) 列表。

        同分时按 doc_index 升序，与内存版 VectorIndex 的稳定排序对齐，
        保证同一份索引换后端不会换名次。
        """
        query_vector = self.embedding_client.embed_texts([query])[0]

        with self.conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT doc_index, 1 - (embedding <=> %s::vector) AS score
                FROM {self.table}
                ORDER BY embedding <=> %s::vector, doc_index
                LIMIT %s
                """,
                (query_vector.tolist(), query_vector.tolist(), top_k),
            )
            rows = cursor.fetchall()

        return [(int(doc_index), float(score)) for doc_index, score in rows]
