"""Original targets and diagnostic coverage share real validated query evidence."""

import copy
import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from evidence_fixtures import with_evidence
from test_turn_prep import _deps

from app.agents.turn_prep import prepare_turn
from app.core.business_facts import render_draft, verified_cells, verified_nulls
from app.core.fact_completion import fact_mode, proposal
from app.core.lookup_scope import bind_goal, make_goal
from app.core.request_plan import ROLES, coverage_note, make_request_plan, request_coverage
from app.tools.tool_manager import ToolResult
from app.tools.turn_tools import delegate_order_query

ORDER = "ORDER-731"
MATERIAL = "Bolt M6"


def payload(database, table, rows, *, sql=None, aliases=None):
    aliases = aliases or {}
    fields = list(rows[0]) if rows else ["order_no", "phone"]
    key = "material_name" if table == "material" else "order_no"
    target = MATERIAL if table == "material" else ORDER
    query = (
        sql
        or f"SELECT {','.join(f'{name} AS {aliases[name]}' if name in aliases else name for name in fields)} FROM {table} WHERE {key}='{target}'"
    )
    metadata, units = {}, {}
    for cap in ROLES.values():
        source = cap["source"]
        if (source["database"], source["table"]) != (database, table) or source["column"] not in fields:
            continue
        field = aliases.get(source["column"], source["column"])
        metadata[field] = {
            **source,
            "schema_revision": cap["schema_revision"],
            "output_field": field,
            "query_hash": hashlib.sha256(query.encode()).hexdigest(),
        }
        if cap["type"] == "number":
            units[field] = cap.get("unit") or "pcs"
    data = with_evidence(
        {
            "queried_database": True,
            "execution_status": "success",
            "degraded": False,
            "has_risk": False,
            "selected_databases": [database],
            "trace_id": "synthetic-plan-trace",
            "run_id": "synthetic-plan-run",
            "result_truncated": False,
            "steps": [
                {
                    "database": database,
                    "success": True,
                    "final_sql": query,
                    "rows": [{aliases.get(k, k): v for k, v in row.items()} for row in rows],
                    "column_metadata": metadata,
                    "column_units": units,
                }
            ],
        }
    )
    # Match the real producer: NULL observations are recovered separately from
    # complete rows, never emitted as ordinary positive evidence cells.
    data["evidence"] = [c for c in data["evidence"] if c["value"] is not None]
    return data


def diagnosed(plan, data, *, outcome=""):
    cells, issues = verified_cells(data)
    return request_coverage(plan, data, cells, verified_nulls(data), issues, outcome=outcome)


@pytest.mark.parametrize(
    "message",
    [
        f"查询物料“{MATERIAL}”的现有库存和占用量。",
        f"库存表中 {MATERIAL} 有多少现存与占用件数？",
        f'Please show on-hand and allocated inventory for "{MATERIAL}".',
    ],
)
def test_material_original_target_beats_case_and_recognition_background(message):
    plan = make_request_plan(
        message, entities={"order_id": ["CONTEXT-8"], "item_code": ["M6"]}, case_refs=["CONTEXT-8"]
    )
    assert plan["targets"] == [
        {
            "kind": "material_name",
            "value": MATERIAL,
            "provenance": "original_text",
            "span": [message.index(MATERIAL), message.index(MATERIAL) + len(MATERIAL)],
        }
    ]
    assert plan["lookup_entities"] == {} and plan["grain"] == "material_inventory"
    assert plan["required_roles"] == ["allocated_qty", "on_hand_qty"]
    assert plan["context_only"]["case_order_refs"] == ["CONTEXT-8"]
    assert plan["planning_only"] is True and not fact_mode({"request_plan": plan})


@pytest.mark.parametrize(
    "message,refs,expected",
    [
        ("查询该订单的订单状态。", [ORDER], [ORDER]),
        ("Show this order status.", [ORDER], [ORDER]),
        ("查询订单状态。", [ORDER], []),
        ("查询该订单的订单状态。", [ORDER, "ORDER-2"], []),
        ("查询该订单 OTHER-23 的订单状态。", [ORDER], ["OTHER-23"]),
    ],
)
def test_case_order_resolves_only_a_real_unambiguous_reference(message, refs, expected):
    plan = make_request_plan(message, case_refs=refs)
    assert [t["value"] for t in plan["targets"] if t["kind"] == "order_id"] == expected
    assert plan["lookup_entities"].get("order_id", []) == expected
    if expected and expected != refs:
        assert "case_target_conflict" in plan["unresolved_roles"]


