"""
OpenAI 兼容层：把 messages 形状的调用翻译成 /chat/completions 是否等价。

这层一旦翻错，症状是"换个供应商就静默降级"——正文抽不出来、token 计量归零、
max_tokens 耗尽不报错。所以逐项钉住：system 位置、stop_reason 映射、usage 字段、
流式增量与最终消息。用假 SDK 客户端或真 SDK 加假传输层，不发网络请求。
"""
import json

import httpx
import pytest
from openai import BadRequestError

from app.core import openai_compat
from app.core.llm_utils import create_llm_client, extract_structured_text, resolve_llm_config
from app.core.openai_compat import AsyncOpenAICompat, _Messages


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content="", finish_reason="stop"):
        self.message = _FakeMessage(content)
        self.finish_reason = finish_reason


class _FakeUsage:
    def __init__(self, prompt_tokens=11, completion_tokens=7):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeResponse:
    def __init__(self, content="", finish_reason="stop", usage=None, model="gpt-test"):
        self.choices = [_FakeChoice(content, finish_reason)]
        self.usage = usage if usage is not None else _FakeUsage()
        self.model = model


class _FakeDelta:
    def __init__(self, content):
        self.content = content


class _FakeStreamChoice:
    def __init__(self, content=None, finish_reason=None):
        self.delta = _FakeDelta(content)
        self.finish_reason = finish_reason


class _FakeChunk:
    def __init__(self, content=None, finish_reason=None, usage=None):
        self.choices = [_FakeStreamChoice(content, finish_reason)] if content or finish_reason else []
        self.usage = usage


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks
        self.closed = False

    def __aiter__(self):
        async def _gen():
            for c in self._chunks:
                yield c
        return _gen()

    async def close(self):
        self.closed = True


class _FakeCompletions:
    """记录收到的参数，按需返回单条响应或流。"""

    def __init__(self, response=None, chunks=None):
        self.response = response
        self.chunks = chunks
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return _FakeStream(self.chunks or [])
        return self.response or _FakeResponse()


class _FakeClient:
    def __init__(self, response=None, chunks=None):
        self.chat = type("Chat", (), {})()
        self.chat.completions = _FakeCompletions(response, chunks)


def _messages(client) -> _Messages:
    return _Messages(client)


async def test_system_becomes_first_message_and_params_map():
    client = _FakeClient(_FakeResponse(content="好的"))
    resp = await _messages(client).create(
        model="gpt-test", max_tokens=1024,
        system="你是客服", messages=[{"role": "user", "content": "在吗"}],
    )
    sent = client.chat.completions.calls[0]
    assert sent["messages"] == [
        {"role": "system", "content": "你是客服"},
        {"role": "user", "content": "在吗"},
    ]
    assert sent["max_tokens"] == 1024
    assert extract_structured_text(resp) == "好的"


async def test_cache_control_is_dropped_and_effort_becomes_reasoning_effort(monkeypatch):
    """
    cache_control 是 Anthropic 独有的，OpenAI 侧没有对应物（前缀缓存是自动的），丢掉。
    effort 翻译成 reasoning_effort：GPT-5 之后的推理模型不传就按默认档位想很久，
    判别式的结构化调用会慢、还会把 max_completion_tokens 花在推理上。
    """
    monkeypatch.setattr(openai_compat, "_NO_REASONING_EFFORT", set())
    client = _FakeClient(_FakeResponse(content="x"))
    await _messages(client).create(
        model="m", max_tokens=64, messages=[{"role": "user", "content": "hi"}],
        output_config={"effort": "low"}, cache_control={"type": "ephemeral"},
    )
    sent = client.chat.completions.calls[0]
    assert sent["reasoning_effort"] == "low"
    assert not {"output_config", "cache_control", "effort"} & sent.keys()


async def test_anthropic_max_effort_maps_to_the_highest_openai_level(monkeypatch):
    monkeypatch.setattr(openai_compat, "_NO_REASONING_EFFORT", set())
    client = _FakeClient(_FakeResponse(content="x"))
    await _messages(client).create(
        model="m", max_tokens=64, messages=[{"role": "user", "content": "hi"}],
        output_config={"effort": "max"},
    )
    assert client.chat.completions.calls[0]["reasoning_effort"] == "high"


