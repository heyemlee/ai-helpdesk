"""Replay the real verified-image handoff failure through Case commit fences."""
import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_attachments import connected as connected  # noqa: F401
from test_attachments import upload
from test_cases import ACTOR, action, claim, delivery, scalar, worker_for
from test_cases import pair as pair  # noqa: F401

from app.agents.agent_orchestrator import AgentType, Request
from app.core.intent_recognizer import IntentCategory, UrgencyLevel
from app.events import EventLog, EventLogConfig
from app.events.projection import project_turn
from app.runs.checkpoint import decode_result
from app.tickets.case_execution import CaseExecutor
from app.tickets.cases import CaseInput


def recording():
    return json.loads((Path(__file__).parent / "fixtures/stage6_image_handoff.json").read_text())


@pytest.fixture
async def event_log():
    log = await EventLog.open(EventLogConfig(mode="full", instance="staff"))
    yield log
    await log.close()


async def verified_continuation(connected):
    a, b, attachments, _ = connected
    recorded = recording()
    image = await upload(attachments)
    receipt = await a.accept_input(ACTOR, CaseInput(
        message_id="recorded-image", conv_id="conversation", new_case=True, issue_type="order",
        message=recorded["request_message"], attachment_ids=[image["id"]],
    ))
    run = await claim(a, receipt)
    review = recorded["image_review"]
    review.update(attachment_id=image["id"], sha256=run.case_context["snapshot"]["review_materials"][0]["sha256"])
    # Refresh only the observation clock and local attachment identity. Business
    # values, units, source row and the recorded verdict remain unchanged.
    for item in review["fields"]:
        item["source"]["observed_at"] = datetime.now(UTC).isoformat()
    payload, applied = await a.complete(run, {"material_reviews": [review], "response": "recorded image review"})
    assert applied
    return a, b, payload["next_run_id"], recorded


def executor_for(service, recorded, *, intent=IntentCategory.ORDER, urgency=UrgencyLevel.LOW,
                 intent_confidence=1., routing_confidence=1., failed_evidence=False,
                 primary_agent=AgentType.ORDER, intent_degraded=False, knowledge_degraded=False, event_log=None):
    original = recorded["original_result"]
    result = decode_result(original)
    # Replay actual intermediate-answer excerpts, not the final renderer body
    # that had already replaced them in the persisted result.
    result.response = "\n".join(v["sentence"] for v in original["policy_violations"])
    result.routing_confidence = routing_confidence
    result.primary_agent = primary_agent
    business = copy.deepcopy(original["business_data"])
    if failed_evidence:
        business = {"attempted": True, "outcome": "failed", "evidence_issues": ["failed"]}
    request = Request(message="图片核对后继续", user_id=ACTOR.actor_id, conv_id="conversation",
                      intent=intent, urgency=urgency, intent_confidence=intent_confidence,
                      intent_degraded=intent_degraded)
    knowledge = {**original["knowledge"], "degraded": knowledge_degraded}
    prepared = SimpleNamespace(request=request, intent=SimpleNamespace(intent_group="order"), knowledge_used=False,
                               evidence={"knowledge": knowledge, "business_data": business})
    return CaseExecutor(service, prepare=AsyncMock(return_value=prepared), answer=AsyncMock(return_value=result),
                        event_log=event_log or EventLog.disabled())


async def finish(service, executor, run_id):
    worker = worker_for(service, executor)
    await worker._handle(await delivery(worker, run_id), False)
    return await service.run(run_id)


async def test_recorded_verified_image_draft_does_not_inherit_discarded_answer_handoff(connected, event_log):
    a, _, run_id, recorded = await verified_continuation(connected)
    assert recorded["original_side_effects"]["handoff"] == 1
    assert recorded["original_score"]["invariant_violations"] == ["unauthorized_or_duplicate_handoff"]
    executor = executor_for(a, recorded, event_log=event_log)
    run = await finish(a, executor, run_id)
    assert run.status == "succeeded" and run.result["case_applied"]
    assert run.result["handoff"] is False
    assert run.result["handoff_review"]["outcome"] == "replaced_by_verified_draft"
    assert run.result["handoff_review"]["original_answer_confidence"] == .15
    assert run.result["answer_confidence"] == .15  # historical diagnostic, not a new draft score
    assert run.result["policy_violations"] == recorded["original_result"]["policy_violations"]
    events = [e.to_dict() for e in await event_log.store.events_of(run.turn_id)]
    restored = project_turn(events)["result"]
    for key in ("response", "handoff", "escalated", "handoff_reason", "handoff_review",
                "answer_confidence", "policy_violations"):
        assert restored[key] == run.result[key]
    assert run.result["handoff_review"]["original_handoff_reason"] not in json.dumps([e["data"] for e in events])
    redacted = project_turn([{**e, "body": None} for e in events])["result"]
    assert redacted["handoff"] is False and redacted["answer_confidence"] == .15
    assert redacted["handoff_review"]["original_handoff_reason"] == ""
    c = await a.get(ACTOR, run.result["case_id"])
    assert c["status"] == "in_progress" and c["control_mode"] == "assistant"
    assert c["drafts"][0]["status"] == "pending" and c["drafts"][0]["approved_by"] is None
    assert c["drafts"][0]["body"].count("图片已核实") == 5
    assert not c["pending_fields"]
    assert await scalar(a.db, "SELECT count(*) FROM case_actions WHERE kind='handoff'") == 0
    assert await scalar(a.db, "SELECT count(*) FROM ticket_notifications") == 0
    # Receipt replay never re-generates or publishes a second effect.
    replay, applied = await a.complete(run, run.checkpoint["case_proposal"])
    assert applied and replay == run.result
    assert executor.answer.await_count == 1


