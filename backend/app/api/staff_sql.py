"""Employee-authorized SQL gateway; downstream keys never reach the browser.

Business rows remain shared internal-single-tenant read scope. Conversation
memory and query history are owned here, without downstream impersonation.
"""
import hashlib
import json
import os
import re
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.staff_api import service
from app.core.actor import get_current_actor
from app.core.redaction import data as redact_data
from app.core.redaction import register_secret
from app.core.staff_auth import admin_write

router = APIRouter(prefix="/sql-api", tags=["Employee data"])
ADMIN_PATHS = {"admin/schema/status", "admin/schema/reload", "admin/circuit/reset", "admin/model",
               "admin/model/models", "admin/model/test", "admin/model/embedding", "admin/model/embedding/models",
               "admin/model/embedding/test", "feedback/stats"}


def route_kind(path, method):
    if path in ADMIN_PATHS:
        return "admin"
    if method == "GET" and (path in {"health", "readyz", "whoami", "history", "catalog"}
                            or re.fullmatch(r"catalog/[\w-]+/[\w-]+", path)
                            or re.fullmatch(r"runs/[\w-]+(?:/events)?", path)):
        return "read"
    if method == "POST" and (path in {"query", "a2a", "feedback"}
                             or re.fullmatch(r"catalog/[\w-]+/[\w-]+/(?:rows|profile)", path)
                             or re.fullmatch(r"runs/[\w-]+/cancel", path)):
        return "read"
    raise HTTPException(404, "Unknown data operation")


def service_key(admin):
    if admin:
        file = os.getenv("SQL_AGENT_ADMIN_API_KEY_FILE", "")
        value = Path(file).read_text().strip() if file else ""
    else:
        value = os.getenv("SQL_AGENT_API_KEY", "").strip()
    if not value:
        raise HTTPException(503, "数据服务凭据未配置")
    register_secret(value)
    return value


async def owned(auth, actor, run_id):
    owner = await auth.db.fetchval("SELECT actor_id FROM staff_sql_runs WHERE run_id=$1 AND instance=$2", run_id, actor.instance)
    if owner != actor.actor_id:
        raise HTTPException(404, "查询不存在或不属于当前员工")


