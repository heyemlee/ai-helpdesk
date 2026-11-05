from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

from ai_sql_agent.core import ModelTransport, get_settings
from ai_sql_agent.core.errors import ModelCircuitOpenError, ModelResponseError
from ai_sql_agent.core.run_context import RunCancelled


@dataclass
class AliyunEmbeddingConfig:
    """
    阿里云百炼 Embedding 接口配置。

    默认使用 OpenAI 兼容 Embedding 接口。留空字段在构造 Client 时从全局
    Settings 补齐（AI_SQL_EMBEDDING_URL / DASHSCOPE_EMBEDDING_URL 可覆盖，
    业务空间专属域名直接填完整 embeddings 地址即可）。
    """

    api_key: str = ""
    """阿里云百炼 API Key。为空时取 Settings（DASHSCOPE_API_KEY）。"""

    embedding_url: str = ""
    """Embedding HTTP 接口地址，为空时取 Settings。"""

    model: str = ""
    """Embedding 模型名称，为空时取 Settings（默认 text-embedding-v4）。"""

    dimensions: int = 0
    """向量维度，0 时取 Settings（默认 1024）。"""

    timeout: Optional[float] = None
    """HTTP 超时时间（秒），None 时取 Settings。"""


class AliyunEmbeddingClient:
    """
    阿里云百炼 Embedding Client。

    负责调用 Embedding API 把文本转成归一化向量；HTTP 超时、限流退避重试、
    错误分类与用量统计统一由 ai_sql_agent.core.ModelTransport 处理。
    """

    COMPONENT = "embedding"

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

        if not self.config.api_key:
            raise ValueError(
                "缺少 Embedding API Key。请在 .env 里配置 OPENAI_API_KEY 或 AI_SQL_EMBEDDING_API_KEY，"
                "或在 AliyunEmbeddingConfig(api_key='...') 中传入。"
            )

        self.transport = ModelTransport(
            self.COMPONENT,
            api_key=self.config.api_key,
            timeout=self.config.timeout,
        )

    @property
    def dimensions(self) -> int:
        return self.config.dimensions

    def embed_texts(self, texts: List[str]) -> np.ndarray:
        """
        批量生成文本向量。

        Args:
            texts: 待向量化的文本列表。

        Returns:
            np.ndarray: 归一化后的向量矩阵，形状为 [文本数量, 向量维度]。
        """
        if not texts:
            return np.empty((0, self.config.dimensions), dtype=np.float32)

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
            embeddings = [
                item["embedding"]
                for item in sorted(result["data"], key=lambda x: x.get("index", 0))
            ]
        except (KeyError, TypeError) as exc:
            raise ModelResponseError(
                f"解析 Embedding 返回失败，原始返回: {str(result)[:500]}",
                component=self.COMPONENT,
                model=self.config.model,
            ) from exc

        vectors = np.array(embeddings, dtype=np.float32)

        # 归一化，后续可以直接用点积计算 cosine similarity。
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors / np.maximum(norms, 1e-8)


@dataclass
class RemoteEmbeddingConfig:
    """运行期远端 Embedding 的连接。`model` 也给 pgvector 索引算指纹用。"""

    url: str
    model: str
    dimensions: int
    batch_size: int = 10


