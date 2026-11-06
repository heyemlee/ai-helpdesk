"""Case worker: proposals may be checkpointed; only the transaction publishes them."""
from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable

from app.core import admission
from app.events import bind
from app.runs.checkpoint import encode_result
from app.runs.memo import bind_run
from app.runs.records import SUCCEEDED
from app.runs.worker import RetryRun, RunContext, RunOutcome

FIELD_LABELS = {
    "order_ref": "订单号", "model": "缺件型号", "quantity": "缺件数量",
    "expected_model": "应收型号", "received_model": "实收型号",
}


class CaseExecutor:
    def __init__(self, service, *, prepare: Callable[..., Awaitable], answer: Callable[..., Awaitable], event_log):
        self.service, self.prepare, self.answer, self.event_log = service, prepare, answer, event_log

    async def __call__(self, ctx: RunContext) -> RunOutcome:
        run = ctx.run
        turn = self.event_log.open_turn(conv_id=run.conv_id, request_id=run.request_id)
        try:
            with bind_run(ctx.scope), bind(turn), (admission.shed() if ctx.degrade.shed else contextlib.nullcontext()):
                await ctx.set_turn(turn.turn_id)
                turn.emit("turn_started", {"transport": "case", "run": {"run_id": run.run_id, "attempt": run.attempt},
                                          "case_id": run.case_context["case_id"]}, {"message": run.message})
                snapshot = run.case_context["snapshot"]
                if run.checkpoint:
                    proposal = run.checkpoint["case_proposal"]
                else:
                    proposal = {"conv_id": run.conv_id, "request_id": run.request_id,
                                "turn_id": turn.turn_id, "intent": "other", "agent_type": "system",
                                "escalated": False, "latency_ms": 0.0, "handoff": False}
                    if snapshot["control_mode"] == "human":
                        proposal["response"] = "此问题已由人工接管，材料已保存。"
                    elif snapshot.get("pending_fields"):
                        fields = "、".join(FIELD_LABELS.get(f, f) for f in snapshot["pending_fields"])
                        proposal.update(response=f"材料已保存，请补充：{fields}。", needs_clarification=True)
                    else:
                        await ctx.publish("progress", {"stage": "helpdesk.gathering", "label": "正在核对工单材料"})
                        prepared = await self.prepare(run, snapshot, ctx.degrade)
                        result = await self.answer(prepared.request)
                        evidence = dict(prepared.evidence.get("knowledge") or {})
                        proposal.update(encode_result(result), intent_group=prepared.intent.intent_group,
                                        knowledge_used=prepared.knowledge_used,
                                        knowledge_sources=evidence.pop("sources", []), knowledge=evidence,
                                        business_data=prepared.evidence.get("business_data") or {})
                        proposal["intent"] = proposal.get("intent") or "other"
                    if not await ctx.commit({"case_proposal": proposal}):
                        raise RetryRun("Case proposal lost its lease")
                # No model delta, ticket, notification or history has been published yet.
                payload, applied = await self.service.complete(run, proposal)
                turn.emit("turn_completed", {"case_applied": applied, "case_id": payload["case_id"],
                                             "case_version": payload["case_version"]}, {"response": payload["response"]})
                return RunOutcome(SUCCEEDED, ("result", payload), result=payload, persisted=True)
        except Exception as exc:
            turn.emit("turn_failed", {"error_type": type(exc).__name__})
            # Input and checkpoint survive. Retrying has the same Case fences.
            raise RetryRun(f"Case execution failed: {type(exc).__name__}") from exc
        finally:
            turn.close()
            await self.event_log.flush(timeout_s=1.0)

    async def dead(self, run, reason):
        payload, _ = await self.service.complete(run, {
            "conv_id": run.conv_id, "intent": "other", "agent_type": "system", "escalated": True,
            "latency_ms": 0.0, "response": "自动处理未完成，已记录人工处理请求。",
            "handoff": True, "handoff_reason": reason,
        }, dead=True)
        return payload
