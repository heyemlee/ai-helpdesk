"""Closed original bodies plus real-store request admission and publication.

Archived model decisions stay unchanged. New controlled-store projections run
before an answer model and use only this test's synthetic signing key.
"""
import copy
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from test_agent_tool_loop import _orchestrator
from test_case_followups import dependencies
from test_cases import ACTOR, PG, claim, scalar
from test_cases import pair as pair  # noqa: F401
from test_fact_completion import worker_context
from test_policy_completion import DOCUMENT, SCOPE, Tools, context

from app.agents.agent_orchestrator import BaseAgent
from app.agents.turn_prep import prepare_turn
from app.core import policy_completion as pc
from app.core.actor import ActorDirectory
from app.core.embedder import Embedder
from app.core.fact_completion import bind_task, valid_fact_header
from app.core.intent_recognizer import IntentCategory, IntentRecognizer
from app.core.lookup_scope import bind_goal, make_goal
from app.core.policy_scope import PolicyScope, bind_policy_scope
from app.events import EventLog
from app.events.replay import rebuild_request, replay_guard
from app.events.shapes import sha256
from app.runs.memo import RunScope, bind_run
from app.runs.store import PgRunStore
from app.storage.vector_store import LocalStore, PgStore
from app.tickets.case_execution import CaseExecutor
from app.tickets.cases import CaseInput, DraftApproval
from app.tools.knowledge_base import KnowledgeBase
from app.tools.sql_agent_client import should_query_business_data

CLOSED = json.loads((Path(__file__).parent / "fixtures/policy_scope_metadata_closed.json").read_text())
RECORDS = CLOSED["records"]


@pytest.fixture(autouse=True)
def controlled_key(monkeypatch):
    monkeypatch.setenv("SQL_AGENT_API_KEY", "controlled-policy-v3-only")


@pytest.mark.parametrize("record", RECORDS, ids=lambda r: r["id"])
async def test_closed_original_guard_body_and_recorded_model_decision(record, orchestrator):
    rebuilt = rebuild_request(record["events"])
    # PostgreSQL JSONB loses insertion order inside entities. _build_messages
    # serializes that mapping as text, so restore the unchanged extractor's
    # schema order and require the archived messages hash before replaying.
    order = object.__new__(IntentRecognizer)._extract_entities("")
    logical_entities = copy.deepcopy(rebuilt.request.entities)
    rebuilt.request.entities = {k: rebuilt.request.entities[k] for k in order if k in rebuilt.request.entities}
    assert rebuilt.request.entities == logical_entities
    started = next(e for e in record["events"] if e["type"] == "agent_started")
    assert sha256(BaseAgent._build_messages(rebuilt.request)) == started["data"]["messages_sha256"]
    assert sha256(started["body"]["system"]) == started["data"]["system_sha256"]
    memory = next(e for e in record["events"] if e["type"] == "memory_loaded")
    assert memory["body"]["reference_times"]["policy_as_of"] == rebuilt.request.reference_times["policy_as_of"]
    replayed = replay_guard(record["events"])
    assert len(replayed) == 1 and not replayed[0].replayed
    assert len(replayed[0].recorded) == len(record["result"]["policy_violations"])
    _, declared = BaseAgent._split_handoff_trailer(record["model_answer"])
    assert declared is record["result"]["answer_handoff_declared"]
    started = next(e for e in record["events"] if e["type"] == "agent_started")
    skill = started["body"]["system"].split("[动态 Skills]\n", 1)[-1]
    for agents in orchestrator._pool.values():
        for agent in agents:
            agent._call_llm = AsyncMock(return_value=record["model_answer"])
            agent._skill_prompt = lambda req: skill
    result = await orchestrator.run(rebuilt.request)
    assert not result.policy_violations
    assert result.answer_handoff_declared is declared and result.handoff is declared
    assert result.answer_confidence == round(min(rebuilt.request.intent_confidence,
                                                result.routing_confidence, .65 if declared else 1.), 3)
    if declared:
        assert record["result"]["business_data"]["lookup_verified"] is False
        assert CLOSED["only_related_SQL_runs"][0]["run_id"] == "53fd3328f78d4565b123af52ddf22502"


