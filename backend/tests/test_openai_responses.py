from types import SimpleNamespace as NS

import httpx
import pytest
from openai import APIConnectionError, AsyncOpenAI
from pydantic import BaseModel

from app.core.openai_compat import AsyncOpenAICompat, GatewayResponseError
from app.core.openai_responses import ResponsesOutputError, _input, _ResponsesMessages


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
    item = NS(type="message", content=[NS(type="output_text", text='{"value":3}')])
    item.model_dump = lambda **_: {"type": "message", "role": "assistant", "content": []}
    fake = client(response([item]))
    result = await _ResponsesMessages(fake).parse(model="gpt-6.1-sol", max_tokens=100,
                                                  messages=[], output_format=Answer)
    assert result.parsed_output == parsed
    assert fake.responses.calls[0]["text"]["format"]["strict"] is True


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


def wire_response(output=None, **overrides):
    return {
        "id": "resp_test", "object": "response", "created_at": 1, "status": "completed",
        "model": "gpt-6.1-sol", "error": None, "incomplete_details": None,
        "output": output if output is not None else [{"type": "message", "id": "msg_1", "role": "assistant",
            "status": "completed", "content": [{"type": "output_text", "text": "ok", "annotations": []}]}],
        "usage": {"input_tokens": 12, "output_tokens": 7, "total_tokens": 19,
                  "input_tokens_details": {"cached_tokens": 3},
                  "output_tokens_details": {"reasoning_tokens": 2}}, **overrides,
    }


@pytest.mark.parametrize("overrides,reason", [
    ({"output": []}, "empty_output"),
    ({"output": [{"type": "reasoning", "id": "rs", "summary": [], "encrypted_content": "opaque"}]}, "empty_output"),
    ({"output": [{"type": "message", "id": "msg", "role": "assistant", "status": "incomplete",
                   "content": [{"type": "output_text", "text": "partial"}]}]}, "incomplete_message"),
    ({"output": [{"type": "function_call", "name": "lookup", "arguments": "{}"}]}, "invalid_tool_call"),
    ({"status": None}, "invalid_status"),
    ({"status": "in_progress"}, "in_progress"),
    ({"status": "cancelled"}, "cancelled"),
    ({"status": "failed", "error": {"code": "server_error", "message": "secret upstream text"}}, "failed"),
    ({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}, "incomplete:max_output_tokens"),
    ({"status": "incomplete", "incomplete_details": {"reason": "content_filter"}}, "incomplete:content_filter"),
    ({"output": [{"type": "message", "id": "msg", "role": "assistant", "content": [
        {"type": "refusal", "refusal": "refused"}]}]}, "refusal"),
])
async def test_unusable_output_fails_closed_but_retains_usage(overrides, reason):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=wire_response(**overrides)))) as http:
        sdk = AsyncOpenAI(api_key="test", base_url="https://example.test/v1", http_client=http, max_retries=0)
        with pytest.raises(ResponsesOutputError) as caught:
            await _ResponsesMessages(sdk).create(model="gpt-6.1-sol", max_tokens=64, messages=[])
    assert caught.value.reason == reason
    assert caught.value.response_message.usage.output_tokens == 7
    assert caught.value.response_message.usage.cache_read_input_tokens == 3
    assert caught.value.response_message.usage.reasoning_tokens == 2
    assert "secret upstream" not in str(caught.value)


async def test_three_turn_reasoning_and_parallel_tool_replay():
    import json
    requests = []
    outputs = [
        [{"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "first"},
         {"type": "function_call", "id": "fc_1", "call_id": "c1", "name": "lookup", "arguments": "{}"},
         {"type": "function_call", "id": "fc_2", "call_id": "c2", "name": "lookup", "arguments": "{bad"}],
        [{"type": "reasoning", "id": "rs_2", "summary": [], "encrypted_content": "second"},
         {"type": "function_call", "id": "fc_3", "call_id": "c3", "name": "lookup", "arguments": "{}"}],
        wire_response()["output"],
    ]

    def handle(req):
        assert req.url.path == "/v1/responses"
        requests.append(json.loads(req.content))
        return httpx.Response(200, json=wire_response(outputs[len(requests)-1]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        sdk = AsyncOpenAI(api_key="test", base_url="https://example.test/v1", http_client=http)
        adapter = _ResponsesMessages(sdk)
        history = [{"role": "user", "content": "lookup"}]
        for turn in range(3):
            result = await adapter.create(model="gpt-6.1-sol", max_tokens=64, messages=history,
                                          output_config={"effort": "low"})
            if turn == 0:
                assert result.content[2].parse_error
            history.append({"role": "assistant", "content": result.content})
            calls = [b for b in result.content if getattr(b, "type", "") == "tool_use"]
            if calls:
                history.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": b.id,
                    "content": "invalid" if b.parse_error else "1", "is_error": bool(b.parse_error)} for b in calls]})
    replay = requests[2]["input"]
    assert [i["encrypted_content"] for i in replay if i.get("type") == "reasoning"] == ["first", "second"]
    assert [i["call_id"] for i in replay if i.get("type") == "function_call_output"] == ["c1", "c2", "c3"]
    assert next(i for i in replay if i.get("id") == "fc_2")["arguments"] == "{bad"
    assert all(r["include"] == ["reasoning.encrypted_content"] and r["store"] is False for r in requests)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("failure", ["429", "timeout", "400"])
