"""Employee session and administrative account lifecycle; no public signup."""
import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.core.actor import get_current_actor
from app.core.staff_auth import csrf

router = APIRouter(tags=["Employee identity"])


def service(request: Request):
    auth = getattr(request.app.state, "staff_auth", None)
    if auth is None:
        raise HTTPException(503, "员工登录未配置")
    return auth


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    login: str = Field(min_length=3, max_length=80)
    password: SecretStr = Field(min_length=1, max_length=128)


@router.post("/auth/login")
async def login(request: Request, auth=Depends(service)):
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise HTTPException(415, "使用 JSON 登录")
    data = bytearray()
    try:
        async with asyncio.timeout(10):
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > 4096:
                    raise HTTPException(413, "登录请求过大")
    except TimeoutError:
        raise HTTPException(408, "登录请求超时") from None
    try:
        body = Login.model_validate(json.loads(data))
    except Exception:
        raise HTTPException(422, "登录请求格式无效") from None
    token = await auth.login(body.login.lower(), body.password.get_secret_value(), request.client.host if request.client else "unknown")
    actor = await auth.session(token)
    response = JSONResponse({"actor": {**actor.snapshot(), "roles": sorted(actor.roles)}, "csrf_token": csrf(token)})
    response.set_cookie(auth.cookie, token, max_age=auth.ttl_s, httponly=True, secure=auth.secure, samesite="strict", path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/auth/session")
async def session(request: Request, actor=Depends(get_current_actor), auth=Depends(service)):
    return {"actor": {**actor.snapshot(), "roles": sorted(actor.roles)},
            "csrf_token": csrf(request.cookies[auth.cookie])}


@router.post("/auth/logout")
async def logout(request: Request, actor=Depends(get_current_actor), auth=Depends(service)):
    await auth.logout(actor)
    request.scope["logout_completed"] = True
    response = JSONResponse({"logged_out": True})
    response.delete_cookie(auth.cookie, path="/", secure=auth.secure, httponly=True, samesite="strict")
    return response


@router.post("/auth/conversations", status_code=201)
async def new_conversation(actor=Depends(get_current_actor), auth=Depends(service)):
    return {"conv_id": await auth.new_conversation(actor)}


class AccountInput(Login):
    role: str


class AccountChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool | None = None
    role: str | None = None
    password: SecretStr | None = Field(default=None, min_length=1, max_length=128)


@router.get("/admin/employees")
async def accounts(actor=Depends(get_current_actor), auth=Depends(service)):
    actor.require_role("admin")
    return {"employees": [dict(r) for r in await auth.db.fetch(
        "SELECT actor_id,login,role,active,auth_version FROM staff_accounts WHERE instance=$1 ORDER BY login", actor.instance)]}


@router.post("/admin/employees", status_code=201)
async def create(body: AccountInput, actor=Depends(get_current_actor), auth=Depends(service)):
    actor.require_role("admin")
    return await auth.create_account(body.login.lower(), body.password.get_secret_value(), body.role, by=actor)


@router.patch("/admin/employees/{actor_id}")
async def change(actor_id: str, body: AccountChange, actor=Depends(get_current_actor), auth=Depends(service)):
    actor.require_role("admin")
    await auth.change_account(actor_id, by=actor, active=body.active, role=body.role,
                              password=body.password.get_secret_value() if body.password is not None else None)
    return {"updated": True, "sessions_revoked": True}


class Adoption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor_id: str
    reason: str = Field(min_length=10, max_length=500)


@router.post("/admin/conversations/{conv_id}/adopt")
async def adopt(conv_id: str, body: Adoption, actor=Depends(get_current_actor), auth=Depends(service)):
    actor.require_role("admin")
    async with auth.db.transaction() as conn:
        await auth.administrator(actor, conn)
        owner = await auth.provider(body.actor_id, actor.instance, conn=conn)
        if not owner:
            raise HTTPException(404, "员工不存在")
        # Explicit historical adoption, not transfer of an already owned chat.
        exists = await conn.fetchval("SELECT 1 FROM conversation_log l LEFT JOIN conversation_log_turns t USING(seq)"
                                    " WHERE l.conv_id=$1 AND COALESCE(t.instance,'') IN ('',$2) LIMIT 1", conv_id, actor.instance)
        if not exists:
            raise HTTPException(404, "历史会话不存在")
        row = await conn.fetchval("INSERT INTO staff_conversations(instance,conv_id,actor_id) VALUES($1,$2,$3)"
                                 " ON CONFLICT DO NOTHING RETURNING conv_id", actor.instance, conv_id, body.actor_id)
        if not row:
            raise HTTPException(409, "会话已有归属")
        # Reason is an explicit operator statement; never used as an identity.
        await auth.audit("legacy_conversation_adopted", actor.actor_id, conv_id + ":" + body.reason, conn=conn)
    return {"adopted": True}