@pytest.mark.parametrize("record", RECORDS, ids=lambda r: r["id"])
def test_full_original_message_admits_exact_server_scope_and_roles(record):
    task = pc.make_task(record["run"]["message"], record["context"], record["run"]["run_id"], 1)
    assert task and pc.valid_task(task)
    assert task["subject"] in {"取消订单", "付款手续费"}
    assert task["scope"] == PolicyScope.model_validate(record["context"]["snapshot"]["policy_scope"]).model_dump(mode="json")
    assert {"text", "citation", "version", "scope", "as_of"} <= set(task["fields"])
    assert ("processing_duration" if record["id"].endswith("024") else "procedure") in task["fields"]


@pytest.mark.parametrize("frame", ["我在整理", "我正在准备", "请帮我编写", "我们需要起草"])
def test_informational_envelope_is_not_tied_to_formal_names_or_dates(frame):
    message = f"{frame} CA 地区 Panel-X 客服材料。根据所选时间及适用范围，返修登记的操作步骤有哪些规定？请列出有效版本和引用。"
    task = pc.make_task(message, context(), "synthetic-run", 1)
    assert task and task["subject"] == "返修登记" and "procedure" in task["fields"]


@pytest.mark.parametrize("change", ["action", "human", "extra", "region", "product", "clock", "record", "mixed_subject"])
def test_unconsumed_or_conflicting_request_never_becomes_a_policy_task(change):
    record = RECORDS[1]
    message = record["run"]["message"]
    if change == "action":
        message += "并直接支付费用。"
    elif change == "human":
        message += "请转人工。"
    elif change == "extra":
        message += "另请核实这笔申请是否获批。"
    elif change == "region":
        message = message.replace("CA 地区", "NV 地区")
    elif change == "product":
        message = message.replace("Juniper-50 cabinets", "other product")
    elif change == "clock":
        message = message.replace("所选日期", "当前日期")
    elif change == "record":
        message = message.replace("付款手续费的", "我的订单记录的")
    else:
        message = message.replace("付款手续费的", "取消订单并付款的")
    assert pc.make_task(message, record["context"], "synthetic-run", 1) is None


@pytest.mark.parametrize("tail", [
    "另外查询订单 SO-TEST-300 的实际状态。", "并帮我取消订单。", "并立即付款。",
    "并请支付费用。", "并提交赔付申请。", "同时核对我的订单记录。", "并批准本单退款。",
])
def test_migrated_mixed_action_and_concrete_record_negatives_at_typed_entry(tail):
    message = "请说明Panel-X的取消订单的处理时限有什么规定？" + tail
    assert pc.make_task(message, context(), "run-control", 1) is None


@pytest.mark.parametrize("subject", ["请帮我取消订单", "立即付款", "我的订单", "实际订单记录"])
def test_migrated_action_and_actual_record_subjects_at_typed_entry(subject):
    message = f"请说明Panel-X的{subject}的处理时限有什么规定？"
    assert pc.make_task(message, context(), "run-control", 1) is None


@pytest.mark.parametrize("subject", [
    "NV 地区取消订单", "内华达州取消订单", "其他区域取消订单", "当前日期取消订单",
    "2036-02-09取消订单", "2036年2月9日取消订单", "明年取消订单", "本月取消订单", "下个月取消订单", "另一产品付款手续费",
    "Widget-Y取消订单", "另一款柜体取消订单", "外部客户付款手续费", "其他适用人群取消订单",
])
@pytest.mark.parametrize("framed", [False, True])
def test_explicit_scope_qualifiers_cannot_be_swallowed_as_an_unknown_subject(subject, framed):
    prefix = "我在整理 CA 地区 Panel-X 客服材料。按所选日期和适用范围，" if framed else "请说明Panel-X的"
    message = prefix + subject + "的办理步骤有什么规定？请列出有效版本和引用。"
    assert pc.make_task(message, context(), "run-control", 1) is None