@pytest.mark.parametrize("options", [
    {"intent": IntentCategory.HUMAN_HANDOFF}, {"intent": IntentCategory.ESCALATION},
    {"urgency": UrgencyLevel.CRITICAL}, {"intent_confidence": .1},
    {"routing_confidence": .1}, {"failed_evidence": True},
    {"primary_agent": AgentType.HANDOFF}, {"intent_degraded": True}, {"knowledge_degraded": True},
])
async def test_direct_or_unresolved_risk_handoffs_remain_durable_and_idempotent(connected, options):
    a, _, run_id, recorded = await verified_continuation(connected)
    run = await finish(a, executor_for(a, recorded, **options), run_id)
    assert run.result["handoff"] is True
    assert await scalar(a.db, "SELECT count(*) FROM case_actions WHERE kind='handoff'") == 1
    assert await scalar(a.db, "SELECT count(*) FROM ticket_notifications") == 1
    await a.complete(run, run.checkpoint["case_proposal"])
    assert await scalar(a.db, "SELECT count(*) FROM case_actions WHERE kind='handoff'") == 1
    assert await scalar(a.db, "SELECT count(*) FROM ticket_notifications") == 1


@pytest.mark.parametrize("change", ["resume", "takeover", "stale_evidence", "mixed_evidence",
                                   "missing_provenance", "pending_image", "ordinary_followup",
                                   "draft_changed", "dead"])
async def test_checkpoint_replay_rechecks_final_draft_and_existing_fences(connected, change):
    a, b, run_id, recorded = await verified_continuation(connected)
    executor = executor_for(a, recorded)
    worker = worker_for(a, executor)
    run = await worker.store.claim(run_id, worker.owner, 60)
    complete = a.complete
    captured = {}

    async def checkpoint_only(run, proposal):
        captured.update(proposal)
        raise RuntimeError("simulated crash after proposal checkpoint")

    a.complete = checkpoint_only
    from app.runs.worker import RetryRun
    ctx = SimpleNamespace(run=run, scope=None, degrade=SimpleNamespace(shed=False),
                          set_turn=AsyncMock(), publish=AsyncMock(), commit=AsyncMock(return_value=True))
    with pytest.raises(RetryRun):
        await executor(ctx)
    a.complete = complete
    await worker.store.checkpoint(run_id, worker.owner, run.attempt, {"case_proposal": captured})
    if change == "takeover":
        c = await b.get(ACTOR, run.case_context["case_id"])
        await b.mutate(ACTOR, c["id"], action(c, "takeover"))
    elif change == "stale_evidence":
        for fact in run.case_context["snapshot"]["facts"].values():
            fact["business_source"]["observed_at"] = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    elif change == "mixed_evidence":
        fact = run.case_context["snapshot"]["facts"]["quantity"]
        fact["business_source"]["observed_at"] = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    elif change == "missing_provenance":
        run.case_context["snapshot"]["facts"]["quantity"].pop("evidence_id")
    elif change == "pending_image":
        run.case_context["snapshot"]["pending_fields"] = ["image:quantity"]
    elif change == "ordinary_followup":
        run.case_context["materials_reviewed"] = False
    elif change == "draft_changed":
        captured["response"] += "\n需要确认的额外内容"
    if change == "dead":
        payload = await executor.dead(run, "worker attempts exhausted")
    else:
        run.checkpoint = {"case_proposal": captured}
        ctx.run = run
        payload = (await executor(ctx)).result
    retained = {"draft_changed", "dead", "pending_image", "ordinary_followup"}
    if change in {"takeover", "stale_evidence", "mixed_evidence", "missing_provenance"}:
        assert payload["case_applied"] is False and payload["handoff"] is False
    else:
        assert payload["case_applied"] is True
        assert payload["handoff"] is (change in retained)
    effects = int(change in retained)
    assert await scalar(a.db, "SELECT count(*) FROM case_actions WHERE kind='handoff'") == effects
    assert await scalar(a.db, "SELECT count(*) FROM ticket_notifications") == effects
    assert executor.answer.await_count == 1
