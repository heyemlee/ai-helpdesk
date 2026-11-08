"""Readonly Case answers cannot turn a model declaration into a durable action.

These deterministic boundary replays cover the seven cross-message failures;
they make no prediction about a supplier model or a fresh full54 campaign.
"""
import copy
import uuid
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from conftest import make_request, stub_agents
from fastapi import HTTPException
from test_case_followups import Tools, dependencies
from test_cases import ACTOR, PG, action, claim, delivery, follow, scalar, worker_for
from test_cases import pair as pair  # noqa: F401
from test_request_plan import payload

from app.agents.agent_orchestrator import AgentType, OrchestratorResult
from app.agents.turn_prep import _case_data_query, prepare_turn
from app.core.fact_completion import _PHRASES
from app.core.intent_recognizer import IntentCategory, UrgencyLevel
from app.core.request_plan import make_request_plan, query_guidance
from app.events import EventLog
from app.tickets.case_execution import CaseExecutor
from app.tickets.cases import CaseInput, DraftApproval
from app.tickets.query_handoff import read_only_staff_query, restrict_query_handoff

ORDER = "CONTROL-842"
SECOND = "CONTROL-963"
BOUND = f"编号确定为 {ORDER}，核验其状态字段。"
REFERENCE = "沿用上文编号，这笔业务在系统中的状态值是什么？"
RETURN = "切回第一个工单；只汇报它对应记录的状态。"
CORRECTION = f"{ORDER} 的现场材料有更新，重新核验状态后出审核稿。"
PRODUCTION = f"另开一案，检索业务 {SECOND} 对应的生产记录状态值。"


def context(*, ref=ORDER, operation="continued"):
    return {"case_id": "selected-case", "message_id": "original-input", "snapshot": {
        "order_refs": [ref], "input_receipt": {"case_id": "selected-case", "message_id": "original-input", "operation": operation},
    }}


def proposal(**changes):
    return {"handoff": True, "escalated": True, "agent_type": "order",
            "answer_handoff_declared": True, "answer_confidence": .65,
            "handoff_reason": "Agent 自评置信度 0.65 低于 0.7", "response": "unchanged draft",
            "handoff_review": {"basis": "unresolved_answer", "outcome": "retained"}, **changes}


@pytest.mark.parametrize("message,ref,operation", [
    (BOUND, ORDER, "continued"), (REFERENCE, ORDER, "continued"), (RETURN, ORDER, "continued"),
    (CORRECTION, ORDER, "continued"), (PRODUCTION, SECOND, "created"),
    (f"查询订单 {ORDER} 的订单状态，不要转人工。", ORDER, "continued"),
    (f"Please show order status for order {ORDER}.", ORDER, "created"),
])
def test_closed_staff_queries_only_authorize_query_and_draft(message, ref, operation):
    saved = context(ref=ref, operation=operation)
    original = proposal()
    before = copy.deepcopy(original)
    result = restrict_query_handoff(original, message=message, context=saved)
    assert result["handoff"] is False and result["escalated"] is False and result["handoff_reason"] == ""
    assert result["response"] == original["response"] and result["answer_confidence"] == .65
    assert result["handoff_review"]["outcome"] == "not_authorized_for_read_only_case"
    assert original == before


@pytest.mark.parametrize("confidence,basis", [(0., "direct"), (.65, "unresolved_answer"), (1., "answer_quality")])
def test_model_scores_and_routing_review_labels_do_not_authorize_a_readonly_case_action(confidence, basis):
    result = restrict_query_handoff(proposal(answer_confidence=confidence, handoff_review={"basis": basis}), message=BOUND, context=context())
    assert result["handoff"] is False


def test_older_model_checkpoint_remains_model_identifiable_without_optional_review():
    value = proposal()
    del value["handoff_review"]
    del value["answer_handoff_declared"]
    assert restrict_query_handoff(value, message=BOUND, context=context())["handoff"] is False


@pytest.mark.parametrize("field", [phrase for phrases in _PHRASES.values() for phrase in phrases if phrase.isascii()])
def test_english_capability_fields_and_sentence_period_keep_the_selected_reference(field):
    message = f"Please show {field} for order {ORDER}."
    assert read_only_staff_query(message, context(operation="created"))
    assert not restrict_query_handoff(proposal(), message=message, context=context(operation="created"))["handoff"]


@pytest.mark.parametrize("suffix", [".9", ".other", "-other", "_other"])
def test_sentence_punctuation_fix_never_accepts_a_longer_foreign_reference(suffix):
    message = f"Please show order status for order {ORDER}{suffix}."
    assert not read_only_staff_query(message, context())
    original = proposal()
    assert restrict_query_handoff(original, message=message, context=context()) is original


