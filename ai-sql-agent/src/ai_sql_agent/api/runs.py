"""
运行记录接口：查一次 run 的状态和事件日志，取消一次正在跑的 run。

身份规则与 `/history` 一致：只能看自己的 run（同一个 caller、同一个 user_id），管理员例外；
鉴权关闭的本地开发里匿名身份即管理员。**不是本人的一律 404**——不区分"不存在"和"没权限"，
免得有人拿它枚举别人的 run（事件里有问题原文和 SQL）。

这是服务端自己的 HTTP 面，不在 a2a-contract 里；A2A 的 `tasks/*` 仍按契约 §7 返回 -32004。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from ai_sql_agent.auth import ApiKeyRecord
from ai_sql_agent.core import get_logger, log_event
from ai_sql_agent.runs import RunManager, runs_of

from .security import require_api_key

_logger = get_logger("api.runs")
router = APIRouter()

_NOT_FOUND = "没有这次运行记录。"


def _manager(request: Request) -> RunManager:
    runs = runs_of(request.app)
    if runs is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return runs


def _authorize(request: Request, runs: RunManager, run_id: str,
               api_key: Optional[ApiKeyRecord], user_id: str):
    principal = request.app.state.auth.resolve_principal(api_key, user_id)
    owner = runs.owner_of(run_id)
    if owner is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    if not principal.is_admin and owner != (principal.caller, principal.user_id):
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return principal


@router.get("/runs/{run_id}")
def get_run(
    run_id: str,
    request: Request,
    user_id: str = "",
    api_key: Optional[ApiKeyRecord] = Depends(require_api_key),
) -> Dict[str, Any]:
    """一次 run 的状态、入口、起止时间、用量和业务结论。"""
    runs = _manager(request)
    _authorize(request, runs, run_id, api_key, user_id)
    stored = runs.get(run_id)
    if stored is None:
        active = runs.active(run_id)
        if active is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        # 关掉持久化时：只有内存里的状态
        return {"run_id": run_id, "trace_id": active.trace_id, "entry": active.entry,
                "status": active.status, "persisted": False}
    stored.pop("owner", None)
    return {**stored, "persisted": True}


@router.get("/runs/{run_id}/events")
def get_run_events(
    run_id: str,
    request: Request,
    after: int = 0,
    limit: int = 500,
    user_id: str = "",
    api_key: Optional[ApiKeyRecord] = Depends(require_api_key),
) -> Dict[str, Any]:
    """按序号返回事件；`after` 是上一页最后一条的 seq，`limit` 最多 1000。"""
    runs = _manager(request)
    _authorize(request, runs, run_id, api_key, user_id)
    limit = max(1, min(int(limit), 1000))
    events = runs.events(run_id, after_seq=max(0, int(after)), limit=limit)
    return {"run_id": run_id, "events": events,
            "next_after": events[-1]["seq"] if events else max(0, int(after))}


@router.post("/runs/{run_id}/cancel", status_code=202)
def cancel_run(
    run_id: str,
    request: Request,
    user_id: str = "",
    api_key: Optional[ApiKeyRecord] = Depends(require_api_key),
) -> Dict[str, Any]:
    """
    取消一次正在跑的 run。生效在下一个检查点：SQL 立即中断，正在进行的那一次模型调用
    要等它返回（≤ 模型超时），结果丢弃，不再发新的调用。审计照写，配额照计。
    """
    runs = _manager(request)
    principal = _authorize(request, runs, run_id, api_key, user_id)
    outcome = runs.cancel(run_id, "api")
    log_event(_logger, logging.INFO, "runs.cancel_requested", run_id=run_id,
              caller=principal.caller, user_id=principal.user_id, outcome=outcome)
    if outcome == "already_finished":
        raise HTTPException(status_code=409, detail="这次运行已经结束。")
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return {"run_id": run_id, "status": outcome}
