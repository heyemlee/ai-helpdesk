"""Recorded development failures: preserve source meaning without promoting metadata.

Stubs replay actual retrieved records, not a prediction of a corrected model
answer. Explicit handoff and strict image gates are covered alongside this file.
"""
import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from evidence_fixtures import with_evidence
from test_case_followups import dependencies, prepare

from app.agents.agent_orchestrator import BaseAgent
from app.attachments.verification import Extraction, verify
from app.core import business_facts as facts
from app.core import policy_scope as ps
from app.core.answer_policy import AnswerPolicy, PolicyStreamGuard
from app.core.intent_recognizer import IntentCategory
from app.events import EventLog, EventLogConfig, bind
from app.events.replay import rebuild_request
from app.tools import turn_tools as tt
from app.tools.sql_agent_client import should_query_business_data
from app.tools.tool_manager import ToolManager, ToolResult


def recorded(name):
    data = json.loads((Path(__file__).parent / "fixtures/stage6_policy_context.json").read_text())
    return next(r for r in data["records"] if r["scenario"] == name)


class Lookup:
    def __init__(self, sources, *, success=True):
        self.result = ToolResult(success=success, data=copy.deepcopy(sources), tool_name="knowledge_search",
                                 evidence_ok=False)
        self.calls = []

    async def call(self, name, params, **_):
        self.calls.append(name)
        assert name == "knowledge_search", "A policy-only question must not run SQL"
        return self.result

    async def search_with_rewrite(self, *_, **__):
        return self.result


@pytest.mark.parametrize("name", ["policies-001", "policies-002", "policies-004"])
async def test_recorded_policy_turns_keep_scope_and_outcome_without_sql(name):
    record = recorded(name)
    tools = Lookup(record["sources"])
    log = await EventLog.open(EventLogConfig(mode="full", instance="staff"))
    try:
        recorder = log.open_turn(conv_id="policy-context", request_id=name)
        recorder.emit("turn_started", {}, {"message": record["message"], "user_id": "u"})
        with ps.bind_policy_scope(ps.PolicyScope.model_validate(record["snapshot"]["policy_scope"])), bind(recorder):
            turn = await prepare(record, dependencies(tools, intent=IntentCategory.DELIVERY))
        recorder.close()
        await log.flush(timeout_s=1.)
        events = [e.to_dict() for e in await log.store.events_of(recorder.turn_id)]
    finally:
        await log.close()
    assert tools.calls == ["knowledge_search"]
    assert not turn.evidence["business_data"]["attempted"]
    assert turn.request.turn.tools["order_data_query"].skip(turn.request.turn)
    assert turn.request.reference_times["policy_as_of"]
    rebuilt = rebuild_request(events).request
    assert rebuilt.context == turn.request.context
    assert rebuilt.reference_times == turn.request.reference_times
    assert rebuilt.grounding_sources == turn.request.grounding_sources
    if record["sources"]:
        source = record["sources"][0]
        assert source["policy_revision"] in turn.request.context
        assert source["policy"]["version"] in turn.request.context
        for key in ("effective_at", "expires_at", "product", "region", "audience"):
            assert key in turn.request.context
        assert "2026-11-05" not in "\n".join(turn.request.grounding_sources)
        assert turn.evidence["knowledge"]["evidence_ok"] is False  # never force sufficiency
    else:
        draft = facts.render_draft([], turn.evidence["business_data"], knowledge_state=turn.evidence["knowledge"])
        assert "未检索到符合所选范围和时点的已发布政策" in draft
        assert "查询成功" not in draft and "513" not in draft


@pytest.mark.parametrize("name", ["policies-001", "policies-002", "policies-004"])
def test_recorded_metadata_field_is_not_a_promise_and_manual_marker_survives(name):
    record = recorded(name)
    times = ps.policy_reference_times(record["snapshot"]["policy_scope"], record["sources"])
    sentence = record["violations"][0]["sentence"]
    result = AnswerPolicy().enforce(sentence, [], reference_times=times)
    assert not result.blocked and result.text == sentence
    stream = PolicyStreamGuard(AnswerPolicy(), [], reference_times=times)
    text = "".join(stream.feed(sentence[i:i+3]) for i in range(0, len(sentence), 3)) + stream.flush()
    assert text == sentence and not stream.violations
    _, declared = BaseAgent._split_handoff_trailer(record["model_answer"])
    assert declared is True


