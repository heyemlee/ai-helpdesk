"""Bounded, read-only Sol capability baseline. Never imports the service or writes settings.

Run from an empty environment, with explicit credential files. --execute authorizes
at most six model HTTP requests; no retries, discovery calls, or protocol fallback.
Only synthetic prompts are sent. Output contains categories/counts, never secrets,
raw model output, reasoning items, URLs or upstream error messages.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from dotenv import dotenv_values
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "ai-sql-agent" / "src")]


class ProbeAnswer(BaseModel):
    value: int


def connections(args):
    chat = json.loads(Path(args.backend_settings).read_text())["chat"]["values"]
    backend = (chat["base_url"], chat["api_key"]) if chat["provider"] == "openai" else None
    sql = None
    if args.sql_container:
        # Captured only in process memory; never print or persist the secret document.
        process = subprocess.run(["docker", "exec", args.sql_container, "python", "-c",
            "from pathlib import Path; p=Path('/app/state/model_settings.json'); print(p.read_text() if p.exists() else '{}')"],
            capture_output=True, timeout=10, check=True)
        values = json.loads(process.stdout).get("values", {})
        if values.get("model_provider") == "openai":
            sql = (values["openai_base_url"], values["openai_api_key"])
    if sql is None and args.sql_env:
        env = dotenv_values(args.sql_env)
        provider = env.get("AI_SQL_MODEL_PROVIDER", env.get("MODEL_PROVIDER", "openai"))
        key = env.get("AI_SQL_OPENAI_API_KEY", env.get("OPENAI_API_KEY", ""))
        base = env.get("AI_SQL_OPENAI_BASE_URL", env.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
        if provider == "openai" and key:
            sql = (base, key)
    return backend, sql


def category(connection):
    if connection is None:
        return "missing_openai_connection"
    parts = urlsplit(connection[0])
    return parts.scheme + ("_official_openai" if parts.hostname == "api.openai.com" else "_third_party")


async def run(args, backend, sql):
    from app.core.llm_utils import extract_text_content
    from app.core.openai_compat import AsyncOpenAICompat
    from app.events.model_calls import usage_of

    report = {"schema_version": 1, "dataset": "protocol-synthetic-v1", "model": args.model,
              "protocol": "responses", "effort": "low", "max_output_tokens": 256,
              "timeout_s": 25, "retries": 0, "request_limit": 6, "requests": 0,
              "timestamp": datetime.now(ZoneInfo("America/Los_Angeles")).isoformat(),
              "connections": {"helpdesk": category(backend), "sql": category(sql)}, "checks": []}
    failed = set()

    async def attempt(name, connection, call):
        if connection is None or connection in failed:
            report["checks"].append({"check": name, "outcome": "unverified", "reason": "missing_connection_or_prior_failure"})
            return None
        assert report["requests"] < 6
        report["requests"] += 1
        start = time.monotonic()
        entry = {"check": name}
        try:
            result = await call()
            message = result if hasattr(result, "usage") else None
            entry.update(outcome="ok", usage=usage_of(message))
            if message is not None:
                entry["returned_model"] = message.model if message.model == args.model else "different_or_missing"
            return result
        except Exception as exc:
            failed.add(connection)
            status = getattr(exc, "status_code", None)
            reason = getattr(exc, "reason", None)
            entry.update(outcome="failed", error_type=type(exc).__name__, http_status=status,
                         reason=reason if isinstance(reason, str) and len(reason) < 60 else "request_or_validation_failed",
                         usage=usage_of(getattr(exc, "response_message", None)))
            return None
        finally:
            entry["latency_ms"] = round((time.monotonic() - start) * 1000)
            report["checks"].append(entry)

    client = AsyncOpenAICompat(api_key=backend[1], base_url=backend[0], api_mode="responses",
                              max_retries=0, timeout=25) if backend else None
    options = {"model": args.model, "max_tokens": 256, "output_config": {"effort": "low"}}
    history = [{"role": "user", "content": "Reply exactly OK."}]

    async def plain():
        msg = await client.messages.create(**options, messages=history)
        assert extract_text_content(msg.content).strip().upper() == "OK", "unexpected_text"
        assert msg.model == args.model, "returned_model_mismatch"
        return msg

    async def structured():
        msg = await client.messages.parse(**options, messages=[{"role": "user", "content": "Return value=7."}], output_format=ProbeAnswer)
        assert msg.parsed_output.value == 7
        return msg

    tool_history = [{"role": "user", "content": "Call protocol_probe with value=7. Then reply with the tool result only."}]
    tools = [{"name": "protocol_probe", "description": "A synthetic test that returns the input number.",
              "input_schema": {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}}]

    async def tool_call():
        msg = await client.messages.create(**options, messages=tool_history, tools=tools,
                                          tool_choice={"type": "tool", "name": "protocol_probe"})
        calls = [b for b in msg.content if getattr(b, "type", None) == "tool_use"]
        assert len(calls) == 1 and calls[0].name == "protocol_probe" and calls[0].input == {"value": 7}
        tool_history.extend([{"role": "assistant", "content": msg.content}, {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": calls[0].id, "content": "7"}]}])
        report["reasoning_items_observed"] = sum(isinstance(b, dict) and b.get("type") == "responses_item" for b in msg.content)
        return msg

    async def tool_reply():
        msg = await client.messages.create(**options, messages=tool_history, tools=tools, tool_choice={"type": "none"})
        assert extract_text_content(msg.content).strip() == "7"
        return msg

    async def streaming():
        async with client.messages.stream(**options, messages=history) as stream:
            parts = [part async for part in stream.text_stream]
            msg = await stream.get_final_message()
        assert "".join(parts).strip().upper() == "OK"
        return msg

    async def sql_text():
        import httpx
        from ai_sql_agent.core import ModelAccessRegistry, ModelTransport, RetryPolicy, usage_scope, usage_summary
        from ai_sql_agent.core.settings import build_endpoint

        with httpx.Client() as http, usage_scope():
            transport = ModelTransport("protocol_probe", api_key=sql[1], api_mode="responses", client=http,
                                       retry_policy=RetryPolicy(max_retries=0), access_registry=ModelAccessRegistry(), timeout=25)
            result = transport.chat(build_endpoint(sql[0], "responses"), history, model=args.model, max_tokens=256)
            assert result.strip().upper() == "OK"
            report["sql_usage"] = usage_summary()
        return result

    try:
        await attempt("helpdesk_text", backend, plain)
        await attempt("sql_text", sql, sql_text)
        await attempt("helpdesk_structured", backend, structured)
        called = await attempt("helpdesk_tool", backend, tool_call)
        if called is not None:
            await attempt("helpdesk_tool_reply", backend, tool_reply)
        else:
            report["checks"].append({"check": "helpdesk_tool_reply", "outcome": "unverified", "reason": "tool_call_not_available"})
        await attempt("helpdesk_stream", backend, streaming)
    finally:
        if client:
            await client.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-settings", required=True)
    parser.add_argument("--sql-env")
    parser.add_argument("--sql-container")
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--output", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    backend, sql = connections(args)
    logging.disable(logging.CRITICAL)
    os.environ["AI_SQL_MODEL_SETTINGS_ENABLED"] = "false"
    if not args.execute:
        print(json.dumps({"helpdesk": category(backend), "sql": category(sql), "same_connection": backend == sql}))
        return
    # No .env discovery or persistent state from either application is permitted.
    with tempfile.TemporaryDirectory(prefix="protocol-probe-") as directory:
        os.chdir(directory)
        report = asyncio.run(run(args, backend, sql))
    report["harness_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
