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


async def test_anthropic_only_knobs_are_dropped_not_forwarded():
    """
    output_config（effort）/ cache_control 是 Anthropic 独有的，OpenAI 侧没有
    对应物。硬映射成 reasoning_effort 会在非推理模型上直接 400——把"省点钱"
    换成"整条链路挂掉"。丢掉它们只影响成本，不影响产出形状。
    """
    client = _FakeClient(_FakeResponse(content="x"))
    await _messages(client).create(
        model="m", max_tokens=64, messages=[{"role": "user", "content": "hi"}],
        output_config={"effort": "low"}, cache_control={"type": "ephemeral"},
    )
    sent = client.chat.completions.calls[0]
    assert not {"output_config", "cache_control", "effort", "reasoning_effort"} & sent.keys()


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
    assert isinstance(create_llm_client(cfg["api_key"], cfg.get("base_url")), AsyncOpenAICompat)

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    cfg = resolve_llm_config()
    assert cfg["provider"] == "anthropic" and "base_url" not in cfg
    assert not isinstance(create_llm_client(cfg["api_key"]), AsyncOpenAICompat)


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