async def test_transport_errors_use_guard_and_never_change_protocol(failure, streaming):
    from openai import APITimeoutError, BadRequestError

    from app.core.llm_guard import GuardedLLMClient, LLMGuard, LLMUnavailableError
    requests = []

    def handle(req):
        requests.append(req.url.path)
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout", request=req)
        status = int(failure)
        return httpx.Response(status, headers={"retry-after": "60"}, json={"error": {
            "message": "limited" if status == 429 else "responses unsupported", "type": "rate_limit_error"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        compat = AsyncOpenAICompat(api_key="test", api_mode="responses", base_url="https://example.test/v1",
                                   http_client=http, max_retries=0)
        wrapped = GuardedLLMClient(compat, LLMGuard("test", provider="openai"))
        expected = {"429": LLMUnavailableError, "400": BadRequestError, "timeout": APITimeoutError}[failure]
        with pytest.raises(expected):
            if streaming:
                async with wrapped.messages.stream(model="gpt-6.1-sol", max_tokens=64, messages=[]) as stream:
                    await stream.get_final_message()
            else:
                await wrapped.messages.create(model="gpt-6.1-sol", max_tokens=64, messages=[])
    assert requests == ["/v1/responses"]


@pytest.mark.parametrize("terminal", ["response.completed", "response.incomplete", "response.failed", "missing"])
async def test_real_sdk_sse_terminal_status_and_usage(terminal):
    import json
    final = wire_response()
    if terminal in ("response.incomplete", "response.failed"):
        final.update(status=terminal.split(".")[1], incomplete_details={"reason": "max_output_tokens"})
    events = [{"type": "response.output_text.delta", "delta": "ok", "item_id": "msg_1", "output_index": 0,
               "content_index": 0, "sequence_number": 0}]
    if terminal != "missing":
        events.append({"type": terminal, "response": final, "sequence_number": 1})
    raw = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=raw))) as http:
        adapter = _ResponsesMessages(AsyncOpenAI(api_key="test", base_url="https://example.test/v1", http_client=http))
        async with adapter.stream(model="gpt-6.1-sol", max_tokens=64, messages=[]) as stream:
            if terminal == "response.completed":
                assert [v async for v in stream.text_stream] == ["ok"]
                assert (await stream.get_final_message()).usage.output_tokens == 7
                assert (await stream.get_final_message()).usage.output_tokens == 7
            else:
                with pytest.raises(GatewayResponseError):
                    await stream.get_final_message()


@pytest.mark.parametrize("body,overrides,valid", [
    ('{"value":3}', {}, True), ('{"value":"bad"}', {}, False),
    ('{"value":3}', {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}, False),
])
async def test_structured_http_validates_status_and_schema(body, overrides, valid):
    import json
    requests = []
    result = wire_response(**overrides)
    result["output"][0]["content"][0]["text"] = body

    def handle(req):
        requests.append(json.loads(req.content))
        return httpx.Response(200, json=result)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        adapter = _ResponsesMessages(AsyncOpenAI(api_key="test", base_url="https://example.test/v1", http_client=http))
        if valid:
            msg = await adapter.parse(model="gpt-6.1-sol", max_tokens=64, messages=[], output_format=Answer)
            assert msg.parsed_output.value == 3
        else:
            with pytest.raises(ResponsesOutputError):
                await adapter.parse(model="gpt-6.1-sol", max_tokens=64, messages=[], output_format=Answer)
    assert requests[0]["text"]["format"]["schema"]["additionalProperties"] is False
    assert len(requests) == 1


async def test_contradictory_terminal_event_is_not_success():
    final = response([])
    item = NS(type="message", content=[NS(type="output_text", text="ok")])
    item.model_dump = lambda **_: {"type": "message", "role": "assistant", "content": []}
    final.output = [item]
    messages = _ResponsesMessages(client(Stream([NS(type="response.failed", response=final)])))
    with pytest.raises(ResponsesOutputError, match="invalid_terminal_event"):
        async with messages.stream(model="gpt-6.1-sol", max_tokens=64, messages=[]) as stream:
            await stream.get_final_message()


def test_chat_schema_fallback_cache_does_not_contaminate_responses(monkeypatch):
    from app.core import openai_compat

    fake = client(response([]))
    model = "gpt-6.1-sol"
    legacy = openai_compat._Messages(fake)
    strict = _ResponsesMessages(fake)
    monkeypatch.setattr(openai_compat, "_SCHEMA_NOT_ENFORCED", {(legacy._root, model)})
    assert not openai_compat.schema_enforced_for(NS(messages=legacy), model)
    assert openai_compat.schema_enforced_for(NS(messages=strict), model)
