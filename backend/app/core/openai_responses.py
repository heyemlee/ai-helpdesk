"""Explicit Responses API adapter for the existing messages interface."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from openai.lib._pydantic import to_strict_json_schema

from app.core.openai_compat import (
    GatewayResponseError,
    _block_get,
    _block_text,
    _flatten_text,
    _is_text_block,
    _Message,
    _Messages,
    _openai_tool,
    _openai_tool_choice,
    _TextBlock,
    _tool_result_text,
    _tool_use_block,
    _Usage,
)


class ResponsesOutputError(GatewayResponseError):
    """A billed response that is unusable; keep usage without exposing partial output."""

    def __init__(self, reason: str, message: _Message):
        super().__init__(f"Responses output rejected: {reason}")
        self.reason = reason
        if message.stop_reason == "end_turn":
            message.stop_reason = reason.split(":")[0]
        self.response_message = message


def _input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role", "user")
        blocks = message.get("content", "")
        if isinstance(blocks, str):
            items.append({"role": role, "content": blocks})
            continue
        content: list[dict[str, Any]] = []

        def flush(content: list[dict[str, Any]] = content, role: str = role) -> None:
            if content:
                items.append({"role": role, "content": list(content)})
                content.clear()

        for block in blocks:
            kind = _block_get(block, "type")
            original_item = _block_get(block, "responses_item")
            if original_item is not None:
                flush()
                items.append(original_item)
                continue
            if kind == "responses_item":
                flush()
                items.append(_block_get(block, "item"))
            elif kind == "tool_use":
                flush()
                raw = _block_get(block, "raw_arguments")
                items.append({"type": "function_call", "call_id": _block_get(block, "id"),
                              "name": _block_get(block, "name"), "arguments": raw if raw is not None
                              else json.dumps(_block_get(block, "input") or {}, ensure_ascii=False)})
            elif kind == "tool_result":
                flush()
                result = _tool_result_text(_block_get(block, "content"))
                if _block_get(block, "is_error"):
                    result = "[error] " + result
                items.append({"type": "function_call_output", "call_id": _block_get(block, "tool_use_id"),
                              "output": result})
            elif _is_text_block(block):
                content.append({"type": "output_text" if role == "assistant" else "input_text",
                                "text": _block_text(block)})
            elif kind == "image" and role == "user":
                source = _block_get(block, "source") or {}
                if source.get("type") == "base64":
                    url = f"data:{source['media_type']};base64,{source['data']}"
                elif source.get("type") == "url":
                    url = source["url"]
                else:
                    raise ValueError("Unsupported image source")
                content.append({"type": "input_image", "image_url": url})
            else:
                raise ValueError(f"Unsupported Responses content block: {kind}")
        flush()
    return items


class _ResponsesMessages(_Messages):
    def schema_enforced(self, model: str) -> bool:
        # Responses never downgrades its strict schema. A legacy Chat rejection
        # on the same root/model must not contaminate this protocol's diagnostics.
        return True

    def _params(self, *, model: str, messages: list[dict[str, Any]], max_tokens: int,
                system: Any = None, output_config: dict[str, Any] | None = None,
                tools: list[Any] | None = None, tool_choice: dict[str, Any] | None = None,
                **_ignored: Any) -> dict[str, Any]:
        params: dict[str, Any] = {"model": model, "input": _input(messages),
                                  "max_output_tokens": max_tokens, "store": False,
                                  "include": ["reasoning.encrypted_content"]}
        instructions = _flatten_text(system)
        if instructions:
            params["instructions"] = instructions
        if tools:
            params["tools"] = [{"type": "function", **_openai_tool(t)["function"], "strict": False}
                               for t in tools]
            choice = _openai_tool_choice(tool_choice)
            if isinstance(choice, dict):
                choice = {"type": "function", "name": choice["function"]["name"]}
            if choice is not None:
                params["tool_choice"] = choice
            if (tool_choice or {}).get("disable_parallel_tool_use"):
                params["parallel_tool_calls"] = False
        effort = (output_config or {}).get("effort")
        if effort:
            params["reasoning"] = {"effort": effort}
        return params

    def _convert(self, response: Any, params: dict[str, Any], parsed: Any = None) -> _Message:
        if isinstance(response, str) or not hasattr(response, "output"):
            raise GatewayResponseError("Responses endpoint returned non-JSON content")
        status = getattr(response, "status", None)
        usage = getattr(response, "usage", None)
        details = getattr(response, "incomplete_details", None)
        reason = "max_tokens" if getattr(details, "reason", None) == "max_output_tokens" else "end_turn"
        result = _Message("", reason, _Usage(getattr(usage, "input_tokens", 0) or 0,
                          getattr(usage, "output_tokens", 0) or 0), getattr(response, "model", params["model"]),
                          parsed_output=parsed)
        if usage is None:
            result.usage = None
        else:
            result.usage.cache_read_input_tokens = getattr(
                getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0
            result.usage.reasoning_tokens = getattr(
                getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0) or 0
        result.content = []
        if status != "completed":
            result.stop_reason = reason if status == "incomplete" else (status or "invalid_status")
            raise ResponsesOutputError(
                f"incomplete:{getattr(details, 'reason', None)}" if status == "incomplete"
                else (status or "invalid_status"), result)
        for item in getattr(response, "output", None) or []:
            kind = getattr(item, "type", None)
            if getattr(item, "status", None) in ("incomplete", "in_progress"):
                raise ResponsesOutputError(f"incomplete_{kind}", result)
            if kind == "reasoning":
                # Preserve opaque reasoning items for the next tool round.
                result.content.append({"type": "responses_item", "item": item.model_dump(exclude_none=True)})
            elif kind == "function_call":
                if not getattr(item, "call_id", None) or not getattr(item, "name", None) or not isinstance(
                        getattr(item, "arguments", None), str):
                    raise ResponsesOutputError("invalid_tool_call", result)
                call = _tool_use_block(item.call_id, item.name, item.arguments)
                call.responses_item = item.model_dump(exclude_none=True)
                result.content.append(call)
                result.stop_reason = "tool_use"
            elif kind == "message":
                raw_item = item.model_dump(exclude_none=True)
                parts = []
                for block in item.content:
                    if getattr(block, "type", None) == "output_text":
                        parts.append(block.text)
                    elif getattr(block, "type", None) == "refusal":
                        result.stop_reason = "refusal"
                        raise ResponsesOutputError("refusal", result)
                text = _TextBlock("".join(parts))
                text.responses_item = raw_item
                result.content.append(text)
            else:
                raise ResponsesOutputError("unsupported_output_item", result)
        if not any(_block_get(b, "type") == "tool_use" or
                   (_block_get(b, "type") == "text" and _block_text(b).strip()) for b in result.content):
            raise ResponsesOutputError("empty_output", result)
        return result

    async def create(self, **kwargs: Any) -> _Message:
        params = self._params(**kwargs)
        return self._convert(await self._client.responses.create(**params), params)

    async def parse(self, *, output_format: type, **kwargs: Any) -> _Message:
        params = self._params(**kwargs)
        # Validate status before parsing: a syntactically valid prefix is still a
        # truncated response, and SDK parse failures can otherwise lose its usage.
        params["text"] = {"format": {"type": "json_schema", "name": output_format.__name__,
                                     "strict": True, "schema": to_strict_json_schema(output_format)}}
        result = self._convert(await self._client.responses.create(**params), params)
        try:
            result.parsed_output = output_format.model_validate_json(
                "".join(_block_text(b) for b in result.content if _is_text_block(b)))
        except ValueError as ex:
            raise ResponsesOutputError("invalid_structured_output", result) from ex
        return result

    def stream(self, **kwargs: Any) -> _ResponsesStream:
        return _ResponsesStream(self, self._params(**kwargs))


class _ResponsesStream:
    def __init__(self, messages: _ResponsesMessages, params: dict[str, Any]):
        self.messages, self.params = messages, params
        self.stream: Any = None
        self.response: Any = None
        self.drained = False

    async def __aenter__(self) -> _ResponsesStream:
        self.stream = await self.messages._client.responses.create(**self.params, stream=True)
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        if self.stream is not None:
            await self.stream.close()

    @property
    def text_stream(self) -> AsyncIterator[str]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[str]:
        if self.drained:
            return
        async for event in self.stream:
            kind = getattr(event, "type", "")
            if kind == "response.output_text.delta":
                yield event.delta
            elif kind in ("response.completed", "response.incomplete", "response.failed"):
                self.response = event.response
                self.drained = True
                # Terminal status is authoritative, including final reasoning items.
                final = self.messages._convert(self.response, self.params)
                if kind != "response.completed":
                    raise ResponsesOutputError("invalid_terminal_event", final)
                return
            elif kind == "error":
                raise GatewayResponseError(f"Responses stream failed: {kind}")
        self.drained = True
        if self.response is None:
            raise GatewayResponseError("Responses stream ended without a final response")

    async def get_final_message(self) -> _Message:
        if not self.drained:
            async for _ in self._iterate():
                pass
        return self.messages._convert(self.response, self.params)
