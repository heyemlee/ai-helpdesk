"""New controlled SQL executions, never rebased historical supplier records."""
import copy
import hashlib
import json
import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from conftest import ScriptedModel, make_request, text_step
from evidence_fixtures import with_evidence
from fastapi import HTTPException
from test_agent_tool_loop import _orchestrator
from test_case_followups import dependencies
from test_cases import ACTOR, ADMIN, PG, action, claim, scalar
from test_cases import pair as pair  # noqa: F401

from app.agents.agent_orchestrator import AgentType, RoutingDecision
from app.agents.turn_prep import prepare_turn
from app.core.actor import ActorDirectory
from app.core.business_facts import verified_cells
from app.core.fact_completion import (
    FIELDS,
    ORIGIN,
    bind_result,
    current_task,
    make_task,
    projection,
    proposal,
    receipt_valid,
)
from app.core.intent_recognizer import IntentCategory, UrgencyLevel
from app.core.lookup_scope import dispatch_for, make_goal
from app.events import EventLog, EventLogConfig
from app.events.projection import project_turn
from app.runs.memo import RedisMemo, RunScope
from app.runs.store import PgRunStore
from app.runs.worker import RetryRun
from app.tickets.case_execution import CaseExecutor
from app.tickets.cases import CaseInput, DraftApproval
from app.tools.sql_agent_client import SQLAgentClient
from app.tools.tool_manager import Tool, ToolManager, ToolResult

KEY = "controlled-sql-service-key"
MESSAGE = "请核对 UNIT-A73 的约定交付日期及当前配送状态，计划日期不能当成实际送达。"
VALUES = {"planned_delivery_date": "2027-02-18", "delivery_status": "配送中",
          "actual_delivery_date": "2027-02-12", "contract_amount": 18273.45,
          "current_stage": "开料", "production_status": "生产中", "order_status": "生产中"}


@pytest.fixture(autouse=True)
def receipt_key(monkeypatch):
    monkeypatch.setenv("SQL_AGENT_API_KEY", KEY)


def context(entity="UNIT-A73"):
    return {"case_id": "case-control", "message_id": "input-control", "actor_id": "employee-1", "auth_version": 0,
            "expected_case_version": 3, "expected_control_version": 0,
            "snapshot": {"case_id": "case-control", "control_mode": "assistant", "issue_type": "order",
                         "order_refs": [entity], "pending_fields": [], "language": "zh", "materials": [],
                         "input_receipt": {"case_id": "case-control", "message_id": "input-control", "operation": "continued"}}}


def task_for(message=MESSAGE, ctx=None, run_id="run-control", attempt=1):
    result = make_task(message, ctx or context(), run_id, attempt)
    assert result is not None
    return result


def goal_for(task, message=MESSAGE):
    goal = make_goal(message, entities={"order_id": [task["entity"]["order_no"]]},
                     owner=task["run_id"], case_id=task["case_id"])
    goal["fact_task"] = task
    return goal


def wire(dispatch, *, values=None, entity=None, sql_change=None):
    """Execute a fresh, typed response with the registered source definitions."""
    task = dispatch["fact_task"]
    entity = entity or task["entity"]["order_no"]
    values = values or VALUES
    sources = {}
    for role in task["fields"]:
        cap = FIELDS[role]
        source = cap["source"]
        sources.setdefault((source["database"], source["table"]), []).append(role)
    steps = []
    for (database, table), roles in sources.items():
        row = {"order_no": entity, **{FIELDS[r]["source"]["column"]: values[r] for r in roles}}
        sql = f"SELECT {'DISTINCT ' if table == 'delivery' else ''}{','.join(row)} FROM {table} WHERE order_no='{entity}'"
        sql = sql_change(sql) if sql_change else sql
        metadata = {}
        for role in roles:
            cap = FIELDS[role]
            field = cap["source"]["column"]
            metadata[field] = {**cap["source"], "schema_revision": cap["schema_revision"], "output_field": field,
                               "query_hash": hashlib.sha256(sql.encode()).hexdigest()}
        steps.append({"database": database, "final_sql": sql, "success": True, "rows": [row],
                      "column_units": {FIELDS[r]["source"]["column"]: FIELDS[r]["unit"] for r in roles if FIELDS[r]["unit"]},
                      "column_metadata": metadata})
    return with_evidence({"answer": "controlled records", "queried_database": True, "execution_status": "success",
                          "has_risk": False, "degraded": False, "selected_databases": [s["database"] for s in steps],
                          "trace_id": dispatch["nonce"], "run_id": "sql-control-run", "steps": steps})


