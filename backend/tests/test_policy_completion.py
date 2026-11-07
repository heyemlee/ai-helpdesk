"""Real local document/chunk authority and durable policy-fact lifecycle controls."""
import hashlib
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from test_cases import ACTOR, PG, scalar
from test_cases import pair as pair  # noqa: F401

from app.agents.turn_prep import prepare_turn
from app.core import policy_completion as pc
from app.core.actor import ActorDirectory
from app.core.embedder import Embedder
from app.core.fact_completion import bind_task
from app.core.intent_recognizer import IntentCategory
from app.core.policy_scope import PolicyMetadata, PolicyScope, bind_policy_scope, policy_digest
from app.storage.vector_store import LocalStore, PgStore
from app.tools.knowledge_base import KnowledgeBase
from app.tools.tool_manager import ToolResult

KEY = "policy-controlled-key"
SCOPE = PolicyScope(product="Panel-X", region="CA", audience="employee", as_of="2034-02-09T07:08:09Z")
MESSAGE = "请查Panel-X的返修登记政策，说明所选日期和适用范围内的办理步骤及有效版本，并提供引用。"
DOCUMENT = {"title": "Panel-X 返修登记 rev-control", "content": "操作说明：第一步登记订单号，第二步核对记录，第三步交由运营审核。未审核不得承诺赔偿。",
            "policy": {"status": "published", "version": "rev-control", "product": "Panel-X", "region": "CA",
                       "audience": "employee", "effective_at": "2034-02-01T00:00:00Z", "expires_at": None}}


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("SQL_AGENT_API_KEY", KEY)


def context():
    return {"case_id": "case-control", "message_id": "input-control", "actor_id": ACTOR.actor_id,
            "instance": ACTOR.instance, "auth_version": ACTOR.auth_version,
            "expected_case_version": 1, "expected_control_version": 0,
            "snapshot": {"control_mode": "assistant", "issue_type": "general", "order_refs": [], "materials": [],
                         "facts": {}, "pending_fields": [], "policy_scope": SCOPE.model_dump(mode="json"),
                         "input_receipt": {"case_id": "case-control", "message_id": "input-control", "operation": "created"}}}


def task_for(message=MESSAGE):
    task = pc.make_task(message, context(), "run-control", 1)
    assert task is not None
    return task


@pytest.fixture
async def kb(tmp_path):
    store = LocalStore(str(tmp_path / "policy.db"))
    kb = KnowledgeBase(store, Embedder(dim=128))
    await kb.import_documents([DOCUMENT])
    try:
        yield kb
    finally:
        await store.close()


async def bound(kb, task=None, change=None):
    task = task or task_for()
    with bind_policy_scope(SCOPE), bind_task(task):
        sources = await kb.search_async("Panel-X 返修登记")
        assert sources
        if change:
            change(sources[0])
        return await kb.bind_policy_projection(task, {"attempted": True, "lookup_status": "found",
            "policy_lookup": True, "evidence_ok": True, "degraded": False, "sources": sources})


async def test_real_local_store_produces_exact_projection_and_witnesses(kb):
    task = task_for()
    knowledge = await bound(kb, task)
    result = pc.proposal(task, knowledge)
    assert result and result["answer_origin"] == pc.ORIGIN and result["answer_model_invoked"] is False
    assert result["handoff"] is False and not result["business_data"]
    assert set(result["fact_receipt"]["coverage"]) == set(task["fields"])
    assert len(result["fact_receipt"]["coverage"]["procedure"]["steps"]) == 3
    assert pc.receipt_valid(result["fact_receipt"], result["knowledge_sources"], message=MESSAGE,
        context=context(), run_id=task["run_id"], attempt=1, body=result["response"])
    assert await kb.validate_policy_projection(result["fact_receipt"], result["knowledge_sources"])


@pytest.mark.parametrize("suffix", ["并告诉我退货天数", "请转人工", "并直接办理退款", "再查询OTHER-902订单",
                                      "比较另一政策", "并确认申请截止日期", "并判断是否获批"])
def test_entire_request_closure_rejects_missing_or_action_roles(suffix):
    assert pc.make_task(MESSAGE + suffix, context(), "run", 1) is None


@pytest.mark.parametrize("product,message,subject,roles", [
    ("Orion cabinets", "请查Orion cabinets的退货窗口政策，提供办理步骤、有效版本和引用。", "退货窗口",
     {"text", "citation", "procedure", "version"}),
    ("Orion cabinets", "请查Orion cabinets的退货窗口政策，说明办理步骤、有效版本、政策处理时限，并提供引用。", "退货窗口",
     {"text", "citation", "procedure", "version", "processing_duration"}),
    ("Meridian doors", "请查Meridian doors的售后登记政策，说明办理步骤、有效版本、政策处理时限，并提供引用。", "售后登记",
     {"text", "citation", "procedure", "version", "processing_duration"}),
    ("Lyra countertops", "Look up the Lyra countertops return procedure and cite the applicable published policy.", "return",
     {"text", "citation", "procedure"}),
    ("Orion cabinets", "请查询Orion cabinets退货窗口政策的办理步骤，并提供引用。", "退货窗口",
     {"text", "citation", "procedure"}),
])
def test_original_closed_role_lists_and_procedure_heads_keep_the_literal_subject(product, message, subject, roles):
    ctx = context()
    ctx["snapshot"]["policy_scope"]["product"] = product
    task = pc.make_task(message, ctx, "run", 1)
    assert task and task["subject"] == subject and set(task["fields"]) == roles