@pytest.mark.parametrize("change", [None, "message", "run", "attempt", "scope", "case", "goal_case", "goal_owner", "goal_message"])
def test_current_request_and_inner_sql_gate_require_current_server_identity(change):
    message = "请说明Panel-X的取消订单的处理时限有什么规定？"
    task = pc.make_task(message, context(), "run-control", 1)
    # Worker store namespaces and business instances are different domains.
    run = RunScope(task["run_id"], 1, task["instance"] + ":case-v3")
    scope, case_id = SCOPE, task["case_id"]
    goal = make_goal(message, owner=run.run_id, case_id=case_id)
    if change == "message":
        message += "请转人工。"
    elif change in {"run", "attempt"}:
        run = replace(run, **{"run_id" if change == "run" else change: 2 if change == "attempt" else "other"})
    elif change == "scope":
        scope = SCOPE.model_copy(update={"region": "NV"})
    elif change == "case":
        case_id = "other-case"
        goal["case_id"] = case_id
    elif change:
        goal[{"goal_case": "case_id", "goal_owner": "owner", "goal_message": "message"}[change]] = "other"
    with bind_task(task), bind_policy_scope(scope), bind_run(run):
        assert (pc.current_request(message, case_id=case_id) is not None) is (change in {None, "goal_case", "goal_owner", "goal_message"})
        assert pc.current_request(message) is None
        with bind_goal(goal):
            assert (pc.current_request(message) is not None) is (change is None)
            assert should_query_business_data(message, IntentCategory.ORDER_STATUS) is (change is not None)


async def test_unsigned_empty_or_topK_miss_cannot_become_qualified_absence(tmp_path):
    store = LocalStore(str(tmp_path / "authority.db"))
    kb = KnowledgeBase(store, Embedder(dim=128))
    message = "我在整理 CA 地区 Panel-X 客服答复。按所选日期和适用范围，返修登记的办理步骤有什么规定？请说明有效版本并保留引用。"
    task = pc.make_task(message, context(), "run-control", 1)
    empty = {"attempted": True, "policy_lookup": True, "lookup_status": "empty", "evidence_ok": None,
             "degraded": False, "sources": [], "lookup_scope": task["scope"]}
    try:
        assert pc.proposal(task, empty) is None
        with bind_task(task), bind_policy_scope(SCOPE):
            assert await kb.bind_policy_projection(task, empty) is None  # Unknown identity stays unqualified.
        draft = copy.deepcopy(DOCUMENT)
        draft["policy"]["status"] = "draft"
        await kb.import_documents([draft])
        with bind_task(task), bind_policy_scope(SCOPE):
            signed = await kb.bind_policy_projection(task, empty)
        result = pc.proposal(task, signed)
        assert result and valid_fact_header(result) and not result["knowledge_sources"]
        assert set(result["fact_receipt"]["coverage"]) == {"lookup_observation"}
        assert result["fact_receipt"]["proof"]["subject_identity"]["documents"]
        assert "document" not in result["fact_receipt"]["proof"]["subject_identity"]
        assert "本次未检索到" in result["response"] and not result.get("answer_confidence")
        assert await kb.validate_policy_projection(result["fact_receipt"], [])
        await kb.import_documents([DOCUMENT])
        assert not await kb.validate_policy_projection(result["fact_receipt"], [])
        with bind_task(task), bind_policy_scope(SCOPE):
            assert await kb.bind_policy_projection(task, empty) is None
        for change in ("degraded", "has_risk", "evidence_ok", "lookup_scope", "conflicts", "lookup_status", "attempted", "policy_lookup"):
            bad = copy.deepcopy(signed)
            bad[change] = {"evidence_ok": False, "lookup_scope": {}, "conflicts": ["unresolved"],
                           "lookup_status": "conflict", "attempted": False, "policy_lookup": False}.get(change, True)
            assert pc.proposal(task, bad) is None
        forged = copy.deepcopy(result)
        forged["knowledge"]["_policy_dispatch"]["signature"] = "forged"
        assert not valid_fact_header(forged)
    finally:
        await store.close()


