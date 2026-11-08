"""Planner uncertainty cannot publish negative facts or hide real limitations."""

import copy
import hashlib
import json
from datetime import UTC, datetime

import pytest
from test_request_plan import ORDER, Tools, diagnosed, payload

from app.core.business_facts import render_draft, verified_cells
from app.core.lookup_scope import bind_goal, make_goal
from app.core.request_plan import coverage_note, make_request_plan
from app.tools.turn_tools import delegate_order_query


def trace(data, coverage):
    cells, issues = verified_cells(data)
    return {
        "attempted": True,
        "outcome": "evidence",
        "evidence": cells,
        "evidence_issues": issues,
        "evidence_payload": data,
        "request_coverage": coverage,
    }


def joined_order():
    data = payload(
        "erp_db", "orders",
        [{"order_no": ORDER, "contract_amount": 1170.25, "order_status": "已取消",
          "delivery_date": "2030-02-18", "model": "CAB-24", "ordered_qty": 7}],
        sql=("SELECT o.order_no,o.contract_amount,o.order_status,o.delivery_date,i.model,i.quantity AS ordered_qty "
             f"FROM orders o JOIN order_item i ON o.order_no=i.order_no WHERE o.order_no='{ORDER}'"),
    )
    data["steps"][0]["column_units"]["ordered_qty"] = "pcs"
    for cell in data["evidence"]:
        if cell["field"] == "ordered_qty":
            cell["unit"] = "pcs"
    return data


def image_facts():
    now = datetime.now(UTC).isoformat()
    return {
        field: {"source": "verified_image", "db_value": value, "unit": "pcs" if field == "quantity" else None,
                "business_source": {"observed_at": now, "as_of": now}}
        for field, value in {"order_ref": ORDER, "model": "CAB-24", "quantity": 7,
                             "date": "2030-02-18", "status": "已取消"}.items()
    }


def test_real_joined_fields_remain_visible_without_unproved_negative_claim_or_complete_upgrade():
    data = joined_order()
    plan = make_request_plan(f"查询订单 {ORDER} 合同金额与订单状态。")
    checked = diagnosed(plan, data)
    before = copy.deepcopy(data)
    assert checked["planning_only"] and not checked["complete"]
    assert {v["status"] for v in checked["roles"].values()} == {"target_unproved"}
    body = render_draft([], trace(data, checked))
    assert "1170.25 USD" in body and "已取消" in body and "7 pcs" in body
    assert "尚未核实：" not in body and "原问的完整目标" not in body
    assert data == before and checked["complete"] is False


@pytest.mark.parametrize("other_table", ["orders", "delivery"])
def test_unknown_join_ownership_stays_unproved_instead_of_wrong_target(other_table):
    data = payload(
        "erp_db", "orders", [{"order_no": ORDER, "order_status": "生产中"}],
        sql=f"SELECT d.order_no,o.order_status FROM orders o CROSS JOIN {other_table} d WHERE d.order_no='{ORDER}'",
    )
    checked = diagnosed(make_request_plan(f"查询订单 {ORDER} 的订单状态。"), data)
    assert verified_cells(data)[0] and not verified_cells(data)[1]
    assert not checked["complete"] and checked["roles"]["order_status"]["status"] == "target_unproved"
    assert "尚未核实：订单状态" not in render_draft([], trace(data, checked))


def test_missing_returned_target_anchor_is_unknown_while_a_different_observed_target_is_wrong():
    plan = make_request_plan(f"查询订单 {ORDER} 的订单状态。")
    unknown = payload("erp_db", "orders", [{"order_status": "生产中"}],
                      sql=f"SELECT order_status FROM orders WHERE order_no='{ORDER}'")
    checked = diagnosed(plan, unknown)
    assert not checked["complete"] and checked["roles"]["order_status"]["status"] == "target_unproved"
    assert "尚未核实：" not in render_draft([], trace(unknown, checked))
    other = payload("erp_db", "orders", [{"order_no": "OTHER-999", "order_status": "生产中"}])
    checked = diagnosed(plan, other)
    assert not checked["complete"] and checked["roles"]["order_status"]["status"] == "wrong_target"
    assert coverage_note(checked) in render_draft([], trace(other, checked))


