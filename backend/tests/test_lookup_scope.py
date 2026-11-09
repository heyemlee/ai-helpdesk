"""Current-target absence coverage, separate from execution health.

Controlled downstream responses deliberately use complete current dispatches.
These are consumer/transport/loop controls, not real supplier judgments.
"""
import copy
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from conftest import ScriptedModel, make_request, text_step, tool_step
from test_agent_tool_loop import _orchestrator, _turn
from test_attachments import connected as connected  # noqa: F401
from test_cases import ACTOR, PG, claim
from test_cases import pair as pair  # noqa: F401

from app.agents.agent_orchestrator import AgentType, BaseAgent
from app.core.business_facts import collect_evidence, render_draft
from app.core.intent_recognizer import IntentCategory
from app.core.lookup_scope import assess_lookup, bind_result, dispatch_for, make_goal
from app.tools.tool_manager import ToolResult


def goal(message="系统是否有订单 CONTROL-A 的状态记录？", **kwargs):
    return make_goal(message, entities={"order_id": ["CONTROL-A"]}, owner="scope-run", **kwargs)


def result(dispatch, *, sql="SELECT order_no,order_status FROM orders WHERE order_no='CONTROL-A'", database="erp_db", source="query_time"):
    now = datetime.now(UTC).isoformat()
    data = {"queried_database": True, "execution_status": "no_data", "has_risk": False,
            "degraded": False, "result_truncated": False, "observed_at": now,
            "trace_id": dispatch["nonce"], "run_id": "current-sql-run", "selected_databases": [database],
            "steps": [{"database": database, "success": True, "rows": [], "row_count": 0,
                       "error": None, "truncated": False, "risk_findings": [], "final_sql": sql,
                       "as_of": now, "as_of_source": source}]}
    return bind_result(data, dispatch)


def trace(g, *, query=None, **kwargs):
    return {"attempted": True, "outcome": "unverified", "evidence": [], "evidence_issues": ["no_data"],
            "lookup_goal": g, "evidence_payload": result(dispatch_for(g, query or g["message"]), **kwargs)}


def test_same_goal_and_bounded_natural_rewording_are_covered():
    g = goal()
    for query in [g["message"], "请查询订单 CONTROL-A 的状态记录是否存在？"]:
        t = trace(g, query=query)
        assert assess_lookup(t, g)["covers_goal"]
        body = render_draft([], t, lookup_goal=g)
        assert "查询成功，未找到记录。" in body
        assert "erp_db.orders" not in body and "order_no =" not in body
        assert t["evidence_payload"]["steps"][0]["as_of"] not in body
        assert assess_lookup(t, g)["scope"]["predicates"] == [("order_no", "=", "CONTROL-A")]


def test_current_case_followup_uses_its_selected_case_target():
    g = make_goal("还有状态记录吗？", case_refs=["CONTROL-A"], owner="case-run", case_id="case-A")
    query = "订单 CONTROL-A\n还有状态记录吗？"
    g["queries"].append(query)
    assert assess_lookup(trace(g, query=query), g)["covers_goal"]
    changed = copy.deepcopy(g)
    changed["entities"] = {"order_id": ["CONTROL-B"]}
    assert not assess_lookup(trace(g, query=query), changed)["covers_goal"]


