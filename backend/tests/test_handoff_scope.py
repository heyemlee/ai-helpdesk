"""Task boundaries and durable risk handling; model replies are deterministic stubs.

These tests verify inputs and guards, not how a fresh supplier model will answer.
Recorded text comes only from the authorized completed development failures.
"""
import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import make_request, stub_agents
from evidence_fixtures import with_evidence
from test_case_followups import Tools, dependencies, prepare
from test_stage6_context import Lookup

from app.agents.agent_orchestrator import BaseAgent, OrderAgent, Request
from app.agents.turn_prep import _case_data_query
from app.core import policy_scope as ps
from app.core.business_facts import verified_cells
from app.core.intent_recognizer import IntentCategory
from app.core.skill_loader import SkillManager
from app.tools import turn_tools as tt


def recorded(name):
    return json.loads((Path(__file__).parent / "fixtures/development_handoff_scope.json").read_text())["records"][name]


def fresh_payload(record):
    data = copy.deepcopy(record["result"]["business_data"]["evidence_payload"])
    # Replay execution values, SQL and hashes exactly; advance observation only.
    data["observed_at"] = datetime.now(UTC).isoformat()
    return data


@pytest.mark.parametrize("name", ["orders-003", "policies-004", "images-001"])
def test_calibration_failures_were_explicit_model_decisions_not_keyword_guesses(name):
    record = recorded(name)
    _, declared = BaseAgent._split_handoff_trailer(record["model_answer"])
    assert declared is True and record["result"]["handoff"] is True
    if name == "orders-003":
        cells, issues = verified_cells(fresh_payload(record))
        assert len(cells) == 4 and not issues
    elif name == "policies-004":
        assert record["result"]["knowledge"]["lookup_status"] == "empty"
        assert record["result"]["knowledge"]["degraded"] is False
    else:
        data = fresh_payload(record)
        assert data["has_risk"] is True
        assert data["steps"][0]["risk_findings"][0]["anomaly"] == "semantic_mismatch"
        assert not verified_cells(data)[0]


@pytest.mark.parametrize("message", [
    "逐项核对附件的订单、型号和日期，标记不能确认的内容。",
    "Compare the uploaded delivery form against order CONTROL-82 and flag differences.",
    "帮我核对订单 CONTROL-91 的附件，并将取消申请交给人工。",
])
def test_sql_delegation_only_requests_rows_while_preserving_the_upstream_goal(message):
    query = _case_data_query(message)
    assert query.endswith(message)
    assert "仅获取业务库记录" in query and "只读" in query
    assert "不要求 SQL 服务读取附件、比对图片、标记核对结果" in query
    assert "这些步骤由上游根据返回的数据行与来源处理" in query
    assert "来源" in query and "字段、时间范围和口径" in query


@pytest.mark.parametrize("success", [True, False])
async def test_empty_policy_lookup_is_distinct_from_unavailable_and_never_grants_approval(success):
    tools, trace = Lookup([], success=success), {}
    text, used = await tt.knowledge_lookup(tools, "查阅区域退货政策", trace=trace)
    assert not used and not trace.get("sources")
    assert trace["lookup_status"] == ("empty" if success else "unavailable")
    if success:
        assert not trace.get("degraded")
        assert "已完成的限定范围检索结果" in text
        assert "不据此认定免费、获准或不存在政策" in text
        assert "要求办理业务而缺少适用依据，仍需补料或人工审核" in text
    else:
        assert trace["degraded"]
        assert "已完成的限定范围检索结果" not in text


@pytest.mark.parametrize("message", [
    "查询订单 CONTROL-54 的计划日期和配送状态。",
    "Read the planned delivery date and current status for order CONTROL-67.",
])
async def test_actual_model_input_carries_scope_rules_and_preserves_user_role(message):
    manager = SkillManager(str(Path(__file__).parent.parent / "skills"))
    manager.load()
    create = AsyncMock(return_value=SimpleNamespace(content=[SimpleNamespace(type="text", text="已说明记录。\n[[handoff:no]]")]))
    agent = OrderAgent(SimpleNamespace(messages=SimpleNamespace(create=create)), "fake", manager)
    request = Request(message=message, user_id="u", conv_id="c",
                      context="系统已有计划与状态记录；不代表新的排期承诺。")
    await agent.handle(request)
    call = create.call_args.kwargs
    system = call["system"][0]["text"]
    assert "计划日期与实际状态属于不同语义" in system
    assert "只读查询已有日期或状态" in system  # loaded business SOP as well as role prompt
    assert "新确认、承诺或变更" in system
    assert "真实冲突" in system and "用户明确要求人工" in system
    assert message not in system
    assert call["messages"][-1] == {"role": "user", "content": message}