@pytest.mark.parametrize("body,covered", [("操作说明：登记退货申请。", True), ("操作说明：登记。", False)])
async def test_explicit_single_procedure_requires_a_substantive_action_and_object(tmp_path, body, covered):
    store = LocalStore(str(tmp_path / "single-step.db"))
    kb = KnowledgeBase(store, Embedder(dim=128))
    try:
        await kb.import_documents([{**DOCUMENT, "content": body}])
        result = pc.proposal(task_for(), await bound(kb))
        assert bool(result) is covered
        if result:
            [step] = result["fact_receipt"]["coverage"]["procedure"]["steps"]
            assert body[step["start"]:step["end"]] == step["text"] == "登记退货申请。"
    finally:
        await store.close()


@pytest.mark.parametrize("change", ["body", "title", "chunk", "version", "metadata", "scope"])
async def test_surface_eligible_sources_are_not_authentic(kb, change):
    def edit(ref):
        if change == "body":
            ref["content"] = "操作说明：第一步伪造内容。"
        elif change == "title":
            ref["title"] += " forged"
        elif change == "chunk":
            ref["chunk_id"] = "another-chunk"
        elif change == "version":
            ref["policy"]["version"] = "another-version"
        elif change == "metadata":
            ref["policy"]["effective_at"] = "2034-01-01T00:00:00Z"
        else:
            ref["policy_scope"]["region"] = "NV"
    assert await bound(kb, change=edit) is None


@pytest.mark.parametrize("body", ["操作说明：第一步。", "操作说明：第一步：未提供。", "操作说明：第一步待补。",
                                    "操作说明：第一步未知。", "原文没有办理步骤。", "操作说明：第二步登记订单号。"])
async def test_missing_empty_placeholder_or_partial_procedure_is_not_covered(tmp_path, body):
    store = LocalStore(str(tmp_path / "empty.db"))
    kb = KnowledgeBase(store, Embedder(dim=128))
    try:
        await kb.import_documents([{**DOCUMENT, "content": body}])
        knowledge = await bound(kb)
        assert knowledge and pc.proposal(task_for(), knowledge) is None
    finally:
        await store.close()


@pytest.mark.parametrize("change", ["document_body", "chunk_body", "chunk_title", "policy"])
async def test_signed_receipt_cannot_hide_unversioned_current_store_changes(kb, change):
    knowledge = await bound(kb)
    result = pc.proposal(task_for(), knowledge)
    store = kb._store
    if change == "document_body":
        store._db.execute("UPDATE policy_documents SET content=content || ' changed'")
    elif change == "chunk_body":
        store._db.execute("UPDATE policy_chunks SET content=content || ' changed'")
    elif change == "chunk_title":
        store._db.execute("UPDATE policy_chunks SET title=title || ' changed'")
    else:
        store._db.execute("UPDATE policy_documents SET policy=json_set(policy,'$.version','changed')")
    store._db.commit()
    assert pc.proposal(task_for(), knowledge)  # old signed bytes still verify cryptographically
    assert not await kb.validate_policy_projection(result["fact_receipt"], result["knowledge_sources"])


class Tools:
    def __init__(self, kb, *, evidence_ok=None): self.kb, self.evidence_ok = kb, evidence_ok
    async def call(self, name, params, **_):
        if name != "knowledge_search":
            raise AssertionError("policy-only task must not query business records")
        return ToolResult(success=True, data=await self.kb.search_async(params["query"]), tool_name=name,
                          evidence_ok=self.evidence_ok)


