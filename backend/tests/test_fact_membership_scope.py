"""Exact entity scope through fresh SQLite, authenticated transport and Case adoption."""
import copy
import hashlib
import json
import sqlite3
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import test_fact_completion as facts
from evidence_fixtures import with_evidence
from test_agent_tool_loop import _orchestrator
from test_case_followups import dependencies
from test_cases import ACTOR, PG, scalar
from test_cases import pair as pair  # noqa: F401

from app.agents.turn_prep import prepare_turn
from app.core.fact_completion import ORIGIN, _scope, bind_result, current_task, proposal, receipt_valid
from app.core.lookup_scope import dispatch_for
from app.events import EventLog
from app.runs.store import PgRunStore
from app.tickets.case_execution import CaseExecutor
from app.tools.sql_agent_client import SQLAgentClient
from app.tools.tool_manager import ToolResult

ENTITY = "UNIT-A73"
SELECT = "SELECT DISTINCT order_no,delivery_status FROM delivery"


@pytest.fixture(autouse=True)
def controlled_key(monkeypatch):
    monkeypatch.setenv("SQL_AGENT_API_KEY", facts.KEY)


def sql_data(dispatch, delivery_sql, *, statuses=("配送中",)):
    """Execute new local statements and construct their real cell metadata."""
    data = facts.wire(dispatch)
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        db.executescript("CREATE TABLE orders(order_no TEXT,delivery_date TEXT);"
                         "CREATE TABLE delivery(order_no TEXT,delivery_status TEXT);")
        db.execute("INSERT INTO orders VALUES (?,?)", (ENTITY, facts.VALUES["planned_delivery_date"]))
        db.execute("INSERT INTO orders VALUES (?,?)", ("OTHER-902", "2029-08-09"))
        db.executemany("INSERT INTO delivery VALUES (?,?)", [(ENTITY, status) for status in statuses])
        db.execute("INSERT INTO delivery VALUES (?,?)", ("OTHER-902", "待配送"))
        for step in data["steps"]:
            if step["database"] == "svc_db":
                step["final_sql"] = delivery_sql
            step["rows"] = [dict(r) for r in db.execute(step["final_sql"])]
            step["row_count"] = len(step["rows"])
            for metadata in step["column_metadata"].values():
                metadata["query_hash"] = hashlib.sha256(step["final_sql"].encode()).hexdigest()
    return with_evidence(data)


def execution(delivery_sql, **kwargs):
    task = facts.task_for()
    dispatch = dispatch_for(facts.goal_for(task), facts.MESSAGE)
    data = sql_data(dispatch, delivery_sql, **kwargs)
    bind_result(data, dispatch, key=facts.KEY)
    return task, facts.trace(data)


POSITIVE = [
    SELECT + " WHERE order_no='UNIT-A73'",
    SELECT + " WHERE order_no IN ('UNIT-A73')",
    SELECT + " WHERE order_no IN ('UNIT-A73','UNIT-A73')",
    "SELECT DISTINCT d.order_no,d.delivery_status FROM delivery d WHERE d.order_no IN ('UNIT-A73')",
    SELECT + " WHERE order_no='UNIT-A73' AND order_no IN ('UNIT-A73')",
    SELECT + " WHERE order_no IN ('UNIT-A73') AND order_no IN ('UNIT-A73','UNIT-A73')",
    SELECT + " WHERE (order_no='UNIT-A73' AND (order_no IN ('UNIT-A73') AND order_no='UNIT-A73'))",
    SELECT + " WHERE 'UNIT-A73'=order_no AND order_no IN ('UNIT-A73')",
]


@pytest.mark.parametrize("sql", POSITIVE)
def test_exact_literal_membership_matches_equality_without_rewriting_receipts(sql):
    task, business = execution(sql, statuses=("配送中", "配送中"))
    assert business["evidence_payload"]["steps"][0]["final_sql"] == sql
    result = proposal(task, business)
    assert result and result["answer_origin"] == ORIGIN and result["answer_model_invoked"] is False
    assert result["handoff"] is False and result["escalated"] is False
    assert not {"answer_confidence", "answer_handoff_declared", "handoff_review"} & result.keys()
    assert facts.VALUES["planned_delivery_date"] in result["response"] and "配送中" in result["response"]
    assert receipt_valid(result["fact_receipt"], business, message=facts.MESSAGE, context=facts.context(),
                         run_id=task["run_id"], attempt=1, body=result["response"])
    changed = copy.deepcopy(business)
    changed["evidence_payload"]["steps"][0]["final_sql"] += " "
    assert proposal(task, changed) is None  # even equivalent SQL cannot edit a signed receipt


