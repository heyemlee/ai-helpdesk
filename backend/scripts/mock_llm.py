"""
假的 OpenAI 兼容模型服务：压测和故障演练用，不花一分钱，结果可以复现。

真模型压测有两个问题：花钱，而且供应商那边的延迟和限流每次都不一样，测出来的数字
说不清是谁的。这个服务实现 `POST /v1/chat/completions` 的三种用法——普通生成、
结构化输出（response_format=json_schema，按 Schema 造一个合法的实例）、流式（带 usage）
——延迟、吐字速度、RPM 上限、出错率都能配，运行中还能改（演练时制造一阵 429）。

把后端指过来：

    LLM_PROVIDER=openai
    OPENAI_BASE_URL=http://mock-llm:9000/v1
    OPENAI_API_KEY=mock
    OPENAI_MODEL=mock-1

起服务（后端镜像里就有，不用另建镜像）：

    python scripts/mock_llm.py --port 9000

| 变量 | 默认 | 说明 |
|---|---|---|
| MOCK_LLM_LATENCY_MS | 800 | 作答（流式）第一个字之前等多久，±30% 抖动 |
| MOCK_LLM_STRUCTURED_LATENCY_MS | 400 | 结构化输出（意图、改写、重排……）一次多久 |
| MOCK_LLM_CHUNK_MS | 60 | 流式两片之间隔多久 |
| MOCK_LLM_RPM | 0 | 每分钟最多接多少请求，超了回 429 + retry-after；0 不限 |
| MOCK_LLM_ERROR_RATE | 0 | 多大比例的请求回 500 |
| MOCK_LLM_HANDOFF_RATE | 0.1 | 作答里多大比例在最后一行声明要人工（[[handoff:yes]]），演练靠它建工单 |

运行中改配置、看计数：

    curl -X POST localhost:9000/admin/config -d '{"rpm": 60}'
    curl -X POST localhost:9000/admin/config -d '{"storm_s": 60}'   # 接下来 60 秒全部 429
    curl localhost:9000/admin/stats
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import time
import uuid
from collections import Counter, deque
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

ANSWERS = [
    "您好，这个问题我帮您看了一下。补件需要先由售后核实缺件清单，核实之后会安排补发。",
    "关于柜门关不严，通常是铰链需要微调。可以先检查铰链上的调节螺丝，如果还是不行，我们安排师傅上门。",
    "报价以销售给您的正式报价单为准，如果对其中某一项有疑问，可以把报价单编号发给我，我帮您转给对应同事核对。",
    "订单的生产和发货进度以系统记录为准。我这边会按流程帮您跟进，有更新会在这里告诉您。",
]


def _env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


class State:
    def __init__(self):
        self.config = {
            "latency_ms": _env("MOCK_LLM_LATENCY_MS", 800),
            "structured_latency_ms": _env("MOCK_LLM_STRUCTURED_LATENCY_MS", 400),
            "chunk_ms": _env("MOCK_LLM_CHUNK_MS", 60),
            "rpm": int(_env("MOCK_LLM_RPM", 0)),
            "error_rate": _env("MOCK_LLM_ERROR_RATE", 0.0),
            "handoff_rate": _env("MOCK_LLM_HANDOFF_RATE", 0.1),
            "storm_until": 0.0,
        }
        self.window: deque[float] = deque()
        self.statuses: Counter = Counter()
        self.kinds: Counter = Counter()
        self.in_flight = 0
        self.max_in_flight = 0
        self.started = time.time()

    def admit(self) -> float | None:
        """这次请求放不放行。不放行返回建议的 retry-after 秒数。"""
        now = time.time()
        if now < self.config["storm_until"]:
            return max(1.0, min(5.0, self.config["storm_until"] - now))
        rpm = self.config["rpm"]
        while self.window and self.window[0] < now - 60:
            self.window.popleft()
        if rpm and len(self.window) >= rpm:
            return max(0.5, self.window[0] + 60 - now)
        self.window.append(now)
        return None


state = State()
app = FastAPI(title="mock-llm")


def _jitter(ms: float) -> float:
    return max(ms * random.uniform(0.7, 1.3), 0) / 1000


def _prompt_tokens(body: dict[str, Any]) -> int:
    chars = sum(len(json.dumps(m.get("content", ""), ensure_ascii=False)) for m in body.get("messages") or [])
    return max(chars // 2, 1)


def instance(schema: dict[str, Any], defs: dict[str, Any], depth: int = 0) -> Any:
    """按 JSON Schema 造一个合法的实例：枚举取第一个，数取区间中点，数组按最少条数。"""
    if depth > 20:
        return None
    if "$ref" in schema:
        return instance(defs.get(schema["$ref"].rsplit("/", 1)[-1], {}), defs, depth + 1)
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = [o for o in schema[key] if o.get("type") != "null"] or schema[key]
            return instance(options[0], defs, depth + 1)
    if "allOf" in schema and schema["allOf"]:
        return instance(schema["allOf"][0], defs, depth + 1)
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "null")
    if kind == "object" or "properties" in schema:
        return {name: instance(sub, defs, depth + 1) for name, sub in (schema.get("properties") or {}).items()}
    if kind == "array":
        item = schema.get("items") or {}
        return [instance(item, defs, depth + 1) for _ in range(int(schema.get("minItems", 0)))]
    if kind == "string":
        return "ok" + "k" * max(int(schema.get("minLength", 0)) - 2, 0)
    if kind in ("number", "integer"):
        lo = schema.get("minimum", schema.get("exclusiveMinimum", 0))
        hi = schema.get("maximum", schema.get("exclusiveMaximum", max(lo, 1)))
        value = (lo + hi) / 2
        return int(round(value)) if kind == "integer" else value
    if kind == "boolean":
        return False
    return None


def _completion(body: dict[str, Any], content: str, finish: str = "stop") -> dict[str, Any]:
    prompt = _prompt_tokens(body)
    completion = max(len(content) // 2, 1)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}", "object": "chat.completion", "created": int(time.time()),
        "model": body.get("model", "mock-1"),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion},
    }


def _answer_text() -> str:
    marker = "[[handoff:yes]]" if random.random() < state.config["handoff_rate"] else "[[handoff:no]]"
    return f"{random.choice(ANSWERS)}\n{marker}"


def _rejection(status: int, retry_after: float | None = None) -> JSONResponse:
    state.statuses[status] += 1
    if status == 429:
        return JSONResponse(
            status_code=429, headers={"retry-after": f"{retry_after:.0f}"},
            content={"error": {"message": "Rate limit reached for requests", "type": "rate_limit_error",
                               "code": "rate_limit_exceeded"}})
    return JSONResponse(status_code=status, content={"error": {"message": "mock upstream error",
                                                               "type": "server_error"}})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    retry_after = state.admit()
    if retry_after is not None:
        return _rejection(429, retry_after)
    if random.random() < state.config["error_rate"]:
        return _rejection(500)

    state.in_flight += 1
    state.max_in_flight = max(state.max_in_flight, state.in_flight)
    response_format = body.get("response_format") or {}
    try:
        if response_format.get("type") == "json_schema":
            state.kinds["structured"] += 1
            schema = (response_format.get("json_schema") or {}).get("schema") or {}
            await asyncio.sleep(_jitter(state.config["structured_latency_ms"]))
            state.statuses[200] += 1
            return _completion(body, json.dumps(instance(schema, schema.get("$defs", {})), ensure_ascii=False))
        if body.get("stream"):
            state.kinds["stream"] += 1
            state.statuses[200] += 1
            return StreamingResponse(_stream(body), media_type="text/event-stream")
        state.kinds["plain"] += 1
        await asyncio.sleep(_jitter(state.config["latency_ms"]))
        state.statuses[200] += 1
        return _completion(body, _answer_text())
    finally:
        if not body.get("stream") or response_format.get("type") == "json_schema":
            state.in_flight -= 1


async def _stream(body: dict[str, Any]):
    try:
        base = {"id": f"chatcmpl-{uuid.uuid4().hex[:24]}", "object": "chat.completion.chunk",
                "created": int(time.time()), "model": body.get("model", "mock-1")}

        def chunk(delta: dict[str, Any], finish: str | None = None) -> str:
            payload = {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        await asyncio.sleep(_jitter(state.config["latency_ms"]))
        yield chunk({"role": "assistant", "content": ""})
        text = _answer_text()
        for start in range(0, len(text), 8):
            yield chunk({"content": text[start:start + 8]})
            await asyncio.sleep(state.config["chunk_ms"] / 1000)
        yield chunk({}, "stop")
        if (body.get("stream_options") or {}).get("include_usage"):
            prompt, completion = _prompt_tokens(body), max(len(text) // 2, 1)
            usage = {**base, "choices": [], "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                                                      "total_tokens": prompt + completion}}
            yield f"data: {json.dumps(usage)}\n\n"
        yield "data: [DONE]\n\n"
    finally:
        state.in_flight -= 1


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": "mock-1", "object": "model", "owned_by": "mock"}]}


@app.post("/admin/config")
async def configure(request: Request):
    changes = await request.json()
    storm = changes.pop("storm_s", None)
    if storm is not None:
        state.config["storm_until"] = time.time() + float(storm)
    for key, value in changes.items():
        if key in state.config:
            state.config[key] = type(state.config[key])(value)
    return {"config": state.config}


@app.get("/admin/stats")
async def stats():
    now = time.time()
    return {
        "config": state.config,
        "uptime_s": round(now - state.started, 1),
        "statuses": {str(k): v for k, v in state.statuses.items()},
        "kinds": dict(state.kinds),
        "last_minute": sum(1 for t in state.window if t >= now - 60),
        "in_flight": state.in_flight,
        "max_in_flight": state.max_in_flight,
    }


@app.post("/admin/reset")
async def reset():
    state.statuses.clear()
    state.kinds.clear()
    state.window.clear()
    state.max_in_flight = state.in_flight
    state.started = time.time()
    return {"ok": True}


@app.get("/health")
async def health():
    return {"status": "ok"}


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="假的 OpenAI 兼容模型服务（压测、演练用）")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
