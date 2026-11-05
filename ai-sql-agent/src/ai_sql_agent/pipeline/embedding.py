"""
Schema 检索用哪种向量。

两种：本地哈希向量（不调服务、没有语义）和远端 Embedding（OpenAI 兼容 /embeddings）。
选哪种看配置的 `embedding_backend`——环境变量、`.env`，或者页面保存的 Embedding 连接
（见 `ai_sql_agent.core.settings`）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ai_sql_agent.core import SqlAgentSettings, get_settings
from ai_sql_agent.retrieval.embedding_client import RemoteEmbeddingClient

from .local_clients import LocalHashEmbeddingClient

LOCAL_DIMENSIONS = 1024
"""本地哈希向量的维度。和以前写死在构建检索服务那里的一样。"""


def local_embedding_client() -> LocalHashEmbeddingClient:
    return LocalHashEmbeddingClient(dimensions=LOCAL_DIMENSIONS)


def remote_embedding_client(
    *, url: str, api_key: str, model: str, dimensions: int, settings: Optional[SqlAgentSettings] = None
) -> RemoteEmbeddingClient:
    """按给定的连接建一个远端 Embedding Client；分批大小和超时取配置。"""
    settings = settings or get_settings()
    return RemoteEmbeddingClient(
        url=url,
        api_key=api_key,
        model=model,
        dimensions=dimensions,
        batch_size=settings.embedding_batch_size,
        timeout=settings.embedding_timeout_seconds,
    )


def schema_embedding_client(settings: Optional[SqlAgentSettings] = None):
    """
    按当前配置给 Schema 检索建 Embedding Client。

    配的是远端、却缺 Key 时抛 ValueError——由构建检索服务的那一侧决定退回本地向量。
    """
    settings = settings or get_settings()
    if settings.embedding_backend != "remote":
        return local_embedding_client()
    return remote_embedding_client(
        url=settings.embedding_url,
        api_key=settings.resolved_embedding_api_key(),
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        settings=settings,
    )


def describe_embedding(client: Any, *, fallback: str = "") -> Dict[str, Any]:
    """检索索引实际在用的向量（给 `/admin/model` 和日志看）。不含 Key。"""
    config = getattr(client, "config", None)
    return {
        "backend": getattr(client, "backend", "local"),
        "model": getattr(config, "model", "") or "local_hash",
        "dimensions": int(getattr(client, "dimensions", 0) or 0),
        "signature": getattr(client, "signature", ""),
        "fallback": fallback,
    }