@pytest.mark.parametrize("identity", ["unrelated", "region", "audience", "product", "legacy", "deleted"])
async def test_unknown_or_unauthorized_catalog_identity_never_qualifies_empty(tmp_path, identity):
    store = LocalStore(str(tmp_path / "identity.db"))
    kb = KnowledgeBase(store, Embedder(dim=128))
    message = "请说明Panel-X的镜向文件复原政策的办理步骤、有效版本和引用。"
    task = pc.make_task(message, context(), "unknown-control", 1)
    assert task
    empty = {"attempted": True, "policy_lookup": True, "lookup_status": "empty", "evidence_ok": None,
             "degraded": False, "sources": [], "lookup_scope": task["scope"]}
    draft = copy.deepcopy(DOCUMENT)
    draft["title"] = "Panel-X 镜向文件复原 rev-control"
    draft["policy"]["status"] = "draft"
    if identity == "unrelated":
        draft["title"] = DOCUMENT["title"]
    elif identity in {"region", "audience", "product"}:
        draft["policy"][identity] = "unauthorized"
    elif identity == "legacy":
        draft["policy"]["version"] = "legacy-unreviewed"
        draft["title"] = "Panel-X 镜向文件复原 legacy-unreviewed"
    try:
        await kb.import_documents([draft])
        with bind_task(task), bind_policy_scope(SCOPE):
            signed = await kb.bind_policy_projection(task, empty)
        if identity == "deleted":
            assert signed
            receipt = pc.proposal(task, signed)["fact_receipt"]
            await store.kb_doc_delete(receipt["proof"]["subject_identity"]["documents"][0]["id"])
            assert not await kb.validate_policy_projection(receipt, [])
        else:
            assert signed is None
    finally:
        await store.close()


def scoped_worker_context(run, service):
    ctx = worker_context(run, PgRunStore(service.db, service.run_instance))
    ctx.scope = RunScope(run.run_id, run.attempt, service.run_instance)
    assert ctx.scope.instance != run.case_context["instance"]
    return ctx


