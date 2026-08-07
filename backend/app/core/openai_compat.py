"""
OpenAI 兼容端点的适配层：把 `client.messages.create/stream` 这套 Anthropic 形状的
调用翻译成 `/chat/completions`。

为什么在客户端这一层翻译：全项目五处 LLM 调用点（编排器、意图识别、工具管理、
记忆、评测）都是按 Anthropic messages 形状写的。要支持 OpenAI 兼容网关，要么把
五处全改成双分支，要么在客户端做一次翻译。选后者——调用点一行不用改，换供应商
只是 .env 里 LLM_PROVIDER 的事，两条链路也不会因为改一处忘一处而产生行为差异。

只翻译项目真正用到的子集：system / messages / max_tokens、文本流式、结构化输出
（create / stream / parse 三个入口），以及 stop_reason 和 usage（token 计量和
"推理把预算吃光"的报错都依赖这两个字段）。项目没有用 tools= 走原生 function
calling（工具编排在 tools/tool_manager.py 里自己做），因此工具调用不在翻译范围内。

Anthropic 的 output_config.effort 翻译成 reasoning_effort；cache_control / thinking
没有对应物，丢掉。理由见 _Messages._params。
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI, BadRequestError
from pydantic import ValidationError

from app.core.model_settings import api_root

logger = logging.getLogger(__name__)


class GatewayResponseError(RuntimeError):
    """
    接口回了 200，内容却不是接口数据——多半是一张网页。

    OpenAI SDK 遇到不是 JSON 的响应会把原文当字符串交回来，调用点接着取 `.choices`，
    报出来的是 `'str' object has no attribute 'choices'`，看不出原因。这里换成一句能照着查的话。
    """


_NOT_API_RESPONSE = (
    "接口返回的不是 JSON（多半是一张网页）：接口地址可能少了 /v1，或者指到了别的服务上。"
)
_NOT_EVENT_STREAM = (
    "流式接口回了 200，却没有任何流式数据（Content-Type: {content_type}）：多半是一张网页或一段报错，"
    "接口地址可能少了 /v1，或者指到了别的服务上。"
)

# OpenAI 新模型把 max_tokens 换成了 max_completion_tokens，而各家兼容网关支持的
# 那一个并不统一。首次被网关拒绝时翻转这个开关，本进程后续请求都用新字段名，
# 不需要用户去 .env 里猜自己的网关是哪一边。
_MAX_TOKENS_FIELD = "max_tokens"

# Anthropic 的 effort → OpenAI 的 reasoning_effort。两边的档位名基本一致，
# Anthropic 多一档 max，OpenAI 没有，落到 high。
_REASONING_EFFORT = {"low": "low", "medium": "medium", "high": "high", "max": "high"}

# 拒收过 reasoning_effort 的模型。非推理模型和一部分兼容网关不认这个参数，
# 会直接 400；第一次被拒后对这个模型就不再传，和 max_tokens 换名一样只付一次代价。
_NO_REASONING_EFFORT: set[str] = set()

# 不按 response_format 约束输出的（接口根地址, 模型）。有的中转站把 JSON Schema 丢掉、
# 只把对话转给模型，模型照常写一段文字。第一次遇到之后，对它们直接把 Schema 写进提示词，
# 省掉一次注定失败的请求。见 _Messages.parse。
_SCHEMA_NOT_ENFORCED: set[tuple[str, str]] = set()


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
    # parsed_output 对齐 anthropic 的 ParsedMessage：create() 出来的消息没有它
    # （None），parse() 出来的带着已校验的 Pydantic 实例。core.llm_utils
    # 的 structured_parse 只认这一个属性，两个供应商因此不用分支。
    def __init__(
        self,
        text: str,
        stop_reason: str,
        usage: _Usage,
        model: str,
        parsed_output: Any = None,
        schema_enforced: bool = True,
    ):
        self.content = [_TextBlock(text)]
        self.stop_reason = stop_reason
        self.usage = usage
        self.model = model
        self.role = "assistant"
        self.parsed_output = parsed_output
        # False：网关没有按 Schema 约束，结果是按提示词要来、再自己校验过的（见 _Messages.parse）
        self.schema_enforced = schema_enforced


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


def _flatten_text(value: Any) -> str:
    """
    把 Anthropic 的"文本块数组"压成一个字符串。

    Agent 回复那条链路会把 system 写成 `[{"type": "text", "text": ...,
    "cache_control": {...}}]`，为的是给缓存划一个明确的前缀边界。cache_control
    是 Anthropic 独有的，OpenAI 侧没有对应物（它的前缀缓存是自动的、不需要标注），
    所以这里只取文本、丢掉标注——两边语义一致，只是省不省钱的差别。
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            b.get("text", "") if isinstance(b, dict) else str(getattr(b, "text", ""))
            for b in value
        )
    return str(value or "")


