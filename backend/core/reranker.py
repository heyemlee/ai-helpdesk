"""
Cross-encoder 重排客户端（Cohere / Voyage / bge-reranker 的 /rerank 端点通用）。

为什么不直接用 LLM 重排：LLM listwise 重排质量不差，但它是一次完整的
生成调用——一次网络往返、一份 prompt token、一段推理时间。cross-encoder
是判别式模型，query 和候选拼在一起过一遍前向，延迟通常在几十毫秒量级，
成本低一到两个数量级。分工是：**便宜的召回拿候选，cross-encoder 排序，
只在最顶端才动用 LLM**。

设计与 core/embedder.py 一致：配置了远端就用远端，没配置或调用失败就
返回 None，让调用方回落到已有的 LLM listwise 重排。返回 None 而不是抛异常，
是因为"重排不可用"不是错误——原始召回顺序仍然是可用结果，链路不该因此中断。

失败后本进程永久降级（`_remote_ok = False`）：重排端点挂掉时，每次请求都
重试一遍只会把它的超时叠加到用户延迟上。熔断交给 MCPToolManager 管工具，
这里管的是一个可选增强项，直接放弃更合适。
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)


class Reranker:
    """远端 cross-encoder 重排，未配置时 enabled=False。"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str = "rerank-multilingual-v3.0",
        timeout_s: float = 10.0,
    ):
        self._base_url = (base_url or "").rstrip("/") or None
        self._api_key = api_key or None
        self._model = model
        self._timeout_s = timeout_s
        self._remote_ok = bool(self._base_url and self._api_key)

    @classmethod
    def from_env(cls) -> Reranker:
        return cls(
            base_url=os.getenv("RERANK_BASE_URL", "").strip() or None,
            api_key=os.getenv("RERANK_API_KEY", "").strip() or None,
            model=os.getenv("RERANK_MODEL", "rerank-multilingual-v3.0").strip(),
            timeout_s=float(os.getenv("RERANK_TIMEOUT_S", "10")),
        )

    @property
    def enabled(self) -> bool:
        return self._remote_ok

    @property
    def backend(self) -> str:
        return f"cross_encoder:{self._model}" if self._remote_ok else "llm_listwise"

    async def rerank(
        self, query: str, documents: list[str], top_k: int
    ) -> list[tuple[int, float]] | None:
        """
        返回 [(原始下标, 相关性分数)]，按分数降序，最多 top_k 条。
        未启用或调用失败时返回 None —— 调用方据此回落 LLM 重排。
        """
        if not self._remote_ok or not documents or not query.strip():
            return None
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                resp = await client.post(
                    f"{self._base_url}/rerank",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "model": self._model,
                        "query": query,
                        "documents": documents,
                        "top_n": top_k,
                    },
                )
                resp.raise_for_status()
                payload = resp.json()
        except Exception as ex:
            logger.warning(f"cross-encoder 重排失败，本进程降级为 LLM 重排: {ex}")
            self._remote_ok = False
            return None

        results = payload.get("results") or payload.get("data") or []
        ranked: list[tuple[int, float]] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            score = item.get("relevance_score", item.get("score", 0.0))
            if isinstance(idx, int) and 0 <= idx < len(documents):
                ranked.append((idx, float(score)))

        if not ranked:
            logger.warning("cross-encoder 返回了无法解析的结果，回落 LLM 重排")
            return None
        ranked.sort(key=lambda x: x[1], reverse=True)
        return ranked[:top_k]