async def setup_case(service, kb, *, message=MESSAGE):
    async def provider(actor_id, instance, **_):
        return ACTOR if actor_id == ACTOR.actor_id and instance == ACTOR.instance else None
    service.actors = ActorDirectory(provider)
    service.knowledge_base = kb
    from test_cases import claim

    from app.tickets.cases import CaseInput
    receipt = await service.accept_input(ACTOR, CaseInput(message_id="policy-control", conv_id="policy-control",
        new_case=True, issue_type="general", message=message, policy_scope=SCOPE))
    return await claim(service, receipt)


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
async def test_actual_pg_preparation_checkpoint_adoption_and_approval(pair):
    from test_agent_tool_loop import _orchestrator
    from test_case_followups import dependencies
    from test_fact_completion import worker_context

    from app.events import EventLog
    from app.runs.store import PgRunStore
    from app.tickets.case_execution import CaseExecutor
    from app.tickets.cases import DraftApproval

    a, b = pair
    kb = KnowledgeBase(PgStore(a.db), Embedder(dim=128))
    await kb.import_documents([DOCUMENT])
    run = await setup_case(a, kb)
    b.actors, b.knowledge_base = a.actors, kb
    deps = replace(dependencies(Tools(kb), intent=IntentCategory.QUERY), bind_policy_projection=kb.bind_policy_projection)
    async def prepare(run, snapshot, degrade):
        return await prepare_turn(run.message, user_id=run.user_id, conv_id=run.conv_id,
            request_id=run.request_id, deps=deps, case_context=snapshot)
    answer = AsyncMock(side_effect=AssertionError("eligible policy facts must precede the answer model"))
    executor = CaseExecutor(a, prepare=prepare, answer=answer, complete_facts=_orchestrator().complete_facts,
                            event_log=EventLog.disabled())
    result = (await executor(worker_context(run, PgRunStore(a.db, a.run_instance)))).result
    answer.assert_not_awaited()
    assert result["case_applied"] and result["answer_origin"] == pc.ORIGIN
    assert result["answer_model_invoked"] is False and not result["handoff"]
    case = await b.get(ACTOR, result["case_id"])
    assert case["drafts"][0]["valid"]
    draft = case["drafts"][0]
    await b.approve(ACTOR, case["id"], draft["id"], DraftApproval(expected_version=case["version"],
        expected_control_version=case["control_version"], body_hash=draft["body_hash"]))
    assert await scalar(a.db, "SELECT count(*) FROM case_actions WHERE kind='handoff'") == 0


@pytest.mark.parametrize("alias", ["excerpt", "doc_title"])
async def test_visible_aliases_cannot_claim_another_body_or_title(kb, alias):
    assert await bound(kb, change=lambda ref: ref.update({alias: "forged display text"})) is None


@pytest.mark.parametrize("bad", ["content", "chunk_id", "policy"])
async def test_one_good_target_fragment_cannot_hide_a_damaged_target_fragment(kb, bad):
    import copy
    task = task_for()
    with bind_policy_scope(SCOPE), bind_task(task):
        refs = await kb.search_async("Panel-X 返修登记")
        altered = copy.deepcopy(refs[0])
        altered[bad] = {**altered[bad], "version": "fake"} if bad == "policy" else "fake"
        assert await kb.bind_policy_projection(task, {"attempted": True, "lookup_status": "found",
            "policy_lookup": True, "sources": [refs[0], altered]}) is None


async def test_an_unrelated_real_policy_is_not_a_conflicting_target(kb):
    await kb.import_documents([{**DOCUMENT, "title": "Panel-X 开票说明 rev-control"}])
    task = task_for()
    with bind_policy_scope(SCOPE), bind_task(task):
        refs = await kb.search_async("Panel-X", top_k=10)
        assert len(refs) == 2
        knowledge = await kb.bind_policy_projection(task, {"attempted": True, "lookup_status": "found",
            "policy_lookup": True, "sources": refs})
    assert knowledge and len(knowledge["sources"]) == 1 and pc.proposal(task, knowledge)


async def add_overlapping_version(kb):
    await kb.import_documents([{**DOCUMENT, "title": "Panel-X 返修登记 rev-next",
        "content": "操作说明：第一步联系维修团队，第二步提交新申请，第三步交由主管审核。无需登记订单号。",
        "policy": {**DOCUMENT["policy"], "version": "rev-next"}}])


@pytest.mark.parametrize("retrieved", [1, 10])
async def test_authoritative_overlapping_versions_block_even_when_retrieval_returns_one(kb, retrieved):
    await add_overlapping_version(kb)
    task = task_for()
    with bind_policy_scope(SCOPE), bind_task(task):
        refs = await kb.search_async("Panel-X 返修登记", top_k=retrieved)
        assert len(refs) == min(retrieved, 2)
        assert await kb.bind_policy_projection(task, {"attempted": True, "lookup_status": "found",
            "policy_lookup": True, "sources": refs}) is None


async def test_a_new_authoritative_version_invalidates_an_already_signed_projection(kb):
    result = pc.proposal(task_for(), await bound(kb))
    assert result and await kb.validate_policy_projection(result["fact_receipt"], result["knowledge_sources"])
    await add_overlapping_version(kb)
    assert pc.proposal(task_for(), result["knowledge"] | {"sources": result["knowledge_sources"]})
    assert not await kb.validate_policy_projection(result["fact_receipt"], result["knowledge_sources"])


async def test_ordered_prose_has_concrete_recomputable_procedure_spans(tmp_path):
    store = LocalStore(str(tmp_path / "ordered.db"))
    kb = KnowledgeBase(store, Embedder(dim=128))
    body = "办理步骤：先登记申请，再核对清单，最后将记录交由运营复核。未经批准不得承诺补偿。"
    try:
        await kb.import_documents([{**DOCUMENT, "content": body}])
        result = pc.proposal(task_for(), await bound(kb))
        assert result
        steps = result["fact_receipt"]["coverage"]["procedure"]["steps"]
        assert len(steps) == 3 and all(body[s["start"]:s["end"]] == s["text"] for s in steps)
    finally:
        await store.close()


