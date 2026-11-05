from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .embedding_client import AliyunEmbeddingClient
from .pgvector_client import PgVectorSchemaIndexClient
from .objects import ColumnSchema, FieldDocument, TableRelation
from .text_builder import SchemaIndexTextBuilder


@dataclass
class SchemaIndexBuildResult:
    """索引构建结果。"""

    field_collection_name: str
    """字段索引表名。"""

    relation_collection_name: str
    """表关系索引表名。"""

    field_document_count: int
    relation_count: int


class SchemaIndexBuilder:
    """
    Schema 索引构建主流程。

    离线写入两类数据：
    1. 字段级索引：用于两阶段召回字段。
    2. 表关系索引：用于根据命中字段所属表构建 SchemaGraph。
    """

    def __init__(
        self,
        text_builder: SchemaIndexTextBuilder,
        embedding_client: AliyunEmbeddingClient,
        index_client: PgVectorSchemaIndexClient,
    ):
        self.text_builder = text_builder
        self.embedding_client = embedding_client
        self.index_client = index_client

    def build(
        self,
        columns: List[ColumnSchema],
        relations: List[TableRelation],
    ) -> SchemaIndexBuildResult:
        """执行索引构建。"""
        if not columns:
            raise ValueError("columns 为空，无法构建字段索引。")

        documents = self.text_builder.build_many(columns)

        vector_texts = [doc.vector_text for doc in documents]
        embeddings = self.embedding_client.embed_texts(vector_texts)

        self.index_client.prepare_collections()

        self.index_client.insert_field_documents(
            documents=documents,
            embeddings=[
                embedding.astype(float).tolist()
                for embedding in embeddings
            ],
        )

        self.index_client.insert_relations(relations)

        return SchemaIndexBuildResult(
            field_collection_name=self.index_client.config.field_table_name,
            relation_collection_name=self.index_client.config.relation_table_name,
            field_document_count=len(documents),
            relation_count=len(relations),
        )

    def build_documents_only(
        self,
        columns: List[ColumnSchema],
    ) -> List[FieldDocument]:
        """只构建字段文档，不写入向量库。"""
        return self.text_builder.build_many(columns)