@pytest.mark.parametrize("name", ["orders-003", "policies-004", "images-001"])
async def test_new_context_does_not_silence_a_model_that_still_requests_handoff(orchestrator, name):
    record = recorded(name)
    data = fresh_payload(record) if name != "policies-004" else {}
    tools = Tools(data)
    intent = IntentCategory.REQUEST if name == "images-001" else IntentCategory.DELIVERY
    deps = dependencies(tools, intent=intent)
    if name == "images-001":
        deps.recognize_intent.return_value.entities = {"order_id": record["snapshot"]["order_refs"][0]}
    with ps.bind_policy_scope(ps.PolicyScope.model_validate(record["snapshot"].get("policy_scope") or {})):
        turn = await prepare(record, deps)
    turn.request.turn = None  # exercise the plain-answer path, no supplier tools
    stub_agents(orchestrator, record["model_answer"])
    result = await orchestrator.run(turn.request)
    assert result.handoff and result.answer_confidence < orchestrator.AGENT_MIN_CONFIDENCE
    assert turn.request.message.endswith(record["message"])
    if name == "images-001":
        assert turn.evidence["business_data"]["evidence_issues"]
        query = next(params["query"] for name, params in tools.calls if name == "order_data_query")
        assert "不要求 SQL 服务读取附件" in query
        assert "execution_metadata_inconsistent" in turn.request.context
        assert "has_risk=true" in turn.request.context


@pytest.mark.parametrize("intent,reply", [
    (IntentCategory.HUMAN_HANDOFF, "记录已查到。\n[[handoff:no]]"),
    (IntentCategory.DELIVERY, "所请求的实际签收证明缺失，需要人工核实。\n[[handoff:yes]]"),
    (IntentCategory.DELIVERY, "同一配送记录有相互矛盾的状态，需要复核。\n[[handoff:yes]]"),
    (IntentCategory.REQUEST, "取消及退款需要员工审批。\n[[handoff:yes]]"),
])
async def test_independent_human_action_missing_fact_and_real_conflict_controls(orchestrator, intent, reply):
    stub_agents(orchestrator, reply)
    result = await orchestrator.run(make_request("订单 CONTROL-83 的相关事项", intent))
    assert result.handoff


async def test_reading_existing_values_and_healthy_negative_lookup_can_complete_without_handoff(orchestrator):
    data = with_evidence({"queried_database": True, "has_risk": False, "degraded": False,
        "trace_id": "control-existing-records", "steps": [{"database": "erp_db", "success": True,
        "final_sql": "SELECT order_no, delivery_date, order_status FROM orders",
        "rows": [{"order_no": "CONTROL-52", "delivery_date": "2031-06-18", "order_status": "已交付"}]}]})
    record = recorded("orders-003")
    record["message"] = "读取订单 CONTROL-52 的计划日期及系统状态。"
    record["snapshot"].update(order_refs=["CONTROL-52"], facts={}, messages=[])
    turn = await prepare(record, dependencies(Tools(data), intent=IntentCategory.DELIVERY))
    turn.request.turn = None
    stub_agents(orchestrator, "已列出系统记录及字段含义，计划日期不代表实际送达。\n[[handoff:no]]")
    result = await orchestrator.run(turn.request)
    assert not result.handoff and "2031-06-18" in result.response

    record = recorded("policies-004")
    record["message"] = "请查阅区域退货政策的当前已发布版本。"
    tools = Lookup([])
    turn = await prepare(record, dependencies(tools, intent=IntentCategory.DELIVERY))
    turn.request.turn = None
    stub_agents(orchestrator, "本次范围检索未找到可引用的已发布条款，无法据此认定获准。\n[[handoff:no]]")
    result = await orchestrator.run(turn.request)
    assert not result.handoff and "未检索到" in result.response
