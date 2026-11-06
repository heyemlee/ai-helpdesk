"""Isolated browser fixture server. NEVER imported by production.

Uses the real API, migrations, worker, Redis queue, private objects, Responses
adapter and SQLAgentClient. Only identity and external model/ERP replies are
deterministic fixtures. It must run from the test runner's env-empty copy.
"""
import asyncio
import base64
import io
import json
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS

from fastapi import Request
from fastapi.responses import JSONResponse
from PIL import Image
from redis.asyncio import Redis

from app.agents.agent_orchestrator import AgentType, OrchestratorResult
from app.api import main
from app.core.actor import ActorContext
from app.core.llm_utils import create_llm_client
from app.runs.config import RunConfig
from app.storage.pg import Database
from app.tools.sql_agent_client import SQLAgentClient

assert os.environ.get("ATTACHMENT_BROWSER_FIXTURE") == "1"
assert os.environ["TEST_POSTGRES_DSN"].rsplit(":", 1)[-1].split("/")[0] != "5433"
actor = ActorContext("browser-employee", frozenset({"employee", "admin"}), "staff")
state = {"active": True, "fail_upload": False, "model_calls": 0, "queries": [], "waiting": ""}
holds = {"model": asyncio.Event(), "answer": asyncio.Event()}
for hold in holds.values():
    hold.set()
app = main.app


async def lookup(actor_id, instance, *, conn=None):
    return actor if state["active"] and actor_id == actor.actor_id and instance == actor.instance else None


@app.middleware("http")
async def test_identity(request, call_next):
    if request.cookies.get("test_employee") == "yes" and state["active"]:
        request.scope["actor"] = actor
    return await call_next(request)


@app.post("/test/login")
async def login():
    state["active"] = True
    response = JSONResponse({"ok": True})
    response.set_cookie("test_employee", "yes", httponly=True, samesite="strict")
    return response


@app.post("/test/revoke")
async def revoke():
    state["active"] = False
    return {"ok": True}


@app.post("/test/fail-upload")
async def fail_upload():
    state["fail_upload"] = True
    return {"ok": True}


@app.get("/test/state")
async def inspection():
    return {**state, "cases": (await main._cases.list(actor))["tickets"]}


@app.post("/test/hold/{stage}")
async def hold_stage(stage: str, release: bool = False):
    if release:
        holds[stage].set()
    else:
        holds[stage].clear()
    return {"ok": True}


@app.post("/test/model/v1/responses")
async def model(request: Request):
    body = await request.json()
    assert body["model"] == "gpt-6.1-sol" and body["text"]["format"]["strict"]
    blocks = [b for item in body["input"] for b in item.get("content", []) if b["type"] == "input_image"]
    assert len(blocks) == 1 and not body.get("tools")
    image = Image.open(io.BytesIO(base64.b64decode(blocks[0]["image_url"].split(",", 1)[1])))
    conflict = image.getpixel((0, 0))[1] < 100
    fields = [{"field": k, "value": v, "unit": "pcs" if k == "quantity" else None,
               "kind": "missing" if k == "quantity" else "unknown", "readability": "clear", "location": "fixture"}
              for k, v in {"order_ref": "SO-1", "model": "A", "quantity": "9" if conflict else "2"}.items()]
    state["model_calls"] += 1
    if not holds["model"].is_set():
        state["waiting"] = "model"
        await holds["model"].wait()
        state["waiting"] = ""
    return {"id": "resp_fixture", "created_at": 1, "object": "response", "status": "completed", "model": "gpt-6.1-sol",
            "output": [{"id": "msg_fixture", "type": "message", "status": "completed", "role": "assistant",
                        "content": [{"type": "output_text", "text": json.dumps({"document_type": "parts", "fields": fields}), "annotations": []}]}],
            "usage": {"input_tokens": 120, "output_tokens": 80, "total_tokens": 200}}


@app.post("/test/sql/query")
async def query(request: Request):
    body = await request.json()
    assert body["user_id"] == "ai-helpdesk" and "SO-1" in body["query"]
    assert "赔偿" not in body["query"] or "不要执行" in body["query"]
    state["queries"].append({"session_id": body["session_id"], "user_id": body["user_id"]})
    return {"answer": "UNTRUSTED ANSWER: quantity 999", "route": "single", "queried_database": True,
            "execution_status": "success", "has_risk": False, "degraded": False, "result_truncated": False,
            "trace_id": "browser-sql-trace", "selected_databases": ["erp_db"],
            "steps": [{"database": "erp_db", "success": True, "truncated": False, "error": None,
                       "rows": [{"order_ref": "SO-1", "model": "A", "missing_qty": 2}], "row_count": 1,
                       "column_units": {"missing_qty": "pcs"}, "as_of": "2026-11-05T10:00:00Z",
                       "as_of_source": "query_time", "source_records": [{"line_id": "line-1"}]}]}


@asynccontextmanager
async def lifespan(_):
    db = Database(os.environ["TEST_POSTGRES_DSN"])
    await db.connect(embedding_dim=128)
    redis = Redis.from_url(os.environ["TEST_REDIS_URL"])
    base = os.environ["BROWSER_API_URL"]
    main._pg_database, main._infra_redis, main._INSTANCE = db, redis, "staff"
    main._RUN_CONFIG = RunConfig(mode="queued", lease_s=2, redispatch_after_s=0.1, wait_s=15)
    main._sql_agent = SQLAgentClient(base_url=base + "/test/sql", api_key="fixture")
    client = create_llm_client("fixture", base + "/test/model/v1", provider="openai", api_mode="responses")
    main._model_runtime = NS(register_llm=lambda component: component.reconfigure_llm(client, "gpt-6.1-sol"))

    async def call(name, params):
        assert name == "order_data_query"
        return NS(success=True, data=await main._sql_agent.handler(params))

    async def answer(req):
        if not holds["answer"].is_set():
            state["waiting"] = "answer"
            await holds["answer"].wait()
            state["waiting"] = ""
        return OrchestratorResult(request_id="fixture", response="已按业务记录核实，请员工确认内部草稿。",
                                  agent_type=AgentType.GENERAL, intent=None)

    async def prepare(*args, **kwargs):
        return NS(request={}, evidence={}, intent=NS(intent_group="order"), knowledge_used=False)

    main._tool_manager = NS(call=call)
    main._orchestrator = NS(run=answer, get_stats=lambda: {}, routing_config=lambda: None)
    main._prepare_request = prepare
    app.state.actor_directory.provider = lookup
    await main._start_cases()
    objects = app.state.attachments.objects
    put = objects.put

    async def flaky(*args):
        if state["fail_upload"]:
            state["fail_upload"] = False
            raise OSError("fixture storage interruption")
        return await put(*args)

    objects.put = flaky
    yield
    await main._case_worker.drain(5)
    await main._case_event_log.close()
    await redis.aclose()
    await db.close()


app.router.lifespan_context = lifespan