@pytest.mark.parametrize("sentence", [
    "保证2026年11月5日送达。", "预计2026年11月5日发货。", "2026年11月5日到账。",
    "所选日期：2026年11月5日，保证当天办理。", "所选日期：保证2026年11月5日送达",
    "所选日期：2026年11月5日；2026年11月6日送达。",
    "所选日期：2026年11月6日", "所选日期：2026年11月5日 20:00:02 UTC",
    "生效日期：2026年11月5日", "所选日期：2026年11月5日，费用513美元。",
    "Selected date: 2026-11-05; guaranteed delivery on 2026-11-05.",
])
def test_metadata_dates_never_authorize_actions_wrong_types_or_extra_dates(sentence):
    times = {"policy_as_of": ["2026-11-05T20:00:01Z"]}
    policy = AnswerPolicy()
    assert policy.enforce(sentence, [], reference_times=times).blocked
    stream = PolicyStreamGuard(policy, [], reference_times=times)
    for token in sentence:
        stream.feed(token)
    stream.flush()
    assert stream.violations


def test_effective_and_expiry_are_separate_typed_sources():
    record = recorded("policies-002")
    times = ps.policy_reference_times(record["snapshot"]["policy_scope"], record["sources"])
    policy = AnswerPolicy()
    assert not policy.enforce("失效日期：2026-11-05T20:00:00+00:00", [], reference_times=times).blocked
    assert policy.enforce("生效日期：2026-11-05", [], reference_times=times).blocked
    assert policy.enforce("所选日期：2026年11月5日", []).blocked
    assert policy.enforce("2026年11月5日保证送达。", [], reference_times=times).blocked


@pytest.mark.parametrize("change", [
    lambda r: r.update(policy_revision=""),
    lambda r: r["policy"].update(status="draft"),
    lambda r: r["policy"].update(region="NV"),
    lambda r: r["policy"].update(audience="customer"),
    lambda r: r.update(policy_as_of="2026-11-05T19:59:59Z"),
])
async def test_wrong_revision_scope_and_time_do_not_supply_metadata(change):
    source = recorded("policies-001")["sources"][0]
    change(source)
    assert ps.policy_source_metadata(source) is None
    assert ps.policy_reference_times(sources=[source]) == {}
    trace = {}
    text, used = await tt.knowledge_lookup(Lookup([source]), "配送政策", trace=trace)
    assert not used and trace["degraded"]
    assert source["content"] not in text


@pytest.mark.parametrize("message,entities", [
    ("请查配送政策和订单 SO-123 的状态", {}), ("根据退款政策查询我实际付款多少", {}),
    ("政策怎么写，库存还剩多少？", {}), ("政策允许的话帮我退款", {}),
    ("Shipping policy and actual delivery records", {}), ("配送政策", {"order_id": ["SO-123"]}),
    ("配送政策", {"item_code": ["BX-12"]}),
    ("配送政策怎么说，我的货到哪了", {}), ("请安排配送并说明政策", {}),
    ("Refund policy and whether this refund has arrived", {}),
])
def test_mixed_policy_and_business_questions_keep_sql(message, entities):
    assert should_query_business_data(message, intent=IntentCategory.DELIVERY, entities=entities)


@pytest.mark.parametrize("failed", [False, True])
async def test_empty_lookup_is_distinct_from_failed_retrieval(failed):
    manager = ToolManager(api_key="test")
    manager._rewritten = AsyncMock(return_value=(["配送政策"], False))
    manager.call = AsyncMock(return_value=ToolResult(success=not failed, data=[], tool_name="knowledge_search"))
    result = await manager._search_with_rewrite("knowledge_search", "配送政策")
    assert result.success is (not failed)
    assert result.lookup_status == ("unavailable" if failed else "empty")
    trace = {"attempted": True}
    lookup = SimpleNamespace(search_with_rewrite=AsyncMock(return_value=result))
    text, used = await tt.knowledge_lookup(lookup, "配送政策", trace=trace)
    outcome = tt.knowledge_outcome(text, used, trace)
    assert not outcome.evidence
    draft = facts.render_draft([], {}, knowledge_state=trace)
    assert ("未检索到符合所选范围和时点的已发布政策" in draft) is (not failed)
    assert ("不能确定发布状态" in draft) is failed


def description_for(step, column="delivery_date"):
    path = Path(__file__).resolve().parents[2] / "ai-sql-agent/tests/fixtures/stage6_field_description.json"
    config = json.loads(path.read_text())
    source = {k: config[k] for k in ("database", "table", "column")}
    source.update(config["metadata"])
    source["column"] = column
    return {**source, "output_field": column, "schema_revision": hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
            "query_hash": hashlib.sha256(step["final_sql"].encode()).hexdigest()}


