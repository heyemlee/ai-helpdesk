from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Sequence

import numpy as np

from .objects import LongTermMemory, MemoryHit, MemoryKind
from .storage import SQLiteMemoryStore
from .summarizer import ExtractiveMemorySummarizer, MemorySummarizer
from .vector_store import LongTermVectorIndex


class EmbeddingClient(Protocol):
    def embed_texts(self, texts: List[str]) -> np.ndarray: ...


class MemoryReranker(Protocol):
    def rerank(self, query: str, documents: List[str], top_n: int) -> List[tuple[int, float]]: ...


class LocalMemoryEmbeddingClient:
    """离线可运行的 Hash 向量；生产环境可注入线上 Embedding Client。"""

    def __init__(self, dimensions: int = 1024):
        self.dimensions = dimensions

    def embed_texts(self, texts: List[str]) -> np.ndarray:
        vectors = np.zeros((len(texts), self.dimensions), dtype=np.float32)
        for row_index, text in enumerate(texts):
            for token in self._tokenize(text):
                digest = hashlib.md5(token.encode("utf-8")).hexdigest()
                vectors[row_index, int(digest, 16) % self.dimensions] += 1.0
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors / np.maximum(norms, 1e-8)

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        lowered = text.lower()
        tokens = re.findall(r"[a-zA-Z0-9_]+", lowered)
        for span in re.findall(r"[\u4e00-\u9fff]+", lowered):
            tokens.extend(list(span))
            tokens.extend(span[index:index + 2] for index in range(len(span) - 1))
            tokens.extend(span[index:index + 3] for index in range(len(span) - 2))
        return [token for token in tokens if token]


@dataclass
class LongTermMemoryConfig:
    default_top_k: int = 5
    candidate_multiplier: int = 4
    min_vector_score: float = 0.05


