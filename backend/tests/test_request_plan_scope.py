"""Case coverage needs full source ranges; ordinary chat keeps its old scope."""

import copy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from evidence_fixtures import with_evidence
from test_lookup_scope import result as empty_result
from test_request_plan import diagnosed, payload
from test_turn_prep import _deps, _memory

from app.agents.turn_prep import prepare_turn
from app.core.business_facts import render_draft, verified_cells
from app.core.lookup_scope import assess_lookup
from app.core.request_plan import coverage_note, make_request_plan
from app.tools.tool_manager import ToolResult

REF = "PEER-314"
SOURCES = [
    ("erp_db", "orders", "order_status", "订单状态", "生产中", "待发货"),
    ("svc_db", "delivery", "delivery_status", "配送状态", "已送达", "配送中"),
    ("prod_db", "work_order", "production_status", "生产状态", "生产中", "已完工"),
    ("prod_db", "work_order", "current_stage", "当前工序", "包装", "开料"),
]
PAIRS = [
    ("prod_db", "work_order", "current_stage", "production_status", "当前工序和生产状态", "包装", "生产中"),
    ("svc_db", "delivery", "delivered_date", "delivery_status", "实际送达日期和配送状态", "2030-02-18", "已送达"),
    ("erp_db", "orders", "delivery_date", "order_status", "计划交付日期和订单状态", "2030-02-18", "待发货"),
]


@pytest.mark.parametrize("database,table,field,phrase,value,other", SOURCES)
@pytest.mark.parametrize("restriction", ["limit", "extra_value_filter", "group"])
def test_partial_order_delivery_and_production_ranges_keep_facts_without_complete(
    database, table, field, phrase, value, other, restriction
):
    suffix = {
        "limit": " LIMIT 1",
        "extra_value_filter": f" AND {field}='{value}'",
        "group": f" GROUP BY id,order_no,{field}",
    }[restriction]
    sql = f"SELECT id,order_no,{field} FROM {table} WHERE order_no='{REF}'" + suffix
    data = payload(database, table, [{"id": 11, "order_no": REF, field: value}], sql=sql)
    before = copy.deepcopy(data)
    cells, issues = verified_cells(data)
    assert cells and issues == []
    plan = make_request_plan(f"查询订单 {REF} 的{phrase}。")
    checked = diagnosed(plan, data)
    assert not checked["complete"] and "range_unproved" in checked["reasons"]
    role = plan["required_roles"][0]
    assert checked["roles"][role]["status"] == "value" and checked["roles"][role]["evidence_ids"]
    assert coverage_note(checked) and data == before and verified_cells(data) == (cells, issues)


@pytest.mark.parametrize("database,table,field,phrase,value,other", SOURCES)
@pytest.mark.parametrize("shape", ["plain", "ordered_reverse_equality", "distinct_values"])
def test_full_multiple_record_ranges_complete_without_discarding_actual_rows(
    database, table, field, phrase, value, other, shape
):
    projection = f"r.id,r.order_no,r.{field}"
    if shape == "distinct_values":
        projection = "DISTINCT " + projection
    predicate = f"'{REF}'=r.order_no" if shape == "ordered_reverse_equality" else f"r.order_no='{REF}'"
    sql = f"SELECT {projection} FROM {table} r WHERE {predicate}"
    if shape == "ordered_reverse_equality":
        sql += " ORDER BY r.id DESC"
    data = payload(
        database,
        table,
        [{"id": 11, "order_no": REF, field: value}, {"id": 22, "order_no": REF, field: other}],
        sql=sql,
    )
    cells, issues = verified_cells(data)
    assert len(cells) == 6 and not issues
    checked = diagnosed(make_request_plan(f"查询订单 {REF} 的{phrase}。"), data)
    assert checked["complete"] and coverage_note(checked) == ""
    [role] = checked["roles"].values()
    assert role["status"] == "value" and {w[3] for w in role["witnesses"]} == {0, 1}


