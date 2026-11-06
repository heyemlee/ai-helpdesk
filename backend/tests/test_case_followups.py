"""Recorded Case follow-ups must request fresh evidence and convey actual operations.

The SQL and answer stubs below do not predict a model's response to the new
context. Real-model quality remains a separate, versioned acceptance run.
"""
import copy
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from evidence_fixtures import with_evidence
from test_cases import ACTOR, PG, claim, follow, scalar
from test_cases import pair as pair  # noqa: F401

from app.agents.tool_loop import LoopConfig, LoopSetup
from app.agents.turn_prep import TurnDeps, prepare_turn
from app.core.intent_recognizer import IntentCategory, UrgencyLevel
from app.events import EventLog, EventLogConfig, bind
from app.tickets.cases import CaseInput
from app.tools.sql_agent_client import SQLAgentClient
from app.tools.tool_manager import ToolResult
from app.tools.turn_tools import Prefetch, loop_tools


def recorded(name):
    path = Path(__file__).parent / "fixtures/stage6_case_followups.json"
    return json.loads(path.read_text())["cases"][name]


class Tools:
    def __init__(self, data):
        self.data, self.calls = data, []

    async def call(self, name, params, **_):
        self.calls.append((name, params))
        return ToolResult(success=True, data=copy.deepcopy(self.data) if name == "order_data_query" else [],
                          tool_name=name)


def dependencies(tools, *, paused="", outage="", intent=IntentCategory.ORDER_STATUS):
    prefetch = Prefetch(tool_manager=tools, sql_agent=SQLAgentClient(base_url="http://unused.invalid"),
                        data_paused=paused)
    return TurnDeps(
        load_memory=AsyncMock(side_effect=AssertionError("Case must use only its own snapshot")),
        recognize_intent=AsyncMock(return_value=SimpleNamespace(
            intent=intent, intent_group="query", urgency=UrgencyLevel.LOW,
            confidence=.98, entities={}, fast_path=True)),
        outage_reason=lambda: outage, prefetch=prefetch,
        loop=LoopSetup(tools=loop_tools(prefetch), config=LoopConfig()),
    )


async def prepare(record, deps):
    return await prepare_turn(record["message"], user_id=ACTOR.actor_id,
                              conv_id="case-staff-" + record["snapshot"]["case_id"],
                              request_id="test-case-followup", deps=deps, case_context=record["snapshot"])


@pytest.mark.parametrize("name", ["followup", "return_to_first"])
async def test_recorded_followups_request_new_rows_without_promoting_history_to_evidence(name):
    record = recorded(name)
    tools = Tools(record["sql_response"])
    deps = dependencies(tools)
    log = await EventLog.open(EventLogConfig(mode="full", instance="staff"))
    try:
        recorder = log.open_turn(conv_id="case-followup", request_id="test")
        with bind(recorder):
            turn = await prepare(record, deps)
        recorder.close()
        await log.flush(timeout_s=1.)
        events = [e.to_dict() for e in await log.store.events_of(recorder.turn_id)]
    finally:
        await log.close()

    query = next(params["query"] for name, params in tools.calls if name == "order_data_query")
    assert "重新查询" in query and "只读" in query
    assert record["delegated_query"] in query  # original scope and question remain intact
    assert "SO-2026-16031" not in query  # the other Case is never a query target
    assert turn.request.message == record["delegated_query"]
    assert deps.recognize_intent.call_args.args == (record["delegated_query"],)
    knowledge_query = next(params["query"] for name, params in tools.calls if name == "knowledge_search")
    assert knowledge_query == record["delegated_query"]
    assert "status 仅表示工单流程状态" in turn.request.context

    # A downstream service can still decline to query. Its plausible old answer
    # must remain unusable: no row, no evidence, no cached status substitution.
    assert turn.evidence["business_data"]["outcome"] == "not_applicable"
    assert turn.request.grounding_sources == []
    assert record["sql_response"]["answer"] not in turn.request.context
    assert not deps.load_memory.called
    loop = turn.request.turn
    call = next(item for item in loop.trajectory if item["tool"] == "order_data_query")
    assert call["args"] == {"query": query}
    logged = next(e for e in events if e["type"] == "tool_call" and e["data"]["tool"] == "order_data_query")
    assert logged["body"]["args"] == call["args"]
    tool = loop.tools["order_data_query"]
    # Both forms identify this turn's one operation, never a previous turn's result.
    for text in (query, turn.request.message):
        assert tool.memo_key(tool.params(query=text)) in loop.memo


async def test_followup_uses_new_observation_instead_of_the_previous_status():
    record = recorded("followup")
    tools = Tools(record["sql_response"])
    deps = dependencies(tools)
    old = await prepare(record, deps)
    tools.data = with_evidence({
        "queried_database": True, "has_risk": False, "degraded": False,
        "selected_databases": ["erp_db"], "trace_id": "new-query", "run_id": "new-run",
        "steps": [{"database": "erp_db", "success": True,
                   "final_sql": "SELECT order_no, order_status FROM orders WHERE order_no = 'SO-2026-11030'"}],
    }, rows=[{"order_no": "SO-2026-11030", "order_status": "配送中"}])
    current = await prepare(record, deps)
    assert old.request.grounding_sources == []
    assert current.evidence["business_data"]["outcome"] == "evidence"
    assert "配送中" in "\n".join(current.request.grounding_sources)
    assert "待发货" not in "\n".join(current.request.grounding_sources)
    assert len([c for c in tools.calls if c[0] == "order_data_query"]) == 2
    assert old.request.turn.memo is not current.request.turn.memo