def test_unparsed_image_comparison_does_not_overrule_individually_verified_image_fields():
    message = "逐项检查附件的单号、型号、数量、日期和状态，跟系统记录比对后标记不能核实的项目。"
    plan = make_request_plan(message, case_refs=[ORDER])
    data = joined_order()
    checked = diagnosed(plan, data)
    assert checked["roles"] == {} and not checked["complete"]
    assert set(checked["reasons"]) == {"target_unresolved", "roles_unsupported", "semantics_unresolved"}
    facts = image_facts()
    before = copy.deepcopy(facts)
    body = render_draft([], trace(data, checked), facts)
    assert body.count("图片已核实") == 5 and "1170.25 USD" in body
    assert "原问的完整目标" not in body and "尚未核实：" not in body
    assert facts == before and not checked["complete"]


def test_unparsed_residual_request_keeps_diagnostic_without_a_global_partial_fact():
    plan = make_request_plan(f"查询订单 {ORDER} 的订单状态，同时说明采购价格。")
    data = payload("erp_db", "orders", [{"order_no": ORDER, "order_status": "生产中"}])
    checked = diagnosed(plan, data)
    assert not checked["complete"] and "semantics_unresolved" in checked["reasons"]
    body = render_draft([], trace(data, checked))
    assert "生产中" in body and "原问的完整目标" not in body


def test_item_code_is_not_treated_as_a_verified_material_name_for_missing_field_notices():
    plan = make_request_plan("查询 M6 的现存和占用量。", entities={"item_code": ["M6"]})
    data = payload("wms_db", "material", [{"material_name": "M6", "on_hand_qty": 7}])
    checked = diagnosed(plan, data)
    assert not checked["complete"] and checked["roles"]["on_hand_qty"]["status"] == "target_unproved"
    assert checked["roles"]["allocated_qty"]["status"] == "omitted"
    body = render_draft([], trace(data, checked))
    assert "7 pcs" in body and "尚未核实：" not in body


@pytest.mark.parametrize("restriction", ["LIMIT 1", "GROUP BY id,order_no,order_status", "AND order_status='生产中'"])
def test_witnessed_partial_range_still_publishes_its_limitation(restriction):
    data = payload(
        "erp_db", "orders", [{"id": 11, "order_no": ORDER, "order_status": "生产中"}],
        sql=f"SELECT id,order_no,order_status FROM orders WHERE order_no='{ORDER}' {restriction}",
    )
    checked = diagnosed(make_request_plan(f"查询订单 {ORDER} 的订单状态。"), data)
    assert not checked["complete"] and "range_unproved" in checked["reasons"]
    body = render_draft([], trace(data, checked))
    assert "生产中" in body and coverage_note(checked) in body


def test_unhandled_projection_is_not_mislabeled_as_a_witnessed_partial_range():
    data = payload(
        "erp_db", "orders", [{"order_no": ORDER, "order_status": "生产中", "duplicate": "生产中"}],
        sql=f"SELECT order_no,order_status,UPPER(order_status) AS duplicate FROM orders WHERE order_no='{ORDER}'",
    )
    checked = diagnosed(make_request_plan(f"查询订单 {ORDER} 的订单状态。"), data)
    assert not checked["complete"] and "range_unproved" in checked["reasons"]
    assert "原问的完整目标" not in render_draft([], trace(data, checked))


@pytest.mark.parametrize("complete_flag", [False, True])
@pytest.mark.parametrize("with_images", [False, True])
def test_saved_complete_or_image_boolean_cannot_hide_actual_row_omissions(complete_flag, with_images):
    data = payload(
        "prod_db", "work_order",
        [{"id": 11, "order_no": ORDER, "current_stage": "包装", "production_status": "生产中"},
         {"id": 22, "order_no": ORDER, "current_stage": "开料"}],
    )
    checked = diagnosed(make_request_plan(f"查询订单 {ORDER} 的当前工序和生产状态。"), data)
    original_note = coverage_note(checked)
    assert "生产状态" in original_note and not checked["complete"]
    checked["complete"] = complete_flag
    business = trace(data, checked)
    business.update(image_review_complete=True, materials_reviewed=True)
    body = render_draft([], business, image_facts() if with_images else None)
    assert "包装" in body and "开料" in body and original_note in body