@pytest.mark.parametrize("change", ["signature", "task", "source", "coverage", "kind", "clock"])
async def test_receipt_and_source_contract_tampering_fails_closed(kb, change, monkeypatch):
    import copy
    from datetime import timedelta
    task = task_for()
    knowledge = await bound(kb)
    result = pc.proposal(task, knowledge)
    receipt = copy.deepcopy(result["fact_receipt"])
    if change == "signature":
        receipt["proof"]["signature"] = "forged"
    elif change == "task":
        receipt["task"]["actor_id"] = "other-actor"
    elif change == "source":
        receipt["proof"]["sources"][0]["content"] += " forged"
    elif change == "coverage":
        receipt["coverage"].pop("procedure")
    elif change == "kind":
        receipt["kind"] = "unknown-kind"
    else:
        original = pc.utc_now()
        monkeypatch.setattr(pc, "utc_now", lambda: original + timedelta(hours=1))
    assert not pc.receipt_valid(receipt, result["knowledge_sources"], message=MESSAGE,
        context=context(), run_id=task["run_id"], attempt=1, body=result["response"])


async def pg_case(pair, *, message=MESSAGE, document=DOCUMENT, kb=None, evidence_ok=None):
    from test_agent_tool_loop import _orchestrator
    from test_case_followups import dependencies

    from app.events import EventLog
    from app.tickets.case_execution import CaseExecutor
    a, b = pair
    if kb is None:
        kb = KnowledgeBase(PgStore(a.db), Embedder(dim=128))
        await kb.import_documents([document])
    run = await setup_case(a, kb, message=message)
    b.actors, b.knowledge_base = a.actors, kb
    deps = replace(dependencies(Tools(kb, evidence_ok=evidence_ok), intent=IntentCategory.QUERY),
                   bind_policy_projection=kb.bind_policy_projection)
    async def prepare(run, snapshot, degrade):
        return await prepare_turn(run.message, user_id=run.user_id, conv_id=run.conv_id,
            request_id=run.request_id, deps=deps, case_context=snapshot)
    answer = AsyncMock(side_effect=AssertionError("projection must precede the answer model"))
    executor = CaseExecutor(a, prepare=prepare, answer=answer, complete_facts=_orchestrator().complete_facts,
                            event_log=EventLog.disabled())
    return a, b, kb, run, executor, answer


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
@pytest.mark.parametrize("change", ["header", "receipt", "kind", "signature", "strip_sources", "checkpoint",
                                   "doc_body", "chunk_body", "chunk_title", "metadata", "acl", "scope", "takeover", "new_version",
                                   "knowledge_not_ok"] )
