"""Case invariants on disposable PostgreSQL, using independent connection pools."""
import asyncio
import hashlib
import os
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.core.actor import ActorContext
from app.runs.store import PgRunStore
from app.tickets.cases import CaseAdoption, CaseInput, CaseMutation, CaseService, DraftApproval

PG = os.getenv("TEST_POSTGRES_DSN", "")
pytestmark = pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
ACTOR = ActorContext("employee-1", frozenset({"employee"}), "staff")
ADMIN = ActorContext("admin-1", frozenset({"admin"}), "staff")
ANSWER = {"response": "请核对草稿。", "handoff": False}


@pytest.fixture
async def pair():
    import asyncpg

    from app.storage.pg import Database

    name = "cases_" + uuid.uuid4().hex[:10]
    admin = await asyncpg.connect(PG)
    await admin.execute(f'CREATE DATABASE "{name}"')
    dsn = PG.rsplit("/", 1)[0] + "/" + name
    a, b = Database(dsn, max_size=3), Database(dsn, max_size=3)
    try:
        await asyncio.gather(a.connect(embedding_dim=128), b.connect(embedding_dim=128))
        yield CaseService(a, "staff", webhook_url="https://test.invalid/handoff"), CaseService(b, "staff")
    finally:
        await asyncio.gather(a.close(), b.close())
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def scalar(db, sql, *args):
    return (await db.fetchrow(sql, *args))[0]


def new(**kwargs):
    return CaseInput(message_id=uuid.uuid4().hex, conv_id="conversation", message="有一个问题", new_case=True, **kwargs)


def follow(c, **kwargs):
    return CaseInput(message_id=uuid.uuid4().hex, conv_id=c["conv_id"], case_id=c["id"],
                     expected_version=c["version"], expected_control_version=c["control_version"], **kwargs)


def action(c, operation, **kwargs):
    return CaseMutation(expected_version=c["version"], expected_control_version=c["control_version"], operation=operation, **kwargs)


async def claim(svc, receipt, owner="worker"):
    return await PgRunStore(svc.db, svc.run_instance).claim(receipt["run_id"], owner, 60)


async def test_duplicate_input_is_one_case_message_run_and_survives_run_purge(pair):
    a, b = pair
    body = new(order_refs=["SO-1"])
    receipts = await asyncio.gather(a.accept_input(ACTOR, body), b.accept_input(ACTOR, body))
    assert len({r["case_id"] for r in receipts}) == len({r["run_id"] for r in receipts}) == 1
    assert sorted(r["deduplicated"] for r in receipts) == [False, True]
    c = await a.get(ACTOR, receipts[0]["case_id"])
    assert c["version"] == 1 and len(c["messages"]) == len(c["runs"]) == 1
    await a.db.execute("DELETE FROM agent_runs")
    again = await b.accept_input(ACTOR, body)
    assert again["run_id"] == receipts[0]["run_id"] and again["deduplicated"]
    with pytest.raises(HTTPException) as exc:
        await a.accept_input(ACTOR, body.model_copy(update={"message": "不同内容"}))
    assert exc.value.status_code == 409


async def test_two_problems_in_one_conversation_do_not_merge_on_order(pair):
    a, _ = pair
    one = await a.accept_input(ACTOR, new(issue_type="missing_parts", order_refs=["SO-1"]))
    two = await a.accept_input(ACTOR, new(issue_type="wrong_item", order_refs=["SO-1"]))
    assert one["case_id"] != two["case_id"]
    assert len((await a.list(ACTOR, "conversation"))["tickets"]) == 2
    with pytest.raises(ValueError):
        CaseInput(message_id="ambiguous", conv_id="conversation", message="刚才那个订单")


