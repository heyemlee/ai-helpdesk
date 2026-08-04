"""
统一文本向量化入口。

背景：Chroma 自带嵌入模型，换 pgvector 后向量要自己生成。这个模块把
"文本 → 向量"收敛到一处，知识库、情景记忆和意图识别共用，两条路：

  1. 远端：配置 EMBEDDING_BASE_URL / EMBEDDING_API_KEY 时走 OpenAI 兼容
     /embeddings 端点（text-embedding-3-small = 1536 维）；
  2. 本地：未配置时用字符 n-gram 哈希向量（256 维，确定性、零依赖），
     与 core/intent_recognizer.py 的本地向量同款算法。

**走哪条路由配置决定，运行期不换。** 两条路产出的向量不在同一个空间里：
拿本地哈希向量去和库里的远端向量算余弦，得到的是一个和内容无关的数。
所以配了远端之后，远端失败就是失败（抛 EmbeddingUnavailable），不拿本地
向量顶上——以前是"失败一次，本进程永久改用本地向量，维度沿用远端的"：
维度对得上所以不报错，查询那一路从此在库里随机取片段，这期间导入的文档
还会把哈希向量写进库，重启也救不回来，只能重建。

失败之后有一段冷却期（默认 30 秒）：期间直接抛错、不再发请求，免得每次
调用都把一次网络超时叠到用户的等待上；过了冷却期再试，恢复了就自动回来。
调用方各自决定怎么降级：知识库检索只走关键词那一路，导入报错不落库，
情景记忆跳过这一轮，意图识别的 Embedding 一路改用本地向量现算。

向量维度写进 pgvector 的建表 DDL，因此**换嵌入后端必须重建库里的向量**
（EMBEDDING_DIM 变了老向量就没有意义）：用 scripts/reembed.py。启动时
Database.init_schema 会校验现有表维度，不一致直接报错而不是静默存入错位向量。
"""
import hashlib
import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

LOCAL_DIM = 256


class EmbeddingUnavailable(RuntimeError):
    """配置了远端 Embedding，但这会儿拿不到向量。怎么降级由调用方决定。"""