async def test_pg_adoption_rejects_mode_source_and_current_authority_changes(pair, change):
    from test_fact_completion import worker_context

    from app.runs.store import PgRunStore
    a, _, kb, run, executor, answer = await pg_case(pair)
    complete = a.complete
    async def intercept(run, result):
        if change == "header":
            result.pop("answer_origin")
        elif change == "receipt":
            result.pop("fact_receipt")
        elif change == "kind":
            result["fact_receipt"]["kind"] = "unknown-kind"
        elif change == "signature":
            result["fact_receipt"]["proof"]["signature"] = "forged"
        elif change == "strip_sources":
            result["knowledge_sources"] = []
        elif change == "checkpoint":
            await a.db.execute("UPDATE agent_runs SET checkpoint=checkpoint-'case_proposal_origin' WHERE run_id=$1", run.run_id)
        elif change == "doc_body":
            await a.db.execute("UPDATE policy_documents SET content=content || ' changed'")
        elif change == "chunk_body":
            await a.db.execute("UPDATE policy_chunks SET content=content || ' changed'")
        elif change == "chunk_title":
            await a.db.execute("UPDATE policy_chunks SET title=title || ' changed'")
        elif change == "metadata":
            await a.db.execute("UPDATE policy_documents SET policy=jsonb_set(policy,'{version}','\"changed\"')")
        elif change == "acl":
            async def revoked(actor_id, instance, **_): return None
            a.actors = ActorDirectory(revoked)
        elif change == "takeover":
            from app.tickets.cases import CaseMutation
            current = await a.get(ACTOR, run.case_context["case_id"])
            await a.mutate(ACTOR, current["id"], CaseMutation(expected_version=current["version"],
                expected_control_version=current["control_version"], operation="takeover"))
        elif change == "new_version":
            await add_overlapping_version(kb)
        elif change == "knowledge_not_ok":
            result["knowledge"]["evidence_ok"] = False
        else:
            await a.db.execute("UPDATE ticket_case_state SET policy_scope=jsonb_set(policy_scope,'{region}','\"NV\"')")
        return await complete(run, result)
    a.complete = intercept
    result = (await executor(worker_context(run, PgRunStore(a.db, a.run_instance)))).result
    answer.assert_not_awaited()
    assert not result["case_applied"] and result["case_rejection"]
    assert await scalar(a.db, "SELECT count(*) FROM case_drafts") == 0
    assert await scalar(a.db, "SELECT count(*) FROM case_actions WHERE kind='handoff'") == 0


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
@pytest.mark.parametrize("surface", ["run_result", "case_receipt"])
async def test_pg_outer_knowledge_state_must_match_the_clean_signed_checkpoint(pair, surface):
    from fastapi import HTTPException
    from test_fact_completion import worker_context

    from app.runs.store import PgRunStore
    from app.tickets.cases import DraftApproval, decode

    a, b, _, run, executor, answer = await pg_case(pair, evidence_ok=True)
    result = (await executor(worker_context(run, PgRunStore(a.db, a.run_instance)))).result
    answer.assert_not_awaited()
    assert result["case_applied"]
    baseline = await b.get(ACTOR, result["case_id"])
    assert baseline["drafts"][0]["valid"]
    checkpoint = decode((await a.run(run.run_id)).checkpoint, {})
    assert checkpoint["case_proposal"]["knowledge"]["evidence_ok"] is True
    if surface == "run_result":
        await a.db.execute("UPDATE agent_runs SET result=jsonb_set(result,'{knowledge,evidence_ok}','false'::jsonb) "
                           "WHERE run_id=$1", run.run_id)
    else:
        await a.db.execute("UPDATE case_run_receipts SET result=jsonb_set(result,'{knowledge,evidence_ok}','false'::jsonb) "
                           "WHERE run_id=$1", run.run_id)
    assert decode((await a.run(run.run_id)).checkpoint, {}) == checkpoint
    case = await b.get(ACTOR, result["case_id"])
    draft = case["drafts"][0]
    assert not draft["valid"]
    with pytest.raises(HTTPException) as error:
        await b.approve(ACTOR, case["id"], draft["id"], DraftApproval(expected_version=case["version"],
            expected_control_version=case["control_version"], body_hash=draft["body_hash"]))
    assert error.value.status_code == 409 and error.value.detail["code"] == "draft_evidence_stale"
    assert await scalar(a.db, "SELECT count(*) FROM case_drafts WHERE status='approved'") == 0


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
@pytest.mark.parametrize("retrieved", [1, 10])
async def test_pg_authoritative_version_set_does_not_depend_on_top_k(pair, retrieved):
    a, _ = pair
    kb = KnowledgeBase(PgStore(a.db), Embedder(dim=128))
    await kb.import_documents([DOCUMENT])
    assert pc.proposal(task_for(), await bound(kb))
    await add_overlapping_version(kb)
    task = task_for()
    with bind_policy_scope(SCOPE), bind_task(task):
        refs = await kb.search_async("Panel-X 返修登记", top_k=retrieved)
        assert len(refs) == min(retrieved, 2)
        assert await kb.bind_policy_projection(task, {"attempted": True, "lookup_status": "found",
            "policy_lookup": True, "sources": refs}) is None


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
@pytest.mark.parametrize("retrieved", [1, 5])
async def test_pg_catalog_conflict_survives_answer_retry_checkpoint_and_published_draft(pair, retrieved):
    import copy

    from conftest import ScriptedModel, text_step, tool_step
    from test_agent_tool_loop import _orchestrator
    from test_case_followups import dependencies
    from test_fact_completion import worker_context

    from app.agents.agent_orchestrator import AgentType
    from app.runs.store import PgRunStore
    from app.tickets.cases import decode

    a, b, kb, run, executor, _ = await pg_case(pair)
    await add_overlapping_version(kb)
    class LimitedTools(Tools):
        async def call(self, name, params, **_):
            assert name == "knowledge_search"
            return ToolResult(success=True, tool_name=name, evidence_ok=True,
                data=await kb.search_async(params["query"], top_k=min(params["top_k"], retrieved)))
    deps = replace(dependencies(LimitedTools(kb), intent=IntentCategory.QUERY),
                   bind_policy_projection=kb.bind_policy_projection)
    captured = {}
    async def prepare(run, snapshot, degrade):
        prepared = await prepare_turn(run.message, user_id=run.user_id, conv_id=run.conv_id,
            request_id=run.request_id, deps=deps, case_context=snapshot)
        captured["prepared"] = copy.deepcopy(prepared.evidence["knowledge"])
        assert "多个适用政策版本" in prepared.request.context
        assert DOCUMENT["content"] not in prepared.request.context
        assert not prepared.knowledge_used and not prepared.request.grounding_sources
        return prepared
    model = ScriptedModel(tool_step(("retry-policy", "knowledge_search",
        {"query": "Panel-X 返修登记 再查", "top_k": 1})), text_step("有效版本是 rev-control。[[handoff:yes]]"))
    orchestrator = _orchestrator((AgentType.GENERAL, model))
    async def answer(request):
        result = await orchestrator.run(request)
        captured["model_result"] = result
        return result
    executor.prepare, executor.answer = prepare, answer
    result = (await executor(worker_context(run, PgRunStore(a.db, a.run_instance)))).result
    assert result["case_applied"] and len(model.requests) == 2
    assert captured["prepared"]["lookup_status"] == "conflict"
    assert len(captured["prepared"]["conflicts"][0]["candidate_set"]["documents"]) == 2
    assert any(e["actor"] == "model" and e["tool"] == "knowledge_search"
               and e["trace"].get("lookup_status") == "found" for e in result["trajectory"])
    assert not result["knowledge_sources"] and result["knowledge"]["lookup_status"] == "conflict"
    assert result["knowledge"]["evidence_ok"] is False
    assert "多个适用政策版本" in result["response"] and "尚未核实" in result["response"]
    assert "rev-control" not in result["response"] and "政策摘录" not in result["response"]
    assert "answer_origin" not in result and "fact_receipt" not in result
    assert result["handoff"] and result["answer_handoff_declared"] is True
    assert result["answer_confidence"] == captured["model_result"].answer_confidence
    checkpoint = decode((await a.run(run.run_id)).checkpoint, {})
    assert checkpoint["case_proposal"]["knowledge"]["lookup_status"] == "conflict"
    draft = (await b.get(ACTOR, result["case_id"]))["drafts"][0]
    assert draft["body"] == result["response"] and draft["valid"]


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
async def test_pg_durable_checkpoint_resume_keeps_original_signed_clock(pair):
    from test_fact_completion import worker_context

    from app.runs.store import PgRunStore
    from app.runs.worker import RetryRun
    from app.tickets.cases import decode
    a, b, _, run, executor, answer = await pg_case(pair)
    store = PgRunStore(a.db, a.run_instance)
    complete = a.complete
    a.complete = AsyncMock(side_effect=RuntimeError("controlled crash after checkpoint"))
    with pytest.raises(RetryRun):
        await executor(worker_context(run, store))
    saved = await a.run(run.run_id)
    original = decode(saved.checkpoint, {})["case_proposal"]["fact_receipt"]
    await a.db.execute("UPDATE agent_runs SET lease_until=0 WHERE run_id=$1", run.run_id)
    resumed = await store.claim(run.run_id, "resumed-worker", 60)
    a.complete = complete
    executor.prepare = AsyncMock(side_effect=AssertionError("resume cannot query or re-sign policy"))
    result = (await executor(worker_context(resumed, store))).result
    answer.assert_not_awaited()
    assert result["case_applied"] and result["fact_receipt"] == original
    assert (await b.get(ACTOR, result["case_id"]))["drafts"][0]["valid"]