def _rejecting(param: str, message: str):
    class _Picky(_FakeCompletions):
        async def create(self, **kwargs):
            self.calls.append(kwargs)
            if param in kwargs:
                raise BadRequestError(
                    message,
                    response=httpx.Response(400, request=httpx.Request("POST", "https://gw/v1")),
                    body=None,
                )
            return _FakeResponse(content="ok")
    return _Picky()


async def test_model_that_rejects_reasoning_effort_is_retried_without_it_once(monkeypatch):
    """
    非推理模型和部分兼容网关不认 reasoning_effort。第一次 400 之后去掉它重试，
    并记住这个模型——之后对它不再多付一次失败请求，整条链路不会因此挂掉。
    """
    monkeypatch.setattr(openai_compat, "_NO_REASONING_EFFORT", set())
    client = _FakeClient()
    client.chat.completions = _rejecting(
        "reasoning_effort",
        "Unsupported parameter: 'reasoning_effort' is not supported with this model.",
    )
    messages = _messages(client)
    kwargs = dict(model="old-model", max_tokens=64, messages=[{"role": "user", "content": "hi"}],
                  output_config={"effort": "low"})

    resp = await messages.create(**kwargs)
    assert extract_structured_text(resp) == "ok"
    assert "reasoning_effort" not in client.chat.completions.calls[1]

    await messages.create(**kwargs)
    assert len(client.chat.completions.calls) == 3          # 第二次直接不带，没有再失败一回
    assert "reasoning_effort" not in client.chat.completions.calls[2]


async def test_both_renames_can_happen_on_the_same_request(monkeypatch):
    """新网关可能既不认 max_tokens、对这个模型也不认 reasoning_effort：两件事各改一次。"""
    monkeypatch.setattr(openai_compat, "_NO_REASONING_EFFORT", set())
    monkeypatch.setattr(openai_compat, "_MAX_TOKENS_FIELD", "max_tokens")

    class _VeryPicky(_FakeCompletions):
        async def create(self, **kwargs):
            self.calls.append(kwargs)
            request = httpx.Request("POST", "https://gw/v1")
            if "reasoning_effort" in kwargs:
                raise BadRequestError("Unsupported parameter: 'reasoning_effort'.",
                                      response=httpx.Response(400, request=request), body=None)
            if "max_tokens" in kwargs:
                raise BadRequestError("Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens' instead.",
                                      response=httpx.Response(400, request=request), body=None)
            return _FakeResponse(content="ok")

    client = _FakeClient()
    client.chat.completions = _VeryPicky()
    resp = await _messages(client).create(
        model="m", max_tokens=64, messages=[{"role": "user", "content": "hi"}],
        output_config={"effort": "low"},
    )
    assert extract_structured_text(resp) == "ok"
    final = client.chat.completions.calls[-1]
    assert "max_completion_tokens" in final and not {"max_tokens", "reasoning_effort"} & final.keys()


async def test_block_form_system_is_flattened():
    """
    Agent 回复那条链路把 system 写成带 cache_control 的文本块数组。
    兼容层要能取出文本，否则 OpenAI 侧会收到一个 list 直接报错。
    """
    client = _FakeClient(_FakeResponse(content="x"))
    await _messages(client).create(
        model="m", max_tokens=64, messages=[{"role": "user", "content": "hi"}],
        system=[{"type": "text", "text": "你是客服", "cache_control": {"type": "ephemeral"}}],
    )
    assert client.chat.completions.calls[0]["messages"][0] == {
        "role": "system", "content": "你是客服",
    }


async def test_usage_maps_to_anthropic_field_names():
    """Prometheus 的 token 计量读 input_tokens/output_tokens，名字错了指标就归零。"""
    client = _FakeClient(_FakeResponse(content="x", usage=_FakeUsage(120, 34)))
    resp = await _messages(client).create(
        model="m", max_tokens=64, messages=[{"role": "user", "content": "hi"}]
    )
    assert (resp.usage.input_tokens, resp.usage.output_tokens) == (120, 34)


async def test_length_finish_reason_raises_instead_of_silent_empty():
    """预算在思考阶段耗尽时要明确报错，而不是把空串当成一句合法回答。"""
    client = _FakeClient(_FakeResponse(content="", finish_reason="length"))
    resp = await _messages(client).create(
        model="m", max_tokens=16, messages=[{"role": "user", "content": "hi"}]
    )
    assert resp.stop_reason == "max_tokens"
    with pytest.raises(ValueError, match="max_tokens"):
        extract_structured_text(resp)


