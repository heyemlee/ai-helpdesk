from types import SimpleNamespace as NS

import httpx
import pytest
from openai import APIConnectionError, AsyncOpenAI
from pydantic import BaseModel

from app.core.openai_compat import AsyncOpenAICompat, GatewayResponseError
from app.core.openai_responses import _input, _ResponsesMessages


class Answer(BaseModel):
    value: int


class FakeResponses:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def client(response):
    return NS(base_url="https://example.test/v1", responses=FakeResponses(response))


def response(output, **kwargs):
    return NS(status="completed", output=output, usage=NS(input_tokens=12, output_tokens=5),
              model="gpt-6.1-sol", incomplete_details=None, **kwargs)


def test_image_and_tool_result_are_preserved():
    items = _input([{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "YWJj"}},
        {"type": "tool_result", "tool_use_id": "call_1", "content": "found"},
    ]}])
    assert items[0]["content"][0]["image_url"] == "data:image/png;base64,YWJj"
    assert items[1] == {"type": "function_call_output", "call_id": "call_1", "output": "found"}


def test_protocol_selection_is_explicit():
    assert isinstance(AsyncOpenAICompat(api_key="test", api_mode="responses").messages, _ResponsesMessages)
    with pytest.raises(ValueError, match="Unknown OpenAI API mode"):
        AsyncOpenAICompat(api_key="test", api_mode="unknown")


async def test_tool_roundtrip_and_usage():
    call = NS(type="function_call", call_id="call_1", name="lookup", arguments='{"id":1}')
    call.model_dump = lambda **_: {"type": call.type, "call_id": call.call_id,
                                   "name": call.name, "arguments": call.arguments}
    fake = client(response([call]))
    messages = _ResponsesMessages(fake)
    result = await messages.create(model="gpt-6.1-sol", max_tokens=100,
                                   messages=[{"role": "user", "content": "find"}],
                                   tools=[{"name": "lookup", "input_schema": {"type": "object"}}])
    assert result.stop_reason == "tool_use"
    assert result.usage.input_tokens == 12
    assert result.content[0].input == {"id": 1}
    assert _input([{"role": "assistant", "content": result.content}])[0]["call_id"] == "call_1"
    assert fake.responses.calls[0]["tools"][0]["strict"] is False


async def test_structured_output_uses_sdk_parse():
    parsed = Answer(value=3)
    fake = client(response([], output_parsed=parsed))
    result = await _ResponsesMessages(fake).parse(model="gpt-6.1-sol", max_tokens=100,
                                                  messages=[], output_format=Answer)
    assert result.parsed_output == parsed
    assert fake.responses.calls[0]["text_format"] is Answer


class Stream:
    def __init__(self, events):
        self.events = events
        self.closed = False

    def __aiter__(self):
        async def iterator():
            for event in self.events:
                yield event
        return iterator()

    async def close(self):
        self.closed = True


async def test_stream_text_and_final_usage():
    item = NS(type="message", content=[NS(type="output_text", text="hello")])
    item.model_dump = lambda **_: {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": "hello"}]}
    final = response([item])
    events = Stream([NS(type="response.output_text.delta", delta="hello"),
                     NS(type="response.completed", response=final)])
    messages = _ResponsesMessages(client(events))
    async with messages.stream(model="gpt-6.1-sol", max_tokens=100, messages=[]) as stream:
        assert [part async for part in stream.text_stream] == ["hello"]
        result = await stream.get_final_message()
    assert result.content[0].text == "hello"
    assert result.usage.output_tokens == 5
    assert events.closed


async def test_stream_without_completion_is_error():
    messages = _ResponsesMessages(client(Stream([])))
    with pytest.raises(GatewayResponseError):
        async with messages.stream(model="gpt-6.1-sol", max_tokens=100, messages=[]) as stream:
            await stream.get_final_message()


async def test_incomplete_response_and_network_errors_are_not_reported_as_success():
    incomplete = response([])
    incomplete.status = "incomplete"
    incomplete.incomplete_details = NS(reason="content_filter")
    with pytest.raises(GatewayResponseError, match="incomplete"):
        await _ResponsesMessages(client(incomplete)).create(model="gpt-6.1-sol", max_tokens=10, messages=[])

    async def fail(_request):
        raise httpx.ConnectError("unavailable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as http:
        sdk = AsyncOpenAI(api_key="test", base_url="https://example.test/v1", http_client=http, max_retries=0)
        with pytest.raises(APIConnectionError):
            await _ResponsesMessages(sdk).create(model="gpt-6.1-sol", max_tokens=10, messages=[])


async def test_real_sdk_decodes_response_and_replays_reasoning():
    calls = []

    async def transport(request):
        import json

        calls.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "resp_1", "object": "response", "created_at": 1,
            "model": "gpt-6.1-sol", "status": "completed", "error": None,
            "incomplete_details": None,
            "output": [
                {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"},
                {"type": "function_call", "id": "fc_1", "call_id": "call_1",
                 "name": "lookup", "arguments": "{}", "status": "completed"},
            ],
            "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
                      "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens_details": {"reasoning_tokens": 0}},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        sdk = AsyncOpenAI(api_key="test", base_url="https://example.test/v1", http_client=http)
        adapter = _ResponsesMessages(sdk)
        result = await adapter.create(model="gpt-6.1-sol", max_tokens=100, messages=[])
        replay = _input([{"role": "assistant", "content": result.content}])
    assert calls[0]["store"] is False
    assert replay[0]["encrypted_content"] == "opaque"
    assert replay[1]["id"] == "fc_1"