@pytest.mark.parametrize("field", ["处理时限为13个工作日。", "办理时限：3-5个工作日。", "processing time: 2-4 business days.",
                                  "政策处理时限：3个工作日。"])
async def test_processing_duration_needs_its_own_source_label_value_and_unit(tmp_path, field):
    store = LocalStore(str(tmp_path / "duration.db"))
    kb = KnowledgeBase(store, Embedder(dim=128))
    message = "请查Panel-X的返修登记政策，说明处理时限并提供引用。"
    task = task_for(message)
    try:
        await kb.import_documents([{**DOCUMENT, "content": field}])
        result = pc.proposal(task, await bound(kb, task))
        assert result and result["answer_model_invoked"] is False
        witness = result["fact_receipt"]["coverage"]["processing_duration"]
        assert field[witness["start"]:witness["end"]] == witness["text"]
        assert witness["label"] and witness["value"] and witness["unit"]
        persisted = json.loads(json.dumps(result))
        assert persisted["fact_receipt"] == result["fact_receipt"]
        assert pc.receipt_valid(persisted["fact_receipt"], persisted["knowledge_sources"], message=message,
            context=context(), run_id=task["run_id"], attempt=1, body=persisted["response"])
    finally:
        await store.close()


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
@pytest.mark.parametrize("duration", ["处理时限：3个工作日。", "办理时限：3-5个工作日。", "政策处理时限：3个工作日。"])
async def test_pg_duration_survives_json_checkpoint_get_and_approval(pair, duration):
    from test_fact_completion import worker_context

    from app.runs.store import PgRunStore
    from app.tickets.cases import DraftApproval, decode

    message = "请查Panel-X的返修登记政策，说明处理时限并提供引用。"
    a, b, kb, run, executor, answer = await pg_case(pair, message=message,
        document={**DOCUMENT, "content": duration})
    result = (await executor(worker_context(run, PgRunStore(a.db, a.run_instance)))).result
    answer.assert_not_awaited()
    assert result["case_applied"] and result["answer_origin"] == pc.ORIGIN
    checkpoint = decode((await a.run(run.run_id)).checkpoint, {})["case_proposal"]
    assert checkpoint["fact_receipt"] == result["fact_receipt"]
    witness = checkpoint["fact_receipt"]["coverage"]["processing_duration"]
    assert all(type(witness[name]) is list for name in ("label_span", "value_span", "unit_span"))
    assert await kb.validate_policy_projection(checkpoint["fact_receipt"], checkpoint["knowledge_sources"])
    case = await b.get(ACTOR, result["case_id"])
    draft = case["drafts"][0]
    assert draft["valid"]
    approved = await b.approve(ACTOR, case["id"], draft["id"], DraftApproval(expected_version=case["version"],
        expected_control_version=case["control_version"], body_hash=draft["body_hash"]))
    assert approved["drafts"][0]["valid"] and approved["drafts"][0]["status"] == "approved"


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
@pytest.mark.parametrize("tamper", ["body", "coverage"])
async def test_pg_persisted_duration_body_or_coverage_tampering_fails_get_and_approval(pair, tamper):
    from fastapi import HTTPException
    from test_fact_completion import worker_context

    from app.runs.store import PgRunStore
    from app.tickets.cases import DraftApproval

    message = "请查Panel-X的返修登记政策，说明处理时限并提供引用。"
    a, b, _, run, executor, answer = await pg_case(pair, message=message,
        document={**DOCUMENT, "content": "处理时限：3个工作日。"})
    result = (await executor(worker_context(run, PgRunStore(a.db, a.run_instance)))).result
    answer.assert_not_awaited()
    assert result["case_applied"]
    draft = (await b.get(ACTOR, result["case_id"]))["drafts"][0]
    assert draft["valid"]
    if tamper == "body":
        body = draft["body"] + " changed"
        await a.db.execute("UPDATE case_drafts SET body=$2,body_hash=$3 WHERE id=$1", draft["id"],
                           body, hashlib.sha256(body.encode()).hexdigest())
    else:
        evidence = draft["evidence"]
        entry = next(e for e in evidence if e.get("source") == "fact_projection")
        entry["receipt"]["coverage"]["processing_duration"]["value"] = "99"
        await a.db.execute("UPDATE case_drafts SET evidence=$2::jsonb WHERE id=$1", draft["id"], json.dumps(evidence))
    case = await b.get(ACTOR, result["case_id"])
    altered = case["drafts"][0]
    assert not altered["valid"]
    with pytest.raises(HTTPException) as error:
        await b.approve(ACTOR, case["id"], altered["id"], DraftApproval(expected_version=case["version"],
            expected_control_version=case["control_version"], body_hash=altered["body_hash"]))
    assert error.value.status_code == 409 and error.value.detail["code"] == "draft_evidence_stale"
    assert await scalar(a.db, "SELECT count(*) FROM case_drafts WHERE status='approved'") == 0


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
@pytest.mark.parametrize("operation", ["draft_to_published", "published_metadata_update"])
async def test_pg_standard_policy_update_accepts_fresh_receipts_and_revokes_old_persisted_receipts(pair, operation):
    from fastapi import HTTPException
    from test_fact_completion import worker_context

    from app.runs.store import PgRunStore
    from app.tickets.cases import DraftApproval

    a, _ = pair
    kb = KnowledgeBase(PgStore(a.db), Embedder(dim=128))
    initial = {**DOCUMENT, "policy": {**DOCUMENT["policy"],
        "status": "draft" if operation == "draft_to_published" else "published"}}
    await kb.import_documents([initial])
    before = await a.db.fetchrow("SELECT id,policy_revision FROM policy_documents")
    metadata = {**DOCUMENT["policy"], "expires_at": "2035-02-01T00:00:00Z"}
    await kb.set_document_policy(before["id"], PolicyMetadata.model_validate(metadata), before["policy_revision"])
    actual = pc.document_payload(await kb._store.kb_fact_document(before["id"]))
    assert actual["revision"] != before["policy_revision"]
    assert actual["revision"] != actual["content_hash"]
    assert actual["content_hash"] == policy_digest([actual["title"].strip(), actual["content"].strip(), actual["policy"]])
    assert pc.document_valid(actual, kb._chunk_text)
    a, b, _, run, executor, answer = await pg_case(pair, kb=kb)
    result = (await executor(worker_context(run, PgRunStore(a.db, a.run_instance)))).result
    answer.assert_not_awaited()
    assert result["case_applied"] and result["answer_origin"] == pc.ORIGIN
    assert result["fact_receipt"]["proof"]["document"]["revision"] == actual["revision"]
    case = await b.get(ACTOR, result["case_id"])
    draft = case["drafts"][0]
    assert draft["valid"]
    approved = await b.approve(ACTOR, case["id"], draft["id"], DraftApproval(expected_version=case["version"],
        expected_control_version=case["control_version"], body_hash=draft["body_hash"]))
    assert approved["drafts"][0]["valid"] and approved["drafts"][0]["status"] == "approved"
    # Another standard metadata update retains applicability but revokes the
    # already persisted receipt; current revision equality is still mandatory.
    await kb.set_document_policy(before["id"], PolicyMetadata.model_validate(
        {**metadata, "expires_at": "2036-02-01T00:00:00Z"}), actual["revision"])
    assert not await kb.validate_policy_projection(result["fact_receipt"], result["knowledge_sources"])
    current = await b.get(ACTOR, result["case_id"])
    assert not current["drafts"][0]["valid"]
    with pytest.raises(HTTPException) as error:
        await b.approve(ACTOR, case["id"], draft["id"], DraftApproval(expected_version=current["version"],
            expected_control_version=current["control_version"], body_hash=draft["body_hash"]))
    assert error.value.status_code == 409 and error.value.detail["code"] == "draft_evidence_stale"