@pytest.mark.parametrize("agent", ["handoff", "system"])
def test_serialized_none_and_review_presence_do_not_prove_an_answer_model_ran(agent):
    original = proposal(agent_type=agent, answer_handoff_declared=None, handoff_review={"basis": "direct"})
    assert restrict_query_handoff(original, message=BOUND, context=context()) is original


@pytest.mark.parametrize("message", [
    BOUND + "请转人工客服。", BOUND + "并执行审批。", BOUND + "取消这笔订单。",
    BOUND + "如果未发货就申请退款。", BOUND + "并调查签收争议。",
    "图片核对后继续", "这项操作还需要进一步处理。", "查询订单 FOREIGN-22 的状态。",
])
def test_explicit_staff_actions_and_unknown_scopes_keep_the_existing_handoff_workflow(message):
    original = proposal()
    assert not read_only_staff_query(message, context())
    assert restrict_query_handoff(original, message=message, context=context()) is original


@pytest.mark.parametrize("change", ["wrong_case", "wrong_message", "wrong_operation", "foreign_reference"])
def test_selection_receipt_and_selected_case_are_not_inferred_from_model_or_other_case(change):
    saved = context()
    if change == "wrong_case":
        saved["snapshot"]["input_receipt"]["case_id"] = "other-case"
    elif change == "wrong_message":
        saved["snapshot"]["input_receipt"]["message_id"] = "other-input"
    elif change == "wrong_operation":
        saved["snapshot"]["input_receipt"]["operation"] = "approved"
    else:
        saved["snapshot"]["order_refs"] = [SECOND]
    assert not read_only_staff_query(BOUND, saved)


@pytest.mark.parametrize("message", [BOUND, CORRECTION, PRODUCTION, REFERENCE, RETURN])
def test_parser_gaps_are_never_forwarded_as_user_field_requirements(message):
    ref = SECOND if message == PRODUCTION else ORDER
    plan = make_request_plan(message, case_refs=[ref], issue_type="order")
    assert "unparsed_request" in plan["unresolved_roles"]  # remains diagnostic
    query = _case_data_query(message, case_refs=[ref], request_plan=plan, issue_type="order")
    assert query.endswith(message) and "尚未明确" not in query
    assert "原问所需字段" not in query and ref in query
    assert not plan["required_roles"] and plan["planning_only"]


def test_positive_field_hints_do_not_hide_unparsed_residual_requests():
    message = f"查询订单 {ORDER} 的订单状态，同时说明采购价格。"
    plan = make_request_plan(message, case_refs=[ORDER])
    assert "unparsed_request" in plan["unresolved_roles"] and plan["required_roles"] == ["order_status"]
    assert "尚未明确" not in query_guidance(plan)
    assert _case_data_query(message, request_plan=plan).endswith(message)


class QueryTools(Tools):
    async def call(self, name, params, **kwargs):
        if name == "order_data_query":
            # This checks the exact boundary that contaminated the actual SQL
            # self-check input; it does not simulate a model quality score.
            assert "原问所需字段：尚未明确" not in params["query"]
            ref = SECOND if SECOND in params["query"] else ORDER
            database, table, field = ("prod_db", "work_order", "production_status") if ref == SECOND else ("erp_db", "orders", "order_status")
            self.data = payload(database, table, [{"order_no": ref, field: "生产中"}])
            self.data["steps"][0]["final_sql"] = f"SELECT order_no,{field} FROM {table} WHERE order_no='{ref}'"
            # Keep the returned row/hash/column binding coherent for the chosen
            # Case, using the existing fixture builder after the SQL changes.
            from test_date_meaning_rendering import rebuild
            self.data = rebuild(self.data)
            failure = getattr(self, "failure_kind", "")
            if failure == "risk":
                self.data["has_risk"] = True
                self.data["steps"][0]["risk_findings"] = [{"anomaly": "semantic_mismatch", "severity": "block", "detail": "Different requested scope"}]
            elif failure == "partial":
                self.data["execution_status"] = "partial_success"
                self.data["selected_databases"].append("svc_db")
                self.data["steps"].append({"database": "svc_db", "success": False, "rows": [], "row_count": 0,
                                           "error": "Unavailable delivery source", "truncated": False,
                                           "final_sql": "SELECT order_no FROM delivery", "column_units": {}})
        return await super().call(name, params, **kwargs)


