from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

from ai_sql_agent.core import ModelTransport, get_settings
from ai_sql_agent.core.errors import ModelResponseError


@dataclass
class AliyunEmbeddingConfig:
    """阿里云百炼 Embedding 配置；留空字段由全局 Settings 补齐。"""

    api_key: str = ""
    embedding_url: str = ""
    model: str = ""
    dimensions: int = 0
    timeout: Optional[float] = None
    use_mock_when_no_api_key: bool = True


class AliyunEmbeddingClient:
    """
    Embedding Client（离线建索引用）。

    有 API Key 时调用阿里云百炼 Embedding（HTTP 治理走 ai_sql_agent.core）。
    无 API Key 时使用本地 Hash Embedding，用于跑通索引构建流程。
    """

    COMPONENT = "embedding_indexing"

    def __init__(self, config: AliyunEmbeddingConfig | None = None):
        self.config = config or AliyunEmbeddingConfig()
        settings = get_settings()

        if not self.config.api_key:
            self.config.api_key = settings.resolved_embedding_api_key()
        if not self.config.embedding_url:
            self.config.embedding_url = settings.embedding_url
        if not self.config.model:
            self.config.model = settings.embedding_model
        if not self.config.dimensions:
            self.config.dimensions = settings.embedding_dimensions

        self.transport = ModelTransport(
            self.COMPONENT,
            api_key=self.config.api_key,
            timeout=self.config.timeout,
        )

    @property
    def dimensions(self) -> int:
        return self.config.dimensions

    def embed_texts(self, texts: List[str]) -> np.ndarray:
        """批量生成文本向量。"""
        if not texts:
            return np.empty((0, self.config.dimensions), dtype=np.float32)

        if self.config.api_key:
            return self._call_aliyun_embedding(texts)

        if self.config.use_mock_when_no_api_key:
            return self._mock_embedding(texts)

        raise ValueError("缺少 Embedding 凭证（AI_SQL_EMBEDDING_API_KEY / OPENAI_API_KEY），无法调用真实 Embedding API。")

    def _call_aliyun_embedding(self, texts: List[str]) -> np.ndarray:
        """调用阿里云百炼 Embedding API。"""
        result = self.transport.post_json(
            self.config.embedding_url,
            {
                "model": self.config.model,
                "input": texts,
                "dimensions": self.config.dimensions,
                "encoding_format": "float",
            },
            model=self.config.model,
        )

        try:
            data_items = sorted(result["data"], key=lambda item: item.get("index", 0))
            embeddings = [item["embedding"] for item in data_items]
        except (KeyError, TypeError) as exc:
            raise ModelResponseError(
                f"解析 Embedding 返回失败，原始返回：{str(result)[:500]}",
                component=self.COMPONENT,
                model=self.config.model,
            ) from exc

        return self._normalize(np.array(embeddings, dtype=np.float32))

    def _mock_embedding(self, texts: List[str]) -> np.ndarray:
        """本地 Hash Embedding。"""
        vectors = np.zeros((len(texts), self.config.dimensions), dtype=np.float32)

        for row_index, text in enumerate(texts):
            for token in self._simple_tokenize(text):
                digest = hashlib.md5(token.encode("utf-8")).hexdigest()
                col_index = int(digest, 16) % self.config.dimensions
                vectors[row_index, col_index] += 1.0

        return self._normalize(vectors)

    def _simple_tokenize(self, text: str) -> List[str]:
        """Mock Embedding 使用的简单中英文切分。"""
        text = text.lower()
        tokens: List[str] = []

        tokens.extend(re.findall(r"[a-zA-Z0-9_]+", text))

        chinese_spans = re.findall(r"[\u4e00-\u9fff]+", text)
        for span in chinese_spans:
            tokens.append(span)
            tokens.extend(list(span))

            for i in range(len(span) - 1):
                tokens.append(span[i:i + 2])

            for i in range(len(span) - 2):
                tokens.append(span[i:i + 3])

        return [token for token in tokens if token.strip()]

    def _normalize(self, vectors: np.ndarray) -> np.ndarray:
        """L2 归一化。"""
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors / np.maximum(norms, 1e-8)
