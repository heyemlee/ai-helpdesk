"""LLM response helpers shared by Anthropic-compatible providers."""
import os
from collections.abc import Iterable
from typing import Any

# 结构化调用（意图识别、LLM Judge、查询改写、重排）的 token 预算。
#
# 为什么不是 256：推理模型（deepseek-v4-pro、claude 的 thinking 模式等）会先输出
# 一整个 thinking 块再输出正文。256 的预算会在 thinking 阶段就耗尽，stop_reason
# 变成 max_tokens，正文块根本不存在——调用方拿到空字符串，JSON 解析失败，然后
# 静默降级。这类失败最阴险的地方是它不报错：意图识别退化成两路投票、Judge 全给
# 0.5 默认分、RAG 改写和重排直接不生效，链路"看起来还在跑"。
#
# 这几个调用的真实产出都不到 150 token，调高上限不增加费用（按实际生成量计费），
# 只是给推理留出余量。
STRUCTURED_MAX_TOKENS = int(os.getenv("AI_HELPDESK_STRUCTURED_MAX_TOKENS", "2048"))

# Agent 生成回复的 token 预算。同样的坑，但更容易踩：system prompt 里注入了
# 整份 SKILL.md，user 侧还挂着 RAG 检索出处，输入长 → 推理也长。1024 的预算
# 在简单问题上够用、在复杂问题上不够，表现为**同一个用例时好时坏**——
# 空正文被当成一句合法的空回答返回给用户，judge 打 0 分，看起来像模型不稳定。
ANSWER_MAX_TOKENS = int(os.getenv("AI_HELPDESK_ANSWER_MAX_TOKENS", "4096"))


def extract_structured_text(resp: Any, budget_env: str = "AI_HELPDESK_STRUCTURED_MAX_TOKENS") -> str:
    """
    取出模型正文，并把"推理把预算吃光"这种静默失败翻译成明确报错。

    直接对空字符串做 json.loads 只会得到 "Expecting value: line 1 column 1"，
    看不出根因；对 Agent 回复来说更糟——空字符串会被当成一句合法的回答发给用户。
    这里把 stop_reason 和 output_tokens 一起报出来，让调用方的降级链路接管。
    """
    text = extract_text_content(getattr(resp, "content", resp))
    if text.strip():
        return text
    if getattr(resp, "stop_reason", None) == "max_tokens":
        used = getattr(getattr(resp, "usage", None), "output_tokens", "?")
        raise ValueError(
            f"模型在生成正文前耗尽了 max_tokens（output_tokens={used}）。"
            f"推理模型会先输出 thinking 块——调高 {budget_env}。"
        )
    raise ValueError("模型返回了空正文")


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