@pytest.mark.parametrize("issue,required", [
    ("order", {"order_ref"}), ("missing_parts", {"order_ref", "model", "quantity"}),
    ("wrong_item", {"order_ref", "expected_model", "received_model"}),
])
async def test_required_materials_wait_then_resume_with_new_run(pair, issue, required):
    a, _ = pair
    first = await a.accept_input(ACTOR, new(issue_type=issue, pending_fields=[]))
    run = await claim(a, first)
    c = await a.get(ACTOR, first["case_id"])
    assert set(c["pending_fields"]) == required
    await a.complete(run, {**ANSWER, "needs_clarification": True})
    c = await a.get(ACTOR, first["case_id"])
    assert c["status"] == "waiting_for_info" and (await a.run(run.run_id)).terminal
    second = await a.accept_input(ACTOR, follow(c, facts={k: "2" for k in required}))
    assert second["run_id"] != first["run_id"]
    c = await a.get(ACTOR, first["case_id"])
    assert not c["pending_fields"] and c["drafts"][0]["status"] == "invalid"
    await a.complete(await claim(a, second), ANSWER)
    c = await a.get(ACTOR, first["case_id"])
    assert c["status"] == "in_progress" and len(c["runs"]) == 2
    assert all(v["source"] == "employee_material" for v in c["facts"].values())


async def test_competing_supplements_use_database_compare_and_swap(pair):
    a, b = pair
    r = await a.accept_input(ACTOR, new())
    c = await a.get(ACTOR, r["case_id"])
    results = await asyncio.gather(a.accept_input(ACTOR, follow(c, message="补料 A")),
                                   b.accept_input(ACTOR, follow(c, message="补料 B")), return_exceptions=True)
    assert sum(isinstance(x, dict) for x in results) == 1
    errors = [x for x in results if isinstance(x, HTTPException)]
    assert len(errors) == 1 and errors[0].status_code == 409
    c = await a.get(ACTOR, r["case_id"])
    assert c["version"] == 2 and len(c["messages"]) == 2


@pytest.mark.parametrize("change", ["supplement", "takeover", "resolved", "cancelled", "release"])
async def test_old_result_cannot_overwrite_material_or_control_changes(pair, change):
    a, b = pair
    r = await a.accept_input(ACTOR, new())
    run = await claim(a, r)
    c = await b.get(ACTOR, r["case_id"])
    if change == "supplement":
        await b.accept_input(ACTOR, follow(c, message="更新型号"))
    else:
        await b.mutate(ACTOR, c["id"], action(c, change if change in {"takeover", "release"} else "status", status=change))
    before = await b.get(ACTOR, c["id"])
    payload, applied = await a.complete(run, {**ANSWER, "handoff": True})
    after = await b.get(ACTOR, c["id"])
    assert not applied and not payload["handoff"] and payload["draft_id"] is None
    assert (after["version"], after["control_version"], after["status"]) == (before["version"], before["control_version"], before["status"])
    assert after["drafts"] == [] and await scalar(a.db, "SELECT count(*) FROM ticket_notifications") == 0


async def test_takeover_racing_completion_never_leaves_live_ai_approval(pair):
    a, b = pair
    r = await a.accept_input(ACTOR, new())
    run = await claim(a, r)
    c = await a.get(ACTOR, r["case_id"])
    outcomes = await asyncio.gather(a.complete(run, ANSWER), b.mutate(ACTOR, c["id"], action(c, "takeover")), return_exceptions=True)
    c = await b.get(ACTOR, c["id"])
    if isinstance(outcomes[1], HTTPException):
        # Completion won. Explicitly retry takeover against the new version.
        c = await b.mutate(ACTOR, c["id"], action(c, "takeover"))
    assert c["control_mode"] == "human"
    assert all(not d["valid"] for d in c["drafts"])


async def test_expired_and_reclaimed_worker_cannot_commit(pair):
    a, b = pair
    r = await a.accept_input(ACTOR, new())
    old = await claim(a, r, "old")
    await a.db.execute("UPDATE agent_runs SET lease_until=0 WHERE run_id=$1", old.run_id)
    with pytest.raises(HTTPException) as exc:
        await a.complete(old, {**ANSWER, "handoff": True})
    assert exc.value.detail["code"] == "run_lease_lost"
    fresh = await claim(b, r, "new")
    with pytest.raises(HTTPException):
        await a.complete(old, ANSWER)
    await b.complete(fresh, ANSWER)
    assert len((await a.get(ACTOR, r["case_id"]))["drafts"]) == 1