async def test_stream_yields_deltas_and_final_message():
    chunks = [
        _FakeChunk(content="补件"),
        _FakeChunk(content="已登记。"),
        _FakeChunk(finish_reason="stop"),
        _FakeChunk(usage=_FakeUsage(50, 9)),
    ]
    client = _FakeClient(chunks=chunks)
    async with _messages(client).stream(
        model="m", max_tokens=256, system="s", messages=[{"role": "user", "content": "补件"}]
    ) as stream:
        got = [t async for t in stream.text_stream]
        final = await stream.get_final_message()

    assert got == ["补件", "已登记。"]
    assert extract_structured_text(final) == "补件已登记。"
    assert final.usage.output_tokens == 9
    assert client.chat.completions.calls[0]["stream"] is True


async def test_provider_switch_picks_client(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://gateway.example/v1")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test")

    cfg = resolve_llm_config()
    assert cfg["model"] == "gpt-test" and cfg["base_url"] == "https://gateway.example/v1"
    # 工厂返回的是带凭证保护的包装，真正出网的客户端在 .unguarded 上。
    assert isinstance(create_llm_client(cfg["api_key"], cfg.get("base_url")).unguarded, AsyncOpenAICompat)

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    cfg = resolve_llm_config()
    assert cfg["provider"] == "anthropic" and "base_url" not in cfg
    assert not isinstance(create_llm_client(cfg["api_key"]).unguarded, AsyncOpenAICompat)


def test_unknown_provider_fails_loudly(monkeypatch):
    """写错供应商名要当场报错，不能默默回落到另一套 key 上。"""
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    with pytest.raises(RuntimeError, match="LLM_PROVIDER"):
        resolve_llm_config()


async def test_retries_with_max_completion_tokens_when_gateway_rejects(monkeypatch):
    """
    网关只认 max_completion_tokens 时自动换名重试，不要求用户先去猜自己网关是哪一边。
    """
    monkeypatch.setattr(openai_compat, "_MAX_TOKENS_FIELD", "max_tokens")

    class _PickyCompletions(_FakeCompletions):
        async def create(self, **kwargs):
            self.calls.append(kwargs)
            if "max_tokens" in kwargs:
                raise BadRequestError(
                    "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens' instead.",
                    response=httpx.Response(400, request=httpx.Request("POST", "https://gw/v1")),
                    body=None,
                )
            return _FakeResponse(content="ok")

    client = _FakeClient()
    client.chat.completions = _PickyCompletions()
    resp = await _messages(client).create(
        model="m", max_tokens=256, messages=[{"role": "user", "content": "hi"}]
    )

    assert extract_structured_text(resp) == "ok"
    assert "max_completion_tokens" in client.chat.completions.calls[1]
    # 翻转是进程级的，后续请求直接用新字段名，不再多跑一次失败请求
    assert openai_compat._MAX_TOKENS_FIELD == "max_completion_tokens"


# ── 中转站：只写到主机的地址、回网页、不认 response_format ──────────────────────

def test_api_root_adds_v1_only_to_a_bare_host():
    from app.core.model_settings import api_root

    assert api_root("https://api.kabi.pro") == "https://api.kabi.pro/v1"
    assert api_root("https://api.kabi.pro/") == "https://api.kabi.pro/v1"
    assert api_root(" http://127.0.0.1:11434 ") == "http://127.0.0.1:11434/v1"
    for kept in ("https://api.openai.com/v1", "https://relay.example.com/api/v1",
                 "https://dashscope.aliyuncs.com/compatible-mode/v1", "https://relay.example.com/api"):
        assert api_root(kept) == kept
    assert api_root("https://generativelanguage.googleapis.com/v1beta/openai/") == \
        "https://generativelanguage.googleapis.com/v1beta/openai"
    assert api_root("") == ""


def test_a_bare_host_is_sent_to_v1():
    """和 ai-sql-agent 同一个口径：同一个地址在两个项目里请求到同一个接口。"""
    client = AsyncOpenAICompat(api_key="sk-test", base_url="https://api.kabi.pro")
    assert str(client._client.base_url) == "https://api.kabi.pro/v1/"


async def test_a_web_page_is_reported_as_a_wrong_address():
    """SDK 拿到不是 JSON 的 200 会把原文当字符串交回来——要说清楚是地址的问题。"""
    client = _FakeClient()

    async def web_page(**kwargs):
        return "<!doctype html><html><title>Relay</title></html>"

    client.chat.completions.create = web_page
    with pytest.raises(openai_compat.GatewayResponseError, match="/v1"):
        await _messages(client).create(model="gpt-5.6-luna", max_tokens=64,
                                       messages=[{"role": "user", "content": "hi"}])


class _SchemaDroppingGateway(_FakeCompletions):
    """
    不认 response_format 的中转站：parse 时把 Schema 丢掉，模型照常写一段文字，
    真实 SDK 在校验那一步抛 ValidationError；create 时按提示词作答。
    """

    def __init__(self, answer):
        super().__init__()
        self.answer = answer
        self.parse_calls = 0

    async def parse(self, **kwargs):
        self.parse_calls += 1
        kwargs["response_format"].model_validate_json("1. 柜子门板缺失如何补件\n2. 门板尺寸测量")

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(content=self.answer)


async def test_schema_goes_into_the_prompt_when_the_gateway_drops_it(monkeypatch):
    from app.core.llm_utils import structured_parse
    from app.tools.tool_manager import QueryRewritePayload

    monkeypatch.setattr(openai_compat, "_SCHEMA_NOT_ENFORCED", set())
    client = _FakeClient()
    client.base_url = "https://api.kabi.pro/v1/"
    client.chat.completions = _SchemaDroppingGateway(
        '好的：\n```json\n{"queries": ["门板缺件怎么补发", "门板尺寸怎么量"]}\n```'
    )
    messages = _messages(client)
    resp = await messages.parse(
        output_format=QueryRewritePayload, model="gpt-5.6-luna", max_tokens=512,
        system="你是检索助手", messages=[{"role": "user", "content": "柜子缺门板"}],
    )
    assert resp.parsed_output.queries == ["门板缺件怎么补发", "门板尺寸怎么量"]
    assert resp.schema_enforced is False
    sent = client.chat.completions.calls[0]
    assert "response_format" not in sent
    assert sent["messages"][0]["role"] == "system"
    assert sent["messages"][0]["content"].startswith("你是检索助手")
    assert "JSON Schema" in sent["messages"][0]["content"] and "queries" in sent["messages"][0]["content"]
    assert openai_compat.schema_enforced_for(type("C", (), {"messages": messages})(), "gpt-5.6-luna") is False

    # 记住了：第二次直接走提示词，不再先撞一次 parse
    await structured_parse(client=type("C", (), {"messages": messages})(), output_format=QueryRewritePayload,
                           model="gpt-5.6-luna", max_tokens=512,
                           messages=[{"role": "user", "content": "柜子缺门板"}])
    assert client.chat.completions.parse_calls == 1
    assert len(client.chat.completions.calls) == 2


async def test_an_answer_that_does_not_match_the_schema_is_still_a_loud_failure(monkeypatch):
    """自己校验和服务端约束一样严：字段不对、不是 JSON，都报错，不拿半个对象凑数。"""
    from app.core.llm_utils import structured_parse
    from app.tools.tool_manager import QueryRewritePayload

    monkeypatch.setattr(openai_compat, "_SCHEMA_NOT_ENFORCED", set())
    for answer in ("1. 门板缺件怎么补发\n2. 门板尺寸怎么量", '{"query": "门板"}', '{"queries": "门板"}'):
        client = _FakeClient()
        client.chat.completions = _SchemaDroppingGateway(answer)
        holder = type("C", (), {"messages": _messages(client)})()
        with pytest.raises(ValueError, match="QueryRewritePayload"):
            await structured_parse(holder, output_format=QueryRewritePayload, model="gpt-5.6-luna",
                                   max_tokens=512, messages=[{"role": "user", "content": "柜子缺门板"}])


async def test_a_gateway_that_honours_the_schema_keeps_the_strict_path(monkeypatch):
    from app.tools.tool_manager import QueryRewritePayload

    monkeypatch.setattr(openai_compat, "_SCHEMA_NOT_ENFORCED", set())

    class _Strict(_FakeCompletions):
        async def parse(self, **kwargs):
            self.calls.append(kwargs)
            response = _FakeResponse(content='{"queries": ["a"]}')
            response.choices[0].message.parsed = QueryRewritePayload(queries=["a"])
            return response

    client = _FakeClient()
    client.chat.completions = _Strict()
    resp = await _messages(client).parse(output_format=QueryRewritePayload, model="gpt-5.6-luna",
                                         max_tokens=512, messages=[{"role": "user", "content": "x"}])
    assert resp.parsed_output.queries == ["a"] and resp.schema_enforced is True
    assert client.chat.completions.calls[0]["response_format"] is QueryRewritePayload


# ── 流式：200 却不是事件流（网页、JSON 报错信封） ──────────────────────────────
# 用真的 openai SDK + 假传输层：SDK 遇到没有 data: 行的响应时什么事件都不给，
# 手写的假客户端模拟不出这一点。

def _sdk_client(status, content_type, body):
    def handle(request):
        return httpx.Response(status, headers={"content-type": content_type}, content=body.encode())

    return AsyncOpenAICompat(
        api_key="sk-test", base_url="https://relay.example/v1", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )


async def _stream_text(client):
    async with client.messages.stream(
        model="m", max_tokens=64, messages=[{"role": "user", "content": "补件"}]
    ) as stream:
        return "".join([t async for t in stream.text_stream])


@pytest.mark.parametrize("content_type, body", [
    ("text/html; charset=utf-8", "<!doctype html><html><title>Relay</title></html>"),
    ("application/json", '{"error": {"message": "upstream unavailable", "type": "server_error"}}'),
])
async def test_a_stream_that_is_not_an_event_stream_fails_instead_of_answering_blank(content_type, body):
    """以前这里静默得到空回复、记成功，客户看到一条空白回答。"""
    with pytest.raises(openai_compat.GatewayResponseError, match="流式"):
        await _stream_text(_sdk_client(200, content_type, body))


async def test_a_real_event_stream_still_streams():
    def event(delta, finish=None):
        return "data: " + json.dumps({
            "id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "m",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }) + "\n\n"

    body = event({"content": "补件"}) + event({"content": "已登记。"}) + event({}, "stop") + "data: [DONE]\n\n"
    assert await _stream_text(_sdk_client(200, "text/event-stream", body)) == "补件已登记。"


# ── 工具调用：tools / tool_choice、tool_use / tool_result、tool_calls ─────────────

TOOLS = [{
    "name": "order_data_query",
    "description": "查订单、生产、送货的系统记录",
    "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
}]


class _FakeFunction:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    """非流式响应里的一次调用，ChatCompletionMessageToolCall 的形状。"""

    def __init__(self, id, name, arguments):
        self.id = id
        self.type = "function"
        self.function = _FakeFunction(name, arguments)


class _FakeToolCallDelta:
    """流里的一片，ChoiceDeltaToolCall 的形状：只有第一片带 id 和函数名。"""

    def __init__(self, index=None, id=None, name=None, arguments=None):
        self.index = index
        self.id = id
        self.type = "function" if id else None
        self.function = _FakeFunction(name, arguments) if (name or arguments is not None) else None


class _ToolChunk:
    def __init__(self, *pieces, finish_reason=None):
        choice = _FakeStreamChoice(None, finish_reason)
        choice.delta.tool_calls = list(pieces)
        self.choices = [choice]
        self.usage = None


def _tool_response(calls, content=None, finish_reason="tool_calls"):
    response = _FakeResponse(content=content, finish_reason=finish_reason)
    response.choices[0].message.tool_calls = calls
    return response


async def test_tools_and_tool_choice_are_translated():
    client = _FakeClient(_FakeResponse(content="x"))
    messages = _messages(client)
    base = dict(model="m", max_tokens=64, tools=TOOLS,
                messages=[{"role": "user", "content": "单号 SO-1 到哪了"}])
    for choice, expected in [
        ({"type": "auto"}, "auto"),
        ({"type": "none"}, "none"),
        ({"type": "any"}, "required"),
        ({"type": "tool", "name": "order_data_query"},
         {"type": "function", "function": {"name": "order_data_query"}}),
    ]:
        await messages.create(**base, tool_choice=choice)
        assert client.chat.completions.calls[-1]["tool_choice"] == expected

    sent = client.chat.completions.calls[0]
    assert sent["tools"] == [{"type": "function", "function": {
        "name": "order_data_query", "description": "查订单、生产、送货的系统记录",
        "parameters": TOOLS[0]["input_schema"],
    }}]
    assert "parallel_tool_calls" not in sent

    await messages.create(**base, tool_choice={"type": "auto", "disable_parallel_tool_use": True})
    assert client.chat.completions.calls[-1]["parallel_tool_calls"] is False


async def test_without_tools_nothing_tool_related_is_sent():
    """OpenAI 对单独出现的 tool_choice 会 400。"""
    client = _FakeClient(_FakeResponse(content="x"))
    await _messages(client).create(model="m", max_tokens=64, tool_choice={"type": "auto"},
                                   messages=[{"role": "user", "content": "hi"}])
    assert not {"tools", "tool_choice", "parallel_tool_calls"} & client.chat.completions.calls[0].keys()


async def test_tool_use_and_tool_result_become_tool_calls_and_tool_messages():
    client = _FakeClient(_FakeResponse(content="SO-1 已出库。"))
    await _messages(client).create(model="m", max_tokens=64, tools=TOOLS, messages=[
        {"role": "user", "content": "单号 SO-1 和 SO-2 到哪了"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "我查一下。"},
            {"type": "tool_use", "id": "call_1", "name": "order_data_query", "input": {"query": "SO-1 进度"}},
            {"type": "tool_use", "id": "call_2", "name": "order_data_query", "input": {"query": "SO-2 进度"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_1", "content": "SO-1 已出库"},
            {"type": "tool_result", "tool_use_id": "call_2", "is_error": True,
             "content": [{"type": "text", "text": "业务系统超时"}]},
            {"type": "text", "text": "顺便问下运费"},
        ]},
    ])
    sent = client.chat.completions.calls[0]["messages"]
    assert sent[1] == {"role": "assistant", "content": "我查一下。", "tool_calls": [
        {"id": "call_1", "type": "function",
         "function": {"name": "order_data_query", "arguments": '{"query": "SO-1 进度"}'}},
        {"id": "call_2", "type": "function",
         "function": {"name": "order_data_query", "arguments": '{"query": "SO-2 进度"}'}},
    ]}
    # tool 消息紧跟在发起调用的那条后面、一个 id 一条；同一条里的文字排在它们后面
    assert sent[2:] == [
        {"role": "tool", "tool_call_id": "call_1", "content": "SO-1 已出库"},
        {"role": "tool", "tool_call_id": "call_2", "content": "[error] 业务系统超时"},
        {"role": "user", "content": "顺便问下运费"},
    ]


async def test_a_reply_with_tool_calls_can_be_sent_back_as_is():
    """工具循环把上一轮回复的 content 原样追加回去：本模块自己的块对象也要翻得回去。"""
    client = _FakeClient(_tool_response([_FakeToolCall("call_9", "order_data_query", '{"query": "SO-9"}')]))
    messages = _messages(client)
    first = await messages.create(model="m", max_tokens=64, tools=TOOLS,
                                  messages=[{"role": "user", "content": "SO-9 到哪了"}])
    await messages.create(model="m", max_tokens=64, tools=TOOLS, messages=[
        {"role": "user", "content": "SO-9 到哪了"},
        {"role": "assistant", "content": first.content},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_9", "content": "已出库"}]},
    ])
    sent = client.chat.completions.calls[1]["messages"]
    assert sent[1] == {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_9", "type": "function",
         "function": {"name": "order_data_query", "arguments": '{"query": "SO-9"}'}},
    ]}
    assert sent[2] == {"role": "tool", "tool_call_id": "call_9", "content": "已出库"}


async def test_tool_calls_in_a_reply_become_tool_use_blocks():
    client = _FakeClient(_tool_response(
        [_FakeToolCall("call_1", "order_data_query", '{"query": "SO-1 进度"}'),
         _FakeToolCall("call_2", "knowledge_search", '{"query": "补件流程", "top_k": 3}')],
        content="我查一下。",
    ))
    resp = await _messages(client).create(model="m", max_tokens=64, tools=TOOLS,
                                          messages=[{"role": "user", "content": "x"}])
    assert resp.stop_reason == "tool_use"
    text, first, second = resp.content
    assert (text.type, text.text) == ("text", "我查一下。")
    assert (first.type, first.id, first.name, first.input) == (
        "tool_use", "call_1", "order_data_query", {"query": "SO-1 进度"})
    assert second.input == {"query": "补件流程", "top_k": 3} and second.parse_error == ""
    assert extract_structured_text(resp) == "我查一下。"     # 正文抽取只认文字块


async def test_unusable_tool_calls_are_reported_not_raised():
    """
    参数不是 JSON、网关没给 id、调了工具却报 stop：块照样产出，原因写在块上，
    由调用方给模型回一条 is_error——少回一条结果，下一轮请求会被拒。
    """
    client = _FakeClient(_tool_response(
        [_FakeToolCall(None, "order_data_query", '{"query": "SO-1'),
         _FakeToolCall("call_2", "order_data_query", '["SO-2"]')],
        finish_reason="stop",
    ))
    resp = await _messages(client).create(model="m", max_tokens=64, tools=TOOLS,
                                          messages=[{"role": "user", "content": "x"}])
    assert resp.stop_reason == "tool_use"
    broken, not_object = resp.content
    assert broken.id.startswith("call_") and broken.input == {} and "JSON" in broken.parse_error
    assert broken.raw_arguments == '{"query": "SO-1'
    assert not_object.parse_error == "参数不是 JSON 对象"

    # 预算在写参数时耗尽：仍然报 max_tokens，不能被当成一次正常的工具调用
    client = _FakeClient(_tool_response([_FakeToolCall("call_3", "order_data_query", '{"qu')],
                                        finish_reason="length"))
    resp = await _messages(client).create(model="m", max_tokens=8, tools=TOOLS,
                                          messages=[{"role": "user", "content": "x"}])
    assert resp.stop_reason == "max_tokens"


async def test_streamed_tool_calls_are_assembled_from_fragments():
    chunks = [
        _FakeChunk(content="我查一下。"),
        _ToolChunk(_FakeToolCallDelta(0, "call_1", "order_data_query", "")),
        _ToolChunk(_FakeToolCallDelta(0, arguments='{"query": '),
                   _FakeToolCallDelta(1, "call_2", "knowledge_search", '{"query"')),
        _ToolChunk(_FakeToolCallDelta(0, arguments='"SO-1 进度"}'),
                   _FakeToolCallDelta(1, arguments=': "补件"}')),
        _ToolChunk(finish_reason="tool_calls"),
        _FakeChunk(usage=_FakeUsage(80, 20)),
    ]
    client = _FakeClient(chunks=chunks)
    async with _messages(client).stream(model="m", max_tokens=256, tools=TOOLS,
                                        messages=[{"role": "user", "content": "x"}]) as stream:
        got = [t async for t in stream.text_stream]
        final = await stream.get_final_message()

    assert got == ["我查一下。"]                              # 工具调用不进文字流
    assert final.stop_reason == "tool_use"
    text, first, second = final.content
    assert text.text == "我查一下。"
    assert (first.id, first.name, first.input) == ("call_1", "order_data_query", {"query": "SO-1 进度"})
    assert (second.id, second.name, second.input) == ("call_2", "knowledge_search", {"query": "补件"})
    assert final.usage.output_tokens == 20
    assert client.chat.completions.calls[0]["tools"][0]["function"]["name"] == "order_data_query"


async def test_a_stream_that_only_calls_tools_is_not_an_empty_reply():
    """不给 index 的网关按 id 拼；只调工具不说话不是空回复；finish_reason 给 stop 也算调了工具。"""
    chunks = [
        _ToolChunk(_FakeToolCallDelta(None, "call_a", "order_data_query", '{"query": "SO-')),
        _ToolChunk(_FakeToolCallDelta(None, None, None, '7"}')),
        _ToolChunk(finish_reason="stop"),
    ]
    client = _FakeClient(chunks=chunks)
    async with _messages(client).stream(model="m", max_tokens=64, tools=TOOLS,
                                        messages=[{"role": "user", "content": "x"}]) as stream:
        final = await stream.get_final_message()          # 没读文字流，也能拿到完整的最终消息

    assert final.stop_reason == "tool_use"
    [call] = final.content
    assert (call.id, call.input) == ("call_a", {"query": "SO-7"})


@pytest.mark.parametrize("message, rejects", [
    ("Unrecognized request argument supplied: tools", True),
    ("Unsupported parameter: 'tools' is not supported with this model.", True),
    ("This model does not support function calling.", True),
    ("tool_choice is not supported", True),
    ("[{'type': 'extra_forbidden', 'loc': ('body', 'tools'), 'msg': 'Extra inputs are not permitted'}]", True),
    # 下面这些提到了工具，但是请求本身写错了：要大声报错，不能当成网关不支持而把工具关掉
    ("Invalid parameter: messages with role 'tool' must be a response to a preceeding message "
     "with 'tool_calls'.", False),
    ("Invalid 'tools[0].function.name': string does not match pattern.", False),
    ("Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens' instead.", False),
])
def test_what_counts_as_a_gateway_without_tool_support(message, rejects):
    assert openai_compat._rejects_tools(message) is rejects


async def test_a_gateway_that_rejects_tools_is_tried_once(monkeypatch):
    monkeypatch.setattr(openai_compat, "_TOOLS_UNSUPPORTED", set())
    client = _FakeClient()
    client.chat.completions = _rejecting("tools", "Unrecognized request argument supplied: tools")
    messages = _messages(client)
    kwargs = dict(model="m", max_tokens=64, tools=TOOLS, messages=[{"role": "user", "content": "x"}])

    with pytest.raises(openai_compat.ToolsUnsupportedError):
        await messages.create(**kwargs)
    with pytest.raises(openai_compat.ToolsUnsupportedError):
        async with messages.stream(**kwargs):
            pass
    assert len(client.chat.completions.calls) == 1           # 第二次没有出网

    # 不带工具的调用照常
    resp = await messages.create(model="m", max_tokens=64, messages=[{"role": "user", "content": "x"}])
    assert extract_structured_text(resp) == "ok"


async def test_a_malformed_tool_message_is_not_mistaken_for_missing_support(monkeypatch):
    monkeypatch.setattr(openai_compat, "_TOOLS_UNSUPPORTED", set())
    client = _FakeClient()
    client.chat.completions = _rejecting(
        "tools",
        "Invalid parameter: messages with role 'tool' must be a response to a preceeding message "
        "with 'tool_calls'.",
    )
    with pytest.raises(BadRequestError):
        await _messages(client).create(model="m", max_tokens=64, tools=TOOLS,
                                       messages=[{"role": "user", "content": "x"}])
    assert openai_compat._TOOLS_UNSUPPORTED == set()


def _capturing_sdk_client(responses):
    """真 SDK + 假传输层：按顺序回 responses 里的 (content_type, body)，记下每次请求的 JSON。"""
    sent = []
    queue = list(responses)

    def handle(request):
        sent.append(json.loads(request.content))
        content_type, body = queue.pop(0)
        return httpx.Response(200, headers={"content-type": content_type}, content=body.encode())

    client = AsyncOpenAICompat(
        api_key="sk-test", base_url="https://relay.example/v1", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    return client, sent


async def test_tool_calls_round_trip_through_the_real_sdk():
    """真 SDK 的请求序列化和响应模型（非流式的 tool_calls、流式的 ChoiceDeltaToolCall）都对得上。"""
    completion = json.dumps({
        "id": "c1", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "order_data_query", "arguments": '{"query": "SO-1"}'}}],
        }}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    })

    def event(delta, finish=None):
        return "data: " + json.dumps({
            "id": "c2", "object": "chat.completion.chunk", "created": 0, "model": "m",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }) + "\n\n"

    stream_body = (
        event({"role": "assistant", "content": None, "tool_calls": [
            {"index": 0, "id": "call_2", "type": "function",
             "function": {"name": "knowledge_search", "arguments": ""}}]})
        + event({"tool_calls": [{"index": 0, "function": {"arguments": '{"query": "补件"}'}}]})
        + event({}, "tool_calls")
        + "data: [DONE]\n\n"
    )
    client, sent = _capturing_sdk_client([("application/json", completion), ("text/event-stream", stream_body)])

    first = await client.messages.create(model="m", max_tokens=64, tools=TOOLS, tool_choice={"type": "auto"},
                                         messages=[{"role": "user", "content": "SO-1 到哪了"}])
    [call] = first.content
    assert (call.id, call.name, call.input) == ("call_1", "order_data_query", {"query": "SO-1"})

    async with client.messages.stream(model="m", max_tokens=64, tools=TOOLS, messages=[
        {"role": "user", "content": "SO-1 到哪了"},
        {"role": "assistant", "content": first.content},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "已出库"}]},
    ]) as stream:
        assert [t async for t in stream.text_stream] == []
        final = await stream.get_final_message()
    [call] = final.content
    assert (call.id, call.name, call.input) == ("call_2", "knowledge_search", {"query": "补件"})
    assert final.stop_reason == "tool_use"

    assert sent[0]["tools"] == [{"type": "function", "function": {
        "name": "order_data_query", "description": TOOLS[0]["description"],
        "parameters": TOOLS[0]["input_schema"],
    }}]
    assert sent[0]["tool_choice"] == "auto"
    assert sent[1]["messages"][1]["tool_calls"][0]["function"] == {
        "name": "order_data_query", "arguments": '{"query": "SO-1"}'}
    assert sent[1]["messages"][2] == {"role": "tool", "tool_call_id": "call_1", "content": "已出库"}