class LongTermMemoryService:
    """用户主动写入、按需开启召回的个人知识库服务。"""

    def __init__(
        self,
        store: SQLiteMemoryStore,
        embedding_client: EmbeddingClient,
        summarizer: Optional[MemorySummarizer] = None,
        reranker: Optional[MemoryReranker] = None,
        vector_index: Optional[LongTermVectorIndex] = None,
        config: Optional[LongTermMemoryConfig] = None,
    ):
        self.store = store
        self.embedding_client = embedding_client
        self.summarizer = summarizer or ExtractiveMemorySummarizer()
        self.reranker = reranker
        self.vector_index = vector_index
        self.config = config or LongTermMemoryConfig()

    def save(
        self,
        *,
        user_id: str,
        kind: MemoryKind | str,
        content: Any,
        metadata: Optional[Dict[str, Any]] = None,
        summary: Optional[str] = None,
    ) -> LongTermMemory:
        self._validate_user(user_id)
        resolved_kind = kind if isinstance(kind, MemoryKind) else MemoryKind(kind)
        resolved_metadata = dict(metadata or {})
        resolved_summary = summary or self.summarizer.summarize_memory(
            kind=resolved_kind,
            content=content,
            metadata=resolved_metadata,
        )
        vectors = self.embedding_client.embed_texts([resolved_summary])
        if vectors.shape[0] != 1:
            raise ValueError("Embedding Client 必须为单条摘要返回一个向量")
        memory = self.store.save_long_term_memory(
            user_id=user_id,
            kind=resolved_kind,
            summary=resolved_summary,
            content=content,
            metadata=resolved_metadata,
            embedding=vectors[0],
        )
        if self.vector_index:
            self.vector_index.upsert(memory)
        return memory

    def save_structured(
        self,
        *,
        user_id: str,
        query: str,
        result: Any,
        database: str = "",
        tables: Optional[List[str]] = None,
        columns: Optional[List[str]] = None,
        filters: Optional[Dict[str, Any]] = None,
        cot: str = "",
        sql: str | List[str] = "",
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> LongTermMemory:
        metadata: Dict[str, Any] = {
            "query": query,
            "database": database,
            "tables": tables or [],
            "columns": columns or [],
            "filters": filters or {},
            "cot": cot,
            "sql": sql,
        }
        metadata.update(extra_metadata or {})
        return self.save(
            user_id=user_id,
            kind=MemoryKind.STRUCTURED,
            content={"query": query, "result": result},
            metadata=metadata,
        )

    def save_unstructured(
        self,
        *,
        user_id: str,
        content: Any,
        title: str = "",
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> LongTermMemory:
        metadata = {"title": title, **(extra_metadata or {})}
        return self.save(
            user_id=user_id,
            kind=MemoryKind.UNSTRUCTURED,
            content=content,
            metadata=metadata,
        )

    def recall(
        self,
        *,
        user_id: str,
        query: str,
        top_k: Optional[int] = None,
        kinds: Optional[Sequence[MemoryKind]] = None,
        metadata_filters: Optional[Dict[str, Any]] = None,
    ) -> List[MemoryHit]:
        self._validate_user(user_id)
        limit = top_k or self.config.default_top_k
        if limit < 1:
            return []
        query_vector = np.asarray(self.embedding_client.embed_texts([query])[0], dtype=np.float32)
        query_norm = float(np.linalg.norm(query_vector))
        if query_norm == 0:
            return []
        scored: List[MemoryHit] = []
        candidate_limit = limit * self.config.candidate_multiplier
        if self.vector_index:
            indexed_hits = self.vector_index.search(
                user_id=user_id,
                query_embedding=query_vector,
                top_k=candidate_limit,
                kinds=kinds,
            )
            for memory_id, score in indexed_hits:
                memory = self.store.get_long_term_memory(user_id=user_id, memory_id=memory_id)
                if memory and self._matches_filters(memory, metadata_filters or {}):
                    if score >= self.config.min_vector_score:
                        scored.append(MemoryHit(memory=memory, score=score, vector_score=score))
        else:
            memories = self.store.list_long_term_memories(user_id=user_id, kinds=kinds)
            memories = [m for m in memories if self._matches_filters(m, metadata_filters or {})]
            for memory in memories:
                vector = np.asarray(memory.embedding, dtype=np.float32)
                if vector.shape != query_vector.shape:
                    continue
                denominator = query_norm * float(np.linalg.norm(vector))
                score = float(np.dot(query_vector, vector) / denominator) if denominator else 0.0
                if score >= self.config.min_vector_score:
                    scored.append(MemoryHit(memory=memory, score=score, vector_score=score))
        scored.sort(key=lambda hit: hit.vector_score, reverse=True)
        candidates = scored[:candidate_limit]
        if not self.reranker or not candidates:
            return candidates[:limit]
        reranked = self.reranker.rerank(
            query, [hit.memory.summary for hit in candidates], min(limit, len(candidates))
        )
        output: List[MemoryHit] = []
        for index, rerank_score in reranked:
            if not 0 <= index < len(candidates):
                continue
            candidate = candidates[index]
            output.append(
                MemoryHit(
                    memory=candidate.memory,
                    score=float(rerank_score),
                    vector_score=candidate.vector_score,
                    rerank_score=float(rerank_score),
                )
            )
        return output[:limit]

    def delete(self, *, user_id: str, memory_id: str) -> bool:
        self._validate_user(user_id)
        if self.vector_index:
            self.vector_index.delete(user_id=user_id, memory_id=memory_id)
        return self.store.delete_long_term_memory(user_id=user_id, memory_id=memory_id)

    @staticmethod
    def _matches_filters(memory: LongTermMemory, filters: Dict[str, Any]) -> bool:
        for key, expected in filters.items():
            actual = memory.metadata.get(key)
            if isinstance(expected, (list, tuple, set)):
                expected_values = set(expected)
                actual_values = set(actual) if isinstance(actual, list) else {actual}
                if not expected_values.intersection(actual_values):
                    return False
            elif actual != expected:
                return False
        return True

    @staticmethod
    def _validate_user(user_id: str) -> None:
        if not user_id.strip():
            raise ValueError("user_id 不能为空")
