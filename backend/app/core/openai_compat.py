"""
OpenAI 兼容端点的适配层：把 `client.messages.create/stream` 这套 Anthropic 形状的
调用翻译成 `/chat/completions`。

为什么在客户端这一层翻译：全项目五处 LLM 调用点（编排器、意图识别、工具管理、
记忆、评测）都是按 Anthropic messages 形状写的。要支持 OpenAI 兼容网关，要么把
五处全改成双分支，要么在客户端做一次翻译。选后者——调用点一行不用改，换供应商
只是 .env 里 LLM_PROVIDER 的事，两条链路也不会因为改一处忘一处而产生行为差异。

只翻译项目真正用到的子集：system / messages / max_tokens、文本流式、结构化输出
（create / stream / parse 三个入口），以及 stop_reason 和 usage（token 计量和
"推理把预算吃光"的报错都依赖这两个字段）。

工具调用也在里面，只限自定义工具：`tools` / `tool_choice` 参数、消息里的
`tool_use` / `tool_result` 块、返回里的 `tool_calls`（非流式和流式都翻成
`tool_use` 块）。流式时工具调用和 Anthropic 一样只从 `get_final_message()` 取，
`text_stream` 照旧只出文字，所以调用方不用分供应商。网关明确拒绝 tools 时抛
`ToolsUnsupportedError`，并记住这份（地址, 模型），之后不再带着 tools 去试。

Anthropic 的 output_config.effort 翻译成 reasoning_effort；cache_control / thinking
没有对应物，丢掉。理由见 _Messages._params。
"""
from __future__ import annotations

import json
import logging
import re
import uuid
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


class ToolsUnsupportedError(RuntimeError):
    """
    网关明确拒绝了工具调用：400，错误里说不认 tools / tool_choice / function calling。

    这不是请求写错了，而是这份配置（地址, 模型）根本不支持工具。记住之后，同样带着
    tools 的请求不再出网，直接抛这个异常，由调用方退回不带工具的调用。只记"明确拒绝"：
    网关把 tools 静默丢掉、照常回一段文字的情况，单次请求看不出来，要靠评测发现。
    """


_NOT_API_RESPONSE = (
    "接口返回的不是 JSON（多半是一张网页）：接口地址可能少了 /v1，或者指到了别的服务上。"
)
_TOOLS_UNSUPPORTED_MESSAGE = "这个接口地址上的模型 {model} 不支持工具调用（网关明确拒绝了 tools 参数）。"
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

# 明确拒绝过 tools 的（接口根地址, 模型）。和上面几个一样只付一次代价：之后带 tools
# 的请求不出网，直接抛 ToolsUnsupportedError。见 _Messages._create_raw。
_TOOLS_UNSUPPORTED: set[tuple[str, str]] = set()

# "网关不支持工具"的判据：400 的错误信息里既点名了工具参数，又说了不支持。两个条件
# 缺一不可——"role 'tool' must be a response to a preceding message with 'tool_calls'"
# 这类报错也提到 tool，但那是消息拼错了，不是网关不支持，不能因此把工具关掉。
_TOOL_PARAM_RE = re.compile(r"\btools?\b|tool_choice|tool[ _]use|function[ _]?call|\bfunctions\b", re.I)
_UNSUPPORTED_RE = re.compile(
    r"not supported|unsupported|does not support|doesn't support|unrecognized|unknown parameter"
    r"|not allowed|not permitted|extra inputs|extra_forbidden",
    re.I,
)


def _rejects_tools(text: str) -> bool:
    return bool(_TOOL_PARAM_RE.search(text) and _UNSUPPORTED_RE.search(text))


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