class RemoteEmbeddingClient:
    """
    Schema 检索运行期用的远端 Embedding（OpenAI 兼容 /embeddings）。

    和上面那个 Client 调的是同一个接口，多了三件运行期才需要的事：

    - **分批。** 建索引时一次要算几百段文本，按 `batch_size` 拆成多次请求（百炼一次最多 10 段）。
    - **缓存。** 检索按关键词逐个做向量召回，库、表、每个库的字段各一层，同一个关键词
      一个问题里要用好几次；Schema 重建时绝大多数文档也没变。算过的向量按文本记住
      （有上限，最久没用的先丢），同一段文本只请求一次。`prefetch` 让一个问题的全部
      关键词合成一次请求。
    - **冷却。** 调用失败之后的一小段时间里直接报错、不发请求：服务挂了的时候，
      一个问题里十几次向量召回不该各自等一遍超时。检索那一侧拿到错误就只走关键词召回。
    """

    COMPONENT = "embedding"
    backend = "remote"
    COOLDOWN_SECONDS = 30.0
    CACHE_SIZE = 4096

    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        model: str,
        dimensions: int,
        batch_size: int = 10,
        timeout: Optional[float] = None,
        transport: Optional[ModelTransport] = None,
        clock=None,
    ):
        if not api_key:
            raise ValueError("缺少 Embedding 的 API Key。")
        if not url or not model or int(dimensions) <= 0:
            raise ValueError("Embedding 的地址、模型名、维度都要有。")
        self.config = RemoteEmbeddingConfig(
            url=url, model=model, dimensions=int(dimensions), batch_size=max(1, int(batch_size))
        )
        self.transport = transport or ModelTransport(
            self.COMPONENT, api_key=api_key, timeout=timeout
        )
        self._clock = clock or time.monotonic
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._lock = threading.Lock()
        self._down_until = 0.0
        self.embedded_count = 0
        """真的请求服务端算过多少段文本（缓存命中的不算）。"""

    @property
    def dimensions(self) -> int:
        return self.config.dimensions

    @property
    def signature(self) -> str:
        """这些向量属于哪个空间：模型名 + 维度。"""
        return f"{self.config.model}/{self.config.dimensions}"

    @property
    def available(self) -> bool:
        """这会儿能不能出向量：刚失败过、还在冷却时不能。"""
        return self._clock() >= self._down_until

    def prefetch(self, texts: List[str]) -> None:
        """把还没算过的几段文本一次算好、记下（一个问题的全部关键词合成一次请求）。"""
        self.embed_texts([text for text in dict.fromkeys(texts) if text])

    def embed_texts(self, texts: List[str]) -> np.ndarray:
        """批量生成归一化向量，形状 [文本数量, 维度]。"""
        if not texts:
            return np.empty((0, self.config.dimensions), dtype=np.float32)

        found: dict = {}
        with self._lock:
            for text in dict.fromkeys(texts):
                vector = self._cache.get(text)
                if vector is not None:
                    self._cache.move_to_end(text)
                    found[text] = vector
        missing = [text for text in dict.fromkeys(texts) if text not in found]

        if missing:
            remaining = self._down_until - self._clock()
            if remaining > 0:
                raise ModelCircuitOpenError(
                    f"Embedding 服务刚刚调用失败，冷却中（约 {remaining:.0f}s 后再试），本次未发请求。",
                    component=self.COMPONENT,
                    model=self.config.model,
                )
            try:
                vectors = self._request(missing)
            except RunCancelled:
                # 请求被取消不是服务故障，不进冷却——否则一次取消会让远程 embedding 停 30 秒
                raise
            except Exception:
                self._down_until = self._clock() + self.COOLDOWN_SECONDS
                raise
            with self._lock:
                for text, vector in zip(missing, vectors):
                    found[text] = vector
                    self._cache[text] = vector
                    self._cache.move_to_end(text)
                while len(self._cache) > self.CACHE_SIZE:
                    self._cache.popitem(last=False)

        return np.vstack([found[text] for text in texts]).astype(np.float32)

    def _request(self, texts: List[str]) -> List[np.ndarray]:
        vectors: List[np.ndarray] = []
        size = self.config.batch_size
        for start in range(0, len(texts), size):
            batch = texts[start:start + size]
            result = self.transport.post_json(
                self.config.url,
                {
                    "model": self.config.model,
                    "input": batch,
                    "dimensions": self.config.dimensions,
                    "encoding_format": "float",
                },
                model=self.config.model,
            )
            try:
                rows = [
                    item["embedding"]
                    for item in sorted(result["data"], key=lambda item: item.get("index", 0))
                ]
            except (KeyError, TypeError, AttributeError) as exc:
                raise ModelResponseError(
                    f"解析 Embedding 返回失败，原始返回: {str(result)[:300]}",
                    component=self.COMPONENT,
                    model=self.config.model,
                ) from exc
            matrix = np.array(rows, dtype=np.float32)
            if matrix.ndim != 2 or matrix.shape[0] != len(batch):
                raise ModelResponseError(
                    f"Embedding 返回了 {len(rows)} 个向量，请求的是 {len(batch)} 段文本。",
                    component=self.COMPONENT,
                    model=self.config.model,
                )
            if matrix.shape[1] != self.config.dimensions:
                raise ModelResponseError(
                    f"Embedding 返回的是 {matrix.shape[1]} 维，配置的是 {self.config.dimensions} 维。",
                    component=self.COMPONENT,
                    model=self.config.model,
                )
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            vectors.extend(matrix / np.maximum(norms, 1e-8))
            self.embedded_count += len(batch)
        return vectors