def test_complete_is_diagnostic_and_does_not_add_scalar_or_human_authority():
    plan = make_request_plan(f"查询订单{ORDER}的合同金额与订单状态。")
    data = payload("erp_db", "orders", [{"order_no": ORDER, "contract_amount": 70.5, "order_status": "生产中"}])
    checked = diagnosed(plan, data)
    assert checked["complete"] and all(v["status"] == "value" for v in checked["roles"].values())
    assert checked["planning_only"] and not fact_mode({"request_coverage": checked})
    assert proposal(None, {"request_coverage": checked, "evidence_payload": data}) is None
    assert not any(k in checked for k in ["handoff", "needs_clarification", "answer_origin", "signature"])


def test_bare_delivery_date_is_not_filled_by_delivery_status_or_planned_date():
    plan = make_request_plan(f"查询订单 {ORDER} 交付日期和配送状态。")
    data = payload("svc_db", "delivery", [{"order_no": ORDER, "delivery_status": "已送达"}])
    checked = diagnosed(plan, data)
    assert checked["roles"]["delivery_status"]["status"] == "value"
    assert checked["roles"]["delivery_date_unspecified"]["status"] == "ambiguous" and not checked["complete"]
    assert "交付日期" in coverage_note(checked)
    trace = {
        "attempted": True,
        "evidence": verified_cells(data)[0],
        "evidence_issues": [],
        "evidence_payload": data,
        "request_coverage": checked,
    }
    body = render_draft([], trace)
    assert "已送达" in body and "尚未核实" in body and "request_coverage" not in body and "planning_only" not in body


def test_only_requested_date_meaning_is_required_and_extra_fields_do_not_expand_it():
    plan = make_request_plan(f"查询订单 {ORDER} 的计划交付日期，不问实际送达日期。")
    assert plan["required_roles"] == ["planned_delivery_date"]
    data = payload("erp_db", "orders", [{"order_no": ORDER, "delivery_date": "2030-02-18", "order_status": "生产中"}])
    assert diagnosed(plan, data)["complete"]
    wrong = payload("svc_db", "delivery", [{"order_no": ORDER, "delivered_date": "2030-02-18"}])
    assert not diagnosed(plan, wrong)["complete"]


def test_parts_keep_each_actual_record_and_missing_progress_visible():
    plan = make_request_plan(f"请提供订单 {ORDER} 补发配件的件数及进度。")
    assert plan["grain"] == "replacement_records" and plan["required_roles"] == ["part_quantity", "part_status"]
    data = payload("svc_db", "replacement_part", [{"id": 17, "order_no": ORDER, "part_name": "Hinge", "quantity": 3}])
    checked = diagnosed(plan, data)
    assert checked["roles"]["part_quantity"]["status"] == "value"
    assert checked["roles"]["part_status"]["status"] == "omitted" and not checked["complete"]
    full = payload(
        "svc_db",
        "replacement_part",
        [
            {"id": 17, "order_no": ORDER, "part_name": "Hinge", "quantity": 3, "part_status": "处理中"},
            {"id": 29, "order_no": ORDER, "part_name": "Panel", "quantity": 1, "part_status": "待处理"},
        ],
    )
    verified = diagnosed(plan, full)
    assert verified["complete"] and len(verified["roles"]["part_quantity"]["witnesses"]) == 2
    assert {w[3] for w in verified["roles"]["part_status"]["witnesses"]} == {0, 1}


def test_partial_records_may_not_be_stitched_or_mistaken_for_full_range():
    plan = make_request_plan(f"提供订单 {ORDER} 补发配件的数量和状态。")
    first = payload("svc_db", "replacement_part", [{"id": 17, "order_no": ORDER, "part_name": "Hinge", "quantity": 3}])
    second = payload(
        "svc_db", "replacement_part", [{"id": 29, "order_no": ORDER, "part_name": "Panel", "part_status": "处理中"}]
    )
    data = with_evidence({**first, "steps": [*first["steps"], *second["steps"]]})
    checked = diagnosed(plan, data)
    assert not checked["complete"] and "grain_mismatch" in checked["reasons"]
    limited = payload(
        "svc_db",
        "replacement_part",
        [{"id": 17, "order_no": ORDER, "part_name": "Hinge", "quantity": 3, "part_status": "处理中"}],
    )
    limited = with_evidence(
        {**limited, "steps": [{**limited["steps"][0], "final_sql": limited["steps"][0]["final_sql"] + " LIMIT 1"}]}
    )
    # Query/source metadata no longer matches, so it cannot establish completeness.
    assert not diagnosed(plan, limited)["complete"]


