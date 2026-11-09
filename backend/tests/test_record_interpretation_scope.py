"""Different recorded attributes do not imply a business-state invariant."""
import pytest
from conftest import ScriptedModel, make_request, text_step
from evidence_fixtures import with_evidence
from test_agent_tool_loop import _orchestrator
from test_case_followups import Tools

from app.agents.agent_orchestrator import AgentType
from app.core.business_facts import verified_cells
from app.core.intent_recognizer import IntentCategory
from app.tools.turn_tools import delegate_order_query


@pytest.mark.parametrize("marker", ["no", "yes"])
async def test_recorded_attributes_are_reported_without_invented_cross_field_rules_and_yes_is_retained(marker):
    data = with_evidence({"queried_database": True, "has_risk": False, "degraded": False,
        "selected_databases": ["prod_db"], "trace_id": "interpretation-query", "run_id": "interpretation-run",
        "steps": [{"database": "prod_db", "success": True,
                   "final_sql": "SELECT order_no,current_stage,production_status FROM work_order WHERE order_no='CONTROL-83'"}]},
        rows=[{"order_no": "CONTROL-83", "current_stage": "cutting", "production_status": "completed"}])
    trace = {}
    text, used = await delegate_order_query(Tools(data), "查询 CONTROL-83 的工序和生产状态", user_id="u", conv_id="c", trace=trace)
    assert used and not verified_cells(data)[1]
    model = ScriptedModel(text_step("工序为 cutting，生产状态为 completed。\n[[handoff:" + marker + "]]"))
    req = make_request("查询 CONTROL-83 的工序和生产状态", IntentCategory.ORDER_STATUS,
                       context=text, grounding_sources=[text], fact_evidence={"business_data": trace})
    result = await _orchestrator((AgentType.ORDER, model)).run(req)
    assert "cutting" in result.response and "completed" in result.response
    assert result.handoff is (marker == "yes")
    system = model.requests[0]["system"]
    assert "未提供二者的联动约束" in str(system)
    assert "同一实体、同一口径的值确实矛盾时才按冲突处理" in str(system)


def test_same_field_conflicting_values_remain_an_actual_evidence_conflict():
    sql = "SELECT order_no,production_status FROM work_order"
    data = with_evidence({"queried_database": True, "has_risk": False, "degraded": False,
        "selected_databases": ["prod_db"], "trace_id": "query", "run_id": "run",
        "steps": [{"database": "prod_db", "success": True, "final_sql": sql}]},
        rows=[{"order_no": "CONTROL-83", "production_status": "completed"},
              {"order_no": "CONTROL-83", "production_status": "not_started"}])
    assert verified_cells(data) == ([], ["conflicting_records"])
