"""
OpenAI 兼容层：把 messages 形状的调用翻译成 /chat/completions 是否等价。

这层一旦翻错，症状是"换个供应商就静默降级"——正文抽不出来、token 计量归零、
max_tokens 耗尽不报错。所以逐项钉住：system 位置、stop_reason 映射、usage 字段、
流式增量与最终消息。全部用假 SDK 客户端，不发网络请求。
"""
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
