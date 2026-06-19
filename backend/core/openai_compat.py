"""
OpenAI 兼容端点的适配层：把 `client.messages.create/stream` 这套 Anthropic 形状的
调用翻译成 `/chat/completions`。

为什么在客户端这一层翻译：全项目五处 LLM 调用点（编排器、意图识别、工具管理、
记忆、评测）都是按 Anthropic messages 形状写的。要支持 OpenAI 兼容网关，要么把
五处全改成双分支，要么在客户端做一次翻译。选后者——调用点一行不用改，换供应商
只是 .env 里 LLM_PROVIDER 的事，两条链路也不会因为改一处忘一处而产生行为差异。

只翻译项目真正用到的子集：system / messages / max_tokens / temperature、文本流式，
以及 stop_reason 和 usage（token 计量和"推理把预算吃光"的报错都依赖这两个字段）。
项目没有用 tools= 走原生 function calling（工具编排在 mcp/tool_manager.py 里自己做），
因此工具调用不在翻译范围内。
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI, BadRequestError

logger = logging.getLogger(__name__)

# OpenAI 新模型把 max_tokens 换成了 max_completion_tokens，而各家兼容网关支持的
# 那一个并不统一。首次被网关拒绝时翻转这个开关，本进程后续请求都用新字段名，
# 不需要用户去 .env 里猜自己的网关是哪一边。
_MAX_TOKENS_FIELD = "max_tokens"


class _Usage:
    """对齐 anthropic 的 usage.input_tokens / output_tokens（Prometheus 计量要读）。"""

    def __init__(self, input_tokens: int = 0, output_tokens: int = 0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _TextBlock:
    """对齐 anthropic 的 content 块结构，core.llm_utils 的抽取逻辑按它取正文。"""

    type = "text"

    def __init__(self, text: str):
        self.text = text


class _Message:
    def __init__(self, text: str, stop_reason: str, usage: _Usage, model: str):
        self.content = [_TextBlock(text)]
        self.stop_reason = stop_reason
        self.usage = usage
        self.model = model
        self.role = "assistant"


def _stop_reason(finish_reason: str | None) -> str:
    """
    finish_reason → anthropic 的 stop_reason。

    只有 length 这一档是有语义要求的：llm_utils.extract_structured_text 靠
    stop_reason == "max_tokens" 把"推理阶段耗尽预算、正文为空"翻译成明确报错，
    映射错了就退回原来那种静默降级。
    """
    return {
        "length": "max_tokens",
        "content_filter": "refusal",
        "tool_calls": "tool_use",
        "function_call": "tool_use",
    }.get(finish_reason or "", "end_turn")


def _to_openai_messages(messages: list[dict[str, Any]], system: str | None) -> list[dict[str, Any]]:
    """system 在 Anthropic 是顶层参数，在 OpenAI 是消息列表里的第一条。"""
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        content = m.get("content", "")
        # 项目里的 content 都是纯字符串；万一传了 Anthropic 的块数组，取文本拼起来
        if isinstance(content, list):
            content = "\n".join(
                b.get("text", "") if isinstance(b, dict) else str(getattr(b, "text", ""))
                for b in content
            )
        out.append({"role": m.get("role", "user"), "content": content})
    return out


def _delta_text(chunk: Any) -> str:
    choices = getattr(chunk, "choices", None) or []
    if not choices:
        return ""
    delta = getattr(choices[0], "delta", None)
    return getattr(delta, "content", None) or "" if delta else ""


def _message_text(choice_message: Any) -> str:
    """
    取正文。推理模型的思考过程在 reasoning_content 里，正文在 content 里——
    这里只要正文，思考内容不该进用户回复，也不该进 JSON 解析。
    """
    return getattr(choice_message, "content", None) or ""


class _Messages:
    """`client.messages` 命名空间：create() 与 stream() 两个入口。"""

    def __init__(self, client: AsyncOpenAI):
        self._client = client

    def _params(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        system: str | None = None,
        temperature: float | None = None,
        **_ignored: Any,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": model,
            "messages": _to_openai_messages(messages, system),
            _MAX_TOKENS_FIELD: max_tokens,
        }
        if temperature is not None:
            params["temperature"] = temperature
        return params

    async def _create_raw(self, params: dict[str, Any], **extra: Any) -> Any:
        """发请求，并在网关只认另一个 token 上限字段名时自动换名重试一次。"""
        global _MAX_TOKENS_FIELD
        try:
            return await self._client.chat.completions.create(**params, **extra)
        except BadRequestError as ex:
            other = "max_completion_tokens" if _MAX_TOKENS_FIELD == "max_tokens" else "max_tokens"
            if other not in str(ex) or _MAX_TOKENS_FIELD not in params:
                raise
            logger.info(f"网关不接受 {_MAX_TOKENS_FIELD}，本进程改用 {other}")
            params = dict(params)
            params[other] = params.pop(_MAX_TOKENS_FIELD)
            _MAX_TOKENS_FIELD = other
            return await self._client.chat.completions.create(**params, **extra)

    async def create(self, **kwargs: Any) -> _Message:
        params = self._params(**kwargs)
        resp = await self._create_raw(params)
        choice = resp.choices[0] if resp.choices else None
        usage = getattr(resp, "usage", None)
        return _Message(
            text=_message_text(getattr(choice, "message", None)) if choice else "",
            stop_reason=_stop_reason(getattr(choice, "finish_reason", None) if choice else None),
            usage=_Usage(
                input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            ),
            model=getattr(resp, "model", params["model"]),
        )

    def stream(self, **kwargs: Any) -> _MessageStream:
        """与 anthropic 一致：同步返回一个 async 上下文管理器，不是协程。"""
        return _MessageStream(self, self._params(**kwargs))


class _MessageStream:
    """`async with client.messages.stream(...) as s: async for t in s.text_stream` 的等价物。"""

    def __init__(self, messages: _Messages, params: dict[str, Any]):
        self._messages = messages
        self._params = params
        self._stream: Any = None
        self._text: list[str] = []
        self._stop_reason = "end_turn"
        self._usage = _Usage()

    async def __aenter__(self) -> _MessageStream:
        try:
            self._stream = await self._messages._create_raw(
                self._params, stream=True, stream_options={"include_usage": True}
            )
        except BadRequestError as ex:
            # 部分兼容网关不认 stream_options，退回普通流（代价只是拿不到 usage 计量）
            logger.info(f"网关不支持 stream_options，流式 token 计量关闭: {ex}")
            self._stream = await self._messages._create_raw(self._params, stream=True)
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        close = getattr(self._stream, "close", None)
        if close:
            try:
                await close()
            except Exception:
                pass

    @property
    def text_stream(self) -> AsyncIterator[str]:
        return self._iter_text()

    async def _iter_text(self) -> AsyncIterator[str]:
        async for chunk in self._stream:
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                self._usage = _Usage(
                    input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                )
            choices = getattr(chunk, "choices", None) or []
            if choices and getattr(choices[0], "finish_reason", None):
                self._stop_reason = _stop_reason(choices[0].finish_reason)
            text = _delta_text(chunk)
            if text:
                self._text.append(text)
                yield text

    async def get_final_message(self) -> _Message:
        return _Message(
            text="".join(self._text),
            stop_reason=self._stop_reason,
            usage=self._usage,
            model=self._params["model"],
        )


class AsyncOpenAICompat:
    """
    暴露 `.messages` 的 OpenAI 客户端包装。

    构造参数与 AsyncAnthropic 对齐（api_key / base_url / timeout / max_retries），
    所以 core.llm_utils.anthropic_client_kwargs 那套统一的超时与重试策略两个
    供应商共用一份，不会出现"换个供应商超时就没人管了"。
    """

    def __init__(self, api_key: str, base_url: str | None = None, **kwargs: Any):
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url or None, **kwargs)
        self.messages = _Messages(self._client)