async def test_human_input_is_recorded_without_ai_draft_or_notification(pair):
    a, _ = pair
    r = await a.accept_input(ACTOR, new())
    c = await a.get(ACTOR, r["case_id"])
    c = await a.mutate(ACTOR, c["id"], action(c, "takeover"))
    r2 = await a.accept_input(ACTOR, follow(c, message="人工补料"))
    result, applied = await a.complete(await claim(a, r2), {**ANSWER, "handoff": True})
    assert not applied and result["case_rejection"] == "human_control"
    c = await a.get(ACTOR, c["id"])
    assert not c["drafts"] and any(m["content"] == "人工补料" for m in c["messages"])


async def test_draft_approval_binds_version_hash_control_and_reopen(pair):
    a, b = pair
    r = await a.accept_input(ACTOR, new())
    await a.complete(await claim(a, r), ANSWER)
    c = await a.get(ACTOR, r["case_id"])
    d = c["drafts"][0]
    approval = DraftApproval(expected_version=c["version"], expected_control_version=c["control_version"], body_hash=d["body_hash"])
    await a.approve(ACTOR, c["id"], d["id"], approval)
    await b.approve(ACTOR, c["id"], d["id"], approval)
    assert await scalar(a.db, "SELECT count(*) FROM case_events WHERE kind='draft_approved'") == 1
    c = await b.mutate(ACTOR, c["id"], action(c, "status", status="resolved"))
    with pytest.raises(HTTPException):
        await a.approve(ACTOR, c["id"], d["id"], approval)
    with pytest.raises(HTTPException):
        await b.mutate(ACTOR, c["id"], action(c, "reopen"))
    c = await b.mutate(ACTOR, c["id"], action(c, "reopen", reason="新证据"))
    assert c["status"] == "open" and not c["drafts"][0]["valid"]
    assert any(e["data"].get("reason") == "新证据" for e in c["events"])


async def test_cross_run_handoff_has_one_durable_action_and_outbox(pair):
    a, _ = pair
    r = await a.accept_input(ACTOR, new())
    run = await claim(a, r)
    first, _ = await a.complete(run, {**ANSWER, "handoff": True})
    again, _ = await a.complete(run, {**ANSWER, "handoff": True})
    assert first == again
    c = await a.get(ACTOR, r["case_id"])
    r2 = await a.accept_input(ACTOR, follow(c, message="再次请求人工"))
    await a.complete(await claim(a, r2), {**ANSWER, "handoff": True})
    assert await scalar(a.db, "SELECT count(*) FROM tickets") == 1
    assert await scalar(a.db, "SELECT count(*) FROM case_actions WHERE kind='handoff'") == 1
    assert await scalar(a.db, "SELECT count(*) FROM ticket_notifications") == 1


async def test_attachment_hash_dedup_and_verified_evidence_recomputes_missing_fields(pair):
    a, _ = pair
    digest = hashlib.sha256(b"image").hexdigest()
    a.attachment_resolver = AsyncMock(return_value=[{"id": "attachment-1", "sha256": digest}])
    r = await a.accept_input(ACTOR, new(issue_type="missing_parts", attachment_ids=["attachment-1"]))
    c = await a.get(ACTOR, r["case_id"])
    again = await a.accept_input(ACTOR, follow(c, attachment_ids=["attachment-1"]))
    assert again["case_version"] == r["case_version"]
    assert await scalar(a.db, "SELECT count(*) FROM case_materials") == 1
    args = dict(expected_version=c["version"], expected_control_version=c["control_version"],
                attachment_id="attachment-1", sha256=digest, facts={"order_ref": "SO-1", "model": "M1", "quantity": "2"}, evidence_id="e1")
    version = await a.submit_evidence(ACTOR, c["id"], **args)
    assert await a.submit_evidence(ACTOR, c["id"], **args) == version
    c = await a.get(ACTOR, c["id"])
    assert c["pending_fields"] == [] and c["version"] == version
    assert c["facts"]["model"]["sha256"] == digest
    await a.accept_input(ACTOR, follow(c, message="another supplement"))
    assert await a.submit_evidence(ACTOR, c["id"], **args) == version
    a.attachment_resolver.side_effect = HTTPException(403, "revoked")
    with pytest.raises(HTTPException):
        await a.submit_evidence(ACTOR, c["id"], **args)


