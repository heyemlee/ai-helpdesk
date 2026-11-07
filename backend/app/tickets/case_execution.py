"""Case worker: proposals may be checkpointed; only the transaction publishes them."""
from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable

from fastapi import HTTPException

from app.core import admission
from app.core.business_facts import collect_evidence, render_draft
from app.core.policy_scope import PolicyScope, bind_policy_scope
from app.events import bind
from app.runs.checkpoint import encode_result
from app.runs.memo import bind_run
from app.runs.records import SUCCEEDED
from app.runs.worker import RetryRun, RunContext, RunOutcome
from app.tickets.draft_decision import handoff_review

logger = logging.getLogger(__name__)

FIELD_LABELS = {
    "order_ref": "订单号", "model": "缺件型号", "quantity": "缺件数量",
    "expected_model": "应收型号", "received_model": "实收型号",
    "status": "业务状态", "date": "日期", "readability": "清晰图片", "review": "完成图片核对",
}


class CaseExecutor:
    def __init__(self, service, *, prepare: Callable[..., Awaitable], answer: Callable[..., Awaitable], event_log,
                 materials=None, complete_facts: Callable[..., Awaitable] | None = None):
        self.service, self.prepare, self.answer, self.event_log = service, prepare, answer, event_log
        self.materials = materials
        self.complete_facts = complete_facts

    async def __call__(self, ctx: RunContext) -> RunOutcome:
        run = ctx.run
        if self.service.actors:
            try:
                actor = await self.service.actors.from_snapshot(run.case_context)
                actor.require_policy(run.case_context["snapshot"].get("policy_scope"))
            except HTTPException:
                payload, _ = await self.service.complete(run, {"response": "员工授权已失效", "actor_access_changed": True})
                return RunOutcome(SUCCEEDED, ("result", payload), result=payload, persisted=True)
        turn = self.event_log.open_turn(conv_id=run.conv_id, request_id=run.request_id)
        phase = "prepare"
        try:
            with bind_policy_scope(PolicyScope.model_validate(run.case_context["snapshot"].get("policy_scope") or {})), bind_run(ctx.scope), bind(turn), (admission.shed() if ctx.degrade.shed else contextlib.nullcontext()):
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
                    elif self.materials and snapshot.get("review_materials") and not run.case_context.get("materials_reviewed"):
                        await ctx.publish("progress", {"stage": "helpdesk.images", "label": "正在读取图片并核对业务记录"})
                        try:
                            proposal["material_reviews"] = await self.materials.review(run)
                            proposal["response"] = "图片核对已完成。"
                        except HTTPException:
                            proposal.update(material_access_changed=True, response="材料访问权限已变化。")
                    elif snapshot.get("pending_fields"):
                        fields = "、".join(("图片核实：" if f.startswith("image:") else "") +
                                          FIELD_LABELS.get(f.removeprefix("image:"), f.removeprefix("image:"))
                                          for f in snapshot["pending_fields"])
                        proposal.update(response=f"材料已保存，请补充：{fields}。", needs_clarification=True)
                    else:
                        await ctx.publish("progress", {"stage": "helpdesk.gathering", "label": "正在核对工单材料"})
                        from app.core.fact_completion import bind_task, make_task
                        task = make_task(run.message, run.case_context, run.run_id, run.attempt) if self.complete_facts else None
                        with bind_task(task):
                            prepared = await self.prepare(run, snapshot, ctx.degrade)
                        facts = await self.complete_facts(prepared.request) if self.complete_facts else None
                        if facts:
                            proposal.update(facts, intent_group=prepared.intent.intent_group,
                                            intent=prepared.intent.intent.value)
                            turn.emit("fact_proposed", {"task_id": facts["fact_receipt"]["task"]["id"], "delivery_task_id": task["id"],
                                                       "receipt_id": facts["fact_receipt"]["id"],
                                                       "answer_origin": facts["answer_origin"], "answer_model_invoked": False,
                                                       "primary": facts.get("primary_agent", ""), "supporting": [],
                                                       "reason": facts.get("routing_reason", ""),
                                                       "confidence": facts.get("routing_confidence", 0.0)},
                                      {"fact_receipt": facts["fact_receipt"]})
                        else:
                            result = await self.answer(prepared.request)
                            evidence = dict(prepared.evidence.get("knowledge") or {})
                            proposal.update(encode_result(result), intent_group=prepared.intent.intent_group,
                                        knowledge_used=prepared.knowledge_used,
                                        knowledge_sources=evidence.pop("sources", []), knowledge=evidence,
                                        business_data=prepared.evidence.get("business_data") or {})
                            proposal["intent"] = proposal.get("intent") or "other"
                            goal = getattr(prepared.request, "lookup_goal", None)
                            proposal["lookup_goal"] = goal
                            selected = collect_evidence(prepared.evidence, proposal.get("trajectory", []), lookup_goal=goal)
                            proposal["knowledge_sources"] = selected["knowledge"]["sources"]
                            proposal["business_data"] = selected["business_data"]
                            proposal["response"] = render_draft(proposal["knowledge_sources"], proposal["business_data"], snapshot.get("facts"), language=snapshot.get("language", "zh"), knowledge_state=proposal.get("knowledge"), lookup_goal=goal)
                            if result.handoff:
                                proposal["handoff_review"] = handoff_review(prepared.request, result, proposal["response"])
                    phase = "checkpoint"
                    checkpoint = {"case_proposal": proposal}
                    from app.core.fact_completion import ORIGIN
                    if proposal.get("answer_origin") == ORIGIN:
                        checkpoint["case_proposal_origin"] = ORIGIN
                    if not await ctx.commit(checkpoint):
                        raise RetryRun("Case proposal lost its lease")
                # No model delta, ticket, notification or history has been published yet.
                phase = "complete"
                payload, applied = await self.service.complete(run, proposal)
                review = dict(payload.get("handoff_review") or {})
                original_reason = review.pop("original_handoff_reason", "")
                turn.emit("turn_completed", {"case_applied": applied, "case_id": payload["case_id"],
                                             "case_version": payload["case_version"],
                                             **{k: payload[k] for k in ("agent_type", "agent_types", "handoff", "escalated",
                                                "needs_clarification", "answer_confidence", "service_unavailable",
                                                "unavailable_reason", "latency_ms", "answer_origin", "answer_model_invoked") if k in payload},
                                             "primary": payload.get("primary_agent", ""),
                                             "supporting": payload.get("supporting_agents", []),
                                             **({"handoff_review": review} if review else {})},
                          {"response": payload["response"], "handoff_reason": payload.get("handoff_reason", ""),
                           "original_handoff_reason": original_reason,
                           "policy_violations": payload.get("policy_violations", [])})
                return RunOutcome(SUCCEEDED, ("result", payload), result=payload, persisted=True)
        except Exception as exc:
            details = {"http_status": exc.status_code} if isinstance(exc, HTTPException) else {}
            body = ({"error_detail": exc.detail,
                     "error_code": exc.detail.get("code", "") if isinstance(exc.detail, dict) else ""}
                    if isinstance(exc, HTTPException) else None)
            turn.emit("turn_failed", {"error_type": type(exc).__name__, "phase": phase, **details}, body)
            if isinstance(exc, HTTPException):
                # Keep the throw site, but exception prose belongs to the
                # redacted event body rather than the structural/log message.
                diagnostic = HTTPException(exc.status_code, "Details retained in turn event body")
                logger.error("Case run %s failed during %s", run.run_id, phase,
                             exc_info=(type(exc), diagnostic, exc.__traceback__))
            else:
                logger.exception("Case run %s failed during %s", run.run_id, phase)
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
