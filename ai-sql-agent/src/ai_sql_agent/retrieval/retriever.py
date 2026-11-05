from __future__ import annotations

from typing import Dict, List

from .bm25 import BM25Index
from .document_builder import FieldDocumentBuilder
from .objects import ColumnSchema, FieldDocument, SchemaHit, TableRelation, TableSchema
from .tokenizer import tokenize


class SchemaRetriever:
    """
    字段级 Schema 检索器。

    当前 Demo 实现：
    - 字段级文档构建
    - BM25关键词召回
    - 简单业务别名 boost

    后续扩展：
    - 加 VectorIndex
    - 加 Reranker
    - 加 LLM Query 关键词提取
    """

    def __init__(
        self,
        tables: Dict[str, TableSchema],
        columns: List[ColumnSchema],
        relations: List[TableRelation],
    ):
        self.tables = tables
        self.columns = columns
        self.relations = relations

        self.documents: List[FieldDocument] = [
            FieldDocumentBuilder.build(column)
            for column in self.columns
        ]

        self.keyword_index: BM25Index | None = None

    def build(self) -> None:
        keyword_docs = [doc.keyword_text for doc in self.documents]
        self.keyword_index = BM25Index(keyword_docs)

    def retrieve(self, query: str, top_k: int = 10) -> List[SchemaHit]:
        if self.keyword_index is None:
            raise RuntimeError("SchemaRetriever 尚未 build，请先调用 retriever.build()。")

        raw_hits = self.keyword_index.search(query, top_k=max(top_k * 3, 20))

        hits: List[SchemaHit] = []

        for doc_idx, bm25_score in raw_hits:
            doc = self.documents[doc_idx]
            boost_score = self._business_boost(query, doc)
            final_score = bm25_score + boost_score

            hits.append(
                SchemaHit(
                    doc_id=doc.doc_id,
                    score=final_score,
                    column=doc.column,
                )
            )

        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:top_k]

    def _business_boost(self, query: str, doc: FieldDocument) -> float:
        """
        简单业务规则加分：
        - Query 直接包含字段名、字段别名、表别名时加分。
        - Query token 与字段描述 token 重合时加分。

        这部分后续可以替换为 rerank 模型。
        """
        query_lower = query.lower()
        query_tokens = set(tokenize(query))
        column = doc.column

        boost = 0.0

        if column.column_name.lower() in query_lower:
            boost += 3.0

        if column.table_name.lower() in query_lower:
            boost += 1.0

        for alias in column.aliases:
            if alias and alias.lower() in query_lower:
                boost += 4.0

        for alias in column.table_aliases:
            if alias and alias.lower() in query_lower:
                boost += 1.5

        description_tokens = set(tokenize(column.description))
        table_description_tokens = set(tokenize(column.table_description))

        boost += 0.2 * len(query_tokens & description_tokens)
        boost += 0.1 * len(query_tokens & table_description_tokens)

        return boost