def trace(data):
    cells, issues = verified_cells(data)
    return {"attempted": True, "outcome": "evidence" if cells else "unverified", "evidence_payload": data,
            "evidence": cells, "evidence_issues": issues, "has_risk": data.get("has_risk"), "degraded": data.get("degraded")}


def execution(task=None, *, message=MESSAGE, **kwargs):
    task = task or task_for(message)
    dispatch = dispatch_for(goal_for(task, message), message)
    data = wire(dispatch, **kwargs)
    bind_result(data, dispatch, key=KEY)
    return task, trace(data)


@pytest.mark.parametrize("entity,message,values", [
    ("UNIT-A73", MESSAGE, VALUES),
    ("OTHER-902", "请查询订单 OTHER-902 的合同金额及当前工序。", {**VALUES, "contract_amount": 875.25, "current_stage": "包装"}),
    ("RANDOM-8", "please check the planned delivery date and delivery status for order RANDOM-8", VALUES),
])
def test_generic_complete_positive_values(entity, message, values):
    ctx = context(entity)
    task, business = execution(task_for(message, ctx), message=message, values=values)
    result = proposal(task, business)
    assert result and result["answer_origin"] == ORIGIN and result["answer_model_invoked"] is False
    assert not {"answer_confidence", "answer_handoff_declared", "handoff_review"} & result.keys()
    assert result["handoff"] is False and result["escalated"] is False
    assert receipt_valid(result["fact_receipt"], business, message=message, context=ctx,
                         run_id=task["run_id"], attempt=1, body=result["response"])
    assert entity in result["response"]
    for role in task["fields"]:
        assert str(values[role]) in result["response"]


@pytest.mark.parametrize("suffix", ["并安排配送", "并保证按时送达", "是否逾期", "请转人工", "去年", "最近一次",
                                      "仅查已送达", "在 archive_db 中", "并比较另一订单 UNIT-B9", "的售后处理结论", "安装日期"])
def test_whole_message_unknown_scope_or_business_effect_stays_on_old_path(suffix):
    assert make_task(MESSAGE + suffix, context(), "run", 1) is None


def test_complete_contract_is_locked_before_dispatch_and_includes_all_fields():
    task = task_for()
    assert task["fields"] == ["delivery_status", "planned_delivery_date"]
    assert task["business_effects"] == [] and task["constraints"] == ["planned_is_not_actual"]
    dispatch = dispatch_for(goal_for(task), MESSAGE)
    data = wire(dispatch)
    # Requested actual date cannot be satisfied by a planned date, even when
    # the physical output name and the result text sound plausible.
    more = task_for("请核对 UNIT-A73 的约定交付日期及实际送达日期。")
    bind_result(data, dispatch, key=KEY)
    assert projection(more, trace(data)) is None
    missing = copy.deepcopy(data)
    missing["steps"].pop()
    assert projection(task, trace(missing)) is None
    actual_dispatch = dispatch_for(goal_for(more), MESSAGE)
    missing_actual = wire(actual_dispatch)
    missing_actual["steps"] = [s for s in missing_actual["steps"] if s["database"] == "erp_db"]
    missing_actual = with_evidence(missing_actual)
    bind_result(missing_actual, actual_dispatch, key=KEY)
    assert projection(more, trace(missing_actual)) is None


def test_same_row_fields_share_the_persisted_source_reference():
    message = "请查询 UNIT-A73 的合同金额及订单状态。"
    task, business = execution(task_for(message), message=message)
    result = proposal(task, business)
    assert result and result["response"].count("[1]") == 1 and "[2]" not in result["response"]
    assert "- 合同金额:" in result["response"] and "- 订单状态:" in result["response"]


@pytest.mark.parametrize("changed", ["many_entities", "human", "pending", "image", "receipt", "wrong_case", "wrong_actor"])
def test_server_selection_and_input_receipt_are_part_of_the_task(changed):
    ctx = context()
    if changed == "many_entities":
        ctx["snapshot"]["order_refs"].append("UNIT-B9")
    elif changed == "human":
        ctx["snapshot"]["control_mode"] = "human"
    elif changed == "pending":
        ctx["snapshot"]["pending_fields"] = ["quantity"]
    elif changed == "image":
        ctx["snapshot"]["materials"] = [{"attachment_id": "image"}]
    elif changed == "receipt":
        ctx["snapshot"]["input_receipt"]["message_id"] = "other-input"
    elif changed == "wrong_case":
        ctx["case_id"] = "other-case"
    else:
        ctx["actor_id"] = ""
    assert make_task(MESSAGE, ctx, "run", 1) is None