@pytest.mark.parametrize("sql", [
    "SELECT order_no,order_status FROM orders WHERE order_no='CONTROL-B'",
    "SELECT order_no,order_status FROM orders WHERE order_no='CONTROL-A' AND order_status='open'",
    "SELECT order_no,order_status FROM orders WHERE order_no='CONTROL-A' OR order_status='open'",
    "SELECT DISTINCT order_no FROM orders WHERE order_no='CONTROL-A'",
    "SELECT COUNT(*) FROM orders WHERE order_no='CONTROL-A'",
    "SELECT order_no FROM orders WHERE order_no='CONTROL-A' LIMIT 1",
    "SELECT order_no FROM orders TABLESAMPLE SYSTEM (1) WHERE order_no='CONTROL-A'",
    "SELECT unnest(ARRAY[]) FROM orders WHERE order_no='CONTROL-A'",
    "SELECT order_no FROM orders WHERE order_no='CONTROL-A' QUALIFY row_number() OVER ()=1",
    'SELECT order_no FROM "Orders" WHERE order_no=\'CONTROL-A\'',
    "WITH x AS (SELECT * FROM orders) SELECT * FROM x WHERE order_no='CONTROL-A'",
])
def test_current_correct_nonce_does_not_prove_a_different_or_complex_scope(sql):
    g = goal()
    checked = assess_lookup(trace(g, sql=sql), g)
    assert checked["execution_verified"] and not checked["covers_goal"]
    assert checked["reasons"] == ["lookup_scope_unproved"]


@pytest.mark.parametrize("query,sql,database", [
    ("在归档源查询订单 CONTROL-A 的状态记录", "SELECT order_no,order_status FROM orders WHERE order_no='CONTROL-A'", "archive_db"),
    ("查询订单 CONTROL-B 的状态记录", "SELECT order_no,order_status FROM orders WHERE order_no='CONTROL-B'", "erp_db"),
    ("在delivery表查询订单 CONTROL-A", "SELECT order_no FROM delivery WHERE order_no='CONTROL-A'", "erp_db"),
])
def test_legitimate_current_tool_query_for_another_scope_is_not_a_goal_answer(query, sql, database):
    g = goal()
    checked = assess_lookup(trace(g, query=query, sql=sql, database=database), g)
    assert checked["execution_verified"] and not checked["covers_goal"]
    assert checked["reasons"] == ["lookup_dispatch_target_changed"]


@pytest.mark.parametrize("database,table,start", [("archive_db", "orders", "2027-01-01"),
    ("sales_db", "delivery", "2027-01-01"), ("sales_db", "orders", "2027-01-02")])
def test_explicit_source_table_and_time_range_must_all_match(database, table, start):
    g = goal("在sales_db.orders查询CONTROL-A，updated_at>='2027-01-01'，updated_at<'2027-02-01'是否有状态记录")
    sql = f"SELECT order_no FROM {table} WHERE order_no='CONTROL-A' AND updated_at>='{start}' AND updated_at<'2027-02-01'"
    checked = assess_lookup(trace(g, sql=sql, database=database), g)
    assert checked["execution_verified"] and not checked["covers_goal"]


def test_explicit_complete_range_positive_and_unknown_temporal_scope():
    g = goal("在sales_db.orders查询CONTROL-A，updated_at>='2027-01-01'，updated_at<'2027-02-01'是否有状态记录")
    sql = "SELECT order_no FROM orders WHERE order_no='CONTROL-A' AND updated_at>='2027-01-01' AND updated_at<'2027-02-01'"
    assert assess_lookup(trace(g, sql=sql, database="sales_db"), g)["covers_goal"]
    historical = goal("去年订单 CONTROL-A 是否有状态记录？")
    assert not assess_lookup(trace(historical), historical)["covers_goal"]


def test_old_receipt_cannot_be_rebased_by_a_new_observation_clock_or_new_goal():
    g = goal()
    old = trace(g)
    new = make_goal(g["message"], entities=g["entities"], owner="next-run")
    old["evidence_payload"]["observed_at"] = datetime.now(UTC).isoformat()
    assert not assess_lookup(old, g)["covers_goal"]
    assert not assess_lookup(old, new)["covers_goal"]
    dispatch = dispatch_for(new, new["message"])
    replay = bind_result(copy.deepcopy(old["evidence_payload"]), dispatch)
    assert not assess_lookup({**old, "lookup_goal": new, "evidence_payload": replay}, new)["covers_goal"]