@pytest.mark.parametrize("body", [
    DOCUMENT["content"], "政策原文有13个工作日。", "处理时限：未提供。", "处理时限：待补。",
    "处理时限：13。", "处理时限：2034-02-09。", "退货窗口：13个工作日。", "申请截止日期：2034-02-09。",
    "处理时限：7-3天。", "处理时限：3天；办理时限：5天。", "实际订单处理用了13个工作日。",
])
async def test_other_times_missing_values_and_ambiguous_durations_do_not_cover_the_role(tmp_path, body):
    store = LocalStore(str(tmp_path / "missing-duration.db"))
    kb = KnowledgeBase(store, Embedder(dim=128))
    task = task_for("请查Panel-X的返修登记政策，说明处理时限并提供引用。")
    try:
        await kb.import_documents([{**DOCUMENT, "content": body}])
        knowledge = await bound(kb, task)
        assert knowledge and pc.proposal(task, knowledge) is None
    finally:
        await store.close()


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
async def test_policy_receipt_cannot_outlive_the_workers_lease(pair):
    from test_fact_completion import worker_context

    from app.runs.store import PgRunStore
    from app.runs.worker import RetryRun
    a, _, _, run, executor, answer = await pg_case(pair)
    complete = a.complete
    async def expired(run, result):
        await a.db.execute("UPDATE agent_runs SET lease_until=0 WHERE run_id=$1", run.run_id)
        return await complete(run, result)
    a.complete = expired
    with pytest.raises(RetryRun):
        await executor(worker_context(run, PgRunStore(a.db, a.run_instance)))
    answer.assert_not_awaited()
    assert await scalar(a.db, "SELECT count(*) FROM case_drafts") == 0
    assert await scalar(a.db, "SELECT count(*) FROM case_actions WHERE kind='handoff'") == 0


