"""Invalidated replay content must not revoke a committed human decision.

All records are synthetic. Real checkpoint decoding, stream completion,
SQLite tickets/outbox, memory deduplication and event projection are exercised.
"""
import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
from evidence_fixtures import with_evidence
from test_runs_api import _ask, _audit, _close, _frames, _queued

from app.agents.agent_orchestrator import AgentType, OrchestratorResult
from app.core.business_facts import verified_cells
from app.core.embedder import Embedder
from app.core.intent_recognizer import IntentCategory
from app.core.policy_scope import PolicyMetadata
from app.runs.checkpoint import encode_result
from app.runs.memo import RunScope, bind_run
from app.storage.vector_store import LocalStore
from app.tickets.notifier import TicketNotifier
from app.tools.knowledge_base import KnowledgeBase

OLD = "REVOKED-CONTROL-CONTENT"
SAFE = "此前依据已失效，请重新查询后核实。"


async def checkpoint_for(desk, monkeypatch, tmp_path, source, decision, *, invalid=True):
    """Start with valid source proof, then independently revoke it."""
    handoff = decision not in {"no", "unknown"}
    declared = True if decision == "yes" else False if decision in {"no", "quality_no"} else None
    direct = decision == "direct"
    final = OrchestratorResult(request_id="replay-control", response=OLD,
        agent_type=AgentType.HANDOFF if direct else AgentType.ORDER,
        primary_agent=AgentType.HANDOFF if direct else AgentType.ORDER,
        intent=IntentCategory.HUMAN_HANDOFF if direct else IntentCategory.REQUEST,
        handoff=handoff, escalated=handoff, answer_handoff_declared=declared,
        handoff_reason={"yes": "已决定交给员工处理", "direct": "用户明确要求人工",
                        "approval": "此操作需要员工审批", "risk": "真实冲突需要人工复核",
                        "quality_no": "回答质量未通过核验，需要员工处理"}.get(decision, ""),
        answer_confidence=.4 if decision in {"risk", "quality_no"} else .65 if handoff else 1.)
    meta = {"knowledge_used": False, "knowledge_sources": [], "knowledge": {}, "business_data": {}}
    store = None
    if source == "policy":
        store = LocalStore(str(tmp_path / "policy.db"))
        kb = KnowledgeBase(store=store, embedder=Embedder())
        monkeypatch.setattr(desk.main, "_knowledge_base", kb)
        policy = PolicyMetadata(status="published", version="control-v1", effective_at="2020-01-01T00:00:00Z")
        [doc] = await kb.import_documents([{"title": "control policy", "content": OLD,
                                            "policy": policy.model_dump(mode="json")}])
        refs = await kb.search_async(OLD)
        assert refs and await kb.validate_policy_refs(refs)
        meta.update(knowledge_used=True, knowledge_sources=refs, knowledge={"evidence_ok": True, "lookup_status": "found"})
        final.trajectory = [{"tool": "knowledge_search", "evidence": True, "trace": {"sources": copy.deepcopy(refs)}}]
        if invalid:
            before = await kb.get_document(doc["id"])
            await kb.set_document_policy(doc["id"], PolicyMetadata(status="retired", version="control-v2"), before["policy_revision"])
            assert not await kb.validate_policy_refs(refs)
    else:
        data = with_evidence({"queried_database": True, "has_risk": False, "degraded": False,
            "trace_id": "control-execution", "steps": [{"database": "erp_db", "success": True,
            "final_sql": "SELECT order_no,order_status FROM orders WHERE order_no='CONTROL-401'"}]},
            rows=[{"order_no": "CONTROL-401", "order_status": OLD}])
        cells, issues = verified_cells(data)
        assert cells and not issues
        meta["business_data"] = {"attempted": True, "outcome": "evidence", "evidence": cells, "evidence_payload": data}
        final.trajectory = [{"tool": "order_data_query", "evidence": True,
                             "trace": copy.deepcopy(meta["business_data"])}]
        if invalid:
            data["observed_at"] = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
            assert verified_cells(data)[1]
    return {"turn_id": "original-control-turn", "meta": meta, "shown": OLD,
            "routing": {"primary_agent": final.primary_agent.value}, "final": encode_result(final)}, store


async def replay(desk, checkpoint, *, attempt=2):
    with bind_run(RunScope("replay-control", attempt=attempt, instance="staff")):
        chunks = [f async for f in desk.main._resume_frames(_ask(desk.main), "c1", "u1", checkpoint)]
    await _audit(desk)
    return _frames(chunks)