class _ToolUseBlock:
    """
    对齐 anthropic 的 tool_use 块：`id` / `name` / `input`（参数，已解析成 dict）。

    模型给的参数不是合法 JSON 对象时，`input` 是空 dict，`parse_error` 写明原因，
    `raw_arguments` 留着原文——这一块回传进下一轮请求时照原文发回去，不替模型改写。
    """

    type = "tool_use"

    def __init__(self, id: str, name: str, input: dict[str, Any],
                 parse_error: str = "", raw_arguments: str | None = None):
        self.id = id
        self.name = name
        self.input = input
        self.parse_error = parse_error
        self.raw_arguments = raw_arguments


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
        tool_calls: list[_ToolUseBlock] | None = None,
    ):
        # 和 anthropic 一样：文字块在前，工具调用块在后。只调工具、不说话时不放空文字块；
        # 什么都没有时照旧给一个空文字块，extract_structured_text 靠它报"正文为空"。
        tool_calls = list(tool_calls or [])
        self.content: list[Any] = [_TextBlock(text)] if text or not tool_calls else []
        self.content.extend(tool_calls)
        if tool_calls and stop_reason == "end_turn":
            # 有的网关调了工具，finish_reason 却给 stop。调用方靠 tool_use 判断要不要执行工具
            stop_reason = "tool_use"
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


def _block_get(block: Any, key: str) -> Any:
    """块可能是调用方自己拼的 dict，也可能是 SDK 或本模块的对象（原样回传的上一轮回复）。"""
    return block.get(key) if isinstance(block, dict) else getattr(block, key, None)


def _block_text(block: Any) -> str:
    return block.get("text", "") if isinstance(block, dict) else str(getattr(block, "text", ""))


def _is_text_block(block: Any) -> bool:
    return _block_get(block, "type") in (None, "text")


