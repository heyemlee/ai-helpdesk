"""
压测、演练用的假模型服务（scripts/mock_llm.py）。

它要冒充得住：本系统每一个结构化输出的 Schema（经 OpenAI SDK 转成严格模式之后的样子）
造出来的实例都要过 Pydantic 校验；流式要带 usage；限流时回 429 和 retry-after，
后端的保护按它退避。都用真的 OpenAI SDK 打过去，走 ASGI，不开端口。
"""
import importlib.util
import pathlib

import httpx
import pytest
from openai import AsyncOpenAI
from openai.lib._pydantic import to_strict_json_schema

from app.core.intent_recognizer import LLMIntentPayload
from app.evaluation.evaluator import JudgePayload
from app.memory.conversation_memory import UserProfilePayload
from app.tools.tool_manager import QueryRewritePayload, RerankPayload

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "mock_llm.py"


@pytest.fixture
def mock():
    spec = importlib.util.spec_from_file_location("mock_llm", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.state.config.update(latency_ms=0, structured_latency_ms=0, chunk_ms=0)
    return module


def _client(mock) -> AsyncOpenAI:
    transport = httpx.ASGITransport(app=mock.app)
    return AsyncOpenAI(api_key="mock", base_url="http://mock/v1", max_retries=0,
                       http_client=httpx.AsyncClient(transport=transport, base_url="http://mock"))


@pytest.mark.parametrize("model", [LLMIntentPayload, QueryRewritePayload, RerankPayload,
                                   UserProfilePayload, JudgePayload])
def test_every_structured_schema_gets_a_valid_instance(mock, model):
    schema = to_strict_json_schema(model)
    model.model_validate(mock.instance(schema, schema.get("$defs", {})))


async def test_structured_output_through_the_sdk(mock):
    resp = await _client(mock).chat.completions.parse(
        model="mock-1", messages=[{"role": "user", "content": "柜门关不严"}], response_format=QueryRewritePayload)
    assert isinstance(resp.choices[0].message.parsed, QueryRewritePayload)
    assert resp.usage.prompt_tokens > 0


async def test_streaming_carries_the_handoff_marker_and_usage(mock):
    mock.state.config["handoff_rate"] = 1.0
    stream = await _client(mock).chat.completions.create(
        model="mock-1", messages=[{"role": "user", "content": "补件"}], stream=True,
        stream_options={"include_usage": True})
    text, usage = "", None
    async for chunk in stream:
        if chunk.choices:
            text += chunk.choices[0].delta.content or ""
        if chunk.usage:
            usage = chunk.usage
    assert text.endswith("[[handoff:yes]]") and usage.completion_tokens > 0
    assert mock.state.in_flight == 0


async def test_rpm_and_storms_answer_429_with_retry_after(mock):
    import openai

    client = _client(mock)
    mock.state.config["rpm"] = 2
    for _ in range(2):
        await client.chat.completions.create(model="mock-1", messages=[{"role": "user", "content": "x"}])
    with pytest.raises(openai.RateLimitError) as limited:
        await client.chat.completions.create(model="mock-1", messages=[{"role": "user", "content": "x"}])
    assert float(limited.value.response.headers["retry-after"]) > 0

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mock.app), base_url="http://mock") as admin:
        await admin.post("/admin/config", json={"rpm": 0, "storm_s": 30})
        assert (await admin.post("/v1/chat/completions", json={"messages": []})).status_code == 429
        await admin.post("/admin/config", json={"storm_s": 0})
        assert (await admin.post("/v1/chat/completions", json={"messages": []})).status_code == 200
        stats = (await admin.get("/admin/stats")).json()
    assert stats["statuses"]["429"] == 2 and stats["statuses"]["200"] == 3
