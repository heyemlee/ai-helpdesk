"""LLM 客户端工厂与响应解析，供 Anthropic 协议与 OpenAI 兼容协议共用。"""
import logging
import os
from collections.abc import Iterable
from typing import Any

from anthropic import AsyncAnthropic

from app.core.llm_guard import GuardedLLMClient, guard_for

logger = logging.getLogger(__name__)

# 全项目按 Anthropic 的 messages 形状调 LLM；OpenAI 兼容端点由
# core/openai_compat.py 翻译成同一套接口，所以调用点只认这个别名。
LLMClient = Any

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"
DEFAULT_OPENAI_MODEL = "gpt-5.6-luna"

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

# Agent 回复的思考深度。默认不传：Claude 这边回复本来就不开思考；OpenAI 的推理模型
# 不传就按它自己的默认档位（通常是 medium），会先想一阵再出第一个字。要压首字延迟
# 就设成 low。结构化调用的档位另见 STRUCTURED_EFFORT。
ANSWER_EFFORT = os.getenv("AI_HELPDESK_ANSWER_EFFORT", "").strip()


def answer_options() -> dict[str, Any]:
    """Agent 回复调用要多带的参数：配了 ANSWER_EFFORT 才有。"""
    return {"output_config": {"effort": ANSWER_EFFORT}} if ANSWER_EFFORT else {}


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


# ── 结构化调用 ────────────────────────────────────────────────────────────────
#
# 历史：这里曾有一整套 temperature 探测降级逻辑。新一代模型开始拒收 temperature
# （400 `temperature` is deprecated），而项目里所有结构化调用都显式传了它，于是
# 这些调用**全部**失败又各自在 except 里静默降级——意图识别退化成 Embedding 单路
# 投票、画像写不进去、RAG 改写与重排不生效、Judge 全给 0.5 默认分，而 Agent 回复
# 不传 temperature，链路"看起来还在跑"。
#
# 现在这套逻辑整个删掉了，因为 anthropic SDK 1.x 直接把 temperature/top_p/top_k
# 从 messages.create 的签名里移除了：再传就是 **TypeError**，不是 400。原来的探测
# 只认 400 + 关键词，TypeError 会被调用点的 `except Exception` 吃掉后照样静默降级
# ——同一个坑换了个形状。所以不做兼容，直接不传。
#
# 确定性由结构化输出接管：schema 由 API 保证，比压低 temperature 可靠得多。

# 结构化调用的思考深度。这几个调用（意图分类、查询改写、重排、判分）都是
# 判别式任务，不需要长推理；low 既省钱又让"推理阶段吃光 max_tokens、正文为空"
# 这类失败基本不会发生。留成环境变量是因为判分质量若不够可以单独抬。
STRUCTURED_EFFORT = os.getenv("AI_HELPDESK_STRUCTURED_EFFORT", "low").strip() or "low"


def _with_effort(kwargs: dict[str, Any]) -> dict[str, Any]:
    """
    给结构化调用统一补上 effort，调用点不用各写一遍。

    合并而不是覆盖：output_format 走的是 SDK 的 helper 参数，而 effort 属于
    output_config，两者可以并存；调用方若已经自带 output_config 就尊重它。
    """
    if not STRUCTURED_EFFORT:
        return kwargs
    config = dict(kwargs.get("output_config") or {})
    config.setdefault("effort", STRUCTURED_EFFORT)
    return {**kwargs, "output_config": config}


async def structured_create(client: LLMClient, **kwargs: Any) -> Any:
    """
    不带 schema 的结构化调用：产出是自由文本而非 JSON（例如会话摘要）。

    需要 JSON 的一律走 structured_parse——手工从正文里抠 `{...}` 已经是历史了。
    """
    return await client.messages.create(**_with_effort(kwargs))


async def structured_parse(
    client: LLMClient,
    *,
    output_format: type,
    budget_env: str = "AI_HELPDESK_STRUCTURED_MAX_TOKENS",
    **kwargs: Any,
) -> Any:
    """
    带 schema 的结构化调用：返回一个已经校验过的 Pydantic 实例。

    替代过去那套 `raw.find("{") … json.loads(raw[s:e])`。手工切片有三种失败方式
    ——模型加了前言、JSON 被 markdown 围栏包住、字段名或类型漂了——三种都表现为
    一次 except 里的静默降级，而不是一个能看见的错误。schema 由 API 保证之后，
    这三类失败在服务端就不成立了。

    拿不到解析结果时**抛错**，不返回 None：调用方各自有降级路径（意图退回投票、
    重排退回原序），但降级必须由它们显式决定，不能是这里悄悄返回一个空对象。
    """
    resp = await client.messages.parse(output_format=output_format, **_with_effort(kwargs))
    parsed = getattr(resp, "parsed_output", None)
    if parsed is not None:
        return parsed

    # 走到这里说明 schema 有了但正文没出来，最常见的原因还是预算被推理吃光。
    # 报错要把 stop_reason 和 output_tokens 带上，否则只剩一个 None 无从查起。
    if getattr(resp, "stop_reason", None) == "max_tokens":
        used = getattr(getattr(resp, "usage", None), "output_tokens", "?")
        raise ValueError(
            f"模型在产出结构化结果前耗尽了 max_tokens（output_tokens={used}）——调高 {budget_env}。"
        )
    raise ValueError(f"模型没有返回可解析的 {getattr(output_format, '__name__', output_format)}")


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


def create_llm_client(
    api_key: str, base_url: str | None = None, provider: str | None = None
) -> LLMClient:
    """
    造客户端。所有创建客户端的模块（编排器、意图识别、工具管理、记忆、评测）都走
    这一个入口，换供应商不需要改调用点。

    provider 不给就按 LLM_PROVIDER；控制台保存的模型连接（core/model_runtime.py）
    会明确给出，它和 .env 里写的可以不是同一家。
    """
    kwargs = anthropic_client_kwargs(api_key, base_url)
    provider = provider or llm_provider()
    if provider == "openai":
        from app.core.openai_compat import AsyncOpenAICompat

        client: Any = AsyncOpenAICompat(**kwargs)
    else:
        client = AsyncAnthropic(**kwargs)
    # 凭证不可用保护套在这唯一的入口上：同一份配置（供应商 + 地址 + 凭证）的所有
    # 客户端共用一份状态，一处被明确拒绝，其余调用点就不再拿同一把凭证出网。
    return GuardedLLMClient(client, guard_for(provider, base_url, api_key))


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
