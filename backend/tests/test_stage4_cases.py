"""Draft and approval fences re-read policies, original observations and actors."""
import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from test_cases import ACTOR, claim, new
from test_cases import pair as pair  # noqa: F401
from test_stage4_evidence import evidence_payload

from app.core import policy_scope as ps
from app.core.actor import ActorDirectory
from app.core.business_facts import render_draft, verified_cells
from app.core.embedder import Embedder
from app.storage.vector_store import PgStore
from app.tickets.cases import DraftApproval
from app.tools.knowledge_base import KnowledgeBase


async def prepare_policy(service, *, expires=None):
    kb = KnowledgeBase(store=PgStore(service.db), embedder=Embedder(dim=128))
    service.knowledge_base = kb
    policy = ps.PolicyMetadata(status="published", version="v1", product="Cab", region="CA", audience="employee",
                               effective_at="2020-01-01T00:00:00Z", expires_at=expires)
    [doc] = await kb.import_documents([{"title":"Shipping", "content":"Shipping fee: $150 per order.", "policy":policy.model_dump(mode="json")}])
    scope = ps.PolicyScope(product="Cab", region="CA", audience="employee")
    with ps.bind_policy_scope(scope):
        refs = await kb.search_async("Shipping fee")
    receipt = await service.accept_input(ACTOR, new(policy_scope=scope))
    run = await claim(service, receipt)
    assert run.case_context["snapshot"]["policy_scope"] == scope.model_dump(mode="json")
    return kb, doc, refs, run


def approval(case, draft):
    return DraftApproval(expected_version=case["version"], expected_control_version=case["control_version"], body_hash=draft["body_hash"])


async def test_policy_withdrawal_revokes_existing_draft_and_approval(pair):
    service, _ = pair
    kb, doc, refs, run = await prepare_policy(service)
    payload, applied = await service.complete(run, {"response":render_draft(refs, {}), "knowledge_sources":refs})
    assert applied
    case = await service.get(ACTOR, payload["case_id"])
    [draft] = case["drafts"]
    assert draft["valid"]
    current = await kb.get_document(doc["id"])
    await kb.set_document_policy(doc["id"], ps.PolicyMetadata.model_validate({**current["policy"], "status":"retired"}), current["policy_revision"])
    assert not (await service.get(ACTOR, case["id"]))["drafts"][0]["valid"]
    with pytest.raises(HTTPException) as error:
        await service.approve(ACTOR, case["id"], draft["id"], approval(case, draft))
    assert error.value.status_code == 409


async def test_clock_only_expiry_blocks_draft_commit(pair, monkeypatch):
    instant = datetime.now(UTC)
    monkeypatch.setattr(ps, "now", lambda: instant)
    service, _ = pair
    _, _, refs, run = await prepare_policy(service, expires=instant + timedelta(seconds=1))
    instant += timedelta(seconds=2)
    result, applied = await service.complete(run, {"response":render_draft(refs, {}), "knowledge_sources":refs})
    assert not applied and result["case_rejection"] == "draft_evidence_stale"
    assert (await service.get(ACTOR, result["case_id"]))["drafts"] == []


async def test_business_ids_survive_event_purge_and_current_actor_is_required(pair):
    service, _ = pair
    raw = evidence_payload()
    cells, issues = verified_cells(raw)
    business = {"attempted": True, "evidence":cells, "evidence_issues":issues, "evidence_payload":raw}
    run = await claim(service, await service.accept_input(ACTOR, new()))
    result, applied = await service.complete(run, {"response":render_draft([],business), "business_data":business})
    assert applied
    case = await service.get(ACTOR, result["case_id"])
    [draft] = case["drafts"]
    ids = [c["evidence_id"] for c in draft["evidence"][0]["evidence"]]
    await service.db.execute("DELETE FROM turn_events")
    again = await service.get(ACTOR, case["id"])
    assert [c["evidence_id"] for c in again["drafts"][0]["evidence"][0]["evidence"]] == ids
    revoked = False
    async def lookup(actor_id, instance, **kwargs):
        return None if revoked else ACTOR
    service.actors = ActorDirectory(lookup)
    assert (await service.approve(ACTOR, case["id"], draft["id"], approval(case,draft)))["drafts"][0]["status"] == "approved"
    revoked = True
    with pytest.raises(HTTPException) as error:
        await service.approve(ACTOR, case["id"], draft["id"], approval(case,draft))
    assert error.value.status_code == 401


async def test_policy_draft_get_reuses_single_connection_and_read_only_snapshot(pair, monkeypatch):
    from app.storage.pg import Database
    from app.tickets.cases import CaseService

    service, _ = pair
    kb, doc, refs, run = await prepare_policy(service)
    result, applied = await service.complete(run, {"response":render_draft(refs, {}), "knowledge_sources":refs})
    assert applied
    db = Database(service.db._dsn, max_size=1)
    await db.connect_pool()
    reader = CaseService(db, "staff")
    reader.knowledge_base = KnowledgeBase(store=PgStore(db), embedder=Embedder(dim=128))
    try:
        # No second connection is available, even with several concurrent readers.
        cases = await asyncio.wait_for(asyncio.gather(*(reader.get(ACTOR, result["case_id"]) for _ in range(4))), 5)
        assert all(c["drafts"][0]["valid"] for c in cases)
        reached, release = asyncio.Event(), asyncio.Event()
        validate = reader.knowledge_base.validate_policy_refs
        async def paused(refs, *, conn=None, lock=True):
            assert conn is not None and lock is False
            reached.set()
            await release.wait()
            return await validate(refs, conn=conn, lock=lock)
        monkeypatch.setattr(reader.knowledge_base, "validate_policy_refs", paused)
        pending = asyncio.create_task(reader.get(ACTOR, result["case_id"]))
        await asyncio.wait_for(reached.wait(), 5)
        current = await kb.get_document(doc["id"])
        await kb.set_document_policy(doc["id"], ps.PolicyMetadata.model_validate({**current["policy"], "status":"retired"}), current["policy_revision"])
        release.set()
        assert (await asyncio.wait_for(pending, 5))["drafts"][0]["valid"]
        assert not (await reader.get(ACTOR, result["case_id"]))["drafts"][0]["valid"]
    finally:
        await db.close()