def _to_openai_messages(messages: list[dict[str, Any]], system: Any = None) -> list[dict[str, Any]]:
    """system 在 Anthropic 是顶层参数，在 OpenAI 是消息列表里的第一条。"""
    out: list[dict[str, Any]] = []
    system_text = _flatten_text(system)
    if system_text:
        out.append({"role": "system", "content": system_text})
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


def _schema_instruction(output_format: type) -> str:
    """网关不认 response_format 时，把同一份 JSON Schema 写进提示词。字段说明一起带上。"""
    schema = json.dumps(output_format.model_json_schema(), ensure_ascii=False)
    return (
        "只输出一个 JSON 对象，不要输出任何别的文字，不要用 Markdown 代码块。"
        "这个 JSON 必须符合下面的 JSON Schema，每个字段该填什么写在 description 里：\n"
        + schema
    )


_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def _validated(output_format: type, text: str) -> Any:
    """
    按提示词要来的 JSON → 校验过的实例；怎么都通不过校验就返回 None（由调用方报错）。

    只容忍两种包装：Markdown 代码块，和 JSON 前后多出来的一两句话。取出来的对象必须
    **完整通过 Schema 校验**——字段少了、类型错了都算失败，和服务端约束出来的结果一样严，
    不会出现「抠出半个对象、缺的字段用默认值顶上」那种静默降级。
    """
    candidates = [text.strip()]
    fenced = _FENCED_JSON.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            return output_format.model_validate_json(candidate)
        except ValidationError:
            continue
    return None


def schema_enforced_for(client: Any, model: str) -> bool:
    """这个客户端上的这个模型，结构化输出是不是由服务端按 Schema 约束的。Anthropic 一律是。"""
    client = getattr(client, "unguarded", client)
    messages = getattr(client, "messages", None)
    if not isinstance(messages, _Messages):
        return True
    return (messages._root, model) not in _SCHEMA_NOT_ENFORCED