async def test_unknown_legacy_scope_requires_explicit_admin_adoption_and_blocks_old_writer(pair):
    import asyncpg

    from app.tickets.ticket_store import PgTicketStore

    a, b = pair
    legacy = PgTicketStore(a.db)
    for status in ("open", "assigned", "resolved", "cancelled"):
        t = await legacy.create("old-conv", "old-user", "old issue")
        if status != "open":
            t = await legacy.update_status(t.id, status)
        mapped = "in_progress" if status == "assigned" else status
        assert await scalar(a.db, "SELECT status FROM ticket_case_state WHERE case_id=$1", t.id) == mapped
        with pytest.raises(HTTPException):
            await a.get(ACTOR, t.id)
        with pytest.raises(HTTPException):
            await a.adopt(ACTOR, t.id, CaseAdoption(expected_ticket_updated_at=t.updated_at, reason="确认归属"))
        adopted = await a.adopt(ADMIN, t.id, CaseAdoption(expected_ticket_updated_at=t.updated_at, reason="确认归属"))
        assert adopted["status"] == mapped and adopted["control_mode"] == "human"
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await b.db.execute("UPDATE tickets SET status='open' WHERE id=$1", t.id)
        assert await legacy.get(t.id) is None
    assert (await CaseService(a.db, "customer").list(ActorContext("other", frozenset({"employee"}), "customer")))["tickets"] == []


async def test_old_scan_claim_and_dispatch_paths_cannot_reach_case_runs(pair):
    a, _ = pair
    r = await a.accept_input(ACTOR, new())
    old = PgRunStore(a.db, "staff")
    assert await old.due_for_dispatch(idle_s=-1, lease_grace_s=0) == []
    assert await old.claim(r["run_id"], "old-worker", 60) is None
    await old.mark_dispatched([r["run_id"]])
    current = PgRunStore(a.db, "staff:case-v1")
    assert r["run_id"] in await current.due_for_dispatch(idle_s=-1, lease_grace_s=0)


async def test_legacy_writer_and_case_mutator_share_lock_order(pair):
    import asyncpg

    a, b = pair
    r = await a.accept_input(ACTOR, new())
    c = await a.get(ACTOR, r["case_id"])
    async def old_write():
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await b.db.execute("UPDATE tickets SET status='assigned' WHERE id=$1", c["id"])
    await asyncio.wait_for(asyncio.gather(old_write(), a.mutate(ACTOR, c["id"], action(c, "takeover"))), 3)


async def test_legacy_history_does_not_expose_case_materials(pair):
    from app.storage.conversation_log import PgConversationLog

    a, _ = pair
    r = await a.accept_input(ACTOR, new())
    await a.complete(await claim(a, r), ANSWER)
    assert await PgConversationLog(a.db).get_conversation("conversation", instance="staff") == []
    assert len((await a.get(ACTOR, r["case_id"]))["messages"]) == 2


def worker_for(svc, execute, **config):
    from app.runs.broker import MemoryBroker
    from app.runs.channel import MemoryChannel
    from app.runs.config import RunConfig
    from app.runs.degrade import DegradePolicy
    from app.runs.memo import MemoryMemo
    from app.runs.worker import RunWorker

    return RunWorker(store=PgRunStore(svc.db, svc.run_instance), broker=MemoryBroker(svc.run_instance),
                     channel=MemoryChannel(), memo=MemoryMemo(), execute=execute,
                     config=RunConfig(mode="queued", **config), policy=DegradePolicy(), on_dead=getattr(execute, "dead", None))


async def delivery(worker, run_id):
    await worker.broker.ensure_group()
    await worker.broker.dispatch(run_id)
    return (await worker.broker.receive(worker.owner, 1, 50))[0]