def test_caller_edited_diagnostic_status_cannot_invent_missing_business_facts():
    data = payload("erp_db", "orders", [{"order_no": ORDER, "order_status": "生产中"}])
    checked = diagnosed(make_request_plan(f"查询订单 {ORDER} 的订单状态。"), data)
    assert checked["complete"]
    checked.update(complete=False, reasons=["range_unproved"])
    checked["roles"]["order_status"]["status"] = "wrong_target"
    body = render_draft([], trace(data, checked))
    assert "生产中" in body and "尚未核实：" not in body and "原问的完整目标" not in body


def test_event_summary_diagnostic_retains_only_hashed_target_binding_and_no_case_context():
    message = f"查询订单 {ORDER} 的订单状态。"
    plan = make_request_plan(message, case_refs=["PRIVATE-CASE-982"])
    data = payload("erp_db", "orders", [{"order_no": ORDER}])
    checked = diagnosed(plan, data)
    encoded = json.dumps(checked, ensure_ascii=False)
    assert ORDER not in encoded and message not in encoded and "PRIVATE-CASE-982" not in encoded
    assert checked["request_scope"]["targets"] == [
        {"kind": "order_id", "value_sha256": hashlib.sha256(ORDER.encode()).hexdigest()}
    ]
    assert "订单状态" in render_draft([], trace(data, checked))
    assert "尚未核实：订单状态" in render_draft([], trace(data, checked))


def test_null_stays_registered_null_and_wrong_unit_stays_a_visible_field_limitation():
    plan = make_request_plan(f"提供订单 {ORDER} 的登记联系电话。")
    data = payload("erp_db", "orders", [{"order_no": ORDER, "phone": None}])
    checked = diagnosed(plan, data)
    assert checked["roles"]["registered_phone"]["status"] == "proven_null"
    assert "未登记" in render_draft([], trace(data, checked))
    amount = payload("erp_db", "orders", [{"order_no": ORDER, "contract_amount": 70.5}])
    for cell in amount["evidence"]:
        if cell["field"] == "contract_amount":
            cell["unit"] = "EUR"
    amount["steps"][0]["column_units"]["contract_amount"] = "EUR"
    checked = diagnosed(make_request_plan(f"查询订单 {ORDER} 的合同金额。"), amount)
    assert not checked["complete"] and checked["roles"]["contract_amount"]["status"] == "unsupported"
    assert coverage_note(checked) in render_draft([], trace(amount, checked))


@pytest.mark.parametrize(
    "issue,notice",
    [("failed", "业务查询失败"), ("partial_success", "查询仅部分成功"), ("truncated", "结果被截断"),
     ("missing_or_stale_time", "证据时点缺失或已过期"), ("missing_unit", "缺少单位"),
     ("conflicting_records", "业务记录相互冲突"), ("no_data", "不能据此认定没有记录")],
)
def test_original_execution_limitation_notices_remain_visible(issue, notice):
    business = {"attempted": True, "evidence": [], "evidence_issues": [issue], "evidence_payload": {}}
    assert notice in render_draft([], business)


async def test_internal_context_keeps_unknown_plan_diagnostics_without_draft_or_authority_upgrade():
    message = f"查询订单 {ORDER} 合同金额与订单状态。"
    plan = make_request_plan(message)
    goal = make_goal(message, entities=plan["lookup_entities"], owner="scope-run", case_id="case")
    goal["request_plan"] = plan
    tools, business = Tools(joined_order()), {}
    with bind_goal(goal):
        context, usable = await delegate_order_query(tools, message, trace=business)
    checked = business["request_coverage"]
    assert usable and not checked["complete"] and checked["planning_only"]
    assert {v["status"] for v in checked["roles"].values()} == {"target_unproved"}
    assert coverage_note(checked) in context and "原问核对说明" in context and len(tools.calls) == 1
    assert not any(k in business for k in ["answer_origin", "fact_receipt", "handoff", "needs_clarification"])
    body = render_draft([], business)
    assert "1170.25 USD" in body and "尚未核实：" not in body
