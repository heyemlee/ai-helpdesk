from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .objects import FieldDocument, TableRelation


@dataclass
class PgVectorSchemaIndexConfig:
    """PostgreSQL + pgvector Schema 索引配置。"""

    dsn: str = ""
    """连接串，为空时取 Settings.pgvector_dsn（AI_SQL_PGVECTOR_DSN）。"""

    field_table_name: str = "schema_field_index"
    relation_table_name: str = "schema_relation_index"

    embedding_dim: int = 1024
    recreate_table: bool = True
    """True 时重建表，对应离线全量重建索引的场景。"""


def _validate_identifier(name: str) -> str:
    """表名只允许字母数字下划线，避免拼接 SQL 时被注入。"""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"非法的表名：{name}")
    return name


class PgVectorSchemaIndexClient:
    """
    PostgreSQL + pgvector Schema 索引 Client。

    与在线检索侧（src/ai_sql_agent/retrieval/pgvector_index.py）共用同一个 Postgres 实例，
    整个项目只维护一套向量栈。这里拆成两张表：

    - field_table：字段级索引（HNSW + 余弦距离），用于两阶段召回字段；
    - relation_table：表关系索引，按 relation_key 反查关联键，
      供 SchemaGraph 构建边使用（关系不参与向量召回，因此不建向量列）。
    """

    def __init__(self, config: Optional[PgVectorSchemaIndexConfig] = None):
        self.config = config or PgVectorSchemaIndexConfig()

        if not self.config.dsn:
            from ai_sql_agent.core import get_settings

            self.config.dsn = get_settings().pgvector_dsn

        if not self.config.dsn:
            raise ValueError(
                "缺少 pgvector 连接串。请设置 AI_SQL_PGVECTOR_DSN，"
                "或在 PgVectorSchemaIndexConfig(dsn='...') 中传入。"
            )

        self.field_table = _validate_identifier(self.config.field_table_name)
        self.relation_table = _validate_identifier(self.config.relation_table_name)

        # 延迟导入，未装 psycopg/pgvector 时不影响其余模块
        import psycopg
        from pgvector.psycopg import register_vector

        self.conn = psycopg.connect(self.config.dsn, autocommit=True)
        self.conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        register_vector(self.conn)

    # ------------------------------------------------------------------
    # 建表
    # ------------------------------------------------------------------

    def prepare_collections(self) -> None:
        """创建字段表与关系表。"""
        self.prepare_field_collection()
        self.prepare_relation_collection()

    def prepare_field_collection(self) -> None:
        """创建字段索引表（含 HNSW 余弦索引）。"""
        if self.config.recreate_table:
            self.conn.execute(f"DROP TABLE IF EXISTS {self.field_table}")

        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.field_table} (
                doc_id TEXT PRIMARY KEY,
                database TEXT NOT NULL,
                table_name TEXT NOT NULL,
                column_name TEXT NOT NULL,
                data_type TEXT NOT NULL DEFAULT '',
                semantic_role TEXT NOT NULL DEFAULT '',
                keyword_text TEXT NOT NULL DEFAULT '',
                vector_text TEXT NOT NULL DEFAULT '',
                rerank_text TEXT NOT NULL DEFAULT '',
                embedding vector({self.config.embedding_dim}) NOT NULL
            )
            """
        )
        self.conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS {self.field_table}_embedding_idx
            ON {self.field_table}
            USING hnsw (embedding vector_cosine_ops)
            """
        )
        self.conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS {self.field_table}_table_idx
            ON {self.field_table} (database, table_name)
            """
        )

    def prepare_relation_collection(self) -> None:
        """
        创建表关系表。

        relation_key 格式：`database:table_a__table_b`（表名排序后拼接），
        例如 `showroom_db:order_item__sales_order`。
        """
        if self.config.recreate_table:
            self.conn.execute(f"DROP TABLE IF EXISTS {self.relation_table}")

        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.relation_table} (
                relation_key TEXT PRIMARY KEY,
                database TEXT NOT NULL,
                source_table TEXT NOT NULL,
                source_column TEXT NOT NULL,
                target_table TEXT NOT NULL,
                target_column TEXT NOT NULL,
                relation_type TEXT NOT NULL DEFAULT 'foreign_key',
                join_condition TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                relation_json JSONB NOT NULL
            )
            """
        )

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def insert_field_documents(
        self,
        documents: List[FieldDocument],
        embeddings: List[List[float]],
    ) -> None:
        """写入字段级索引文档和向量。"""
        if len(documents) != len(embeddings):
            raise ValueError("documents 和 embeddings 数量不一致。")

        if not documents:
            return

        import numpy as np

        rows = [
            (
                doc.doc_id,
                doc.column.database,
                doc.column.table_name,
                doc.column.column_name,
                doc.column.data_type,
                doc.column.semantic_role,
                doc.keyword_text,
                doc.vector_text,
                doc.rerank_text,
                np.asarray(embedding, dtype=np.float32),
            )
            for doc, embedding in zip(documents, embeddings)
        ]

        with self.conn.cursor() as cursor:
            cursor.executemany(
                f"""
                INSERT INTO {self.field_table} (
                    doc_id, database, table_name, column_name, data_type,
                    semantic_role, keyword_text, vector_text, rerank_text, embedding
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (doc_id) DO UPDATE SET
                    database = EXCLUDED.database,
                    table_name = EXCLUDED.table_name,
                    column_name = EXCLUDED.column_name,
                    data_type = EXCLUDED.data_type,
                    semantic_role = EXCLUDED.semantic_role,
                    keyword_text = EXCLUDED.keyword_text,
                    vector_text = EXCLUDED.vector_text,
                    rerank_text = EXCLUDED.rerank_text,
                    embedding = EXCLUDED.embedding
                """,
                rows,
            )

    def insert_relations(self, relations: List[TableRelation]) -> None:
        """
        写入表间关系。

        关系不是字段召回的对象，而是 SchemaGraph 构建阶段的边信息。
        """
        if not relations:
            return

        rows = []
        for relation in relations:
            payload = {
                "database": relation.database,
                "source_table": relation.source_table,
                "source_column": relation.source_column,
                "target_table": relation.target_table,
                "target_column": relation.target_column,
                "relation_type": relation.relation_type,
                "join_condition": relation.join_condition,
                "description": relation.description,
            }
            rows.append(
                (
                    relation.relation_key,
                    relation.database,
                    relation.source_table,
                    relation.source_column,
                    relation.target_table,
                    relation.target_column,
                    relation.relation_type,
                    relation.join_condition,
                    relation.description,
                    json.dumps(payload, ensure_ascii=False),
                )
            )

        with self.conn.cursor() as cursor:
            cursor.executemany(
                f"""
                INSERT INTO {self.relation_table} (
                    relation_key, database, source_table, source_column,
                    target_table, target_column, relation_type,
                    join_condition, description, relation_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (relation_key) DO UPDATE SET
                    relation_json = EXCLUDED.relation_json,
                    relation_type = EXCLUDED.relation_type,
                    join_condition = EXCLUDED.join_condition,
                    description = EXCLUDED.description
                """,
                rows,
            )

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def search_fields(
        self,
        query_embedding: List[float],
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """字段向量检索（余弦距离转相似度分）。"""
        import numpy as np

        vector = np.asarray(query_embedding, dtype=np.float32)

        rows = self.conn.execute(
            f"""
            SELECT doc_id, database, table_name, column_name,
                   keyword_text, vector_text, rerank_text,
                   1 - (embedding <=> %s) AS score
            FROM {self.field_table}
            ORDER BY embedding <=> %s
            LIMIT %s
            """,
            (vector, vector, top_k),
        ).fetchall()

        return [
            {
                "doc_id": row[0],
                "database": row[1],
                "table_name": row[2],
                "column_name": row[3],
                "keyword_text": row[4],
                "vector_text": row[5],
                "rerank_text": row[6],
                "distance": float(row[7]),
            }
            for row in rows
        ]

    def query_relations_by_table_pairs(
        self,
        database: str,
        table_pairs: List[Tuple[str, str]],
    ) -> List[TableRelation]:
        """
        根据表名组合查询表间关系。

        输入 [(sales_order, order_item)] 会转成 relation_key 后按主键查询。
        """
        if not table_pairs:
            return []

        relation_keys = [
            self.build_relation_key(database, left, right)
            for left, right in table_pairs
        ]

        rows = self.conn.execute(
            f"""
            SELECT relation_json
            FROM {self.relation_table}
            WHERE relation_key = ANY(%s)
            """,
            (relation_keys,),
        ).fetchall()

        relations: List[TableRelation] = []
        for (payload,) in rows:
            data = payload if isinstance(payload, dict) else json.loads(payload)
            relations.append(
                TableRelation(
                    database=data["database"],
                    source_table=data["source_table"],
                    source_column=data["source_column"],
                    target_table=data["target_table"],
                    target_column=data["target_column"],
                    relation_type=data.get("relation_type", "foreign_key"),
                    description=data.get("description", ""),
                )
            )
        return relations

    def build_relation_key(
        self,
        database: str,
        table_a: str,
        table_b: str,
    ) -> str:
        """构建无向表关系 key。"""
        left, right = sorted([table_a, table_b])
        return f"{database}:{left}__{right}"

    def close(self) -> None:
        self.conn.close()