NEGATIVE = [
    SELECT + " WHERE order_no IN ('UNIT-A73','UNRETURNED-8')",
    SELECT + " WHERE order_no IN ('UNIT-A73',NULL)",
    SELECT + " WHERE order_no IN (NULL)",
    SELECT + " WHERE order_no IN ()",
    SELECT + " WHERE order_no IN (73)",
    SELECT + " WHERE order_no IN (CAST('UNIT-A73' AS TEXT))",
    SELECT + " WHERE order_no IN ('UNIT-A73' COLLATE NOCASE)",
    SELECT + " WHERE order_no IN (SELECT 'UNIT-A73')",
    SELECT + " WHERE order_no NOT IN ('OTHER-902')",
    SELECT + " WHERE order_no IN ('UNIT-A73') OR order_no='UNRETURNED-8'",
    SELECT + " WHERE order_no IN ('UNIT-A73') AND delivery_status='配送中'",
    SELECT + " WHERE delivery_status IN ('配送中')",
    SELECT + ' WHERE "order_no" IN (\'UNIT-A73\')',
    SELECT + " WHERE order_no IN ('UNIT-A73') LIMIT 1",
    SELECT + " WHERE order_no IN ('UNIT-A73') GROUP BY order_no,delivery_status",
    SELECT + " WHERE order_no IN ('UNIT-A73') UNION ALL " + SELECT + " WHERE 1=0",
    "SELECT DISTINCT d.order_no,d.delivery_status FROM delivery d JOIN orders o "
    "ON o.order_no=d.order_no WHERE o.order_no IN ('UNIT-A73')",
    "SELECT order_no,MAX(delivery_status) AS delivery_status FROM delivery WHERE order_no IN ('UNIT-A73')",
    "SELECT order_no FROM delivery WHERE order_no IN ('UNIT-A73')",
    SELECT + " WHERE order_no IN ('UNIT-A73') AND order_no IN ('UNIT-A73','UNRETURNED-8')",
    SELECT + " WHERE order_no IN ('UNIT-A73') AND NOT order_no='OTHER-902'",
    SELECT + " WHERE (order_no IN ('UNIT-A73') OR order_no='UNIT-A73')",
    SELECT + " WHERE order_no IN ('UNIT-A73') AND delivery_status IN ('配送中')",
]


@pytest.mark.parametrize("sql", NEGATIVE)
def test_single_entity_result_does_not_prove_a_different_query_scope(sql):
    task, business = execution(sql)
    step = business["evidence_payload"]["steps"][0]
    assert _scope(step, task, ["delivery_status"]) is False
    assert proposal(task, business) is None


@pytest.mark.parametrize("statuses", [(None,), ("配送中", "已送达")])
def test_exact_membership_does_not_make_null_or_conflicting_values_complete(statuses):
    task, business = execution(POSITIVE[1], statuses=statuses)
    assert _scope(business["evidence_payload"]["steps"][0], task, ["delivery_status"])
    assert proposal(task, business) is None


@pytest.mark.parametrize("change", ["risk", "missing_step", "spurious_unit", "wrong_source", "stale", "unsigned"])
def test_membership_still_requires_the_entire_authenticated_fact_contract(change):
    task = facts.task_for()
    dispatch = dispatch_for(facts.goal_for(task), facts.MESSAGE)
    data = sql_data(dispatch, POSITIVE[1])
    if change == "risk":
        data["has_risk"] = True
    elif change == "missing_step":
        data["steps"].pop()
    elif change == "spurious_unit":
        # A spurious unit cannot be laundered into a unitless date/status fact.
        data["steps"][0]["column_units"]["delivery_status"] = "days"
    elif change == "wrong_source":
        data["steps"][0]["column_metadata"]["delivery_status"]["table"] = "orders"
    elif change == "stale":
        data["steps"][0]["as_of"] = "2020-01-01T00:00:00Z"
    data = with_evidence(data)
    bind_result(data, dispatch, key=facts.KEY)
    if change == "unsigned":
        data["_fact_dispatch"]["signature"] = "remote-forgery"
    assert proposal(task, facts.trace(data)) is None