def recorded_business():
    record = recorded("images-001")
    data = record["sql_response"]
    data["observed_at"] = datetime.now(UTC).isoformat()  # refresh observation only, retain actual values/SQL
    for step in data["steps"]:
        step["column_metadata"] = {"delivery_date": description_for(step)}
    return record, data


def test_recorded_image_date_keeps_source_meaning_in_context_and_review():
    record, data = recorded_business()
    cells, issues = facts.verified_cells(data)
    assert cells and not issues
    date = next(c for c in cells if c["field"] == "delivery_date")
    assert "计划送货日期" in date["column_metadata"]["description"]
    trace = {"attempted": True, "evidence": cells, "evidence_issues": issues, "evidence_payload": data}
    text = facts.render_cells(cells, model_context=True)
    assert "计划送货日期" in text and "实际送达日期在 svc_db.delivery.delivered_date" in text
    assert "计划送货日期" not in tt.business_grounding(text, trace)
    fields = []
    for field, fact in record["snapshot"]["facts"].items():
        if fact.get("source") == "verified_image":
            fields.append(dict(field=field, value=str(fact["value"]), unit=fact.get("unit"),
                               kind={"quantity":"ordered", "date":"delivery"}.get(field, "unknown"),
                               readability="clear", location="recorded image"))
    extraction = Extraction(document_type="order", fields=fields)
    review = verify(extraction, data, record["snapshot"]["order_refs"][0])
    assert len(review["fields"]) == 5 and all(f["outcome"] == "matched" for f in review["fields"])
    source = next(f["source"] for f in review["fields"] if f["field"] == "date")
    assert source["column_metadata"] == date["column_metadata"]
    assert source["step_index"] == date["step_index"]
    assert source["query_hash"] == date["query_hash"]
    assert hashlib.sha256(source["final_sql"].encode()).hexdigest() == date["query_hash"]
    assert source["final_sql"] == data["steps"][source["step_index"]]["final_sql"]
    assert source["output_columns"] == list(data["steps"][source["step_index"]]["rows"][source["row_index"]])
    draft = facts.render_draft([], trace)
    assert "计划送货日期" in draft and "不是安装日期" in draft
    assert "svc_db" not in draft and "erp_db" not in draft and "schema_revision" not in draft and "DATE(" not in draft


@pytest.mark.parametrize("change", [
    lambda s: s.pop("column_metadata"),
    lambda s: s["column_metadata"]["delivery_date"].update(query_hash="0" * 64),
    lambda s: s["column_metadata"]["delivery_date"].update(database="svc_db"),
    lambda s: s["column_metadata"]["delivery_date"].update(column="delivered_date"),
    lambda s: s["column_metadata"]["delivery_date"].update(description="Actually delivered"),
    lambda s: s["column_metadata"]["delivery_date"].update(description="x" * 2001),
    lambda s: s.update(column_metadata=[]),
])
def test_missing_or_mismatched_source_meaning_is_unknown(change):
    _, data = recorded_business()
    step = data["steps"][0]
    change(step)
    cells, issues = facts.verified_cells(data)
    assert cells and not issues
    date = next(c for c in cells if c["field"] == "delivery_date")
    assert not date["column_metadata"]
    assert "计划送货日期" not in facts.render_cells(cells)


def test_same_value_with_conflicting_field_meaning_cannot_become_image_confirmation():
    record, data = recorded_business()
    other = copy.deepcopy(data["steps"][0])
    meaning = other["column_metadata"]["delivery_date"]
    meaning["description"] = "Actual delivery date."
    source = {k: v for k, v in meaning.items() if k not in {"query_hash", "schema_revision", "output_field"}}
    meaning["schema_revision"] = hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    data["steps"].append(other)
    data["succeeded_steps"] = 2
    fields = [dict(field="order_ref", value=record["snapshot"]["order_refs"][0], kind="unknown", unit=None,
                   readability="clear", location="header"),
              dict(field="model", value=record["snapshot"]["facts"]["model"]["value"], kind="unknown", unit=None,
                   readability="clear", location="line"),
              dict(field="date", value="2026-11-22", kind="delivery", unit=None, readability="clear", location="date")]
    review = verify(Extraction(document_type="order", fields=fields), data, record["snapshot"]["order_refs"][0])
    date = next(f for f in review["fields"] if f["field"] == "date")
    assert date["outcome"] == "conflict" and date["source"] is None


