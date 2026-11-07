"""Completed development failures and independent absence/identity controls."""
import copy
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import make_request, stub_agents
from test_lookup_scope import BoundDataTools

from app.agents.agent_orchestrator import BaseAgent
from app.agents.turn_prep import _case_data_query
from app.core.answer_policy import AnswerPolicy
from app.core.business_facts import verified_lookup_receipt
from app.core.intent_recognizer import IntentCategory
from app.core.lookup_scope import make_goal
from app.tools.turn_tools import delegate_order_query


def records():
    return json.loads((Path(__file__).parent / "fixtures/completed_c6167a8_failures.json").read_text())["records"]


def empty():
    data = copy.deepcopy(records()["development-orders-012"]["business_data"]["evidence_payload"])
    data["observed_at"] = datetime.now(UTC).isoformat()
    return data


def lookup_goal():
    message = records()["development-orders-012"]["message"]
    return make_goal(message, entities={"order_id": [re.search(r"SO-[\w-]+", message)[0]]}, owner="control")


@pytest.mark.parametrize("name", list(records()))
def test_completed_handoffs_were_explicit_and_cannot_be_cleared_as_keyword_guesses(name):
    r = records()[name]
    assert r["decision"]["handoff"] and r["decision"]["answer_handoff_declared"] is True
    assert BaseAgent._split_handoff_trailer(r["raw_answers"][-1])[1] is True
    if name != "development-orders-012":
        finding = r["business_data"]["evidence_payload"]["steps"][0]["risk_findings"][0]
        assert finding["anomaly"] == "semantic_mismatch" and "schema记录键" in finding["detail"]


@pytest.mark.parametrize("message", ["逐行返回订单及型号明细", "统计每种型号的总数量", "去重列出型号", "按状态分组计数"])
def test_delegation_preserves_requested_grain_without_an_unconditional_internal_key(message):
    query = _case_data_query(message)
    assert query.endswith(message) and "内部主键只作溯源" in query
    assert "不因未投影而追加业务条件或改变粒度" in query and "所问真实业务标识" in query
    assert "明细保留schema记录键" not in query


async def test_verified_empty_receipt_binds_exact_source_scope_time_and_execution_identity():
    data, trace = empty(), {}
    g = lookup_goal()
    text, is_value_source = await delegate_order_query(BoundDataTools(data), g["message"], user_id="u", conv_id="c", trace=trace, lookup_goal=g)
    receipt = trace["lookup_evidence"]
    assert not is_value_source  # SQL IDs/timestamps must never ground a price or deadline.
    assert receipt == verified_lookup_receipt(trace["evidence_payload"])
    step = receipt["steps"][0]
    assert step["database"] == data["steps"][0]["database"]
    assert step["final_sql"] == data["steps"][0]["final_sql"] and step["row_count"] == 0
    assert step["query_hash"] == hashlib.sha256(step["final_sql"].encode()).hexdigest()
    assert trace["lookup_coverage"]["receipt"] == receipt
    assert step["final_sql"] not in text and step["as_of"] not in text
    assert "结构化证据" in text and "不是业务日期、交期或承诺" in text
    assert "存在记录" in text and "已回答该问题" in text
    assert "审批或明确人工" in text and trace["lookup_coverage"]["covers_goal"]


@pytest.mark.parametrize("change", ["failed", "risk", "truncated", "stale", "partial", "unqueried", "count_bool",
                                    "missing_source", "sql_object", "sql_number", "db_object"])
async def test_incomplete_or_failed_lookup_cannot_receive_absence_grounding(change):
    data, trace = empty(), {}
    if change == "failed":
        data["execution_status"] = "failed"
        data["steps"][0].update(success=False, error="failure")
    elif change == "risk":
        data["has_risk"] = True
    elif change == "truncated":
        data["result_truncated"] = True
    elif change == "stale":
        data["observed_at"] = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    elif change == "partial":
        data["steps"].append({**data["steps"][0], "success": False, "error": "failure"})
    elif change == "unqueried":
        data["queried_database"] = False
    elif change == "count_bool":
        data["steps"][0]["row_count"] = False
    elif change == "missing_source":
        data["steps"][0].pop("database")
    elif change == "db_object":
        data["steps"][0]["database"] = {"name": "erp_db"}
    else:
        data["steps"][0]["final_sql"] = {"sql": "SELECT 1"} if change == "sql_object" else 123
    g = lookup_goal()
    text, _ = await delegate_order_query(BoundDataTools(data), g["message"], user_id="u", conv_id="c", trace=trace, lookup_goal=g)
    assert not trace.get("lookup_evidence") and verified_lookup_receipt(data) is None
    assert "已回答该问题" not in text
    request = make_request(g["message"], IntentCategory.ORDER_STATUS, fact_evidence={"business_data": trace}, lookup_goal=g)
    assert not BaseAgent._answer_grounded(request, [])


@pytest.mark.parametrize("reply,expected", [("查询已成功，未找到记录。\n[[handoff:no]]", False),
    ("还需要人工核实。\n[[handoff:yes]]", True), ("此操作需要员工批准。\n[[handoff:yes]]", True)])
@pytest.mark.parametrize("mode", ["plain", "stream", "tools"])
async def test_typed_absence_grounding_does_not_cancel_yes_or_authorize_value_claims(orchestrator, monkeypatch, reply, expected, mode):
    data, trace = empty(), {}
    g = lookup_goal()
    text, _ = await delegate_order_query(BoundDataTools(data), g["message"], user_id="u", conv_id="c", trace=trace, lookup_goal=g)
    request = make_request(g["message"], IntentCategory.ORDER_STATUS, context=text,
                           fact_evidence={"business_data": trace}, lookup_goal=g)
    assert BaseAgent._answer_grounded(request, []) and not request.grounding_sources
    assert AnswerPolicy().enforce("保证2026年11月6日送达，费用998美元。", request.grounding_sources).blocked
    if mode == "stream":
        async def chunks(*_):
            yield reply
        for agents in orchestrator._pool.values():
            for agent in agents:
                monkeypatch.setattr(agent, "_stream_llm", chunks)
        result = [e["result"] async for e in orchestrator.run_stream(request) if e["type"] == "result"][0]
    else:
        if mode == "tools":
            from conftest import ScriptedModel, text_step
            from test_agent_tool_loop import _Tools, _turn
            request.turn = _turn(_Tools())
            request.turn.lookup_goal = g
            for agents in orchestrator._pool.values():
                for agent in agents:
                    agent._client = ScriptedModel(text_step(reply))
        else:
            stub_agents(orchestrator, reply)
        result = await orchestrator.run(request)
    assert result.handoff is expected and result.answer_handoff_declared is expected
    assert "未找到记录" in result.response