@pytest.mark.parametrize(
    "case,status",
    [
        ("null", "proven_null"),
        ("omitted", "omitted"),
        ("constant", "unsupported"),
        ("empty", "no_rows"),
        ("failed", "failed"),
    ],
)
def test_phone_real_null_omission_constant_empty_and_outage_are_distinct(case, status):
    plan = make_request_plan(f"提供订单 {ORDER} 的登记联系电话。")
    data = payload("erp_db", "orders", [{"order_no": ORDER, "phone": None}])
    if case == "omitted":
        data = payload("erp_db", "orders", [{"order_no": ORDER}])
    elif case == "constant":
        data = payload(
            "erp_db",
            "orders",
            [{"order_no": ORDER, "phone": None}],
            sql=f"SELECT order_no,NULL AS phone FROM orders WHERE order_no='{ORDER}'",
        )
    elif case == "empty":
        data = payload("erp_db", "orders", [])
        data["execution_status"] = "no_data"
    elif case == "failed":
        data = {"execution_status": "failed", "degraded": True, "degrade_reason": "model_unavailable", "steps": []}
    checked = diagnosed(plan, data)
    assert checked["roles"]["registered_phone"]["status"] == status
    assert checked["complete"] is (case == "null")


@pytest.mark.parametrize(
    "change", ["wrong_target", "stale", "wrong_meaning", "unknown_clause", "extra_filter", "human"]
)
def test_no_complete_for_wrong_target_stale_meaning_unparsed_scope_or_action(change):
    message = f"查询物料“{MATERIAL}”的现有库存和占用量。"
    if change == "unknown_clause":
        message += "同时说明采购价格。"
    if change == "extra_filter":
        message += "只看仓库A。"
    if change == "human":
        message += "请转人工。"
    plan = make_request_plan(message, case_refs=["CONTEXT-8"])
    data = payload("wms_db", "material", [{"material_name": MATERIAL, "on_hand_qty": 7, "allocated_qty": 2}])
    if change == "wrong_target":
        data = payload("wms_db", "material", [{"material_name": "Different", "on_hand_qty": 7, "allocated_qty": 2}])
    elif change == "stale":
        data["observed_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    elif change == "wrong_meaning":
        data["steps"][0]["column_metadata"]["on_hand_qty"]["schema_revision"] = "bad"
    assert not diagnosed(plan, data)["complete"]


class Tools:
    def __init__(self, data):
        self.data = data
        self.calls = []

    async def call(self, name, params, context=None, **_):
        self.calls.append((name, copy.deepcopy(params), copy.deepcopy(context)))
        return ToolResult(success=True, data=copy.deepcopy(self.data), tool_name=name)


async def test_plan_flows_through_existing_prefetch_and_tool_context_without_wire_or_authority_change():
    message = f"查询物料“{MATERIAL}”的现有库存和占用量。"
    data = payload("wms_db", "material", [{"material_name": MATERIAL, "on_hand_qty": 7}])
    tools = Tools(data)
    deps = _deps(tools)

    async def recognize(*_, **__):
        return SimpleNamespace(
            intent="order_status",
            intent_group="order",
            urgency="low",
            confidence=0.9,
            entities={"order_id": ["CONTEXT-8"]},
            fast_path=True,
        )

    deps = replace(deps, recognize_intent=recognize)
    context = {"case_id": "case-control", "order_refs": ["CONTEXT-8"], "issue_type": "order", "messages": []}
    turn = await prepare_turn(
        message, user_id="employee", conv_id="case-conv", request_id="req", deps=deps, case_context=context
    )
    queries = [c for c in tools.calls if c[0] == "order_data_query"]
    assert len(queries) == 1
    _, params, delegation = queries[0]
    assert set(params) == {"query", "user_id", "session_id"}
    assert message in params["query"] and "CONTEXT-8" not in params["query"] and "订单问题" not in params["query"]
    assert "原始任务背景：\n\n订单问题" not in params["query"]
    plan = turn.request.lookup_goal["request_plan"]
    assert delegation["request_plan"] == plan and turn.request.lookup_goal["entities"] == {}
    assert not any(k in delegation["lookup_dispatch"] for k in ["request_plan", "request_coverage", "fact_task"])
    trace = turn.evidence["business_data"]
    assert trace["outcome"] == "evidence" and trace["request_coverage"]["complete"] is False
    assert trace["request_coverage"]["roles"]["allocated_qty"]["status"] == "omitted"
    assert "尚未核实" in turn.request.context and "已占用库存" in turn.request.context
    assert turn.request.message.startswith("订单问题\n订单 CONTEXT-8\n")
    assert not any(k in data for k in ["request_plan", "request_coverage"])


async def test_direct_delegation_retains_source_usability_and_failed_diagnostic_without_extra_calls():
    message = f"提供订单 {ORDER} 的登记联系电话。"
    plan = make_request_plan(message)
    goal = make_goal(message, entities=plan["lookup_entities"], owner="owner", case_id="case")
    goal["request_plan"] = plan
    tools = Tools(
        {"execution_status": "failed", "queried_database": True, "degraded": True, "has_risk": False, "steps": []}
    )
    trace = {}
    with bind_goal(goal):
        text, usable = await delegate_order_query(tools, message, trace=trace)
    assert len(tools.calls) == 1 and not usable and trace["outcome"] == "failed"
    assert trace["request_coverage"]["roles"]["registered_phone"]["status"] == "failed"
    assert "未登记" not in text and "尚未核实" in text


@pytest.mark.parametrize(
    "message",
    [
        "查询物料“Bolt M6”的现有库存和占用量。",
        'Please show on-hand and allocated inventory for "Panel-Z9".',
    ],
)
async def test_independent_material_sql_and_goal_ignore_case_number_and_issue_changes(message):
    calls = []
    for ref, issue in [("SO-2027-159", "order"), ("SO-2099-864", "missing_parts")]:
        tools = Tools(payload("wms_db", "material", [{"material_name": MATERIAL, "on_hand_qty": 7}]))
        deps = _deps(tools)
        seen, gated = [], []
        original_prefetch = deps.prefetch.order_data_context

        async def recognize(text, history=None, *, seen=seen, ref=ref):
            seen.append(text)
            return SimpleNamespace(
                intent="order_status",
                intent_group="order",
                urgency="low",
                confidence=0.9,
                entities={"order_id": [ref]},
                fast_path=True,
            )

        async def prefetch(_self, text, *, gated=gated, original_prefetch=original_prefetch, **kwargs):
            gated.append(copy.deepcopy(kwargs["entities"]))
            return await original_prefetch(text, **kwargs)

        with patch.object(type(deps.prefetch), "order_data_context", prefetch):
            turn = await prepare_turn(
                message,
                user_id="employee",
                conv_id="same-selected-case",
                request_id="req",
                deps=replace(deps, recognize_intent=recognize),
                case_context={"case_id": "selected-case", "order_refs": [ref], "issue_type": issue, "messages": []},
            )
        [(name, params, _)] = [c for c in tools.calls if c[0] == "order_data_query"]
        assert name == "order_data_query" and gated == [{}]
        assert ref in seen[0] and ref in turn.request.message and turn.request.entities == {"order_id": [ref]}
        assert ref in turn.request.context  # Selected Case remains intact in the application.
        assert ref not in params["query"] and "订单问题" not in params["query"] and "缺件问题" not in params["query"]
        goal = turn.request.lookup_goal
        assert goal["entities"] == {} and goal["message"] == message
        assert goal["request_plan"]["context_only"]["case_order_refs"] == [ref]
        calls.append((params["query"], goal["entities"], goal["request_plan"]["targets"]))
    assert calls[0] == calls[1]


@pytest.mark.parametrize("ref", ["SO-2027-159", "SO-2099-864"])
async def test_real_order_pronoun_binds_unique_selected_case_in_query_and_goal(ref):
    message = "查询该订单的订单状态。"
    tools = Tools(payload("erp_db", "orders", [{"order_no": ref, "order_status": "生产中"}]))
    turn = await prepare_turn(
        message,
        user_id="employee",
        conv_id="same-selected-case",
        request_id="req",
        deps=_deps(tools),
        case_context={"case_id": "selected-case", "order_refs": [ref], "issue_type": "order", "messages": []},
    )
    [(_, params, _)] = [c for c in tools.calls if c[0] == "order_data_query"]
    assert ref in params["query"] and "SO-2024-006" not in params["query"]
    assert turn.request.lookup_goal["entities"] == {"order_id": [ref]}
    assert turn.request.lookup_goal["request_plan"]["targets"][0]["provenance"] == "case_reference"


def test_generic_inventory_meaning_cannot_disappear_behind_allocated_value():
    plan = make_request_plan(f"查询物料“{MATERIAL}”的库存和占用量。")
    checked = diagnosed(plan, payload("wms_db", "material", [{"material_name": MATERIAL, "allocated_qty": 2}]))
    assert not checked["complete"] and checked["roles"]["inventory_quantity_unspecified"]["status"] == "ambiguous"
    assert "库存数量口径" in coverage_note(checked)


def test_bound_scalar_task_reuses_exact_roles_and_original_entity_without_new_authority():
    from test_fact_completion import MESSAGE, task_for, wire

    from app.core.lookup_scope import dispatch_for
    from app.core.request_plan import query_guidance

    task = task_for()
    plan = make_request_plan(MESSAGE, fact_task=task, case_refs=["UNIT-A73"])
    assert plan["existing_fact_fields_reused"] and plan["required_roles"] == task["fields"]
    assert plan["lookup_entities"] == {"order_id": ["UNIT-A73"]}
    assert plan["targets"][0]["provenance"] == "original_text"
    assert "UNIT-A73" in query_guidance(plan)
    goal = make_goal(MESSAGE, entities=plan["lookup_entities"], owner=task["run_id"], case_id=task["case_id"])
    goal["fact_task"] = task
    data = wire(dispatch_for(goal, MESSAGE))
    checked = diagnosed(plan, data)
    assert checked["complete"] and coverage_note(checked) == ""
    assert not any(k in checked for k in ["fact_task", "fact_receipt", "handoff", "needs_clarification"])


@pytest.mark.parametrize("name", [MATERIAL, "Panel-Z9"])
def test_complete_inventory_preserves_units_and_original_target(name):
    message = f'Please show on-hand and allocated inventory for "{name}".'
    plan = make_request_plan(message, case_refs=["SO-2049-875"])
    data = payload(
        "wms_db",
        "material",
        [{"material_name": name, "on_hand_qty": 7, "allocated_qty": 2}],
        sql=f"SELECT material_name,on_hand_qty,allocated_qty FROM material WHERE material_name='{name}'",
    )
    checked = diagnosed(plan, data)
    assert checked["complete"] and coverage_note(checked, "en") == ""
    assert set(checked["roles"]) == {"on_hand_qty", "allocated_qty"}
    assert {c["unit"] for c in verified_cells(data)[0] if c["field"] != "material_name"} == {"pcs"}


def test_original_validator_conflict_is_not_repaired_by_diagnostic_grain():
    plan = make_request_plan(f"提供订单 {ORDER} 补发配件的数量和状态。")
    data = payload(
        "svc_db",
        "replacement_part",
        [
            {"order_no": ORDER, "part_name": "Hinge", "quantity": 3, "part_status": "处理中"},
            {"order_no": ORDER, "part_name": "Panel", "quantity": 1, "part_status": "待处理"},
        ],
    )
    assert verified_cells(data) == ([], ["conflicting_records"])
    assert not diagnosed(plan, data)["complete"]


@pytest.mark.parametrize("joined_table", ["delivery", "orders"])
def test_target_key_from_another_join_side_cannot_complete_a_scalar_request(joined_table):
    plan = make_request_plan(f"查询订单 {ORDER} 的订单状态。")
    data = payload(
        "erp_db",
        "orders",
        [{"order_no": ORDER, "order_status": "生产中"}],
        sql=f"SELECT d.order_no,o.order_status FROM orders o CROSS JOIN {joined_table} d WHERE d.order_no='{ORDER}'",
    )
    assert verified_cells(data)[0] and not verified_cells(data)[1]
    assert not diagnosed(plan, data)["complete"]


def test_fixed_unit_meaning_is_not_replaced_by_an_arbitrary_nonempty_unit():
    plan = make_request_plan(f"查询订单 {ORDER} 的合同金额。")
    data = payload("erp_db", "orders", [{"order_no": ORDER, "contract_amount": 70.5}])
    data["steps"][0]["column_units"]["contract_amount"] = "EUR"
    data = with_evidence(data)
    assert verified_cells(data)[0] and not verified_cells(data)[1]
    checked = diagnosed(plan, data)
    assert not checked["complete"] and checked["roles"]["contract_amount"]["status"] == "unsupported"


@pytest.mark.parametrize(
    "template",
    [
        "查询物料“{name}”的现有库存和占用量。",
        "库存表中 {name} 有多少现存与占用件数？",
    ],
)
def test_complete_chinese_inventory_templates_do_not_leave_quantity_suffix_unparsed(template):
    plan = make_request_plan(template.format(name=MATERIAL), case_refs=["SO-2049-875"])
    assert plan["unresolved_roles"] == []
    data = payload("wms_db", "material", [{"material_name": MATERIAL, "on_hand_qty": 7, "allocated_qty": 2}])
    assert diagnosed(plan, data)["complete"]