def test_stream_metadata_arriving_later_never_retroactively_authorizes_a_promise():
    stream = PolicyStreamGuard(AnswerPolicy(), [])
    stream.feed("所选日期：2026年11月5日\n")
    stream.add_reference_times({"policy_as_of": ["2026-11-05T20:00:01Z"]})
    stream.feed("所选日期：2026年11月5日\n")
    stream.feed("保证2026年11月5日到账。\n")
    stream.flush()
    assert len(stream.violations) == 2


def test_new_metadata_description_dates_do_not_enter_numeric_grounding():
    _, data = recorded_business()
    meaning = data["steps"][0]["column_metadata"]["delivery_date"]
    meaning["description"] += " Example date: 2031-02-03."
    source = {k: v for k, v in meaning.items() if k not in {"query_hash", "schema_revision", "output_field"}}
    meaning["schema_revision"] = hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    cells, _ = facts.verified_cells(data)
    text = facts.render_cells(cells)
    assert "2031-02-03" in text
    grounding = tt.business_grounding(text, {"evidence": cells})
    assert "2031-02-03" not in grounding
    assert AnswerPolicy().enforce("保证2031年2月3日送达。", [grounding]).blocked


def test_optional_field_metadata_is_schema_checked_without_requiring_old_payloads_to_have_it():
    from test_contract_fixtures import _validator_for
    _, data = recorded_business()
    validator = _validator_for("schema/artifact-data-part.schema.json")
    assert validator.is_valid(data)
    data["steps"][0]["column_metadata"]["delivery_date"]["description"] = []
    assert not validator.is_valid(data)
    del data["steps"][0]["column_metadata"]
    assert validator.is_valid(data)


def test_same_query_cannot_swap_descriptions_between_output_columns():
    step = dict(database="erp_db", final_sql="SELECT delivery_date AS planned, actual_date AS actual FROM orders")
    planned = description_for(step)
    planned["output_field"] = "planned"
    actual = {**planned, "column": "actual_date", "output_field": "actual", "description": "Actual delivery date."}
    source = {k: v for k, v in actual.items() if k not in {"schema_revision", "query_hash", "output_field"}}
    actual["schema_revision"] = hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    step["column_metadata"] = {"planned": planned, "actual": actual}
    assert facts.verified_column_metadata(step, "planned") == planned
    assert facts.verified_column_metadata(step, "actual") == actual
    step["column_metadata"] = {"planned": actual, "actual": planned}
    assert not facts.verified_column_metadata(step, "planned")
    assert not facts.verified_column_metadata(step, "actual")


def test_same_definition_from_two_queries_is_not_a_semantic_conflict():
    sql = "SELECT order_ref,delivery_date FROM orders"
    data = with_evidence(dict(queried_database=True, has_risk=False, degraded=False, trace_id="two-queries",
        steps=[dict(database="erp_db", success=True, final_sql=q) for q in (sql, sql + " ORDER BY order_ref")]),
        rows=[{"order_ref": "SO-1", "delivery_date": "2026-11-22"}])
    for step in data["steps"]:
        step["column_metadata"] = {"delivery_date": description_for(step)}
    cells, issues = facts.verified_cells(data)
    assert len(cells) == 4 and not issues
    first, second = [s["column_metadata"]["delivery_date"] for s in data["steps"]]
    assert first["query_hash"] != second["query_hash"] and first["schema_revision"] == second["schema_revision"]
    second["description"] = "Actual delivery date."
    source = {k: v for k, v in second.items() if k not in {"schema_revision", "query_hash", "output_field"}}
    second["schema_revision"] = hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    assert facts.verified_cells(data) == ([], ["conflicting_records"])


def test_unverified_no_data_cannot_be_rendered_as_a_successful_empty_query():
    record = recorded("policies-004")
    data = record["sql_response"]
    draft = facts.render_draft([], {"attempted": True, "evidence_issues": ["no_data"], "evidence_payload": data})
    assert "查询成功" not in draft
    safe = dict(queried_database=True, has_risk=False, degraded=False, execution_status="no_data",
                trace_id="empty-query", observed_at=datetime.now(UTC).isoformat(), evidence=[], steps=[dict(database="erp_db", success=True,
                    rows=[], row_count=0, truncated=False, final_sql="SELECT order_ref FROM orders WHERE order_ref='absent'",
                    as_of=datetime.now(UTC).isoformat(), as_of_source="query_time", risk_findings=[], error=None)])
    assert facts.verified_empty(safe)
    safe["observed_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    assert not facts.verified_empty(safe)