@pytest.mark.parametrize("kind", ["unavailable", "paused", "not_business"])
async def test_case_query_instruction_does_not_open_a_closed_prefetch_gate(kind):
    record = recorded("followup")
    tools = Tools(record["sql_response"])
    deps = dependencies(tools, paused="load_shed" if kind == "paused" else "",
                        outage="credential_rejected" if kind == "unavailable" else "",
                        intent=IntentCategory.GREETING if kind == "not_business" else IntentCategory.ORDER_STATUS)
    turn = await prepare(record, deps)
    assert not [c for c in tools.calls if c[0] == "order_data_query"]
    assert not turn.evidence["business_data"]["attempted"]


async def test_non_case_query_keeps_its_original_semantics():
    from app.memory.conversation_memory import MemoryContext

    record = recorded("followup")
    tools = Tools(record["sql_response"])
    deps = replace(dependencies(tools), load_memory=AsyncMock(return_value=MemoryContext(
        recent_messages=[], relevant_history=[], user_profile={}, summary="")))
    await prepare_turn(record["message"], user_id="u", conv_id="c", request_id="r", deps=deps)
    assert next(params["query"] for name, params in tools.calls if name == "order_data_query") == record["message"]


async def test_case_without_an_order_does_not_gain_a_business_query_instruction():
    record = recorded("followup")
    record["message"] = "请查配送政策。"
    record["snapshot"].update(order_refs=[], facts={}, issue_type="general")
    tools = Tools(record["sql_response"])
    await prepare(record, dependencies(tools, intent=IntentCategory.DELIVERY))
    assert next(params["query"] for name, params in tools.calls if name == "order_data_query") == record["message"]


@pytest.mark.parametrize("receipt_kind", ["created", "continued", "missing", "wrong_case", "employee_fact"])
async def test_creation_claim_requires_the_servers_receipt_for_this_case(receipt_kind):
    record = recorded("new_case")
    receipt = {"case_id": record["snapshot"]["case_id"], "message_id": "accepted-input", "operation": "created"}
    if receipt_kind == "continued":
        receipt["operation"] = "continued"
    if receipt_kind == "wrong_case":
        receipt["case_id"] = "tk_another_case"
    if receipt_kind == "employee_fact":
        record["snapshot"]["facts"]["input_receipt"] = {"value": json.dumps(receipt), "source": "employee_material"}
    elif receipt_kind != "missing":
        record["snapshot"]["input_receipt"] = receipt
    turn = await prepare(record, dependencies(Tools(record["sql_response"])))
    assert ("系统已创建当前新工单" in turn.request.context) == (receipt_kind == "created")
    if receipt_kind == "continued":
        assert "已保存到选定的现有工单" in turn.request.context
    assert "status 仅表示工单流程状态" in turn.request.context
    assert all("工单操作回执" not in source for source in turn.request.grounding_sources)


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
async def test_receipt_is_persisted_from_actual_input_and_survives_deduplication(pair):
    a, b = pair
    record = recorded("new_case")
    body = CaseInput(message_id="case-created", conv_id="conversation", new_case=True, issue_type="order",
                     order_refs=record["snapshot"]["order_refs"], message=record["message"])
    receipt = await a.accept_input(ACTOR, body)
    duplicate = await b.accept_input(ACTOR, body)
    assert duplicate["deduplicated"] and duplicate["run_id"] == receipt["run_id"]
    created = (await b.run(receipt["run_id"])).case_context["snapshot"]
    assert created["input_receipt"] == {"case_id": receipt["case_id"], "message_id": body.message_id,
                                        "operation": "created"}
    c = await a.get(ACTOR, receipt["case_id"])
    # The same words on an existing Case do not mean a new Case was created.
    continued = await a.accept_input(ACTOR, follow(c, message=record["message"]))
    snapshot = (await b.run(continued["run_id"])).case_context["snapshot"]
    assert snapshot["input_receipt"]["operation"] == "continued"
    assert snapshot["input_receipt"]["case_id"] == receipt["case_id"]
    assert snapshot["input_receipt"]["message_id"] == continued["message_id"]
    assert len((await a.list(ACTOR, "conversation"))["tickets"]) == 1


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
async def test_operation_receipt_does_not_cancel_an_explicit_handoff(pair):
    a, _ = pair
    record = recorded("new_case")
    receipt = await a.accept_input(ACTOR, CaseInput(
        message_id="created-but-needs-human", conv_id="conversation", new_case=True,
        issue_type="order", order_refs=record["snapshot"]["order_refs"], message=record["message"]))
    run = await claim(a, receipt)
    assert run.case_context["snapshot"]["input_receipt"]["operation"] == "created"
    payload, applied = await a.complete(run, {
        "response": record["model_answer"], "handoff": True, "answer_confidence": .65,
        "policy_violations": [], "handoff_reason": "Model explicitly requested a human",
    })
    assert applied and payload["handoff"] is True
    assert await scalar(a.db, "SELECT count(*) FROM case_actions WHERE kind='handoff'") == 1
