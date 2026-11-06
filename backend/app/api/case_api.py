"""Versioned employee Case API, mounted on the existing ticket resource."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.core.actor import ActorContext, get_current_actor
from app.tickets.cases import CaseAdoption, CaseInput, CaseMutation, CaseService, DraftApproval

router = APIRouter(tags=["Tickets"])


def get_case_service(request: Request) -> CaseService:
    service = getattr(request.app.state, "case_service", None)
    if service is None or service.runs is None:
        raise HTTPException(503, {"code": "case_unavailable", "message": "跨消息工单需要 PostgreSQL、队列和明确的实例配置"})
    return service


def optional_actor(request: Request) -> ActorContext | None:
    return get_current_actor(request) if "actor" in request.scope else None


def require_actor(actor) -> ActorContext:
    if not isinstance(actor, ActorContext):
        raise HTTPException(401, {"code": "employee_identity_required", "message": "需要已验证的员工登录；共享服务密钥不能代替员工身份"})
    actor.require_employee()
    return actor


@router.get("/tickets/cases")
async def list_cases(conv_id: str | None = None, actor=Depends(get_current_actor), service=Depends(get_case_service)):
    return await service.list(actor, conv_id)


@router.post("/tickets/cases/inputs", status_code=202)
async def accept_case_input(body: CaseInput, actor=Depends(get_current_actor), service=Depends(get_case_service)):
    return await service.accept_input(actor, body)


@router.get("/tickets/{case_id}/case")
async def get_case(case_id: str, actor=Depends(get_current_actor), service=Depends(get_case_service)):
    return await service.get(actor, case_id)


@router.post("/tickets/{case_id}/case/adopt")
async def adopt_case(case_id: str, body: CaseAdoption, actor=Depends(get_current_actor), service=Depends(get_case_service)):
    return await service.adopt(actor, case_id, body)


@router.post("/tickets/{case_id}/case/actions")
async def mutate_case(case_id: str, body: CaseMutation, actor=Depends(get_current_actor), service=Depends(get_case_service)):
    return await service.mutate(actor, case_id, body)


@router.post("/tickets/{case_id}/case/drafts/{draft_id}/approve")
async def approve_draft(case_id: str, draft_id: str, body: DraftApproval,
                        actor=Depends(get_current_actor), service=Depends(get_case_service)):
    return await service.approve(actor, case_id, draft_id, body)


async def readable_case_run(service, actor, run_id):
    service.authorize(require_actor(actor))
    run = await service.run(run_id)
    if run is None:
        raise HTTPException(404, "Not Found")
    # Shared company read scope, restricted to this verified employee instance.
    await service.get(actor, run.case_context["case_id"])
    return run


@router.get("/tickets/cases/runs/{run_id}")
async def get_case_run(run_id: str, actor=Depends(get_current_actor), service=Depends(get_case_service)):
    return (await readable_case_run(service, actor, run_id)).public()


@router.get("/tickets/cases/runs/{run_id}/events")
async def case_run_events(run_id: str, after: str | None = None,
                          actor=Depends(get_current_actor), service=Depends(get_case_service)):
    if after and not re.fullmatch(r"\d{1,20}-\d{1,20}", after):
        raise HTTPException(422, "无效的事件位置")
    run = await readable_case_run(service, actor, run_id)
    return StreamingResponse(service.runs.events(run, after=after), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