async def execute(service, receipt, tools):
    deps = dependencies(tools)

    async def prepare(run, snapshot, degrade):
        return await prepare_turn(run.message, user_id=run.user_id, conv_id=run.conv_id, request_id=run.request_id,
                                  deps=deps, case_context=snapshot)

    answer = AsyncMock(return_value=OrchestratorResult(
        request_id="model-proposal", response="需要人工处理。", agent_type=AgentType.ORDER,
        intent=IntentCategory.ORDER_STATUS, primary_agent=AgentType.ORDER, routing_confidence=.99,
        handoff=True, escalated=True, answer_handoff_declared=True, answer_confidence=.65,
        handoff_reason="Agent 自评置信度 0.65 低于 0.7"))
    executor = CaseExecutor(service, prepare=prepare, answer=answer, event_log=EventLog.disabled())
    worker = worker_for(service, executor)
    await worker._handle(await delivery(worker, receipt["run_id"]), False)
    return await service.run(receipt["run_id"])


async def input_to(service, case_id=None, *, message, ref=None, facts=None):
    options = dict(message=message, order_refs=[ref]) if ref else dict(message=message)
    if facts:
        options["facts"] = facts
    body = follow(await service.get(ACTOR, case_id), **options) if case_id else CaseInput(
        message_id=uuid.uuid4().hex, conv_id="shared-conversation", new_case=True, issue_type="order", **options)
    return await service.accept_input(ACTOR, body)


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
@pytest.mark.parametrize("flow", ["reference-a", "multiple-cases-a", "correction-a", "reopen", "reference-b", "multiple-cases-b", "correction-b"],
                         ids=["cross002", "cross003", "cross004", "cross005", "cross007", "cross008", "cross009"])