async def test_worker_commit_then_delayed_flush_keeps_terminal_publish_and_ack(pair):
    from app.events import EventLog
    from app.tickets.case_execution import CaseExecutor

    a, _ = pair
    r = await a.accept_input(ACTOR, new(issue_type="missing_parts"))
    committed, release = asyncio.Event(), asyncio.Event()
    log = EventLog.disabled()
    async def slow_flush(**_):
        committed.set()
        await release.wait()
        return True
    log.flush = slow_flush
    executor = CaseExecutor(a, prepare=AsyncMock(), answer=AsyncMock(), event_log=log)
    worker = worker_for(a, executor, lease_s=0.15)
    original = worker.store.heartbeat
    heartbeat_after_commit = asyncio.Event()
    async def heartbeat(*args):
        result = await original(*args)
        if not result:
            heartbeat_after_commit.set()
        return result
    worker.store.heartbeat = heartbeat
    item = await delivery(worker, r["run_id"])
    task = asyncio.create_task(worker._handle(item, False))
    await asyncio.wait_for(committed.wait(), 2)
    await asyncio.wait_for(heartbeat_after_commit.wait(), 2)
    release.set()
    await asyncio.wait_for(task, 2)
    assert await worker.broker.backlog() == 0
    frames = await worker.channel.read(r["run_id"], "0-0", block_ms=1)
    assert sum(f.event == "result" for f in frames) == 1
    assert not any(f.event == "delta" for f in frames)
    assert await scalar(a.db, "SELECT count(*) FROM case_run_receipts") == 1
    assert not executor.prepare.called and not executor.answer.called


@pytest.mark.parametrize("dead", [False, True])
async def test_worker_resume_and_dead_letter_both_recheck_case_fences(pair, dead):
    from app.events import EventLog
    from app.tickets.case_execution import CaseExecutor

    a, b = pair
    r = await a.accept_input(ACTOR, new())
    old = await claim(a, r)
    store = PgRunStore(a.db, a.run_instance)
    await store.checkpoint(old.run_id, old.lease_owner, old.attempt, {"case_proposal": {**ANSWER, "handoff": True}})
    await store.release(old.run_id, old.lease_owner, old.attempt, outcome="retry")
    c = await b.get(ACTOR, r["case_id"])
    await b.mutate(ACTOR, c["id"], action(c, "takeover"))
    executor = CaseExecutor(a, prepare=AsyncMock(), answer=AsyncMock(), event_log=EventLog.disabled())
    worker = worker_for(a, executor, max_attempts=1 if dead else 3)
    item = await delivery(worker, r["run_id"])
    await worker._handle(item, False)
    finished = await a.run(r["run_id"])
    assert finished.status == ("dead" if dead else "succeeded")
    assert finished.result["case_applied"] is False
    assert await scalar(a.db, "SELECT count(*) FROM ticket_notifications") == 0
    assert not executor.prepare.called and not executor.answer.called
    assert await worker.broker.backlog() == 0


async def test_two_notifiers_claim_one_delivery_and_retry_uses_same_key(pair, monkeypatch):
    from types import SimpleNamespace

    from app.tickets.notifier import TicketNotifier
    from app.tickets.ticket_store import PgTicketStore

    a, b = pair
    r = await a.accept_input(ACTOR, new())
    await a.complete(await claim(a, r), {**ANSWER, "handoff": True})
    keys = []
    class Client:
        def __init__(self, **_): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def post(self, url, **kwargs):
            keys.append(kwargs["headers"]["Idempotency-Key"])
            await asyncio.sleep(0.05)
            return SimpleNamespace(status_code=200, text="ok")
    monkeypatch.setattr("httpx.AsyncClient", Client)
    one, two = (TicketNotifier(PgTicketStore(s.db), "https://test.invalid") for s in (a, b))
    await asyncio.gather(one.drain_once(), two.drain_once())
    assert len(keys) == 1
    assert await scalar(a.db, "SELECT status FROM ticket_notifications") == "delivered"
    await one.drain_once()
    assert len(keys) == 1


async def test_case_history_retention_preserves_input_receipt(pair):
    from app.storage.conversation_log import PgConversationLog

    a, _ = pair
    body = new()
    r = await a.accept_input(ACTOR, body)
    assert await PgConversationLog(a.db).purge_before(9999999999) == 1
    assert (await a.accept_input(ACTOR, body))["run_id"] == r["run_id"]


