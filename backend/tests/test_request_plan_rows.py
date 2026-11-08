"""Consumer controls: omitted fields in actual rows are not proven NULL values."""

import copy

import pytest
from evidence_fixtures import with_evidence
from test_request_plan import Tools, diagnosed, payload

from app.core.business_facts import verified_cells, verified_nulls
from app.core.lookup_scope import bind_goal, make_goal
from app.core.request_plan import coverage_note, make_request_plan
from app.tools.turn_tools import delegate_order_query

REF = "PEER-314"
MESSAGE = f"查询订单 {REF} 的当前工序和生产状态。"


def work_orders(rows):
    columns = ["id"] if "id" in rows[0] else []
    columns += ["order_no", "current_stage", "production_status"]
    return payload(
        "prod_db", "work_order", rows, sql=f"SELECT {','.join(columns)} FROM work_order WHERE order_no='{REF}'"
    )


@pytest.mark.parametrize("has_id", [True, False])
@pytest.mark.parametrize("omission", ["single_role", "one_of_two", "both_roles"])
def test_each_requested_role_must_cover_every_actual_returned_row(has_id, omission):
    rows = [{"order_no": REF, "current_stage": "包装", "production_status": "生产中"}, {"order_no": REF}]
    if has_id:
        rows[0]["id"], rows[1]["id"] = 11, 22
    if omission == "one_of_two":
        rows[1]["current_stage"] = "包装"
    data = work_orders(rows)
    before = copy.deepcopy(data)
    cells, issues = verified_cells(data)
    assert cells and issues == [] and verified_nulls(data) == []
    message = f"查询订单 {REF} 的当前工序。" if omission == "single_role" else MESSAGE
    checked = diagnosed(make_request_plan(message), data)
    assert not checked["complete"] and "row_coverage_incomplete" in checked["reasons"]
    missing = ["production_status"] if omission == "one_of_two" else list(checked["roles"])
    for role in missing:
        item = checked["roles"][role]
        assert item["status"] == "omitted" and item["evidence_ids"]
        assert {r[3] for r in item["missing_rows"]} == {1}
        assert {r[3] for r in item["witnesses"]} == {0}
    if omission == "one_of_two":
        assert checked["roles"]["current_stage"]["status"] == "value"
        assert "missing_rows" not in checked["roles"]["current_stage"]
    assert coverage_note(checked) and data == before and verified_cells(data) == (cells, issues)
    if not has_id:
        assert all("id" not in r for r in rows) and "SELECT id" not in data["steps"][0]["final_sql"]


@pytest.mark.parametrize("has_id", [True, False])
def test_complete_multirow_does_not_require_an_internal_primary_key(has_id):
    rows = [{"order_no": REF, "current_stage": "包装", "production_status": "生产中"} for _ in range(2)]
    if has_id:
        rows[0]["id"], rows[1]["id"] = 11, 22
    checked = diagnosed(make_request_plan(MESSAGE), work_orders(rows))
    assert checked["complete"] and coverage_note(checked) == ""
    for role in checked["roles"].values():
        assert {r[3] for r in role["witnesses"]} == {0, 1} and "missing_rows" not in role


@pytest.mark.parametrize("first_value", [None, "2030-02-18"])
@pytest.mark.parametrize("omitted", [False, True])
def test_permitted_null_observations_cover_rows_but_omitted_fields_do_not(first_value, omitted):
    rows = [
        {"order_no": REF, "delivered_date": first_value, "delivery_status": "配送中"},
        {"order_no": REF, "delivery_status": "配送中"},
    ]
    if not omitted:
        rows[1]["delivered_date"] = None
    data = payload(
        "svc_db",
        "delivery",
        rows,
        sql=f"SELECT order_no,delivered_date,delivery_status FROM delivery WHERE order_no='{REF}'",
    )
    cells, issues = verified_cells(data)
    assert cells and not issues
    nulls = verified_nulls(data)
    assert any(c["row_index"] == 1 and c["field"] == "delivered_date" for c in nulls) is (not omitted)
    checked = diagnosed(make_request_plan(f"查询订单 {REF} 的实际送达日期和配送状态。"), data)
    assert checked["complete"] is (not omitted)
    role = checked["roles"]["actual_delivery_date"]
    if omitted:
        assert role["status"] == "omitted" and {r[3] for r in role["missing_rows"]} == {1}
    else:
        assert role["status"] == ("proven_null" if first_value is None else "value")
        assert {r[3] for r in role["witnesses"]} == {0, 1} and "missing_rows" not in role
    assert checked["roles"]["delivery_status"]["status"] == "value"


def test_each_independent_source_covers_its_own_actual_rows():
    a = payload(
        "erp_db",
        "orders",
        [{"order_no": REF, "delivery_date": "2030-02-18"} for _ in range(2)],
        sql=f"SELECT order_no,delivery_date FROM orders WHERE order_no='{REF}'",
    )
    b = payload(
        "svc_db",
        "delivery",
        [{"order_no": REF, "delivery_status": "已送达"} for _ in range(3)],
        sql=f"SELECT order_no,delivery_status FROM delivery WHERE order_no='{REF}'",
    )
    data = with_evidence({**a, "steps": [*a["steps"], *b["steps"]]})
    checked = diagnosed(make_request_plan(f"查询订单 {REF} 的计划交付日期和配送状态。"), data)
    assert checked["complete"]
    assert {r[3] for r in checked["roles"]["planned_delivery_date"]["witnesses"]} == {0, 1}
    assert {r[3] for r in checked["roles"]["delivery_status"]["witnesses"]} == {0, 1, 2}


async def test_row_omission_keeps_original_verified_facts_and_source_usability():
    data = work_orders(
        [
            {"id": 11, "order_no": REF, "current_stage": "包装", "production_status": "生产中"},
            {"id": 22, "order_no": REF},
        ]
    )
    cells, issues = verified_cells(data)
    plan = make_request_plan(MESSAGE)
    goal = make_goal(MESSAGE, entities=plan["lookup_entities"], owner="scope-run", case_id="selected-case")
    goal["request_plan"] = plan
    tools, trace = Tools(data), {}
    with bind_goal(goal):
        text, usable = await delegate_order_query(tools, MESSAGE, trace=trace)
    assert (
        usable
        and trace["outcome"] == "evidence"
        and trace["evidence"] == cells
        and trace["evidence_issues"] == issues == []
    )
    assert not trace["request_coverage"]["complete"] and "包装" in text and "生产中" in text
    assert coverage_note(trace["request_coverage"]) in text and len(tools.calls) == 1
    assert not any(k in trace for k in ["handoff", "needs_clarification", "fact_task", "fact_receipt"])