async def test_cross_message_queries_publish_current_facts_without_first_unauthorized_action(pair, flow):
    a, _ = pair
    tools = QueryTools({})
    first = await input_to(a, message="我要跟踪一笔业务；暂时缺少编号。")
    waiting = await execute(a, first, tools)
    assert waiting.result["case_status"] == "waiting_for_info"
    bound = await input_to(a, first["case_id"], message=BOUND, ref=ORDER)
    answered = await execute(a, bound, tools)
    assert answered.result["business_data"]["outcome"] == "evidence" and "生产中" in answered.result["response"]
    if flow.startswith("multiple"):
        secondary = await input_to(a, message=PRODUCTION, ref=SECOND)
        second = await execute(a, secondary, tools)
        assert second.result["case_id"] != first["case_id"] and not second.result["handoff"]
        current = await execute(a, await input_to(a, first["case_id"], message=RETURN), tools)
        assert SECOND not in current.result["response"]
    elif flow.startswith("correction"):
        old = (await a.get(ACTOR, first["case_id"]))["drafts"][0]
        receipt = await input_to(a, first["case_id"], message=CORRECTION, facts={"site_note": "A new count was supplied"})
        current = await execute(a, receipt, tools)
        with pytest.raises(HTTPException) as error:
            await a.approve(ACTOR, first["case_id"], old["id"], DraftApproval(
                expected_version=old["version"], expected_control_version=old["control_version"], body_hash=old["body_hash"]))
        assert error.value.status_code == 409
        case = await a.get(ACTOR, first["case_id"])
        draft = case["drafts"][0]
        approved = await a.approve(ACTOR, case["id"], draft["id"], DraftApproval(
            expected_version=case["version"], expected_control_version=case["control_version"], body_hash=draft["body_hash"]))
        assert approved["drafts"][0]["status"] == "approved"
    elif flow == "reopen":
        case = await a.get(ACTOR, first["case_id"])
        case = await a.mutate(ACTOR, case["id"], action(case, "status", status="resolved"))
        await a.mutate(ACTOR, case["id"], action(case, "reopen", reason="New records"))
        current = await execute(a, await input_to(a, first["case_id"], message=BOUND), tools)
    else:
        current = await execute(a, await input_to(a, first["case_id"], message=REFERENCE), tools)
    assert current.result["case_applied"] and current.result["business_data"]["evidence"]
    assert "生产中" in current.result["response"] and not current.result["handoff"]
    assert current.result["handoff_review"]["outcome"] == "not_authorized_for_read_only_case"
    replay, applied = await a.complete(current, current.checkpoint["case_proposal"])
    assert applied and replay == current.result
    assert await scalar(a.db, "SELECT count(*) FROM case_actions") == 0
    assert await scalar(a.db, "SELECT count(*) FROM ticket_notifications") == 0


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
@pytest.mark.parametrize("blocked", ["risk", "partial"])
async def test_missing_evidence_stays_unverified_without_authorizing_a_model_action(pair, blocked):
    a, _ = pair
    receipt = await input_to(a, message=BOUND, ref=ORDER)
    tools = QueryTools({})
    tools.failure_kind = blocked
    run = await execute(a, receipt, tools)
    result = run.result
    assert result["case_applied"] and result["business_data"]["evidence_issues"]
    assert not result["handoff"] and result["business_data"]["evidence"] == [] and "生产中" not in result["response"]
    assert result["business_data"]["evidence_payload"]["steps"][0]["rows"][0]["order_status"] == "生产中"
    assert await scalar(a.db, "SELECT count(*) FROM case_actions") == 0


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
async def test_persisted_staff_request_overrides_mutated_worker_context_and_actions_stay_idempotent(pair):
    a, _ = pair
    receipt = await input_to(a, message=BOUND + "请转人工。", ref=ORDER)
    run = await claim(a, receipt)
    run.message = BOUND
    result, applied = await a.complete(run, proposal())
    assert applied and result["handoff"]
    await a.complete(run, proposal())
    assert await scalar(a.db, "SELECT count(*) FROM case_actions") == 1


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
async def test_query_action_boundary_cannot_bypass_new_case_control_version(pair):
    a, b = pair
    receipt = await input_to(a, message=BOUND, ref=ORDER)
    run = await claim(a, receipt)
    case = await b.get(ACTOR, receipt["case_id"])
    await b.mutate(ACTOR, case["id"], action(case, "takeover"))
    result, applied = await a.complete(run, proposal())
    assert not applied and not result["handoff"] and result["draft_id"] is None
    assert result["case_rejection"] == "case_control_changed"
    assert await scalar(a.db, "SELECT count(*) FROM case_actions") == 0


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
async def test_actual_critical_upstream_route_without_answer_model_keeps_its_case_action(pair, orchestrator, recognizer):
    a, _ = pair
    reference = "URGENT-842"
    message = f"查询订单 {reference} 的订单状态。"
    receipt = await input_to(a, message=message, ref=reference)
    tools = Tools(payload("erp_db", "orders", [{"order_no": reference, "order_status": "生产中"}],
                          sql=f"SELECT order_no,order_status FROM orders WHERE order_no='{reference}'"))
    # The unchanged real recognizer takes its rule fast path and derives
    # CRITICAL from this valid identifier. Never stub the urgency or final route.
    deps = replace(dependencies(tools), recognize_intent=recognizer.recognize)
    recognizer._llm_recognize = AsyncMock(side_effect=AssertionError("Rule fast path must not call an intent model"))
    recognizer._embedding_recognize = AsyncMock(side_effect=AssertionError("Rule fast path must not embed"))
    orchestrator._execute = AsyncMock(side_effect=AssertionError("Direct route must not invoke an answer agent"))
    requests = []

    async def prepare(run, snapshot, degrade):
        prepared = await prepare_turn(run.message, user_id=run.user_id, conv_id=run.conv_id, request_id=run.request_id,
                                      deps=deps, case_context=snapshot)
        requests.append(prepared.request)
        return prepared

    executor = CaseExecutor(a, prepare=prepare, answer=orchestrator.run, event_log=EventLog.disabled())
    worker = worker_for(a, executor)
    await worker._handle(await delivery(worker, receipt["run_id"]), False)
    run = await a.run(receipt["run_id"])
    assert run.status == "succeeded" and run.result["case_applied"]
    assert read_only_staff_query(message, run.case_context)
    assert requests[0].urgency is UrgencyLevel.CRITICAL and recognizer.fast_path_hits == 1
    assert orchestrator._execute.await_count == 0
    assert recognizer._llm_recognize.await_count == recognizer._embedding_recognize.await_count == 0
    assert run.result["agent_type"] == "handoff" and run.result["answer_handoff_declared"] is None
    assert run.result["handoff_review"]["basis"] == "direct" and run.result["handoff_review"]["outcome"] == "retained"
    assert run.result["handoff"] and run.result["escalated"]
    replay, applied = await a.complete(run, run.checkpoint["case_proposal"])
    assert applied and replay == run.result
    assert await scalar(a.db, "SELECT count(*) FROM case_actions") == 1
    assert await scalar(a.db, "SELECT count(*) FROM ticket_notifications") == 1


async def test_non_case_orchestrator_keeps_its_existing_model_handoff(orchestrator):
    stub_agents(orchestrator, "需要人工处理。\n[[handoff:yes]]")
    result = await orchestrator.run(make_request(BOUND, IntentCategory.ORDER_STATUS))
    assert result.handoff and result.answer_handoff_declared is True and result.answer_confidence == .65
