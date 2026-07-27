"""
统一文本向量化入口。

背景：Chroma 自带嵌入模型，换 pgvector 后向量要自己生成。这个模块把
"文本 → 向量"收敛到一处，知识库和情景记忆共用，两条路：

  1. 远端：配置 EMBEDDING_BASE_URL / EMBEDDING_API_KEY 时走 OpenAI 兼容
     /embeddings 端点（text-embedding-3-small = 1536 维）；
  2. 本地：未配置时退化为字符 n-gram 哈希向量（256 维，确定性、零依赖），
     与 core/intent_recognizer.py 的本地向量同款算法。

向量维度写进 pgvector 的建表 DDL，因此**换嵌入后端必须重建向量表**
（EMBEDDING_DIM 变了老向量就没有意义）。启动时 Database.init_schema
会校验现有表维度，不一致直接报错而不是静默存入错位向量。
"""
import hashlib
import logging
import os

import httpx

logger = logging.getLogger(__name__)

LOCAL_DIM = 256


class Embedder:
    """远端优先、本地兜底的文本向量化。失败一次后本进程永久降级本地。"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str = "text-embedding-3-small",
        dim: int | None = None,
    ):
        self._base_url = (base_url or "").rstrip("/") or None
        self._api_key = api_key or None
        self._model = model
        self._remote_ok = bool(self._base_url and self._api_key)
        # 维度：显式指定 > 远端默认 1536 > 本地 256
        if dim:
            self.dim = int(dim)
        else:
            self.dim = 1536 if self._remote_ok else LOCAL_DIM

    @classmethod
    def from_env(cls) -> "Embedder":
        dim_raw = os.getenv("EMBEDDING_DIM", "").strip()
        return cls(
            base_url=os.getenv("EMBEDDING_BASE_URL", "").strip() or None,
            api_key=os.getenv("EMBEDDING_API_KEY", "").strip() or None,
            model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small").strip(),
            dim=int(dim_raw) if dim_raw else None,
        )

    @property
    def backend(self) -> str:
        return "remote" if self._remote_ok else "local_ngram"

    async def embed(self, text: str) -> list[float]:
        if self._remote_ok:
            try:
                return await self._remote_embed(text)
            except Exception as ex:
                self._remote_ok = False
                logger.warning(f"远端 Embedding 不可用，本进程降级为本地向量: {ex}")
        return self.local_embedding(text, self.dim)

    async def _remote_embed(self, text: str) -> list[float]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self._base_url}/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self._model, "input": text},
            )
            resp.raise_for_status()
            vec = list(resp.json()["data"][0]["embedding"])
        if len(vec) != self.dim:
            raise ValueError(
                f"远端向量维度 {len(vec)} 与配置的 EMBEDDING_DIM={self.dim} 不符"
            )
        return vec

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