async def controlled_case(pair, record):
    a, b = pair
    scope = PolicyScope.model_validate(record["context"]["snapshot"]["policy_scope"])
    kb = KnowledgeBase(PgStore(a.db), Embedder(dim=128))
    if record["result"]["knowledge_sources"]:
        ref = record["result"]["knowledge_sources"][0]
        await kb.import_documents([{"title": ref["title"], "content": ref["content"], "policy": ref["policy"]}])
    else:
        # A newly created synthetic identity witness, not a historical KB receipt
        # or a claim that the closed archive contains an authoritative catalog.
        parsed = pc.make_task(record["run"]["message"], record["context"], record["run"]["run_id"], 1)
        await kb.import_documents([{"title": scope.product + " " + parsed["subject"] + " identity-control",
            "content": "受控未发布目录项，仅供核对主题身份，不提供可发布的政策事实。",
            "policy": {"status": "draft", "version": "identity-control", "product": scope.product,
                       "region": scope.region, "audience": scope.audience, "effective_at": None, "expires_at": None}}])
    async def provider(actor_id, instance, **_):
        return ACTOR if actor_id == ACTOR.actor_id and instance == ACTOR.instance else None
    a.actors = b.actors = ActorDirectory(provider)
    a.knowledge_base = b.knowledge_base = kb
    accepted = await a.accept_input(ACTOR, CaseInput(message_id="closed-policy-control", conv_id="closed-policy-control",
        new_case=True, issue_type="general", message=record["run"]["message"], policy_scope=scope))
    run = await claim(a, accepted)
    deps = replace(dependencies(Tools(kb), intent=rebuild_request(record["events"]).request.intent),
                   bind_policy_projection=kb.bind_policy_projection)
    prepared = []
    async def prepare(run, snapshot, degrade):
        value = await prepare_turn(run.message, user_id=run.user_id, conv_id=run.conv_id,
            request_id=run.request_id, deps=deps, case_context=snapshot)
        prepared.append(value)
        task = (value.request.lookup_goal or {}).get("fact_task")
        assert task, {"lookup_goal": value.request.lookup_goal}
        assert value.request.turn.policy_only
        assert pc.proposal(task, value.evidence["knowledge"]), {"knowledge": value.evidence["knowledge"], "task": task}
        assert _orchestrator().fact_preflight(value.request), {"intent": value.request.intent, "confidence": value.request.intent_confidence,
                                                           "clarify": _orchestrator()._needs_clarification(value.request)}
        return value
    answer = AsyncMock(side_effect=AssertionError("qualified policy outcome must precede the answer model"))
    executor = CaseExecutor(a, prepare=prepare, answer=answer, complete_facts=_orchestrator().complete_facts,
                            event_log=EventLog.disabled())
    return a, b, kb, run, executor, answer, prepared


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
@pytest.mark.parametrize("record", RECORDS, ids=lambda r: r["id"])
async def test_closed_request_uses_real_store_and_adoption_before_answer_model(pair, record):
    a, b, kb, run, executor, answer, prepared = await controlled_case(pair, record)
    result = (await executor(scoped_worker_context(run, a))).result
    answer.assert_not_awaited()
    assert result["case_applied"] and result["answer_origin"] == pc.ORIGIN and result["answer_model_invoked"] is False
    assert not result["handoff"] and "answer_confidence" not in result and not result["business_data"]
    assert prepared[0].request.turn.policy_only and prepared[0].request.message == run.message
    assert prepared[0].evidence["business_data"]["attempted"] is False
    case = await b.get(ACTOR, result["case_id"])
    draft = case["drafts"][0]
    assert draft["valid"]
    await b.approve(ACTOR, case["id"], draft["id"], DraftApproval(expected_version=case["version"],
        expected_control_version=case["control_version"], body_hash=draft["body_hash"]))
    assert await scalar(a.db, "SELECT count(*) FROM case_actions WHERE kind='handoff'") == 0


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
@pytest.mark.parametrize("change", ["signature", "scope", "acl", "checkpoint", "new_source", "identity_scope"])
async def test_empty_observation_rechecks_current_authority_at_adoption(pair, change):
    from app.core.policy_scope import PolicyMetadata
    a, _, kb, run, executor, answer, _ = await controlled_case(pair, RECORDS[0])
    complete = a.complete
    async def intercept(run, result):
        if change == "signature":
            result["fact_receipt"]["proof"]["signature"] = "forged"
        elif change == "scope":
            await a.db.execute("UPDATE ticket_case_state SET policy_scope=jsonb_set(policy_scope,'{region}','\"NV\"')")
        elif change == "acl":
            a.actors = ActorDirectory(AsyncMock(return_value=None))
        elif change == "checkpoint":
            await a.db.execute("UPDATE agent_runs SET checkpoint=checkpoint-'case_proposal_origin' WHERE run_id=$1", run.run_id)
        elif change == "identity_scope":
            await a.db.execute("UPDATE policy_documents SET policy=jsonb_set(policy,'{audience}','\"other\"')")
        else:
            scope = PolicyScope.model_validate(run.case_context["snapshot"]["policy_scope"])
            policy = PolicyMetadata(status="published", version="new-control", product=scope.product,
                region=scope.region, audience=scope.audience, effective_at="2020-01-01T00:00:00Z")
            await kb.import_documents([{"title": scope.product+" 取消订单 new-control", "content": "处理时限为3个工作日。",
                                        "policy": policy.model_dump(mode="json")}])
        return await complete(run, result)
    a.complete = intercept
    result = (await executor(scoped_worker_context(run, a))).result
    answer.assert_not_awaited()
    assert not result["case_applied"]
    assert await scalar(a.db, "SELECT count(*) FROM case_drafts") == 0