@pytest.mark.parametrize("database,table,first,second,phrase,value_a,value_b", PAIRS)
def test_complementary_fields_from_different_records_must_not_be_stitched(
    database, table, first, second, phrase, value_a, value_b
):
    a = payload(
        database,
        table,
        [{"id": 11, "order_no": REF, first: value_a}],
        sql=f"SELECT id,order_no,{first} FROM {table} WHERE order_no='{REF}'",
    )
    b = payload(
        database,
        table,
        [{"id": 22, "order_no": REF, second: value_b}],
        sql=f"SELECT id,order_no,{second} FROM {table} WHERE order_no='{REF}'",
    )
    data = with_evidence({**a, "steps": [*a["steps"], *b["steps"]]})
    cells, issues = verified_cells(data)
    assert len(cells) == 6 and not issues
    checked = diagnosed(make_request_plan(f"查询订单 {REF} 的{phrase}。"), data)
    assert not checked["complete"] and "grain_mismatch" in checked["reasons"]
    assert all(v["status"] == "grain_mismatch" and v["evidence_ids"] for v in checked["roles"].values())
    assert verified_cells(data) == (cells, issues)


@pytest.mark.parametrize("database,table,first,second,phrase,value_a,value_b", PAIRS)
def test_requested_fields_from_each_same_actual_record_form_complete_range(
    database, table, first, second, phrase, value_a, value_b
):
    data = payload(
        database,
        table,
        [
            {"id": 11, "order_no": REF, first: value_a, second: value_b},
            {"id": 22, "order_no": REF, first: value_a, second: value_b},
        ],
        sql=f"SELECT id,order_no,{first},{second} FROM {table} WHERE order_no='{REF}'",
    )
    checked = diagnosed(make_request_plan(f"查询订单 {REF} 的{phrase}。"), data)
    assert checked["complete"]
    witnesses = [v["witnesses"] for v in checked["roles"].values()]
    assert witnesses[0] == witnesses[1] and len(witnesses[0]) == 2


def test_different_physical_sources_independently_cover_their_requested_fields():
    a = payload(
        "erp_db",
        "orders",
        [{"order_no": REF, "delivery_date": "2030-02-18"}],
        sql=f"SELECT order_no,delivery_date FROM orders WHERE order_no='{REF}'",
    )
    b = payload(
        "svc_db",
        "delivery",
        [{"order_no": REF, "delivery_status": "已送达"}],
        sql=f"SELECT DISTINCT order_no,delivery_status FROM delivery WHERE order_no='{REF}'",
    )
    data = with_evidence({**a, "steps": [*a["steps"], *b["steps"]]})
    checked = diagnosed(make_request_plan(f"查询订单 {REF} 的计划交付日期和配送状态。"), data)
    assert checked["complete"] and len(checked["roles"]) == 2


@pytest.mark.parametrize("ref", [REF, "PEER-972"])
async def test_non_case_history_target_prefetch_and_original_absence_coverage_are_preserved(ref):
    message = "它还有状态记录吗？"
    entities = {"order_id": [ref]}
    calls, prefetch_calls = [], []

    class Tools:
        async def call(self, name, params, context=None, **_):
            calls.append((name, copy.deepcopy(params), copy.deepcopy(context)))
            data = (
                []
                if name == "knowledge_search"
                else empty_result(
                    context["lookup_dispatch"], sql=f"SELECT order_no,order_status FROM orders WHERE order_no='{ref}'"
                )
            )
            return ToolResult(success=True, data=data, tool_name=name)

    deps = _deps(Tools(), turns=2, loop=True)
    memory = _memory(2)
    memory.recent_messages[0].content = "查询订单 " + ref

    async def load_memory(*_, **__):
        return memory

    async def recognize(text, history=None):
        assert text == message and any(ref in m["content"] for m in history)
        return SimpleNamespace(
            intent="order_status",
            intent_group="order",
            urgency="low",
            confidence=0.9,
            entities=entities,
            fast_path=True,
        )

    original_prefetch = type(deps.prefetch).order_data_context

    async def prefetch(self, text, **kwargs):
        prefetch_calls.append((text, kwargs.copy()))
        return await original_prefetch(self, text, **kwargs)

    with patch.object(type(deps.prefetch), "order_data_context", prefetch):
        turn = await prepare_turn(
            message,
            user_id="employee",
            conv_id="ordinary-conv",
            request_id="request",
            deps=replace(deps, load_memory=load_memory, recognize_intent=recognize),
        )
    [(query, kwargs)] = prefetch_calls
    assert query == message and kwargs["entities"] is entities and "query_planned" not in kwargs
    goal = turn.request.lookup_goal
    assert goal["entities"] == entities and goal["message"] == goal["request_message"] == message
    assert goal["queries"] == [message] and "request_plan" not in goal
    business = turn.evidence["business_data"]
    assert "request_coverage" not in business and business["outcome"] == "unverified"
    assert business["lookup_verified"] and assess_lookup(business, goal)["covers_goal"]
    assert "查询成功，未找到记录。" in render_draft([], business, lookup_goal=goal)
    assert turn.request.turn.lookup_goal == goal
    [(_, params, context)] = [c for c in calls if c[0] == "order_data_query"]
    assert params == {"query": message, "user_id": "employee", "session_id": "ordinary-conv"}
    assert "request_plan" not in context


