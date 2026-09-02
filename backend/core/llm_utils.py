"""LLM 客户端工厂与响应解析，供 Anthropic 协议与 OpenAI 兼容协议共用。"""
import logging
import os
from collections.abc import Iterable
from typing import Any

from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)

# 全项目按 Anthropic 的 messages 形状调 LLM；OpenAI 兼容端点由
# core/openai_compat.py 翻译成同一套接口，所以调用点只认这个别名。
LLMClient = Any

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5-20250929"
DEFAULT_OPENAI_MODEL = "gpt-4o"

# 结构化调用（意图识别、LLM Judge、查询改写、重排）的 token 预算。
#
# 为什么不是 256：推理模型（claude 的 thinking 模式、OpenAI 的 reasoning 系列等）会先输出
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


# ── temperature 兼容 ──────────────────────────────────────────────────────────
#
# 新一代模型不再接受 temperature，网关直接回：
#   400 invalid_request_error: `temperature` is deprecated for this model
#
# 而项目里所有结构化调用（意图识别 / 记忆摘要与画像 / 查询改写 / 重排 / LLM Judge）
# 原本都显式传 temperature，于是这些调用**全部**失败，又各自在 except 里静默降级：
# 意图识别退化成 Embedding 单路投票、摘要和画像写不进去、RAG 改写与重排不生效、
# Judge 全给 0.5 默认分。Agent 回复不传 temperature，所以链路"看起来还在跑"——
# 这正是这类失败最难发现的地方（一次真实事故：问候语被 Embedding 噪声判成
# payment_issue 0.11，路由到账款 Agent，再因为置信度过低立刻开转人工工单）。
#
# 处理方式和远端 Embedding 那一路一致：探测一次，被拒就本进程永久降级。
# 不写死"哪些模型不支持"——那份名单一定会过期，而 400 的信号是准确的。
_SEND_TEMPERATURE = os.getenv("LLM_SEND_TEMPERATURE", "true").strip().lower() not in (
    "0", "false", "no", "off",
)


def temperature_enabled() -> bool:
    """当前是否还在给结构化调用发送 temperature。供 /health 和排障读取。"""
    return _SEND_TEMPERATURE


def reset_temperature_support(enabled: bool = True) -> None:
    """重置探测状态。供测试和配置热更新使用。"""
    global _SEND_TEMPERATURE
    _SEND_TEMPERATURE = enabled


def _rejects_temperature(ex: Exception) -> bool:
    """判断异常是不是"这个模型不接受 temperature"，而不是别的 400。"""
    status = getattr(ex, "status_code", None)
    if status is not None and status != 400:
        return False
    text = str(ex).lower()
    if "temperature" not in text:
        return False
    return any(
        kw in text
        for kw in ("deprecat", "unsupported", "not supported", "no longer", "invalid_request")
    )


async def structured_create(
    client: LLMClient,
    *,
    temperature: float | None = None,
    **kwargs: Any,
) -> Any:
    """
    结构化调用的统一入口：所有需要确定性输出的调用都走这里，不要直接
    `client.messages.create(temperature=...)`。

    带 temperature 发一次；模型明确拒收就关掉开关、原样重试一次，
    之后本进程不再发送该参数。降级只发生一次，不会每条消息白等一轮 400。
    """
    global _SEND_TEMPERATURE

    if temperature is None or not _SEND_TEMPERATURE:
        return await client.messages.create(**kwargs)

    try:
        return await client.messages.create(temperature=temperature, **kwargs)
    except Exception as ex:
        if not _rejects_temperature(ex):
            raise
        _SEND_TEMPERATURE = False
        logger.warning(
            "模型不接受 temperature（%s）；本进程起结构化调用不再发送该参数。", ex
        )
        return await client.messages.create(**kwargs)


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


def llm_provider() -> str:
    """
    当前使用哪套协议：anthropic（默认）或 openai。

    只影响"用哪个 SDK、读哪组环境变量"，不影响调用形状——两边都暴露
    client.messages.create/stream，见 core/openai_compat.py。
    """
    provider = os.getenv("LLM_PROVIDER", "anthropic").strip().lower() or "anthropic"
    if provider not in ("anthropic", "openai"):
        raise RuntimeError(f"LLM_PROVIDER 只支持 anthropic / openai，收到: {provider}")
    return provider


def create_llm_client(api_key: str, base_url: str | None = None) -> LLMClient:
    """
    按 LLM_PROVIDER 造客户端。所有创建客户端的模块（编排器、意图识别、
    工具管理、记忆、评测）都走这一个入口，换供应商不需要改调用点。
    """
    kwargs = anthropic_client_kwargs(api_key, base_url)
    if llm_provider() == "openai":
        from core.openai_compat import AsyncOpenAICompat

        return AsyncOpenAICompat(**kwargs)
    return AsyncAnthropic(**kwargs)


def resolve_llm_config() -> dict[str, Any]:
    """
    从环境变量解析出 api_key / model / base_url 三件套。

    两组变量各管各的，互不覆盖：切换 LLM_PROVIDER 时另一组原样留在 .env 里，
    随时切回去，不用重填 key。
    """
    provider = llm_provider()
    if provider == "openai":
        key = os.getenv("OPENAI_API_KEY", "").strip()
        if not key:
            raise RuntimeError("LLM_PROVIDER=openai 但未设置 OPENAI_API_KEY")
        cfg: dict[str, Any] = {
            "provider": provider,
            "api_key": key,
            "model": os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL).strip(),
        }
        base_url = os.getenv("OPENAI_BASE_URL", "").strip()
    else:
        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise RuntimeError("未设置 ANTHROPIC_API_KEY")
        cfg = {
            "provider": provider,
            "api_key": key,
            "model": os.getenv("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL).strip(),
        }
        base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip()
    if base_url:
        cfg["base_url"] = base_url
    return cfg


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