class _Messages:
    """`client.messages` 命名空间：create() 与 stream() 两个入口。"""

    def __init__(self, client: AsyncOpenAI):
        self._client = client
        self._root = str(getattr(client, "base_url", "") or "")

    def _params(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        system: Any = None,
        output_config: dict[str, Any] | None = None,
        **_ignored: Any,
    ) -> dict[str, Any]:
        """
        翻译两边都有的那部分参数。

        `output_config.effort` 翻译成 `reasoning_effort`。GPT-5 之后的模型都是推理
        模型，不传就按模型默认（通常是 medium）去想：意图、改写、重排、判分这类
        判别式调用会慢很多，max_completion_tokens 还会先被推理花掉——那正是
        "正文为空"那类静默失败的来源（见 core/llm_utils.STRUCTURED_MAX_TOKENS）。
        不认这个参数的模型第一次 400 之后就不再传（_create_raw），所以映射过去
        不会把"省点钱"变成"整条链路挂掉"。

        落进 `_ignored` 被丢掉的是 `cache_control`、`thinking`：OpenAI 侧没有对应物
        （它的前缀缓存是自动的）。丢掉它们只影响成本，不影响产出形状。

        temperature 也不在这里了：anthropic SDK 1.x 已经把它从签名里删除，
        调用点一处都不再传，留着就是一段没人走的分支。见 core/llm_utils.py。
        """
        params: dict[str, Any] = {
            "model": model,
            "messages": _to_openai_messages(messages, system),
            _MAX_TOKENS_FIELD: max_tokens,
        }
        effort = _REASONING_EFFORT.get(str((output_config or {}).get("effort") or "").lower())
        if effort and model not in _NO_REASONING_EFFORT:
            params["reasoning_effort"] = effort
        return params

    async def _create_raw(self, params: dict[str, Any], _method: str = "create", **extra: Any) -> Any:
        """
        发请求。网关只认另一个 token 上限字段名、或模型不认 reasoning_effort 时，
        改掉那一项再试——两件事可能先后各撞一次，所以最多重试两回。
        """
        global _MAX_TOKENS_FIELD
        call = getattr(self._client.chat.completions, _method)
        params = dict(params)
        for attempt in range(3):
            try:
                result = await call(**params, **extra)
            except AttributeError as ex:
                # parse() 拿到网页时，SDK 自己在解析那一步就挂了，挂出来的就是这一句
                if "'str' object" in str(ex):
                    raise GatewayResponseError(_NOT_API_RESPONSE) from ex
                raise
            except BadRequestError as ex:
                text = str(ex)
                other = "max_completion_tokens" if _MAX_TOKENS_FIELD == "max_tokens" else "max_tokens"
                if attempt < 2 and "reasoning_effort" in params and "reasoning" in text:
                    logger.info(f"模型 {params['model']} 不接受 reasoning_effort，之后对它不再传")
                    _NO_REASONING_EFFORT.add(params["model"])
                    params.pop("reasoning_effort")
                    continue
                if attempt < 2 and other in text and _MAX_TOKENS_FIELD in params:
                    logger.info(f"网关不接受 {_MAX_TOKENS_FIELD}，本进程改用 {other}")
                    params[other] = params.pop(_MAX_TOKENS_FIELD)
                    _MAX_TOKENS_FIELD = other
                    continue
                raise
            if isinstance(result, str):
                raise GatewayResponseError(_NOT_API_RESPONSE)
            return result
        raise AssertionError("unreachable")

    async def create(self, **kwargs: Any) -> _Message:
        params = self._params(**kwargs)
        return self._message(await self._create_raw(params), params)

    async def parse(self, *, output_format: type, **kwargs: Any) -> _Message:
        """
        结构化输出。对齐 `anthropic` 的 `messages.parse(output_format=Model)`。

        OpenAI 侧走 `chat.completions.parse(response_format=Model)`——同样是服务端
        按 JSON Schema 约束解码，不是"提示模型输出 JSON 然后祈祷"。两边都由服务端
        保证形状，所以调用点拿到的东西是一样的：一个已校验的 Pydantic 实例。

        网关明确拒绝结构化输出（400）时照常抛错，**不做降级**。

        只有一种情况换个做法：网关回了 200，正文却不是这个 Schema 的 JSON。那是它把
        `response_format` 丢掉了，只把对话转给了模型——有的中转站就是这样，在它上面
        结构化输出一次也不会成功。这时把同一份 Schema 写进提示词再要一次，
        拿到的 JSON 照样完整校验（`_validated`），通不过就是 `parsed_output=None`，
        由 `structured_parse` 报错；记住这个（地址, 模型），之后直接这么要。
        `schema_enforced=False` 让连接测试能如实说出来走的是哪条路。
        """
        params = self._params(**kwargs)
        key = (self._root, params["model"])
        if key not in _SCHEMA_NOT_ENFORCED:
            try:
                resp = await self._create_raw(
                    {**params, "response_format": output_format}, _method="parse"
                )
            except ValidationError:
                logger.warning(
                    f"网关不按 response_format 约束输出（{self._root or '默认地址'}，{params['model']}），"
                    "之后改为在提示词里给出 JSON Schema，并逐条校验模型的回答"
                )
                _SCHEMA_NOT_ENFORCED.add(key)
            else:
                choice = resp.choices[0] if resp.choices else None
                message = getattr(choice, "message", None)
                return self._message(resp, params, parsed_output=getattr(message, "parsed", None))

        messages = list(params["messages"])
        instruction = _schema_instruction(output_format)
        if messages and messages[0].get("role") == "system":
            messages[0] = {"role": "system", "content": f"{messages[0]['content']}\n\n{instruction}"}
        else:
            messages.insert(0, {"role": "system", "content": instruction})
        resp = await self._create_raw({**params, "messages": messages})
        choice = resp.choices[0] if resp.choices else None
        text = _message_text(getattr(choice, "message", None)) if choice else ""
        return self._message(
            resp, params, parsed_output=_validated(output_format, text), schema_enforced=False
        )

    def _message(
        self, resp: Any, params: dict[str, Any], *, parsed_output: Any = None,
        schema_enforced: bool = True,
    ) -> _Message:
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
            parsed_output=parsed_output,
            schema_enforced=schema_enforced,
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
        self._finished = False   # 收到过 finish_reason：真正的事件流结尾都会带一个

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
                self._finished = True
            text = _delta_text(chunk)
            if text:
                self._text.append(text)
                yield text
        if not self._text and not self._finished:
            # SDK 遇到没有 data: 行的响应（网页、没套事件格式的 JSON 报错）一个事件都不给，
            # 照常结束。不在这里拦住，调用方就拿一条空回复当成功发给客户。
            response = getattr(self._stream, "response", None)
            content_type = getattr(response, "headers", {}).get("content-type", "") or "未知"
            raise GatewayResponseError(_NOT_EVENT_STREAM.format(content_type=content_type))

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
        # 只写到主机的地址补上 /v1（见 model_settings.api_root），和 ai-sql-agent 同一个口径
        self._client = AsyncOpenAI(api_key=api_key, base_url=api_root(base_url or "") or None, **kwargs)
        self.messages = _Messages(self._client)