@pytest.mark.parametrize("source", ["policy", "business"])
@pytest.mark.parametrize("decision", ["yes", "direct", "approval", "risk", "quality_no", "no", "unknown"])
async def test_invalid_content_is_rejected_without_changing_committed_handoff(monkeypatch, tmp_path, source, decision):
    desk = await _queued(monkeypatch, tmp_path, start_worker=False)
    store = None
    try:
        checkpoint, store = await checkpoint_for(desk, monkeypatch, tmp_path, source, decision)
        if decision == "unknown":
            checkpoint["final"].pop("answer_handoff_declared")  # Legacy checkpoints lack this field.
        original = copy.deepcopy(checkpoint)
        frames = await replay(desk, checkpoint)
        result = frames[-1][1]
        expected = original["final"]["handoff"]
        assert result["handoff"] is expected
        assert result["escalated"] is original["final"]["escalated"]
        assert result["handoff_reason"] == original["final"]["handoff_reason"]
        assert result["response"] == SAFE == "".join(d["text"] for k, d, _ in frames if k == "delta")
        assert OLD not in json.dumps(frames, ensure_ascii=False)
        meta = next(d for k, d, _ in frames if k == "meta")
        assert meta["knowledge_sources"] == [] and not meta["knowledge_used"]
        assert not meta["business_data"]["evidence"]
        assert result["trajectory"] == []
        assert len(await desk.tickets.list()) == int(expected)
        assert bool(result["ticket_id"]) is expected
        assert desk.memory.turns == [("补件到哪了", SAFE)]
        assert await _audit(desk) == [("user", "补件到哪了"), ("assistant", SAFE)]
        restored = await desk.main.get_turn(result["turn_id"])
        assert restored["result"]["handoff"] is expected and restored["result"]["response"] == SAFE
        assert not restored["meta"]["knowledge_sources"]
        assert not restored["meta"]["business_data"].get("evidence")
        assert OLD not in json.dumps(restored, ensure_ascii=False)
        assert checkpoint == original  # Recovery never rewrites the saved record.
        assert not desk.model.requests
    finally:
        if store:
            await store.close()
        await _close(desk)


@pytest.mark.parametrize("source", ["policy", "business"])
@pytest.mark.parametrize("preexisting", [False, True])
async def test_replay_retries_keep_one_handoff_ticket_notification_memory_and_audit(monkeypatch, tmp_path, source, preexisting):
    desk = await _queued(monkeypatch, tmp_path, start_worker=False)
    store = None
    try:
        checkpoint, store = await checkpoint_for(desk, monkeypatch, tmp_path, source, "yes")
        notifier = TicketNotifier(desk.tickets, "https://test.invalid/handoff")
        attempts = []

        async def no_network(delivery):
            attempts.append(delivery.id)
        monkeypatch.setattr(notifier, "_attempt", no_network)
        monkeypatch.setattr(desk.main, "_ticket_notifier", notifier)
        first_ticket = None
        if preexisting:
            from app.runs.checkpoint import decode_result

            with bind_run(RunScope("replay-control", attempt=1, instance="staff")):
                first_ticket = await desk.main._create_handoff_ticket(decode_result(checkpoint["final"]), "u1", "c1", "补件到哪了")
            await _audit(desk)
        desk.memory.write_failures = 1  # Fail after the durable handoff effect.
        from app.runs.worker import RetryRun
        with pytest.raises(RetryRun):
            await replay(desk, checkpoint)
        frames = await replay(desk, checkpoint, attempt=3)
        result = frames[-1][1]
        [ticket] = await desk.tickets.list()
        assert result["handoff"] and result["ticket_id"] == ticket.id
        if preexisting:
            assert ticket.id == first_ticket
        assert result["response"] == SAFE and OLD not in json.dumps(frames, ensure_ascii=False)
        assert (await desk.tickets.notify_stats())["pending"] == 1 and len(attempts) == 1
        assert desk.memory.turns == [("补件到哪了", SAFE)]
        assert await _audit(desk) == [("user", "补件到哪了"), ("assistant", SAFE)]
        assert not desk.model.requests
    finally:
        if store:
            await store.close()
        await _close(desk)


@pytest.mark.parametrize("source", ["policy", "business"])
@pytest.mark.parametrize("decision", ["yes", "no"])
async def test_valid_checkpoint_keeps_the_original_content_and_decision(monkeypatch, tmp_path, source, decision):
    desk = await _queued(monkeypatch, tmp_path, start_worker=False)
    store = None
    try:
        checkpoint, store = await checkpoint_for(desk, monkeypatch, tmp_path, source, decision, invalid=False)
        result = (await replay(desk, checkpoint))[-1][1]
        assert result["response"] == OLD and result["handoff"] is (decision == "yes")
        assert result["trajectory"] == checkpoint["final"]["trajectory"]
        assert not desk.model.requests
    finally:
        if store:
            await store.close()
        await _close(desk)