@pytest.mark.parametrize("transport", ["/chat", "/chat/stream", "/runs", "/tickets/cases/inputs"])
async def test_public_entrypoints_use_same_case_worker_and_employee_boundary(pair, monkeypatch, transport):
    from types import SimpleNamespace

    import httpx
    import redis.asyncio as redis

    from app.api import main
    from app.api.case_api import optional_actor
    from app.core.actor import get_current_actor
    from app.runs.config import RunConfig

    a, _ = pair
    client = redis.from_url(os.environ["TEST_REDIS_URL"], decode_responses=True)
    namespace = "test-" + uuid.uuid4().hex[:10]
    actor = ActorContext("trusted-employee", frozenset({"employee"}), namespace)
    monkeypatch.setattr(main, "_pg_database", a.db)
    monkeypatch.setattr(main, "_infra_redis", client)
    monkeypatch.setattr(main, "_INSTANCE", namespace)
    monkeypatch.setattr(main, "_RUN_CONFIG", RunConfig(mode="inline", wait_s=2))
    model = AsyncMock()
    monkeypatch.setattr(main, "_orchestrator", SimpleNamespace(run=model))
    monkeypatch.setattr(main, "_cases", None)
    monkeypatch.setattr(main, "_case_worker", None)
    monkeypatch.setattr(main, "_case_event_log", main._case_event_log)
    previous = getattr(main.app.state, "case_service", None)
    await main._start_cases()
    try:
        body = new(issue_type="missing_parts").model_dump(exclude_unset=True)
        if transport != "/tickets/cases/inputs":
            body["user_id"] = "browser-admin-is-not-trusted"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as http:
            refused = await http.post(transport, json=body, headers={"X-API-Key": "fake", "X-Actor-Id": "forged-admin"})
            assert refused.status_code == 401
            main.app.dependency_overrides[get_current_actor] = lambda: actor
            main.app.dependency_overrides[optional_actor] = lambda: actor
            response = await http.post(transport, json=body)
            assert response.status_code in (200, 202), response.text
            cases = (await http.get("/tickets/cases")).json()["tickets"]
            assert len(cases) == 1
            case = cases[0]
            for _ in range(100):
                detail = (await http.get(f"/tickets/{case['id']}/case")).json()
                if detail["status"] == "waiting_for_info":
                    break
                await asyncio.sleep(0.02)
            assert detail["status"] == "waiting_for_info"
            assert detail["pending_fields"] == ["order_ref", "model", "quantity"]
            run_id = detail["runs"][0]["run_id"]
            public = await http.get(f"/runs/{run_id}")
            assert public.status_code == 200 and public.json()["status"] == "succeeded"
            record = await main._cases.run(run_id)
            assert record.user_id == actor.actor_id and record.instance == namespace + ":case-v1"
            if transport == "/chat/stream":
                assert "event: result" in response.text and "event: delta" not in response.text
            # Complete the same Case with explicit material, then approve the
            # exact resulting draft and invalidate it with human takeover.
            from app.agents.agent_orchestrator import AgentType, OrchestratorResult
            from app.agents.turn_prep import PreparedTurn

            model.return_value = OrchestratorResult("test", "已核对材料，待人工确认。", AgentType.ORDER, None)
            prepare = AsyncMock(return_value=PreparedTurn(SimpleNamespace(), SimpleNamespace(intent_group="order"), False, {}))
            monkeypatch.setattr(main, "_prepare_request", prepare)
            supplement = follow(detail, facts={"order_ref": "SO-1", "model": "M1", "quantity": "2"}).model_dump(exclude_unset=True)
            continuation = await http.post("/chat", json=supplement)
            assert continuation.status_code == 200, continuation.text
            assert continuation.json()["case_applied"] is True
            detail = (await http.get(f"/tickets/{case['id']}/case")).json()
            assert detail["status"] == "in_progress" and detail["pending_fields"] == []
            assert len(detail["runs"]) == 2 and model.await_count == 1
            assert prepare.call_args.args[1] == f"case-{namespace}-{case['id']}"
            assert prepare.call_args.args[2] == actor.actor_id
            assert prepare.call_args.kwargs["case_context"]["facts"]["order_ref"]["value"] == "SO-1"
            draft = next(d for d in detail["drafts"] if d["valid"])
            approval = {"expected_version": detail["version"], "expected_control_version": detail["control_version"], "body_hash": draft["body_hash"]}
            approved = await http.post(f"/tickets/{case['id']}/case/drafts/{draft['id']}/approve", json=approval)
            assert approved.status_code == 200
            takeover = await http.post(f"/tickets/{case['id']}/case/actions", json=action(detail, "takeover").model_dump())
            assert takeover.status_code == 200 and all(not d["valid"] for d in takeover.json()["drafts"])
            assert (await http.post(f"/tickets/{case['id']}/case/drafts/{draft['id']}/approve", json=approval)).status_code == 409
            main.app.dependency_overrides.clear()
            assert (await http.get(f"/runs/{run_id}")).status_code == 401
            assert (await http.get(f"/tickets/{case['id']}/case")).status_code == 401
            assert model.await_count == 1  # only the material-complete continuation called the model
    finally:
        main.app.dependency_overrides.clear()
        await main._case_worker.drain(2)
        await main._case_event_log.close()
        main.app.state.case_service = previous
        await client.aclose()