@pytest.mark.parametrize("flag,value", [("has_risk", True), ("evidence_issues", ["mismatch"]), ("conflicts", ["conflict"])])
async def test_known_risk_states_block_signing_projection_and_model_preflight(kb, flag, value):
    import copy

    from conftest import make_request
    from test_agent_tool_loop import _orchestrator
    task = task_for()
    good = await bound(kb, task)
    bad = copy.deepcopy(good)
    bad[flag] = value
    assert pc.proposal(task, bad) is None
    with bind_task(task), bind_policy_scope(SCOPE):
        assert await kb.bind_policy_projection(task, bad) is None
    request = make_request(MESSAGE, IntentCategory.QUERY, lookup_goal={"fact_task": task},
                           fact_evidence={"knowledge": good, "business_data": {flag: value}})
    assert await _orchestrator().complete_facts(request) is None


@pytest.mark.parametrize("missing", ["title", "version"])
async def test_target_identity_is_resolved_before_a_malformed_refs_claims(kb, missing):
    import copy
    task = task_for()
    with bind_policy_scope(SCOPE), bind_task(task):
        refs = await kb.search_async("Panel-X 返修登记")
        malformed = copy.deepcopy(refs[0])
        if missing == "title":
            malformed.pop("title")
        else:
            malformed["policy"].pop("version")
        assert await kb.bind_policy_projection(task, {"attempted": True, "policy_lookup": True,
            "lookup_status": "found", "sources": [refs[0], malformed]}) is None


@pytest.mark.parametrize("message", [
    "请查询Panel-X返修登记政策的有效版本与办理流程，并引用原文。",
    "请说明Panel-X返修登记政策的办理步骤和有效版本，附政策出处。",
    "请查询Panel-X返修登记政策的有效版本，并提供引用。",
    "请查询Panel-X返修登记政策的办理步骤，并提供引用。",
])
async def test_generic_policy_request_word_order_closes_against_the_same_literal_subject(kb, message):
    task = task_for(message)
    assert pc.proposal(task, await bound(kb, task))


@pytest.mark.parametrize("message", [
    "Look up the return-window policy for Panel-X, explain the procedure and applicable version for the selected scope and time, and cite the policy.",
    "Please show the applicable version of the Panel-X return-window policy and provide a citation.",
])
async def test_english_word_order_and_real_imperative_source_steps(tmp_path, message):
    store = LocalStore(str(tmp_path / "english.db"))
    kb = KnowledgeBase(store, Embedder(dim=128))
    try:
        await kb.import_documents([{**DOCUMENT, "title": "Panel-X return-window policy", "content":
            "Register the request, verify the item list, and submit the record for operations review. A deadline is not provided."}])
        task = task_for(message)
        with bind_policy_scope(SCOPE), bind_task(task):
            refs = await kb.search_async("Panel-X return-window")
            knowledge = await kb.bind_policy_projection(task, {"attempted": True, "policy_lookup": True,
                "lookup_status": "found", "sources": refs})
        result = pc.proposal(task, knowledge)
        assert result and not result["handoff"]
    finally:
        await store.close()