class SQLiteTools:
    def __init__(self, sql):
        self.sql, self.calls = sql, []

    async def call(self, name, params, context=None, **_):
        self.calls.append(name)
        if name != "order_data_query":
            return ToolResult(success=True, data=[], tool_name=name)
        dispatch = context["lookup_dispatch"]
        assert current_task() == dispatch["fact_task"]
        def handler(request):
            assert request.headers["X-API-Key"] == facts.KEY
            assert request.headers["X-Trace-Id"] == dispatch["nonce"]
            assert json.loads(request.content)["query"] == params["query"]
            return httpx.Response(200, json=sql_data(dispatch, self.sql), headers={"X-Run-Id": "local-sql-run"})
        original = httpx.AsyncClient
        with patch("httpx.AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)):
            data = await SQLAgentClient(base_url="http://controlled.invalid", api_key=facts.KEY).query(
                params["query"], lookup_dispatch=dispatch)
        return ToolResult(success=True, data=data, tool_name=name)


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
@pytest.mark.parametrize("sql", POSITIVE[1:])
async def test_membership_runs_before_answer_and_persists_no_handoff(pair, sql):
    a, b = pair
    run = await facts.fresh_case(a)
    tools = SQLiteTools(sql)
    orch = _orchestrator()
    answer = AsyncMock(side_effect=AssertionError("complete scoped facts must precede the answer model"))
    async def prepare(run, snapshot, degrade):
        return await prepare_turn(run.message, user_id=run.user_id, conv_id=run.conv_id,
                                  request_id=run.request_id, deps=dependencies(tools), case_context=snapshot)
    executor = CaseExecutor(a, prepare=prepare, answer=answer, complete_facts=orch.complete_facts,
                            event_log=EventLog.disabled())
    result = (await executor(facts.worker_context(run, PgRunStore(a.db, a.run_instance)))).result
    answer.assert_not_awaited()
    assert tools.calls.count("order_data_query") == 1
    assert result["case_applied"] and result["answer_origin"] == ORIGIN
    assert result["answer_model_invoked"] is False and result["handoff"] is False
    case = await b.get(ACTOR, result["case_id"])
    assert case["control_mode"] == "assistant" and case["status"] == "in_progress"
    assert len(case["drafts"]) == 1 and case["drafts"][0]["valid"]
    assert await scalar(a.db, "SELECT count(*) FROM case_actions WHERE kind='handoff'") == 0
    assert await scalar(a.db, "SELECT count(*) FROM ticket_notifications") == 0


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
async def test_unproven_membership_keeps_the_model_answer_and_records_its_handoff_proposal(pair):
    from conftest import ScriptedModel, text_step

    from app.agents.agent_orchestrator import AgentType

    a, _ = pair
    run = await facts.fresh_case(a)
    tools = SQLiteTools(NEGATIVE[0])  # the second entity has no returned row
    model = ScriptedModel(text_step("已核对资料，仍需人工复核。\n[[handoff:yes]]"))
    orch = _orchestrator((AgentType.ORDER, model))
    async def prepare(run, snapshot, degrade):
        return await prepare_turn(run.message, user_id=run.user_id, conv_id=run.conv_id,
                                  request_id=run.request_id, deps=dependencies(tools), case_context=snapshot)
    executor = CaseExecutor(a, prepare=prepare, answer=orch.run, complete_facts=orch.complete_facts,
                            event_log=EventLog.disabled())
    result = (await executor(facts.worker_context(run, PgRunStore(a.db, a.run_instance)))).result
    # The model answer is used (no deterministic projection), and its handoff stays
    # a recorded proposal: the staff message only looks something up.
    assert len(model.requests) == 1 and result["handoff"] is False
    assert result["answer_handoff_declared"] is True and result["answer_confidence"] == .65
    assert result["handoff_review"]["outcome"] == "not_authorized_for_read_only_case"
    assert result.get("answer_origin") != ORIGIN
    assert await scalar(a.db, "SELECT count(*) FROM case_actions WHERE kind='handoff'") == 0