def test_unsigned_transport_cannot_enable_the_projection_and_key_rotation_invalidates_it():
    task = task_for()
    dispatch = dispatch_for(goal_for(task), MESSAGE)
    data = wire(dispatch)
    data["_fact_dispatch"] = {"signature": "remote proof"}
    bind_result(data, dispatch, key=None)
    assert "_fact_dispatch" not in data and projection(task, trace(data)) is None
    bind_result(data, dispatch, key=KEY)
    assert projection(task, trace(data), key="rotated-key") is None


@pytest.mark.parametrize("change", [
    "wrong_entity", "constant", "limit", "extra_filter", "join", "history", "different_source", "wrong_meaning",
    "null", "multi_value", "missing_field", "risk", "degraded", "partial", "truncated", "snapshot", "old_asof",
    "old_trace", "missing_run", "unsigned", "payload_edit", "clock_refresh", "goal_replay", "task_edit",
])
def test_positive_coverage_controls(change):
    task = task_for()
    dispatch = dispatch_for(goal_for(task), MESSAGE)
    transforms = {
        "constant": lambda s: s.replace("delivery_status", "'配送中' AS delivery_status"),
        "limit": lambda s: s + " LIMIT 1",
        "extra_filter": lambda s: s + " AND delivery_status='配送中'",
        "join": lambda s: s + " JOIN orders o ON o.order_no=delivery.order_no",
        "history": lambda s: s + " AND updated_at<'2025-01-01'",
    }
    data = wire(dispatch, entity="OTHER-ORDER" if change == "wrong_entity" else None,
                sql_change=transforms.get(change))
    if change == "different_source":
        data["steps"][0]["database"] = "archive_db"
    if change == "wrong_meaning":
        meta = data["steps"][0]["column_metadata"]["delivery_status"]
        meta["description"] = "Archive delivery history"
        meta["schema_revision"] = hashlib.sha256(json.dumps({k: v for k, v in meta.items() if k not in {
            "schema_revision", "output_field", "query_hash"}}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if change in {"null", "missing_field"}:
        data["steps"][0]["rows"][0]["delivery_status"] = None
    if change == "multi_value":
        data["steps"][0]["rows"].append({"order_no": "UNIT-A73", "delivery_status": "已送达"})
        data["steps"][0]["row_count"] = 2
    if change in {"null", "missing_field", "multi_value", "different_source"}:
        data = with_evidence(data)
        if change in {"null", "missing_field"}:
            data["evidence"] = [c for c in data["evidence"] if c["value"] is not None]
    if change in {"risk", "degraded"}:
        data["has_risk" if change == "risk" else "degraded"] = True
    if change == "partial":
        data["execution_status"] = "partial_success"
    if change == "truncated":
        data["steps"][0]["truncated"] = True
    if change == "snapshot":
        data["steps"][0]["as_of_source"] = "snapshot"
    if change == "old_asof":
        data["steps"][0]["as_of"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    if change in {"old_asof", "snapshot"}:
        data = with_evidence(data)
    if change == "old_trace":
        data["trace_id"] = "old-response"
    if change == "missing_run":
        data["run_id"] = ""
    bind_result(data, dispatch, key=KEY)
    if change == "unsigned":
        data["_fact_dispatch"]["signature"] = "forged"
    if change == "payload_edit":
        data["answer"] += "edited after authentication"
    if change == "clock_refresh":
        data["observed_at"] = datetime.now(UTC).isoformat()
    if change in {"goal_replay", "task_edit"}:
        task = {**task, "run_id": "another-run"} if change == "goal_replay" else {**task, "fields": ["delivery_status"]}
    assert projection(task, trace(data)) is None, change


@pytest.mark.parametrize("mode", ["http", "send", "stream"])
@pytest.mark.parametrize("binding", ["current", "old_trace", "different_question"])
async def test_authenticated_transport_binds_positive_proofs_and_strips_remote_proofs(monkeypatch, mode, binding):
    task = task_for()
    dispatch = dispatch_for(goal_for(task), MESSAGE)
    outgoing = "please check the order status for order OTHER-ORDER" if binding == "different_question" else MESSAGE
    calls = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"capabilities": {"streaming": mode == "stream"}})
        assert request.headers["X-API-Key"] == KEY
        assert request.headers["X-Trace-Id"] == dispatch["nonce"]
        body = json.loads(request.content)
        actual = body["query"] if mode == "http" else body["params"]["message"]["parts"][0]["text"]
        assert actual == outgoing
        calls.append(request)
        data = wire(dispatch)
        data["trace_id"] = "old-response" if binding == "old_trace" else request.headers["X-Trace-Id"]
        data["_fact_dispatch"] = {"signature": "remote-forgery"}
        if mode == "http":
            return httpx.Response(200, json=data, headers={"X-Run-Id": "sql-control-run"})
        envelope = {"id": "rpc", "jsonrpc": "2.0", "result": {"kind": "task", "id": "sql-control-run",
            "status": {"state": "completed"}, "artifacts": [{"parts": [{"kind": "data", "data": data}]}]}}
        if mode == "stream":
            event = {"id": "rpc", "jsonrpc": "2.0", "result": {"kind": "artifact-update", "taskId": "sql-control-run",
                     "artifact": {"parts": [{"kind": "data", "data": data}]}, "lastChunk": True}}
            terminal = {"id": "rpc", "jsonrpc": "2.0", "result": {"kind": "status-update", "taskId": "sql-control-run",
                        "status": {"state": "completed"}, "final": True}}
            return httpx.Response(200, text=f"data: {json.dumps(event)}\n\ndata: {json.dumps(terminal)}\n\n",
                                  headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json=envelope)

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    client = SQLAgentClient(base_url="http://controlled.invalid", api_key=KEY)
    data = await (client.query(outgoing, lookup_dispatch=dispatch) if mode == "http" else
                  client.query_a2a(outgoing, lookup_dispatch=dispatch))
    assert len(calls) == 1
    assert data.get("_fact_dispatch", {}).get("signature") != "remote-forgery"
    if binding == "different_question":
        assert "_fact_dispatch" not in data
    assert bool(proposal(task, trace(data))) is (binding == "current")


@pytest.mark.parametrize("guard", ["human", "critical", "intent_low", "routing_low", "multi", "unavailable", "degraded"])
async def test_existing_preflight_guards_stay_ahead_of_fact_completion(guard):
    task, business = execution()
    orch = _orchestrator()
    request = make_request(MESSAGE, IntentCategory.ORDER_STATUS, lookup_goal=goal_for(task),
                           fact_evidence={"business_data": business})
    if guard == "human":
        request.intent = IntentCategory.HUMAN_HANDOFF
    elif guard == "critical":
        request.urgency = UrgencyLevel.CRITICAL
    elif guard == "intent_low":
        request.intent_confidence = .1
    elif guard == "degraded":
        request.intent_degraded = True
    elif guard == "unavailable":
        orch.llm_unavailable_reason = lambda: "controlled outage"
    else:
        orch._route_decision = lambda req: RoutingDecision(primary_agent=AgentType.ORDER, confidence=.1 if guard == "routing_low" else 1,
            supporting_agents=[AgentType.TECHNICAL] if guard == "multi" else [])
    assert await orch.complete_facts(request) is None


async def test_legacy_model_yes_is_not_cleared_by_fact_availability():
    task, business = execution()
    model = ScriptedModel(text_step("需要人工核实。\n[[handoff:yes]]"))
    result = await _orchestrator((AgentType.ORDER, model)).run(make_request(
        MESSAGE, IntentCategory.ORDER_STATUS, lookup_goal=goal_for(task), fact_evidence={"business_data": business}))
    assert result.handoff and result.answer_handoff_declared is True
    assert result.answer_confidence == .65 and len(model.requests) == 1


class FreshTools:
    def __init__(self, values=None):
        self.calls = []
        self.values = values

    async def call(self, name, params, context=None, **_):
        self.calls.append((name, params, context))
        if name != "order_data_query":
            return ToolResult(success=True, data=[], tool_name=name)
        dispatch = context["lookup_dispatch"]
        assert current_task() == dispatch["fact_task"]  # before any result exists
        def handler(request):
            assert request.headers["X-API-Key"] == KEY and request.headers["X-Trace-Id"] == dispatch["nonce"]
            assert json.loads(request.content)["query"] == params["query"]
            return httpx.Response(200, json=wire(dispatch, values=self.values), headers={"X-Run-Id": "sql-control-run"})
        original = httpx.AsyncClient
        with patch("httpx.AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)):
            data = await SQLAgentClient(base_url="http://controlled.invalid", api_key=KEY).query(
                params["query"], lookup_dispatch=dispatch)
        return ToolResult(success=True, data=data, tool_name=name)


async def fresh_case(service, *, message=MESSAGE, entity="UNIT-A73"):
    receipt = await service.accept_input(ACTOR, CaseInput(message_id=uuid.uuid4().hex,
        conv_id="facts-control", message=message, new_case=True, issue_type="order", order_refs=[entity]))
    return await claim(service, receipt)


def executor(service, log=None, values=None):
    tools = FreshTools(values)
    orch = _orchestrator()
    answer = AsyncMock(side_effect=AssertionError("complete facts must not invoke the answer model"))

    async def prepare(run, snapshot, degrade):
        return await prepare_turn(run.message, user_id=run.user_id, conv_id=run.conv_id, request_id=run.request_id,
                                  deps=dependencies(tools), case_context=snapshot)

    return CaseExecutor(service, prepare=prepare, answer=answer, complete_facts=orch.complete_facts,
                        event_log=log or EventLog.disabled()), tools


def worker_context(run, store=None):
    async def checkpoint(value):
        return await store.checkpoint(run.run_id, run.lease_owner, run.attempt, value) if store else True
    async def set_turn(turn_id):
        if store:
            await store.set_turn(run.run_id, run.lease_owner, run.attempt, turn_id, 0)
    return SimpleNamespace(run=run, scope=RunScope(run.run_id, run.attempt), degrade=SimpleNamespace(shed=False),
                           set_turn=set_turn, publish=AsyncMock(), commit=checkpoint)


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
@pytest.mark.parametrize("message,entity,values", [(MESSAGE, "UNIT-A73", VALUES),
    ("请查看订单 OTHER-902 的计划交付日期和配送状态。", "OTHER-902",
     {**VALUES, "planned_delivery_date": "2027-03-05", "delivery_status": "已送达"})])
async def test_actual_case_preparation_projection_transaction_display_and_approval(pair, message, entity, values):
    a, b = pair
    log = await EventLog.open(EventLogConfig(mode="full", instance="staff"))
    try:
        run = await fresh_case(a, message=message, entity=entity)
        ex, tools = executor(a, log, values=values)
        result = (await ex(worker_context(run, PgRunStore(a.db, a.run_instance)))).result
        assert result["case_applied"] and result["answer_origin"] == ORIGIN
        ex.answer.assert_not_awaited()
        assert len([c for c in tools.calls if c[0] == "order_data_query"]) == 1
        case = await b.get(ACTOR, result["case_id"])
        draft = case["drafts"][0]
        assert case["status"] == "in_progress" and draft["valid"] and draft["status"] == "pending"
        assert "answer_confidence" not in result and "answer_handoff_declared" not in result
        approved = await b.approve(ACTOR, case["id"], draft["id"], DraftApproval(expected_version=case["version"],
            expected_control_version=case["control_version"], body_hash=draft["body_hash"]))
        assert approved["drafts"][0]["status"] == "approved"
        duplicate, applied = await b.complete(run, result)
        assert applied and duplicate == result
        assert await scalar(a.db, "SELECT count(*) FROM case_drafts") == 1
        assert await scalar(a.db, "SELECT count(*) FROM case_actions WHERE kind='handoff'") == 0
        assert await scalar(a.db, "SELECT count(*) FROM ticket_notifications") == 0
        events = [e.to_dict() for e in await log.store.events_of(result["turn_id"])]
        assert not any(e["type"] in {"agent_started", "agent_answered", "model_call"} for e in events)
        assert all(entity not in json.dumps(e["data"], ensure_ascii=False) for e in events)
        fact = next(e for e in events if e["type"] == "fact_proposed")
        assert fact["body"]["fact_receipt"] == result["fact_receipt"]
        restored = project_turn(events)["result"]
        assert restored["answer_origin"] == ORIGIN and restored["answer_model_invoked"] is False
        assert "answer_confidence" not in restored
        redacted = project_turn([{**e, "body": None} for e in events])
        assert redacted["redacted"] and redacted["result"]["answer_origin"] == ORIGIN
        assert "answer_confidence" not in redacted["result"]
    finally:
        await log.close()


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
@pytest.mark.parametrize("change", ["none", "new_attempt", "takeover", "version", "lease", "actor", "body", "proof", "origin", "confidence", "declared", "stale"])
async def test_checkpoint_resume_revalidates_facts_and_all_case_fences(pair, change, monkeypatch):
    a, b = pair
    run = await fresh_case(a)
    ex, _ = executor(a)
    store = PgRunStore(a.db, a.run_instance)
    complete = a.complete

    async def crash(*_):
        raise RuntimeError("controlled crash after durable checkpoint")

    a.complete = crash
    with pytest.raises(RetryRun):
        await ex(worker_context(run, store))
    a.complete = complete
    saved = await a.run(run.run_id)
    run.checkpoint = saved.checkpoint
    proposal_value = run.checkpoint["case_proposal"]
    if change == "new_attempt":
        await a.db.execute("UPDATE agent_runs SET lease_until=0 WHERE run_id=$1", run.run_id)
        run = await store.claim(run.run_id, "replacement-worker", 60)
        assert run.attempt == 2 and run.checkpoint["case_proposal"]["fact_receipt"]["task"]["execution_attempt"] == 1
    elif change in {"takeover", "version"}:
        case = await b.get(ACTOR, run.case_context["case_id"])
        await b.mutate(ACTOR, case["id"], action(case, "takeover" if change == "takeover" else "status", status="resolved"))
    elif change == "lease":
        await a.db.execute("UPDATE agent_runs SET lease_owner='other-worker' WHERE run_id=$1", run.run_id)
    elif change == "actor":
        a.actors = ActorDirectory(AsyncMock(return_value=None))
    elif change == "body":
        proposal_value["response"] += "\n已批准送货"
    elif change == "proof":
        proposal_value["business_data"]["evidence_payload"]["_fact_dispatch"]["signature"] = "fake"
    elif change == "origin":
        proposal_value.pop("fact_receipt")
    elif change == "confidence":
        proposal_value["answer_confidence"] = 1.0
    elif change == "declared":
        proposal_value["answer_handoff_declared"] = False
    elif change == "stale":
        future = datetime.now(UTC) + timedelta(seconds=301)
        monkeypatch.setattr("app.core.fact_completion.utc_now", lambda: future)
    ex.prepare = AsyncMock(side_effect=AssertionError("resume must not query again"))
    if change == "lease":
        with pytest.raises(RetryRun):
            await ex(worker_context(run, store))
        assert await scalar(a.db, "SELECT count(*) FROM case_drafts") == 0
        return
    result = (await ex(worker_context(run, store))).result
    assert result["case_applied"] is (change in {"none", "new_attempt"})
    assert "answer_confidence" not in result and "answer_handoff_declared" not in result
    ex.prepare.assert_not_awaited()
    ex.answer.assert_not_awaited()
    assert await scalar(a.db, "SELECT count(*) FROM case_actions WHERE kind='handoff'") == 0
    assert await scalar(a.db, "SELECT count(*) FROM ticket_notifications") == 0


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
@pytest.mark.parametrize("change", ["stale", "body", "signature", "clock_refresh", "receipt", "actor"])
async def test_draft_body_source_signature_and_freshness_are_rechecked_at_approval(pair, monkeypatch, change):
    a, _ = pair
    approver = ACTOR
    run = await fresh_case(a)
    ex, _ = executor(a)
    result = (await ex(worker_context(run))).result
    case = await a.get(ACTOR, result["case_id"])
    assert case["drafts"][0]["valid"]
    if change == "stale":
        future = datetime.now(UTC) + timedelta(seconds=301)
        monkeypatch.setattr("app.core.fact_completion.utc_now", lambda: future)
    elif change == "actor":
        # The approving employee can be valid while the submitting employee's
        # saved authorization has changed.
        async def lookup(actor_id, instance, **_):
            return replace(ACTOR, auth_version=1) if actor_id == ACTOR.actor_id else ADMIN
        a.actors = ActorDirectory(lookup)
        approver = ADMIN
    else:
        draft = case["drafts"][0]
        evidence = copy.deepcopy(draft["evidence"])
        if change == "body":
            await a.db.execute("UPDATE case_drafts SET body=body||' changed' WHERE id=$1", draft["id"])
        else:
            business = next(e for e in evidence if e.get("source") == "business_query")
            if change == "signature":
                business["evidence_payload"]["_fact_dispatch"]["signature"] = "forged"
            elif change == "clock_refresh":
                business["evidence_payload"]["observed_at"] = datetime.now(UTC).isoformat()
            else:
                next(e for e in evidence if e.get("source") == "fact_projection")["receipt"] = {"task": "invalid task"}
            await a.db.execute("UPDATE case_drafts SET evidence=$2::jsonb WHERE id=$1", draft["id"], json.dumps(evidence))
    stale = await a.get(approver, result["case_id"])
    assert not stale["drafts"][0]["valid"]
    with pytest.raises(HTTPException) as exc:
        await a.approve(approver, case["id"], case["drafts"][0]["id"], DraftApproval(expected_version=case["version"],
            expected_control_version=case["control_version"], body_hash=case["drafts"][0]["body_hash"]))
    assert exc.value.detail["code"] == "draft_evidence_stale"


@pytest.mark.skipif(not PG or not os.getenv("TEST_REDIS_URL"), reason="disposable PostgreSQL and Redis are required")
async def test_restart_after_sql_before_proposal_reuses_original_authenticated_execution(pair):
    import redis.asyncio as redis

    a, _ = pair
    run = await fresh_case(a)
    store = PgRunStore(a.db, a.run_instance)
    cache = redis.from_url(os.environ["TEST_REDIS_URL"], decode_responses=True)
    prefix = "fact-control-" + uuid.uuid4().hex
    tools = FreshTools()
    manager = ToolManager(api_key="test-key")

    async def order(params, context):
        return (await tools.call("order_data_query", params, context=context)).data

    async def knowledge(params, context):
        return []

    manager.register(Tool("order_data_query", "controlled", order, {"type": "object"}))
    manager.register(Tool("knowledge_search", "controlled", knowledge, {"type": "object"}))
    ex, _ = executor(a)
    original_completion = ex.complete_facts

    async def prepare(run, snapshot, degrade):
        return await prepare_turn(run.message, user_id=run.user_id, conv_id=run.conv_id, request_id=run.request_id,
                                  deps=dependencies(manager), case_context=snapshot)

    ex.prepare = prepare
    ex.complete_facts = AsyncMock(side_effect=RuntimeError("crash after SQL, before proposal"))
    try:
        first = worker_context(run, store)
        first.scope = RunScope(run.run_id, run.attempt, memo=RedisMemo(cache, prefix=prefix))
        with pytest.raises(RetryRun):
            await ex(first)
        assert not (await a.run(run.run_id)).checkpoint
        assert len([c for c in tools.calls if c[0] == "order_data_query"]) == 1
        await a.db.execute("UPDATE agent_runs SET lease_until=0 WHERE run_id=$1", run.run_id)
        resumed = await store.claim(run.run_id, "replacement-worker", 60)
        second = worker_context(resumed, store)
        second.scope = RunScope(run.run_id, resumed.attempt, memo=RedisMemo(cache, prefix=prefix))
        ex.complete_facts = original_completion
        result = (await ex(second)).result
        assert result["case_applied"] and result["answer_origin"] == ORIGIN
        assert result["fact_receipt"]["task"]["execution_attempt"] == 1 and result["run"]["attempt"] == 2
        assert result["business_data"]["evidence_payload"]["_fact_dispatch"]["query"] == next(
            params["query"] for name, params, _ in tools.calls if name == "order_data_query")
        assert len([c for c in tools.calls if c[0] == "order_data_query"]) == 1
        ex.answer.assert_not_awaited()
        case = await a.get(ACTOR, result["case_id"])
        assert case["drafts"][0]["valid"]
    finally:
        await cache.delete(RedisMemo(cache, prefix=prefix)._key(run.run_id))
        await cache.aclose()


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
@pytest.mark.parametrize("change", ["lost_inner_mode", "lost_all_inner_headers", "v2_false_only", "unknown_inner", "unknown_outer", "legacy_yes"])
async def test_durable_checkpoint_cannot_downgrade_fact_mode_to_legacy(pair, change):
    service, _ = pair
    run = await fresh_case(service)
    ex, _ = executor(service)
    store = PgRunStore(service.db, service.run_instance)
    complete = service.complete

    async def crash(*_):
        raise RuntimeError("controlled stop after checkpoint")

    service.complete = crash
    with pytest.raises(RetryRun):
        await ex(worker_context(run, store))
    service.complete = complete
    checkpoint = copy.deepcopy((await store.get(run.run_id)).checkpoint)
    assert checkpoint["case_proposal_origin"] == ORIGIN
    value = checkpoint["case_proposal"]
    if change in {"lost_inner_mode", "lost_all_inner_headers", "v2_false_only"}:
        value.pop("answer_origin")
        value.pop("fact_receipt")
        if change == "lost_all_inner_headers":
            value.pop("answer_model_invoked")
        if change == "v2_false_only":
            checkpoint.pop("case_proposal_origin")
    elif change == "unknown_inner":
        value["answer_origin"] = "unknown-origin"
    elif change == "unknown_outer":
        checkpoint["case_proposal_origin"] = "unknown-origin"
    else:
        # A real old checkpoint has none of the new fields. Signed tool values
        # alone must not erase its explicit model decision.
        checkpoint = {"case_proposal": {"response": "保留原模型人工决定。", "handoff": True, "escalated": True,
            "answer_handoff_declared": True, "answer_confidence": .65, "policy_violations": [],
            "agent_type": "order", "intent": "delivery", "knowledge_sources": [], "business_data": value["business_data"]}}
    assert await store.checkpoint(run.run_id, run.lease_owner, run.attempt, checkpoint)
    await service.db.execute("UPDATE agent_runs SET lease_until=0 WHERE run_id=$1", run.run_id)
    resumed = await store.claim(run.run_id, "mode-recovery-worker", 60)
    assert resumed.attempt == 2
    ex.prepare = AsyncMock(side_effect=AssertionError("checkpoint must not invoke prepare"))
    ex.complete_facts = AsyncMock(side_effect=AssertionError("checkpoint must not recompute facts"))
    result = (await ex(worker_context(resumed, store))).result
    ex.prepare.assert_not_awaited()
    ex.complete_facts.assert_not_awaited()
    ex.answer.assert_not_awaited()
    if change == "legacy_yes":
        # The recovered decision is the old checkpoint's own; the staff message only
        # looks up verified facts, so the read-only boundary then withholds the action.
        assert result["case_applied"] and result["answer_handoff_declared"] is True
        assert result["answer_confidence"] == .65 and not result["handoff"]
        assert result["handoff_review"]["outcome"] == "not_authorized_for_read_only_case"
        assert result["handoff_review"]["original_answer_confidence"] == .65
    else:
        assert not result["case_applied"] and result["case_rejection"] == "draft_evidence_stale"
        assert result["answer_model_invoked"] is False
        assert await scalar(service.db, "SELECT count(*) FROM case_drafts") == 0
    assert await scalar(service.db, "SELECT count(*) FROM case_actions WHERE kind='handoff'") == 0
    assert await scalar(service.db, "SELECT count(*) FROM ticket_notifications") == 0


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
@pytest.mark.parametrize("change", ["missing_fact", "empty_evidence", "duplicate_fact", "missing_run_header", "missing_applied_header",
                                   "missing_run", "missing_applied", "changed_body"])
async def test_fact_draft_requires_agreement_with_durable_run_and_case_receipt(pair, change):
    service, _ = pair
    run = await fresh_case(service)
    ex, _ = executor(service)
    result = (await ex(worker_context(run, PgRunStore(service.db, service.run_instance)))).result
    case = await service.get(ACTOR, result["case_id"])
    draft = case["drafts"][0]
    assert draft["valid"]
    evidence = copy.deepcopy(draft["evidence"])
    if change in {"missing_fact", "empty_evidence", "duplicate_fact"}:
        if change == "missing_fact":
            evidence = [e for e in evidence if e.get("source") != "fact_projection"]
        elif change == "empty_evidence":
            evidence = []
        else:
            evidence.append(next(e for e in evidence if e.get("source") == "fact_projection"))
        await service.db.execute("UPDATE case_drafts SET evidence=$2::jsonb WHERE id=$1", draft["id"], json.dumps(evidence))
    elif change == "missing_run_header":
        await service.db.execute("UPDATE agent_runs SET result=result-'answer_origin'-'fact_receipt'-'answer_model_invoked' WHERE run_id=$1", run.run_id)
    elif change == "missing_applied_header":
        await service.db.execute("UPDATE case_run_receipts SET result=result-'answer_origin'-'fact_receipt'-'answer_model_invoked' WHERE run_id=$1", run.run_id)
    elif change == "missing_run":
        await service.db.execute("DELETE FROM agent_runs WHERE run_id=$1", run.run_id)
    elif change == "missing_applied":
        await service.db.execute("DELETE FROM case_run_receipts WHERE run_id=$1", run.run_id)
    else:
        await service.db.execute("UPDATE case_drafts SET body=body||' changed' WHERE id=$1", draft["id"])
    shown = await service.get(ACTOR, result["case_id"])
    assert shown["drafts"][0]["valid"] is False
    with pytest.raises(HTTPException) as error:
        await service.approve(ACTOR, case["id"], draft["id"], DraftApproval(expected_version=case["version"],
            expected_control_version=case["control_version"], body_hash=draft["body_hash"]))
    assert error.value.detail["code"] == "draft_evidence_stale"
    assert await scalar(service.db, "SELECT count(*) FROM case_actions") == 0
    assert await scalar(service.db, "SELECT count(*) FROM ticket_notifications") == 0
