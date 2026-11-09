"""Readonly Case answers cannot turn a model declaration into a durable action.

A staff message that looks something up and asks for no action is read-only,
however it is phrased; any request to act keeps the existing workflow.

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
PARTS = f"订单 {ORDER} 的补件申请现在什么状态？补件数量也请查清。"
PART_ROLES = {"part_name", "part_quantity", "part_status"}
MATERIAL = "Hinge BX-842-24"
STOCK = {"on_hand_qty", "allocated_qty"}


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
def test_a_longer_identifier_is_another_lookup_target_and_never_hides_an_action(suffix):
    message = f"Please show order status for order {ORDER}{suffix}."
    assert read_only_staff_query(message, context())
    original = proposal()
    escalate = f"Please escalate order {ORDER}{suffix}."
    assert restrict_query_handoff(original, message=escalate, context=context()) is original


@pytest.mark.parametrize("agent", ["handoff", "system"])
def test_serialized_none_and_review_presence_do_not_prove_an_answer_model_ran(agent):
    original = proposal(agent_type=agent, answer_handoff_declared=None, handoff_review={"basis": "direct"})
    assert restrict_query_handoff(original, message=BOUND, context=context()) is original


@pytest.mark.parametrize("message", [
    BOUND + "请转人工客服。", BOUND + "并执行审批。", BOUND + "取消这笔订单。",
    BOUND + "如果未发货就申请退款。", BOUND + "并调查签收争议。",
    "图片核对后继续", "这项操作还需要进一步处理。", "好的，谢谢。",
])
def test_explicit_staff_actions_and_messages_without_a_lookup_keep_the_existing_handoff_workflow(message):
    original = proposal()
    assert not read_only_staff_query(message, context())
    assert restrict_query_handoff(original, message=message, context=context()) is original


@pytest.mark.parametrize("message", [
    PARTS, f"请提供 {ORDER} 补发配件的件数及进度。", f"查询订单 {ORDER} 的补件状态和补件数量。",
    f"订单 {ORDER} 的补件名称是什么？", f"{ORDER} 补发配件数量是多少？",
    f"Please show replacement part status and quantity for order {ORDER}.",
])
@pytest.mark.parametrize("operation", ["created", "continued"])
def test_replacement_part_status_and_quantity_queries_do_not_authorize_a_model_handoff(message, operation):
    saved = context(operation=operation)
    assert read_only_staff_query(message, saved)
    result = restrict_query_handoff(proposal(), message=message, context=saved)
    assert result["handoff"] is False and result["escalated"] is False and result["handoff_reason"] == ""
    assert result["handoff_review"]["outcome"] == "not_authorized_for_read_only_case"
    # The boundary recognizes a parts record only where the planner plans parts roles.
    plan = make_request_plan(message, case_refs=[ORDER])
    assert set(plan["required_roles"]) & PART_ROLES and not plan["business_effects"]


@pytest.mark.parametrize("message", [
    PARTS + "请转人工。", f"订单 {ORDER} 安排补件，数量 3 件。", f"给订单 {ORDER} 补发配件。",
    f"订单 {ORDER} 的补件数量改成 5。", f"订单 {ORDER} 的补件状态改为已解决。", f"订单 {ORDER} 的补件申请加急处理。",
    f"订单 {ORDER} 补件申请状态，如果未处理就申请退款。",
    f"Please escalate the replacement part status for order {ORDER}.",
    f"订单 {ORDER} 的补件申请。", f"给订单 {ORDER} 补件 3 件。",
])
def test_replacement_part_actions_and_records_named_without_a_question_keep_the_existing_handoff_workflow(message):
    original = proposal()
    assert not read_only_staff_query(message, context())
    assert restrict_query_handoff(original, message=message, context=context()) is original


@pytest.mark.parametrize("data", [
    None, {}, {"outcome": "unverified", "evidence_issues": ["execution_metadata_inconsistent"]},
    {"outcome": "unverified", "lookup_status": "empty", "evidence": []}, {"outcome": "failed", "evidence_issues": ["failed"]},
    {"outcome": "evidence", "evidence": [{"field": "order_status"}], "evidence_issues": ["partial"]},
    {"outcome": "evidence", "evidence": []},
])
def test_unverified_evidence_is_reported_in_the_draft_not_turned_into_a_model_action(data):
    result = restrict_query_handoff(proposal(business_data=data), message=BOUND, context=context())
    assert result["handoff"] is False and result["business_data"] == data
    assert result["handoff_review"]["original_answer_confidence"] == .65


@pytest.mark.parametrize("change", [{"service_unavailable": True}, {"agent_type": "handoff", "answer_handoff_declared": None}])
def test_service_failures_and_direct_routes_still_hand_off_a_replacement_part_query(change):
    original = proposal(**change)
    assert restrict_query_handoff(original, message=PARTS, context=context()) is original


@pytest.mark.parametrize("message,refs,roles", [
    (f"系统是否有 {ORDER} 的状态记录？", [ORDER], set()),
    (f"系统里有没有 {ORDER} 的状态记录？", [ORDER], set()),
    (f"提供 {ORDER} 的登记联系电话。", [ORDER], {"registered_phone"}),
    (f"查询订单 {ORDER} 的手机号。", [ORDER], {"registered_phone"}),
    (f"Please show the registered phone for order {ORDER}.", [ORDER], {"registered_phone"}),
    (f"订单 {ORDER} 对应物料 {MATERIAL} 的现有库存和已占用库存分别是多少？", [ORDER], STOCK),
    (f"库存表中 {MATERIAL} 有多少现存与占用件数？", [], STOCK),
    (f"Please show on-hand and allocated quantities of “{MATERIAL}”.", [], STOCK),
])
@pytest.mark.parametrize("operation", ["created", "continued"])
def test_record_existence_phone_and_inventory_queries_do_not_authorize_a_model_handoff(message, refs, roles, operation):
    saved = context(operation=operation)
    saved["snapshot"]["order_refs"] = refs
    assert read_only_staff_query(message, saved)
    result = restrict_query_handoff(proposal(), message=message, context=saved)
    assert result["handoff"] is False and result["escalated"] is False
    assert result["handoff_review"]["outcome"] == "not_authorized_for_read_only_case"
    plan = make_request_plan(message, case_refs=refs)
    assert roles <= set(plan["required_roles"]) and not plan["business_effects"]


@pytest.mark.parametrize("message,refs", [
    (f"系统是否有 {ORDER} 的状态记录？没有就转人工。", [ORDER]),
    (f"提供 {ORDER} 的登记联系电话，并通知客户。", [ORDER]),
    (f"把订单 {ORDER} 的联系电话改成 13800000000。", [ORDER]),
    (f"打电话给订单 {ORDER} 的客户。", [ORDER]),
    (f"库存表中 {MATERIAL} 有多少现存？请转人工。", []),
    (f"库存表中 {MATERIAL} 请转人工 有多少现存？", []),
    (f"物料 {MATERIAL} escalate 的库存是多少？", []),
    (f"物料 refund {MATERIAL} 的库存是多少？", []),
    (f"库存表中 {MATERIAL} 有多少现存？并安排补发。", [ORDER]),
    (f"把订单 {ORDER} 的联系电话更正为 13900000000。", [ORDER]),
    (f"订单 {ORDER} 客户投诉配送延误，查下状态。", [ORDER]),
    (f"订单 {ORDER} 签收有争议，查一下配送记录。", [ORDER]),
    ("我要跟踪一笔业务；暂时缺少编号。", []),
    ("配送费用的处理时限有什么规定？", []),
    ("我在整理 CA 地区 Juniper-47 cabinets 客服答复。付款手续费的处理时限有什么规定？", []),
    (f"系统是否有 {ORDER} 的状态记录？", []),
])
def test_lookup_actions_hidden_requests_and_untargeted_messages_keep_the_existing_handoff_workflow(message, refs):
    saved = context()
    saved["snapshot"]["order_refs"] = refs
    original = proposal()
    assert not read_only_staff_query(message, saved)
    assert restrict_query_handoff(original, message=message, context=saved) is original


@pytest.mark.parametrize("message,refs", [
    # follow-ups in a multi-message Case
    ("那它现在是什么状态？", [ORDER]), ("这个单子现在到哪一步了？", [ORDER]),
    (f"刚才单号说错了，应该是 {ORDER}，请查它的订单状态。", [ORDER]),
    (f"更正：订单号是 {ORDER}，麻烦看下配送状态。", [ORDER]),
    ("重新打开这个工单，再查一次当前订单状态。", [ORDER]),
    (f"第二个工单 {SECOND} 的交付日期是多少？", [ORDER]),
    (f"{ORDER} 和 {SECOND} 的订单状态分别是什么？", [ORDER]),
    (f"请问 {ORDER} 当前的订单状态？", [ORDER]),
    # an action word that names a record is part of the question
    (f"{ORDER} 的退款状态是什么？", [ORDER]), (f"{ORDER} 的补件申请处理进度如何？", [ORDER]),
    (f"订单 {ORDER} 是否已取消？", [ORDER]),
    # records the closed grammar could not scope
    (f"查询订单 {ORDER} 的数量和进度。", [ORDER]), (f"Please show part quantity for order {ORDER}.", [ORDER]),
    ("查询订单 FOREIGN-22 的补件状态。", [ORDER]),
    (f"库存表中 {MATERIAL} 有多少现存？订单 FOREIGN-22 也查一下。", []),
    (f"订单 {SECOND} 对应物料 {MATERIAL} 的现有库存是多少？", [ORDER]),
    ("库存表中 铰链BX-842 有多少现存？", []), (f"{MATERIAL} 的库存是多少？", []),
    (f"What is the current status of {ORDER}?", [ORDER]),
])
def test_lookups_without_action_intent_do_not_authorize_a_model_handoff(message, refs):
    saved = context()
    saved["snapshot"]["order_refs"] = refs
    assert read_only_staff_query(message, saved)
    result = restrict_query_handoff(proposal(), message=message, context=saved)
    assert result["handoff"] is False and result["handoff_review"]["outcome"] == "not_authorized_for_read_only_case"


@pytest.mark.parametrize("message", [
    f"查询订单 {ORDER} 的订单状态，没有就转人工。", f"{ORDER} 的状态查完请转人工。",
    f"帮我把 {ORDER} 转给主管处理。", f"取消订单 {ORDER}。", f"{ORDER} 的数量不对，安排补发。",
    f"把 {ORDER} 的交付日期改到下周。", f"通知客户 {ORDER} 已发货。", f"{ORDER} 加急处理。",
    f"Cancel order {ORDER} and tell me the status.", f"Notify the customer about order {ORDER}.",
])
def test_any_request_to_act_keeps_the_existing_handoff_workflow(message):
    original = proposal()
    assert not read_only_staff_query(message, context())
    assert restrict_query_handoff(original, message=message, context=context()) is original


@pytest.mark.parametrize("change", ["wrong_case", "wrong_message", "wrong_operation"])
def test_selection_receipt_and_selected_case_are_not_inferred_from_model_or_other_case(change):
    saved = context()
    if change == "wrong_case":
        saved["snapshot"]["input_receipt"]["case_id"] = "other-case"
    elif change == "wrong_message":
        saved["snapshot"]["input_receipt"]["message_id"] = "other-input"
    else:
        saved["snapshot"]["input_receipt"]["operation"] = "approved"
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