class Embedder:
    """文本向量化。配了远端就只用远端，没配才用本地 n-gram，两种向量不混用。"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str = "text-embedding-3-small",
        dim: int | None = None,
        batch_size: int = 10,
        retry_after_s: float = 30.0,
        query_prefix: str = "",
    ):
        self._base_url = (base_url or "").rstrip("/") or None
        self._api_key = api_key or None
        self._model = model
        self._remote = bool(self._base_url and self._api_key)
        # 维度：显式指定 > 远端默认 1536 > 本地 256
        if dim:
            self.dim = int(dim)
        else:
            self.dim = 1536 if self._remote else LOCAL_DIM
        # 一次请求带几段文本。各家上限不一样（有的只收 10 段），所以默认取小的。
        self._batch_size = max(1, int(batch_size))
        self._retry_after_s = retry_after_s
        # 只加在检索查询前面的任务说明，见 embed_query。本地 n-gram 向量用不上。
        self._query_prefix = query_prefix if self._remote else ""
        self._down_until = 0.0   # time.monotonic() 时刻；在这之前不再打远端
        self._last_error = ""

    @classmethod
    def from_env(cls) -> "Embedder":
        dim_raw = os.getenv("EMBEDDING_DIM", "").strip()
        batch_raw = os.getenv("EMBEDDING_BATCH_SIZE", "").strip()
        return cls(
            base_url=os.getenv("EMBEDDING_BASE_URL", "").strip() or None,
            api_key=os.getenv("EMBEDDING_API_KEY", "").strip() or None,
            model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small").strip(),
            dim=int(dim_raw) if dim_raw else None,
            batch_size=int(batch_raw) if batch_raw else 10,
            # 换行在配置里写成 \n，这里还原：.env 和 compose 的 env_file 对引号、
            # 转义的处理不一样，不指望它们替我们转。
            query_prefix=os.getenv("EMBEDDING_QUERY_PREFIX", "").replace("\\n", "\n"),
        )

    @property
    def backend(self) -> str:
        return "remote" if self._remote else "local_ngram"

    @property
    def semantic(self) -> bool:
        """
        这种向量有没有语义。

        本地 n-gram 向量量的是字符重合，和关键词检索是同一种信号、只是更吵。
        检索融合时不能把它当成独立的一路来投票，见 knowledge_base.lexical_first_fuse。
        """
        return self._remote

    @property
    def signature(self) -> str:
        """
        这个 Embedder 产出的向量属于哪个空间：模型名 + 维度。

        库里记着存量向量的 signature（见 vector_store.check_embedding_signature），
        和这里对不上就说明换过模型，得先重建向量。
        """
        return f"{self._model}/{self.dim}" if self._remote else f"local_ngram/{self.dim}"

    @property
    def available(self) -> bool:
        """这会儿能不能出向量。本地向量永远可以；远端在失败后的冷却期内不行。"""
        return not self._remote or time.monotonic() >= self._down_until

    def status(self) -> dict[str, Any]:
        """给 /health 看的。不带端点地址和报错原文——/health 在对外入口上匿名可读。"""
        return {
            "backend": self.backend,
            "model": self._model if self._remote else "local_ngram",
            "dim": self.dim,
            "available": self.available,
        }

    async def embed(self, text: str) -> list[float]:
        return (await self.embed_many([text]))[0]

    async def embed_query(self, text: str) -> list[float]:
        """
        检索查询的向量。

        有的 Embedding 模型区分"查询"和"文档"：文档原样送去，查询前面要加一句任务
        说明（Qwen3-Embedding 的模型卡写的是不加会掉 1%–5%）。这句话由
        EMBEDDING_QUERY_PREFIX 配置，没配就和 embed 一样。它只影响查询这一侧，
        库里的文档向量不用重算，所以不算进 signature。
        """
        return await self.embed(f"{self._query_prefix}{text}")

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        """
        一批文本 → 等长、同序的一批向量。

        远端按 batch_size 分批请求：导入一篇切成十几片的文档、或者意图识别
        首次加载几十条模板时，一段一个请求会把延迟叠成十几秒。
        """
        if not self._remote:
            return [self.local_embedding(text, self.dim) for text in texts]
        if not texts:
            return []
        if time.monotonic() < self._down_until:
            raise EmbeddingUnavailable(f"远端 Embedding 冷却中，上次失败：{self._last_error}")
        try:
            vectors: list[list[float]] = []
            for start in range(0, len(texts), self._batch_size):
                vectors.extend(await self._remote_embed(texts[start:start + self._batch_size]))
            return vectors
        except Exception as ex:
            self._down_until = time.monotonic() + self._retry_after_s
            self._last_error = f"{type(ex).__name__}: {ex}"[:300]
            logger.warning(
                f"远端 Embedding 不可用，{self._retry_after_s:.0f} 秒内不再重试: {self._last_error}"
            )
            raise EmbeddingUnavailable(self._last_error) from ex

    async def _remote_embed(self, texts: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self._base_url}/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self._model, "input": texts},
            )
            resp.raise_for_status()
            data = resp.json()["data"]
        # 对应关系看 index，不看返回的先后：接口没有承诺按输入顺序排
        ordered = sorted(data, key=lambda item: item.get("index", 0))
        vectors = [list(item["embedding"]) for item in ordered]
        if len(vectors) != len(texts):
            raise ValueError(f"送去 {len(texts)} 段文本，远端返回 {len(vectors)} 个向量")
        for vec in vectors:
            if len(vec) != self.dim:
                raise ValueError(
                    f"远端向量维度 {len(vec)} 与配置的 EMBEDDING_DIM={self.dim} 不符"
                )
        return vectors

    @staticmethod
    def local_embedding(text: str, dims: int = LOCAL_DIM) -> list[float]:
        """稳定的字符 n-gram 哈希向量。与 intent_recognizer 的本地向量同算法。"""
        normalized = (text or "").lower().strip()
        vec = [0.0] * dims
        tokens = set()
        for n in (1, 2, 3):
            if len(normalized) >= n:
                tokens.update(normalized[i:i + n] for i in range(len(normalized) - n + 1))
        if not tokens:
            tokens.add(normalized)
        for token in tokens:
            digest = hashlib.md5(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % dims
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[idx] += sign
        return vec


def cosine(a: list[float], b: list[float]) -> float:
    """纯 Python 余弦相似度，本地兜底后端的暴力检索用。"""
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0
