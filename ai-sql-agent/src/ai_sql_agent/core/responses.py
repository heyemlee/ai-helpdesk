"""Responses wire format for SQL's independent, text-only model transport.

SQL generation has no client-owned function tools. Reject such output instead of
executing or silently discarding it. Helpdesk owns tool/reasoning round trips.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterator

from .errors import ModelResponseError


def payload(messages, *, model, max_tokens, effort, stream, schema=None, name="result") -> Dict[str, Any]:
    result = {"model": model, "input": messages, "max_output_tokens": max_tokens,
              "store": False, "include": ["reasoning.encrypted_content"], "stream": stream}
    if effort:
        result["reasoning"] = {"effort": effort}
    if schema is not None:
        result["text"] = {"format": {"type": "json_schema", "name": name,
                                     "strict": True, "schema": schema}}
    return result


def text(result: Any, *, component: str = "", model: str = "") -> str:
    def fail(reason):
        raise ModelResponseError(f"Responses output rejected: {reason}", component=component, model=model)

    if not isinstance(result, dict):
        fail("invalid_response")
    status = result.get("status")
    if status != "completed":
        reason = (result.get("incomplete_details") or {}).get("reason", "unknown")
        fail(f"{status}:{reason}" if status == "incomplete" else status or "missing_status")
    if not isinstance(result.get("output"), list):
        fail("missing_output")
    parts = []
    for item in result["output"]:
        if not isinstance(item, dict):
            fail("invalid_item")
        if item.get("status") in ("incomplete", "in_progress"):
            fail("incomplete_item")
        if item.get("type") == "reasoning":
            continue
        if item.get("type") != "message":
            fail("unsupported_or_incomplete_item")
        for block in item.get("content", []):
            if block.get("type") == "refusal":
                fail("refusal")
            if block.get("type") == "output_text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
    content = "".join(parts).strip()
    if not content:
        fail("empty_output")
    return content


def stream_text(lines: Iterator[str], on_usage: Callable[[dict], Any]) -> Iterator[str]:
    """Require a terminal event; never treat a disconnected SSE prefix as SQL."""
    data = []

    def events():
        for line in lines:
            if not line:
                if data:
                    raw = "\n".join(data)
                    data.clear()
                    if raw == "[DONE]":
                        return
                    try:
                        yield json.loads(raw)
                    except ValueError as exc:
                        raise ModelResponseError("Responses stream contains invalid JSON") from exc
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())

    for event in events():
        kind = event.get("type")
        if kind == "response.output_text.delta":
            yield event["delta"]
        elif kind in ("response.completed", "response.incomplete", "response.failed"):
            final = event.get("response")
            on_usage(final)
            text(final)
            if kind != "response.completed":
                raise ModelResponseError("Responses stream returned an inconsistent terminal event")
            return
        elif kind == "error":
            raise ModelResponseError("Responses stream returned an error event")
    raise ModelResponseError("Responses stream ended without a final response")