async def record(auth, actor, run_id, trace_id="", history=None):
    if run_id:
        async with auth.db.transaction() as conn:
            live = await auth.provider(actor.actor_id, actor.instance, conn=conn)
            if live is None or live.auth_version != actor.auth_version:
                raise HTTPException(401, "员工授权已变化")
            await conn.execute("INSERT INTO staff_sql_runs(run_id,instance,actor_id,trace_id,history) VALUES($1,$2,$3,$4,$5::jsonb)"
                               " ON CONFLICT(run_id) DO UPDATE SET trace_id=CASE WHEN excluded.trace_id<>'' THEN excluded.trace_id ELSE staff_sql_runs.trace_id END, history=staff_sql_runs.history || excluded.history"
                               " WHERE staff_sql_runs.actor_id=excluded.actor_id",
                               run_id, actor.instance, actor.actor_id, trace_id, json.dumps(history or {}))


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy(path: str, request: Request, actor=Depends(get_current_actor), auth=Depends(service)):
    admin = route_kind(path, request.method) == "admin"
    if admin:
        actor.require_role("admin")
    match = re.fullmatch(r"runs/([\w-]+)(?:/events|/cancel)?", path)
    if match:
        await owned(auth, actor, match[1])
    if path == "history":
        try:
            limit = max(1, min(100, int(request.query_params.get("limit", 50))))
            offset = max(0, int(request.query_params.get("offset", 0)))
        except ValueError:
            raise HTTPException(422, "Invalid pagination") from None
        rows = await auth.db.fetch("SELECT run_id,trace_id,created_at,history FROM staff_sql_runs WHERE instance=$1 AND actor_id=$2 ORDER BY created_at DESC,run_id LIMIT $3 OFFSET $4", actor.instance, actor.actor_id, limit, offset)
        return {"user_id": actor.actor_id, "items": [{**json.loads(r["history"]), "run_id": r["run_id"], "trace_id": r["trace_id"], "created_at": r["created_at"].isoformat()} for r in rows]}
    body = None
    if request.method in {"POST", "PUT"}:
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 65536:
                raise HTTPException(413, "请求过大")
        try:
            body = json.loads(data) if data else {}
            if not isinstance(body, dict):
                raise ValueError()
        except ValueError:
            raise HTTPException(422, "请求格式无效") from None
    principal = os.getenv("SQL_AGENT_USER_ID", "ai-helpdesk")
    if body is not None and not admin:
        body.pop("user_id", None)
        if path == "query":
            body["user_id"] = principal
            body["enable_long_term"] = False
            body["session_id"] = hashlib.sha256((actor.actor_id + ":" + str(body.get("session_id", ""))).encode()).hexdigest()
        elif path == "a2a":
            if body.get("method") != "message/stream":
                raise HTTPException(403, "Only new query streams are supported")
            params_body = body.get("params")
            if not isinstance(params_body, dict):
                raise HTTPException(422, "Invalid query parameters")
            message = params_body.get("message", {})
            if not isinstance(message, dict) or message.get("taskId"):
                raise HTTPException(422, "Invalid new query")
            message["contextId"] = hashlib.sha256((actor.actor_id + ":" + str(message.get("contextId", ""))).encode()).hexdigest()
            message["metadata"] = {"user_id": principal, "enable_long_term": False}
            body["params"] = {"message": message}
        elif path == "feedback":
            exists = await auth.db.fetchval("SELECT 1 FROM staff_sql_runs WHERE instance=$1 AND actor_id=$2 AND trace_id=$3 AND trace_id<>''",
                                            actor.instance, actor.actor_id, body.get("trace_id", ""))
            if not exists:
                raise HTTPException(404, "反馈查询不属于当前员工")
    params = {k: v for k, v in request.query_params.items() if k in {"limit", "after", "refresh"}}
    key = service_key(admin)
    base = os.getenv("SQL_AGENT_URL", "http://sql-agent:8000").rstrip("/")
    client = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=10), follow_redirects=False,
                               transport=getattr(request.app.state, "sql_transport", None))
    handed_off = False
    try:
        req = client.build_request(request.method, base + "/" + path, params=params, json=body,
                                   headers={"X-API-Key": key, "Accept": request.headers.get("accept", "application/json")})
        if admin:
            # Serialize dispatch/completion with revocation. Once accepted by the
            # separate SQL service, its operation is an audited administrative job.
            async with admin_write() as conn:
                await auth.audit("sql_admin_dispatch", actor.actor_id, path, conn=conn)
                response = await client.send(req)
        else:
            live = await auth.provider(actor.actor_id, actor.instance)
            if live is None or live.auth_version != actor.auth_version:
                raise HTTPException(401, "员工授权已变化")
            response = await client.send(req, stream=path == "a2a")
        if response.status_code == 401:
            # The employee session was already authenticated. A downstream
            # service credential failure must not sign the employee out or erase
            # their pending material/recovery receipt in the console.
            await response.aclose()
            raise HTTPException(502, "数据服务凭据被拒绝，请联系管理员")
        initial = {"query": str((body or {}).get("query", "")), "session_id": (body or {}).get("session_id", ""),
                   "route": "", "databases": [], "tables": [], "sqls": [], "success": False, "degraded": False,
                   "has_risk": False, "execution_status": "", "failed_steps": 0, "repaired_steps": 0,
                   "status_code": response.status_code, "duration_ms": 0, "total_tokens": 0, "error": None}
        if path == "a2a" and response.status_code == 200 and "text/event-stream" in response.headers.get("content-type", ""):
            message = body["params"]["message"]
            initial["query"] = " ".join(p.get("text", "") for p in message.get("parts", []) if isinstance(p, dict) and p.get("kind") == "text")
            initial["session_id"] = message["contextId"]
            async def stream():
                run_id = ""
                try:
                    async for line in response.aiter_lines():
                        if line.startswith("data:"):
                            event = json.loads(line[5:]).get("result", {})
                            if event.get("kind") == "task":
                                run_id = event.get("id", "")
                                await record(auth, actor, run_id, history=initial)
                            trace = event.get("metadata", {}).get("trace_id", "")
                            if event.get("kind") == "artifact-update":
                                parts = event.get("artifact", {}).get("parts", [])
                                result = next((p.get("data", {}) for p in parts if p.get("kind") == "data"), {})
                                await record(auth, actor, run_id, result.get("trace_id", ""), history=history_result(result))
                            if trace or event.get("final"):
                                await record(auth, actor, run_id, trace, {"success": event.get("status", {}).get("state") == "completed"})
                            line = "data: " + json.dumps(redact_data(json.loads(line[5:])), ensure_ascii=False)
                        yield line + "\n"
                finally:
                    await response.aclose()
                    await client.aclose()
            handed_off = True
            return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})
        await response.aread()
        try:
            payload = response.json()
        except ValueError:
            raise HTTPException(502, "数据服务响应无效") from None
        if response.is_success and path == "query":
            await record(auth, actor, response.headers.get("X-Run-Id", ""), payload.get("trace_id", ""), {**initial, **history_result(payload)})
        if response.is_success and path == "whoami":
            payload["identity"] = {"user_id": actor.actor_id, "caller": "employee-console", "is_admin": "admin" in actor.roles,
                                   "can_impersonate": False, "anonymous": False}
        return JSONResponse(redact_data(payload) if response.is_success else {"detail": "数据服务请求失败"}, status_code=response.status_code,
                            headers={k: v for k, v in response.headers.items() if k.lower() in {"x-run-id", "x-trace-id"}})
    except httpx.HTTPError:
        raise HTTPException(502, "数据服务暂时不可用") from None
    finally:
        if not handed_off:
            await client.aclose()


def history_result(result):
    return {"route": result.get("route", ""), "databases": result.get("selected_databases", []),
            "success": result.get("execution_status") in {"success", "no_records"},
            **{key: result[key] for key in ("degraded", "has_risk", "execution_status", "failed_steps", "repaired_steps") if key in result}}