def _tool_result_text(content: Any) -> str:
    """tool_result 的 content 可以是字符串，也可以是文本块数组。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_block_text(b) for b in content if _is_text_block(b))
    return str(content)


def _tool_messages(role: str, blocks: list[Any]) -> list[dict[str, Any]]:
    """
    带工具块的一条消息 → OpenAI 的一条或几条消息。

    - assistant：文字拼进 content，tool_use 变成 tool_calls；只调工具不说话时 content 为
      None。thinking 之类 OpenAI 没有对应物，丢掉。
    - user：每个 tool_result 单独一条 role=tool 的消息——OpenAI 要求它们紧跟在发起调用的
      assistant 消息后面、一个 id 一条。同一条里剩下的文字放在这些 tool 消息之后另起一条。
      `is_error` 没有对应字段，写进内容开头。
    """
    if role == "assistant":
        texts: list[str] = []
        calls: list[dict[str, Any]] = []
        for b in blocks:
            if _block_get(b, "type") == "tool_use":
                raw = _block_get(b, "raw_arguments")
                arguments = raw if raw is not None else json.dumps(
                    _block_get(b, "input") or {}, ensure_ascii=False)
                calls.append({
                    "id": _block_get(b, "id"),
                    "type": "function",
                    "function": {"name": _block_get(b, "name"), "arguments": arguments},
                })
            elif _is_text_block(b):
                texts.append(_block_text(b))
        message: dict[str, Any] = {"role": "assistant", "content": "\n".join(t for t in texts if t) or None}
        if calls:
            message["tool_calls"] = calls
        return [message]

    out: list[dict[str, Any]] = []
    texts = []
    for b in blocks:
        if _block_get(b, "type") == "tool_result":
            content = _tool_result_text(_block_get(b, "content"))
            if _block_get(b, "is_error"):
                content = f"[error] {content}"
            out.append({"role": "tool", "tool_call_id": _block_get(b, "tool_use_id"), "content": content})
        elif _is_text_block(b):
            texts.append(_block_text(b))
    text = "\n".join(t for t in texts if t)
    if text:
        out.append({"role": role, "content": text})
    return out


def _to_openai_messages(messages: list[dict[str, Any]], system: Any = None) -> list[dict[str, Any]]:
    """system 在 Anthropic 是顶层参数，在 OpenAI 是消息列表里的第一条。"""
    out: list[dict[str, Any]] = []
    system_text = _flatten_text(system)
    if system_text:
        out.append({"role": "system", "content": system_text})
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if not isinstance(content, list):
            out.append({"role": role, "content": content})
        elif any(_block_get(b, "type") in ("tool_use", "tool_result") for b in content):
            out.extend(_tool_messages(role, content))
        else:
            # 不带工具块的块数组：取文本拼起来
            out.append({"role": role, "content": "\n".join(_block_text(b) for b in content)})
    return out


def _openai_tool(tool: Any) -> dict[str, Any]:
    """Anthropic 的自定义工具 `{name, description, input_schema}` → OpenAI 的 function 工具。"""
    kind = _block_get(tool, "type")
    if kind not in (None, "custom"):
        raise ValueError(f"OpenAI 兼容端点只支持自定义工具，不支持 {kind}")
    function: dict[str, Any] = {"name": _block_get(tool, "name")}
    description = _block_get(tool, "description")
    if description:
        function["description"] = description
    function["parameters"] = _block_get(tool, "input_schema") or {"type": "object", "properties": {}}
    return {"type": "function", "function": function}


def _openai_tool_choice(choice: Any) -> Any:
    """Anthropic 的 tool_choice → OpenAI 的。没给就不传，按 OpenAI 的默认（auto）。"""
    if not choice:
        return None
    kind = choice.get("type")
    if kind == "auto":
        return "auto"
    if kind == "none":
        return "none"
    if kind == "any":
        return "required"
    if kind == "tool":
        return {"type": "function", "function": {"name": choice["name"]}}
    raise ValueError(f"不认识的 tool_choice: {choice!r}")


def _new_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:24]}"


def _tool_use_block(call_id: str | None, name: str | None, arguments: str | None) -> _ToolUseBlock:
    """
    OpenAI 的一次 tool_call → tool_use 块。

    参数解析不了不抛错：块照样产出（调用方要给每个调用回一条结果，少一条下一轮请求就会
    被拒），`parse_error` 写明原因，由调用方回一条 is_error 的结果让模型自己改。
    网关没给 id 时补一个，后面的 tool_result 要靠它对上。
    """
    call_id = call_id or _new_call_id()
    raw = arguments or ""
    try:
        parsed = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as ex:
        return _ToolUseBlock(call_id, name or "", {}, parse_error=f"参数不是合法 JSON：{ex.msg}",
                             raw_arguments=raw)
    if not isinstance(parsed, dict):
        return _ToolUseBlock(call_id, name or "", {}, parse_error="参数不是 JSON 对象", raw_arguments=raw)
    return _ToolUseBlock(call_id, name or "", parsed)


def _tool_calls_of(choice_message: Any) -> list[_ToolUseBlock]:
    """非流式响应里的 tool_calls → tool_use 块。"""
    blocks = []
    for call in getattr(choice_message, "tool_calls", None) or []:
        function = getattr(call, "function", None)
        blocks.append(_tool_use_block(
            getattr(call, "id", None), getattr(function, "name", None), getattr(function, "arguments", None)))
    return blocks


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
        tools: list[Any] | None = None,
        tool_choice: dict[str, Any] | None = None,
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

        `tools` 和 `tool_choice` 是显式参数，不能落进 `_ignored`：落进去的话工具被静默
        丢掉，模型照常回一段文字，调用方只会以为"模型这次没想调工具"。没有 tools 时
        tool_choice 不传——OpenAI 对单独出现的 tool_choice 会 400。
        `disable_parallel_tool_use` 翻译成 `parallel_tool_calls=false`。

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
        if tools:
            params["tools"] = [_openai_tool(t) for t in tools]
            choice = _openai_tool_choice(tool_choice)
            if choice is not None:
                params["tool_choice"] = choice
            if (tool_choice or {}).get("disable_parallel_tool_use"):
                params["parallel_tool_calls"] = False
        effort = _REASONING_EFFORT.get(str((output_config or {}).get("effort") or "").lower())
        if effort and model not in _NO_REASONING_EFFORT:
            params["reasoning_effort"] = effort
        return params

    async def _create_raw(self, params: dict[str, Any], _method: str = "create", **extra: Any) -> Any:
        """
        发请求。网关只认另一个 token 上限字段名、或模型不认 reasoning_effort 时，
        改掉那一项再试——两件事可能先后各撞一次，所以最多重试两回。

        网关明确拒绝 tools 时不重试：抛 ToolsUnsupportedError，并记住这份（地址, 模型），
        之后带着 tools 的请求直接在这里抛，不再出网。
        """
        global _MAX_TOKENS_FIELD
        tools_key = (self._root, params["model"])
        if "tools" in params and tools_key in _TOOLS_UNSUPPORTED:
            raise ToolsUnsupportedError(_TOOLS_UNSUPPORTED_MESSAGE.format(model=params["model"]))
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
                if "tools" in params and _rejects_tools(text):
                    logger.warning(
                        f"网关拒绝了工具调用（{self._root or '默认地址'}，{params['model']}），"
                        f"之后对它不再带 tools: {text}"
                    )
                    _TOOLS_UNSUPPORTED.add(tools_key)
                    raise ToolsUnsupportedError(
                        _TOOLS_UNSUPPORTED_MESSAGE.format(model=params["model"])) from ex
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
        message = getattr(choice, "message", None) if choice else None
        usage = getattr(resp, "usage", None)
        return _Message(
            text=_message_text(message) if choice else "",
            stop_reason=_stop_reason(getattr(choice, "finish_reason", None) if choice else None),
            usage=_Usage(
                input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            ),
            model=getattr(resp, "model", params["model"]),
            parsed_output=parsed_output,
            schema_enforced=schema_enforced,
            tool_calls=_tool_calls_of(message),
        )

    def stream(self, **kwargs: Any) -> _MessageStream:
        """与 anthropic 一致：同步返回一个 async 上下文管理器，不是协程。"""
        return _MessageStream(self, self._params(**kwargs))


class _MessageStream:
    """
    `async with client.messages.stream(...) as s: async for t in s.text_stream` 的等价物。

    工具调用和 anthropic 一样不进 `text_stream`：流里边读边拼，读完从 `get_final_message()`
    的 content 里取 tool_use 块。
    """

    def __init__(self, messages: _Messages, params: dict[str, Any]):
        self._messages = messages
        self._params = params
        self._stream: Any = None
        self._text: list[str] = []
        self._stop_reason = "end_turn"
        self._usage = _Usage()
        self._finished = False   # 收到过 finish_reason：真正的事件流结尾都会带一个
        self._drained = False    # 流读完了
        # 正在拼的工具调用，按第一次出现的顺序：键 → {"id", "name", "arguments": [片段]}
        self._calls: dict[tuple[str, Any], dict[str, Any]] = {}

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
            if choices:
                self._collect_tool_calls(getattr(choices[0], "delta", None))
            text = _delta_text(chunk)
            if text:
                self._text.append(text)
                yield text
        self._drained = True
        if not self._text and not self._calls and not self._finished:
            # SDK 遇到没有 data: 行的响应（网页、没套事件格式的 JSON 报错）一个事件都不给，
            # 照常结束。不在这里拦住，调用方就拿一条空回复当成功发给客户。
            # 只调工具、一个字都没说的回复不算空：它的内容在工具调用里。
            response = getattr(self._stream, "response", None)
            content_type = getattr(response, "headers", {}).get("content-type", "") or "未知"
            raise GatewayResponseError(_NOT_EVENT_STREAM.format(content_type=content_type))

    def _collect_tool_calls(self, delta: Any) -> None:
        """
        流里的工具调用是分片来的：同一个 index 的第一片带 id 和函数名，后面的片只带
        arguments 的一截。按 index 拼；不给 index 的网关按 id 拼；两样都没有的片接到
        上一个调用后面。
        """
        for piece in getattr(delta, "tool_calls", None) or []:
            index = getattr(piece, "index", None)
            call_id = getattr(piece, "id", None)
            if index is not None:
                key: tuple[str, Any] = ("index", index)
            elif call_id:
                key = ("id", call_id)
            elif self._calls:
                key = next(reversed(self._calls))
            else:
                key = ("index", 0)
            call = self._calls.setdefault(key, {"id": None, "name": None, "arguments": []})
            function = getattr(piece, "function", None)
            name = getattr(function, "name", None)
            arguments = getattr(function, "arguments", None)
            if call_id and not call["id"]:
                call["id"] = call_id
            if name and not call["name"]:
                call["name"] = name
            if arguments:
                call["arguments"].append(arguments)

    async def get_final_message(self) -> _Message:
        if not self._drained:
            # 和 anthropic 一样：还没读完的流先读完，最终消息才完整
            async for _ in self._iter_text():
                pass
        return _Message(
            text="".join(self._text),
            stop_reason=self._stop_reason,
            usage=self._usage,
            model=self._params["model"],
            tool_calls=[_tool_use_block(call["id"], call["name"], "".join(call["arguments"]))
                        for call in self._calls.values()],
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
