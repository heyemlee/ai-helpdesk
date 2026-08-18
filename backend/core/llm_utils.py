"""LLM response helpers shared by Anthropic-compatible providers."""
import os
from collections.abc import Iterable
from typing import Any


def anthropic_client_kwargs(api_key: str, base_url: str | None = None) -> dict[str, Any]:
    """
    构造 AsyncAnthropic 的公共参数，统一超时和重试策略。

    没有超时的 LLM 调用在网关 hang 住时会让请求无限挂起；SDK 默认重试
    也应该显式可配，而不是散落在各处的隐式默认。所有创建客户端的模块
    （编排器、意图识别、工具管理、记忆、评测）都走这一个入口。
    """
    kwargs: dict[str, Any] = {
        "api_key": api_key,
        "timeout": float(os.getenv("LLM_TIMEOUT_S", "60")),
        "max_retries": int(os.getenv("LLM_MAX_RETRIES", "2")),
    }
    if base_url:
        kwargs["base_url"] = base_url
    return kwargs


def extract_text_content(content: Iterable[Any]) -> str:
    """Return text blocks from Anthropic-style response content."""
    texts: list[str] = []
    for block in content or []:
        if isinstance(block, str):
            texts.append(block)
            continue

        block_type = getattr(block, "type", None)
        text = getattr(block, "text", None)
        if isinstance(block, dict):
            block_type = block.get("type", block_type)
            text = block.get("text", text)

        if isinstance(text, str) and (block_type in (None, "text")):
            texts.append(text)

    return "\n".join(t for t in texts if t)