async def test_lease_expiring_while_waiting_for_case_lock_rolls_back_all_effects(pair):
    a, b = pair
    r = await a.accept_input(ACTOR, new())
    run = await PgRunStore(a.db, a.run_instance).claim(r["run_id"], "slow", 0.12)
    async with b.db.transaction() as conn:
        await conn.fetchrow("SELECT id FROM tickets WHERE id=$1 FOR UPDATE", r["case_id"])
        job = asyncio.create_task(a.complete(run, {**ANSWER, "handoff": True}))
        await asyncio.sleep(0.2)
    with pytest.raises(HTTPException) as exc:
        await job
    assert exc.value.detail["code"] == "run_lease_lost"
    c = await a.get(ACTOR, r["case_id"])
    assert c["version"] == 1 and not c["drafts"] and len(c["messages"]) == 1
    assert await scalar(a.db, "SELECT count(*) FROM case_run_receipts") == 0
    assert await scalar(a.db, "SELECT count(*) FROM ticket_notifications") == 0


async def test_migration_backfills_preexisting_rows_without_changing_legacy_shape(pair):
    from app.tickets.case_schema import migrate
    from app.tickets.ticket_store import Ticket

    a, _ = pair
    # Recreate the pre-Case situation inside this test's disposable database.
    async with a.db.transaction() as conn:
        await conn.execute("DROP TRIGGER helpdesk_case_insert ON tickets; DROP TRIGGER helpdesk_case_legacy_write ON tickets")
        await conn.execute("DROP TABLE ticket_case_state CASCADE; DROP TABLE helpdesk_migrations")
        for i, status in enumerate(["open", "assigned", "resolved", "cancelled"]):
            await conn.execute("INSERT INTO tickets(id,conv_id,user_id,reason,status,created_at,updated_at) VALUES($1,'same','old-user','old',$2,1,1)", f"old-{i}", status)
        await migrate(conn)
        await migrate(conn)
    states = await a.db.fetch("SELECT status,instance,managed FROM ticket_case_state ORDER BY case_id")
    assert [r["status"] for r in states] == ["open", "in_progress", "resolved", "cancelled"]
    assert all(r["instance"] == "" and not r["managed"] for r in states)
    legacy = [Ticket(**dict(r)) for r in await a.db.fetch("SELECT * FROM tickets ORDER BY id")]
    assert [t.status for t in legacy] == ["open", "assigned", "resolved", "cancelled"]
    assert (await a.list(ACTOR))["tickets"] == []


async def test_model_proposal_cannot_change_facts_owner_or_lifecycle_directly(pair):
    a, _ = pair
    r = await a.accept_input(ACTOR, new(facts={"model": "M-1"}))
    await a.complete(await claim(a, r), {**ANSWER, "status": "resolved", "facts": {"model": "forged"},
                                       "owner_actor_id": "admin", "version": 999, "control_mode": "human"})
    c = await a.get(ACTOR, r["case_id"])
    assert c["facts"]["model"]["value"] == "M-1"
    assert c["status"] == "in_progress" and c["owner_actor_id"] is None and c["control_mode"] == "assistant"
    assert c["version"] == 2


async def test_idempotency_distinguishes_absent_fields_from_explicit_clear(pair):
    a, _ = pair
    r = await a.accept_input(ACTOR, new(customer_ref="customer-A", order_refs=["SO-1"]))
    c = await a.get(ACTOR, r["case_id"])
    body = follow(c, message="补充说明")
    await a.accept_input(ACTOR, body)
    changed = body.model_copy(update={"customer_ref": "", "order_refs": []})
    with pytest.raises(HTTPException) as exc:
        await a.accept_input(ACTOR, changed)
    assert exc.value.detail["code"] == "message_id_conflict"