async def test_case_partial_range_diagnostic_does_not_change_original_source_outcome_or_usability():
    from test_request_plan import Tools

    from app.core.lookup_scope import bind_goal, make_goal
    from app.tools.turn_tools import delegate_order_query

    message = f"查询订单 {REF} 的配送状态。"
    plan = make_request_plan(message)
    goal = make_goal(message, entities=plan["lookup_entities"], owner="scope-run", case_id="selected-case")
    goal["request_plan"] = plan
    data = payload(
        "svc_db",
        "delivery",
        [{"order_no": REF, "delivery_status": "已送达"}],
        sql=f"SELECT order_no,delivery_status FROM delivery WHERE order_no='{REF}' LIMIT 1",
    )
    cells, issues = verified_cells(data)
    tools, trace = Tools(data), {}
    with bind_goal(goal):
        text, usable = await delegate_order_query(tools, message, trace=trace)
    assert (
        usable
        and trace["outcome"] == "evidence"
        and trace["evidence"] == cells
        and trace["evidence_issues"] == issues == []
    )
    assert not trace["request_coverage"]["complete"] and len(tools.calls) == 1
    assert "已送达" in text and coverage_note(trace["request_coverage"]) in text
    assert not any(k in trace for k in ["handoff", "needs_clarification", "fact_task", "fact_receipt"])


@pytest.mark.parametrize(
    "flags,complete",
    [("global_omitted", True), ("explicit_false", True), ("step_omitted", False), ("global_true", False)],
)
def test_range_uses_explicit_step_observation_even_when_optional_global_flag_is_omitted(flags, complete):
    data = payload(
        "svc_db",
        "delivery",
        [{"order_no": REF, "delivery_status": "已送达"}],
        sql=f"SELECT order_no,delivery_status FROM delivery WHERE order_no='{REF}'",
    )
    if flags == "global_omitted":
        data.pop("result_truncated")
    elif flags == "step_omitted":
        data["steps"][0].pop("truncated")
    elif flags == "global_true":
        data["result_truncated"] = True
    checked = diagnosed(make_request_plan(f"查询订单 {REF} 的配送状态。"), data)
    assert checked["complete"] is complete


@pytest.mark.parametrize("shape", ["sampled_source", "set_returning_projection"])
def test_source_sampling_or_set_returning_projection_cannot_prove_full_range(shape):
    row = {"id": 11, "order_no": REF, "delivery_status": "已送达"}
    source, projection = "delivery", "id,order_no,delivery_status"
    if shape == "sampled_source":
        source += " TABLESAMPLE SYSTEM (50)"
    else:
        projection += ",unnest(ARRAY[1]) AS ignored_id"
        row["ignored_id"] = 1
    data = payload("svc_db", "delivery", [row], sql=f"SELECT {projection} FROM {source} WHERE order_no='{REF}'")
    assert verified_cells(data)[0] and verified_cells(data)[1] == []
    assert not diagnosed(make_request_plan(f"查询订单 {REF} 的配送状态。"), data)["complete"]