def test_snapshot_and_old_execution_clock_do_not_become_current_lookup_evidence():
    g = goal()
    assert not assess_lookup(trace(g, source="snapshot"), g)["covers_goal"]
    t = trace(g)
    t["evidence_payload"]["steps"][0]["as_of"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    dispatch = t["evidence_payload"]["_lookup_dispatch"]
    bind_result(t["evidence_payload"], dispatch)
    assert not assess_lookup(t, g)["covers_goal"]


def test_selection_preserves_a_failure_or_good_A_when_B_is_empty():
    g = goal()
    b = trace(g, query="查订单 CONTROL-B 状态", sql="SELECT order_no FROM orders WHERE order_no='CONTROL-B'")
    trajectory = [{"tool": "order_data_query", "trace": b}]
    failure = {"attempted": True, "outcome": "failed", "evidence_issues": ["failed"]}
    selected = collect_evidence({"business_data": failure}, trajectory, lookup_goal=g)["business_data"]
    assert selected["outcome"] == "failed" and selected["limited_lookups"]
    assert "未找到记录。" not in render_draft([], selected, lookup_goal=g)
    selected = collect_evidence({"business_data": trace(g)}, trajectory, lookup_goal=g)["business_data"]
    assert assess_lookup(selected, g)["covers_goal"] and selected["limited_lookups"]


class BoundTools:
    def __init__(self, g):
        self.goal, self.calls = g, []

    async def call(self, name, params, context=None, **_):
        self.calls.append((name, params, context))
        if name != "order_data_query":
            return ToolResult(success=True, data=[], tool_name=name)
        target = "CONTROL-B" if "CONTROL-B" in params["query"] else "CONTROL-A"
        data = result(context["lookup_dispatch"], sql=f"SELECT order_no,order_status FROM orders WHERE order_no='{target}'")
        return ToolResult(success=True, data=data, tool_name=name)


class BoundDataTools:
    """A new transport execution control, keeping archived business SQL intact."""
    def __init__(self, data):
        self.data, self.calls = data, []

    async def call(self, name, params, context=None, **_):
        self.calls.append((name, params))
        if name != "order_data_query":
            return ToolResult(success=True, data=[], tool_name=name)
        data = copy.deepcopy(self.data)
        dispatch = (context or {}).get("lookup_dispatch")
        if dispatch:
            data["trace_id"] = dispatch["nonce"]
            for step in data.get("steps", []):
                step["as_of"] = datetime.now(UTC).isoformat()
            bind_result(data, dispatch)
        return ToolResult(success=True, data=data, tool_name=name)


@pytest.mark.parametrize("failed_A", [False, True])
@pytest.mark.parametrize("declared", [None, "no", "yes"])
async def test_real_tool_loop_wrong_B_cannot_replace_A_or_silence_explicit_yes(failed_A, declared):
    g = goal()
    tools = BoundTools(g)
    turn = _turn(tools)
    turn.lookup_goal = g
    trailer = "" if declared is None else "\n[[handoff:" + declared + "]]"
    model = ScriptedModel(tool_step(("lookup-B", "order_data_query", {"query": "查询订单 CONTROL-B 的状态记录"})), text_step("未找到记录。" + trailer))
    req = make_request(g["message"], IntentCategory.ORDER_STATUS, lookup_goal=g, turn=turn,
                       fact_evidence={"business_data": {"attempted": True, "outcome": "failed", "evidence_issues": ["failed"]} if failed_A else {}})
    orch = _orchestrator((AgentType.ORDER, model))
    result_value = await orch.run(req)
    assert "查询成功，未找到记录。" not in result_value.response
    assert not BaseAgent._answer_grounded(req, [])
    assert result_value.handoff is (declared == "yes" or (declared is None and failed_A))
    assert any(c[0] == "order_data_query" for c in tools.calls)


async def test_same_goal_success_survives_actual_tool_loop_and_unmarked_fallback():
    g = goal()
    turn = _turn(BoundTools(g))
    turn.lookup_goal = g
    model = ScriptedModel(tool_step(("lookup-A", "order_data_query", {"query": "请查询订单 CONTROL-A 的状态记录是否存在？"})), text_step("本次未找到。"))
    req = make_request(g["message"], IntentCategory.ORDER_STATUS, lookup_goal=g, turn=turn, fact_evidence={"business_data": {}})
    value = await _orchestrator((AgentType.ORDER, model)).run(req)
    assert "查询成功，未找到记录。" in value.response and not value.handoff
    assert BaseAgent._answer_grounded(req, [])


@pytest.mark.parametrize("old_trace", [False, True])
async def test_real_http_adapter_checks_the_current_trace_challenge(monkeypatch, old_trace):
    import app.tools.sql_agent_client as client_module
    from app.tools.sql_agent_client import SQLAgentClient

    g = goal()
    dispatch = dispatch_for(g, g["message"])
    calls = []

    class Client:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def post(self, url, *, headers, json):
            calls.append((url, headers, json))
            data = result(dispatch)
            data["trace_id"] = "previous-execution" if old_trace else headers["X-Trace-Id"]
            return SimpleNamespace(headers={"X-Run-Id": "sql-current"}, raise_for_status=lambda: None, json=lambda: data)

    monkeypatch.setattr(client_module.httpx, "AsyncClient", Client)
    data = await SQLAgentClient(base_url="http://controlled.invalid").handler(
        {"query": g["message"]}, context={"lookup_dispatch": dispatch})
    checked = assess_lookup({"lookup_goal": g, "evidence_payload": data}, g)
    assert checked["execution_verified"] and checked["covers_goal"] is (not old_trace)
    assert calls[0][1]["X-Trace-Id"] == dispatch["nonce"]


@pytest.mark.parametrize("mode", ["send", "stream", "unsupported_stream"])
@pytest.mark.parametrize("old_trace", [False, True])
async def test_a2a_transport_keeps_dispatch_binding_through_supported_routes(monkeypatch, mode, old_trace):
    from test_a2a_client import DONE, TASK, _sse

    from app.tools.sql_agent_client import SQLAgentClient

    g = goal()
    dispatch = dispatch_for(g, g["message"])
    methods = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"capabilities": {"streaming": mode != "send"}})
        method = json.loads(request.content)["method"]
        methods.append(method)
        assert request.headers["X-Trace-Id"] == dispatch["nonce"]
        if method == "message/stream" and mode == "unsupported_stream":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": "r", "error": {"code": -32004}})
        data = result(dispatch)
        data.pop("_lookup_dispatch")
        data["trace_id"] = "previous-execution" if old_trace else request.headers["X-Trace-Id"]
        artifact = {"artifactId": "empty", "parts": [{"kind": "data", "data": data}]}
        if method == "message/stream":
            return _sse(TASK, {"kind": "artifact-update", "taskId": "t1", "contextId": "c1",
                               "lastChunk": True, "artifact": artifact}, DONE)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "r", "result": {
            **TASK, "status": {"state": "completed"}, "artifacts": [artifact]}})

    transport, original = httpx.MockTransport(handler), httpx.AsyncClient

    class Client(original):
        def __init__(self, **kwargs):
            super().__init__(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    client = SQLAgentClient(base_url="http://controlled.invalid", transport="a2a")
    data = await client.handler({"query": g["message"]}, context={"lookup_dispatch": dispatch})
    checked = assess_lookup({"lookup_goal": g, "evidence_payload": data}, g)
    assert checked["execution_verified"] and checked["covers_goal"] is (not old_trace)
    assert methods == (["message/stream", "message/send"] if mode == "unsupported_stream" else
                       ["message/send" if mode == "send" else "message/stream"])


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
@pytest.mark.parametrize("wrong_scope", [False, True])
async def test_case_adoption_and_saved_draft_recheck_the_same_goal(connected, wrong_scope):
    from fastapi import HTTPException

    from app.tickets.cases import CaseInput, DraftApproval

    service, _, _, _ = connected
    receipt = await service.accept_input(ACTOR, CaseInput(message_id="scoped-case", conv_id="conversation",
        new_case=True, issue_type="general", message="系统是否有订单 CONTROL-A 的状态记录？", order_refs=["CONTROL-A"]))
    run = await claim(service, receipt)
    g = make_goal(run.message, entities={"order_id": ["CONTROL-A"]}, owner=run.run_id, case_id=receipt["case_id"])
    business = trace(g, sql="SELECT order_no FROM orders WHERE order_no='CONTROL-B'" if wrong_scope else
                     "SELECT order_no FROM orders WHERE order_no='CONTROL-A'")
    proposal = {"response": "查询成功，未找到记录。", "business_data": business, "lookup_goal": g,
                "handoff": True, "handoff_reason": "已有人工审批决定"}
    value, applied = await service.complete(run, proposal)
    assert applied and value["handoff"]
    assert ("查询成功，未找到记录。" in value["response"]) is (not wrong_scope)
    assert (await service.complete(run, proposal))[0] == value
    detail = await service.get(ACTOR, receipt["case_id"])
    draft = detail["drafts"][0]
    assert draft["valid"]
    if not wrong_scope:
        import json
        evidence = draft["evidence"]
        entry = next(e for e in evidence if e.get("source") == "business_query")
        entry["evidence_payload"]["observed_at"] = datetime.now(UTC).isoformat()
        await service.db.execute("UPDATE case_drafts SET evidence=$2 WHERE id=$1", draft["id"], json.dumps(evidence))
        assert not (await service.get(ACTOR, receipt["case_id"]))["drafts"][0]["valid"]
        with pytest.raises(HTTPException):
            await service.approve(ACTOR, receipt["case_id"], draft["id"], DraftApproval(expected_version=detail["version"],
                expected_control_version=detail["control_version"], body_hash=draft["body_hash"]))


@pytest.mark.parametrize("wrong_scope", [False, True])
@pytest.mark.parametrize("handoff", [False, True])
async def test_saved_resume_rechecks_scope_without_revoking_human_decisions(monkeypatch, tmp_path, wrong_scope, handoff):
    from test_runs_api import _ask, _audit, _close, _frames, _queued

    from app.agents.agent_orchestrator import OrchestratorResult
    from app.runs.checkpoint import encode_result
    from app.runs.memo import RunScope, bind_run

    desk = await _queued(monkeypatch, tmp_path, start_worker=False)
    try:
        g = make_goal("系统是否有订单 CONTROL-A 的状态记录？", entities={"order_id": ["CONTROL-A"]}, owner="scope-resume")
        business = trace(g, sql="SELECT order_no FROM orders WHERE order_no='CONTROL-B'" if wrong_scope else
                         "SELECT order_no FROM orders WHERE order_no='CONTROL-A'")
        body = "查询成功，未找到记录。"
        final = OrchestratorResult(request_id="scope-resume", response=body, agent_type=AgentType.ORDER,
                                    intent=IntentCategory.ORDER_STATUS, handoff=handoff, escalated=handoff,
                                    answer_handoff_declared=handoff, handoff_reason="保留原决定" if handoff else "")
        checkpoint = {"turn_id": "original", "shown": body, "final": encode_result(final),
                      "meta": {"knowledge_sources": [], "business_data": business, "lookup_goal": g}}
        original = copy.deepcopy(checkpoint)
        with bind_run(RunScope("scope-resume", attempt=2, instance="staff")):
            frames = _frames([f async for f in desk.main._resume_frames(_ask(desk.main, message=g["message"]), "c1", "u1", checkpoint)])
        value = frames[-1][1]
        assert value["handoff"] is handoff
        assert ("查询成功，未找到记录。" in value["response"]) is (not wrong_scope)
        assert value["response"] == "".join(d["text"] for k, d, _ in frames if k == "delta")
        assert checkpoint == original and not desk.model.requests
        await _audit(desk)
    finally:
        await _close(desk)
