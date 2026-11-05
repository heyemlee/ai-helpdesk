from __future__ import annotations

from typing import List

from ai_sql_agent.retrieval.rerank_client import RerankDocument


class AliyunMemoryReranker:
    """把项目现有的阿里云 Rerank Client 适配到长期记忆候选排序。"""

    def __init__(self, client):
        self.client = client

    def rerank(self, query: str, documents: List[str], top_n: int) -> List[tuple[int, float]]:
        results = self.client.rerank(
            query=query,
            documents=[
                RerankDocument(doc_index=index, text=document)
                for index, document in enumerate(documents)
            ],
            top_n=top_n,
        )
        return [(item.doc_index, item.rerank_score) for item in results]
