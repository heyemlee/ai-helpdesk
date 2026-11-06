"""Development evidence replay and independent neighboring negative controls."""
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import make_request, stub_agents
from fastapi import HTTPException
from test_attachments import connected as connected  # noqa: F401
from test_attachments import upload
from test_case_followups import Tools, dependencies, prepare
from test_cases import ACTOR, PG, claim
from test_cases import pair as pair  # noqa: F401
from test_image_query_regression import evidence, extraction

from app.attachments.verification import enforce_review_dependencies, verify
from app.core.answer_policy import AnswerPolicy, PolicyStreamGuard
from app.core.business_facts import render_draft, verified_nulls
from app.core.intent_recognizer import IntentCategory
from app.core.policy_scope import policy_reference_times, read_only_policy_query
from app.runs.checkpoint import decode_result, encode_result
from app.tickets.cases import CaseInput, DraftApproval
from app.tickets.draft_decision import reconcile_handoff


def record(name):
    return json.loads((Path(__file__).parent / "fixtures/remaining_development_failures.json").read_text())["records"]["development-" + name]


def payload(name):
    value = record(name)["result"]["business_data"]["evidence_payload"]
    value["observed_at"] = datetime.now(UTC).isoformat()
    return value


@pytest.mark.parametrize("number", ["005", "006", "008", "010", "011", "012", "013", "015"])
def test_recorded_metadata_dates_are_checked_by_role_and_stream_identically(number):
    result = record("policies-" + number)["result"]
    times = policy_reference_times(result["knowledge"]["lookup_scope"], result["knowledge_sources"])
    text = "\n".join(v["sentence"] for v in result["policy_violations"])
    guard = AnswerPolicy()
    checked = guard.enforce(text, [], reference_times=times)
    assert not checked.blocked, checked.violations
    stream = PolicyStreamGuard(guard, [], reference_times=times)
    streamed = "".join(stream.feed(text[i:i+2]) for i in range(0, len(text), 2)) + stream.flush()
    assert streamed == text and not stream.violations


@pytest.mark.parametrize("extra", [
    "；保证2026年11月5日送达。", "；费用998美元。", "；保证当天完成。",
])
def test_a_metadata_date_never_authorizes_other_claims_in_the_same_sentence(extra):
    times = {"policy_as_of": ["2026-11-05T20:00:01Z"]}
    assert AnswerPolicy().enforce("查询时点：2026年11月5日 20:00:01（UTC）" + extra,
                                  [], reference_times=times).blocked


@pytest.mark.parametrize("sentence", [
    "查询时点：2026年11月6日 20:00:01（UTC）。",
    "查询时点：2026年11月5日 20:00:02（UTC）。",
    "自2026年11月5日 20:00:01（UTC）起生效。",
])
def test_wrong_date_clock_and_effective_role_are_still_rejected(sentence):
    assert AnswerPolicy().enforce(sentence, [], reference_times={"policy_as_of": ["2026-11-05T20:00:01Z"]}).blocked


@pytest.mark.parametrize("sentence", [
    "我们保证在查询时点：2026-11-05 20:00:01 UTC 发货。",
    "查询时点：2026-11-05 20:00:01 UTC，我们保证在该时点发货。",
    "按所选时点2026-11-05 20:00:01 UTC，我们会在当天完成退款。",
    "我们承诺自2026-11-05 20:00:00 UTC生效并按此时点交付。",
    "查询时点：2026-11-05 20:00:01 UTC，届时赔付。",
    "查询时点：2026-11-05 20:00:01 UTC，届时补发。",
    "查询时点：2026-11-05 20:00:01 UTC，届时返工好。",
])
def test_metadata_and_promise_cannot_share_one_authorizing_timestamp(sentence):
    times = {"policy_as_of": ["2026-11-05T20:00:01Z"], "policy_effective_at": ["2026-11-05T20:00:00Z"]}
    assert AnswerPolicy().enforce(sentence, [], reference_times=times).blocked


@pytest.mark.parametrize("missing", [False, True])
async def test_projected_null_reaches_model_but_omitted_column_does_not_become_null(missing):
    r, data = record("orders-006"), payload("orders-006")
    if missing:
        data["steps"][0]["rows"][0].pop("phone")
    turn = await prepare(r, dependencies(Tools(data)))
    assert bool(re.search(r"(?:手机号|电话): 未登记", turn.request.context)) is (not missing)
    assert bool(verified_nulls(data)) is (not missing)
    assert "本轮业务查询回执" in turn.request.context
    assert "新执行并通过核验" in turn.request.context


@pytest.mark.parametrize("change", ["healthy", "risk", "truncated", "failed"])
async def test_empty_lookup_needs_a_complete_non_risky_execution(change):
    r, data = record("orders-012"), payload("orders-012")
    data["has_risk"] = False
    data["steps"][0]["risk_findings"] = []
    if change == "risk":
        data["has_risk"] = True
        data["steps"][0]["risk_findings"] = [{"anomaly": "semantic_mismatch", "severity": "block"}]
    elif change == "truncated":
        data["result_truncated"] = True
    elif change == "failed":
        data["execution_status"] = "failed"
        data["steps"][0].update(success=False, error="test failure")
    turn = await prepare(r, dependencies(Tools(data)))
    state = turn.evidence["business_data"]
    assert (state.get("lookup_status") == "empty" and state.get("lookup_verified") is True) is (change == "healthy")
    draft = render_draft([], state)
    assert ("查询成功，未找到记录。" in draft) is (change == "healthy")


@pytest.mark.parametrize("change", ["healthy", "risk", "truncated", "partial", "unqueried", "stale", "failed"])
def test_image_absence_uses_the_same_strict_empty_lookup_proof(change):
    data = payload("orders-012")
    data["has_risk"] = False
    data["steps"][0]["risk_findings"] = []
    if change == "risk":
        data["has_risk"] = True
    elif change == "truncated":
        data["result_truncated"] = True
    elif change == "partial":
        data["steps"].append({**data["steps"][0], "success": False, "error": "failed"})
    elif change == "unqueried":
        data["queried_database"] = False
    elif change == "stale":
        data["observed_at"] = (datetime.now(UTC) - timedelta(seconds=301)).isoformat()
    elif change == "failed":
        data["execution_status"] = "failed"
        data["steps"][0].update(success=False, error="failed")
    checked = verify(extraction(), data, "SO-2026-11039")
    assert (checked["query_outcome"] == "no_record") is (change == "healthy")
    assert not any(f["outcome"] == "matched" for f in checked["fields"])


@pytest.mark.parametrize("projection", ["with_model", "without_model"])
def test_a_missing_visible_model_cannot_be_repaired_by_removing_sql_columns(projection):
    extracted = extraction()
    extracted.fields = [f for f in extracted.fields if f.field != "model"]
    row = {"order_ref": "SO-2026-11039", "ordered_qty": 5, "delivery_date": "2026-11-22", "order_status": "已交付"}
    if projection == "with_model":
        row["model"] = "BX-11039-24"
    data = evidence([{"database": "erp_db", "success": True, "truncated": False,
                      "rows": [row], "row_count": 1, "column_units": {"ordered_qty": "pcs"}}])
    reviewed = verify(extracted, data, row["order_ref"])
    assert not any(f["outcome"] == "matched" for f in reviewed["fields"])


@pytest.mark.parametrize("key", ["database", "step_index", "row_index", "order_ref", "query_hash"])
def test_missing_source_identity_parts_cannot_match_as_none_tuples(key):
    from test_case_draft_handoff import complete_review_control, recording
    review = complete_review_control(recording()["image_review"])
    for item in review["fields"]:
        item["source"].pop(key, None)
    result = enforce_review_dependencies(review)
    assert not any(f["outcome"] == "matched" for f in result["fields"])


@pytest.mark.parametrize("kind", [None, "unknown", "order", "delivery", "parts", "label"])
def test_composite_field_dependency_cannot_be_bypassed_with_a_document_type(kind):
    review = record("images-007")["image_reviews"][0]["result"]
    review["document_type"] = kind
    assert not any(f["outcome"] == "matched" for f in enforce_review_dependencies(review)["fields"])


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
async def test_recorded_false_matches_are_rechecked_before_persisting_image_facts(connected):
    service, _, attachments, _ = connected
    image = await upload(attachments)
    receipt = await service.accept_input(ACTOR, CaseInput(message_id="anchor-guard", conv_id="conversation",
        new_case=True, issue_type="general", message="核对附件记录", attachment_ids=[image["id"]]))
    run = await claim(service, receipt)
    review = record("images-007")["image_reviews"][0]["result"]
    review.update(attachment_id=image["id"], sha256=run.case_context["snapshot"]["review_materials"][0]["sha256"])
    for item in review["fields"]:
        if item.get("source"):
            item["source"]["observed_at"] = datetime.now(UTC).isoformat()
    result, applied = await service.complete(run, {"material_reviews": [review], "response": "candidate review"})
    assert applied
    continuation = await service.run(result["next_run_id"])
    snapshot = continuation.case_context["snapshot"]
    assert not snapshot["facts"]
    assert set(snapshot["pending_fields"]) == {"image:" + field for field in ("order_ref", "model", "quantity", "date", "status")}


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
@pytest.mark.parametrize("change", ["other_case", "missing_model", "source_binding"])
async def test_persisted_review_is_rechecked_on_draft_display_and_approval(connected, change):
    from test_case_draft_handoff import executor_for, finish, verified_continuation
    from test_cases import new

    service, _, run_id, recorded = await verified_continuation(connected)
    run = await finish(service, executor_for(service, recorded), run_id)
    case_id = run.result["case_id"]
    detail = await service.get(ACTOR, case_id)
    draft = detail["drafts"][0]
    assert draft["valid"]
    saved = await service.db.fetchrow("SELECT * FROM case_image_reviews WHERE case_id=$1", case_id)
    if change == "other_case":
        other = await service.accept_input(ACTOR, new())
        await service.db.execute("UPDATE case_image_reviews SET case_id=$2 WHERE case_id=$1", case_id, other["case_id"])
    else:
        review = json.loads(saved["result"]) if isinstance(saved["result"], str) else saved["result"]
        if change == "missing_model":
            review["fields"] = [f for f in review["fields"] if f["field"] != "model"]
        else:
            review["fields"][0]["source"].pop("query_hash")
        await service.db.execute("UPDATE case_image_reviews SET result=$2 WHERE case_id=$1", case_id, json.dumps(review))
    assert not (await service.get(ACTOR, case_id))["drafts"][0]["valid"]
    approval = DraftApproval(expected_version=detail["version"], expected_control_version=detail["control_version"],
                             body_hash=draft["body_hash"])
    with pytest.raises(HTTPException) as exc:
        await service.approve(ACTOR, case_id, draft["id"], approval)
    assert exc.value.status_code == 409


@pytest.mark.parametrize("negative", [None, "yes", "unknown", "direct", "uncertain", "insufficient", "risk", "extra_claim"])
def test_policy_draft_reconciliation_is_limited_to_proven_quality_only_handoffs(negative):
    r = record("policies-005")
    result = r["result"]
    result["handoff_review"].update(answer_handoff_declared=False, read_only_policy_lookup=True)
    if negative in {"yes", "unknown"}:
        result["handoff_review"]["answer_handoff_declared"] = True if negative == "yes" else None
    elif negative in {"direct", "uncertain"}:
        result["handoff_review"]["basis"] = negative
    elif negative == "insufficient":
        result["knowledge"]["evidence_ok"] = False
    elif negative == "risk":
        result["business_data"] = {"attempted": True, "evidence_issues": ["failed"]}
    elif negative == "extra_claim":
        from app.tickets.draft_decision import digest
        result["response"] += "额外收费998美元。"
        result["handoff_review"]["draft_sha256"] = digest(result["response"])
    checked = reconcile_handoff(result, {"snapshot": r["snapshot"]})
    assert checked["handoff"] is (negative is not None)


@pytest.mark.parametrize("message,expected", [
    ("请根据政策批准此次申请。", False),
    ("请查阅政策并核准此次申请。", False),
    ("请说明政策并审核这项请求。", False),
    ("请查阅政策并同意这项变更。", False),
    ("请查阅政策并授权操作。", False),
    ("请查阅政策并提交申请。", False),
    ("Explain the policy and approve this request.", False),
    ("Look up the policy and grant approval.", False),
    ("请说明政策中的审批流程。", True),
    ("请查阅政策并说明批准条件。", True),
    ("请按政策准备一份待审核草稿。", True),
    ("Explain the approval process in the policy.", True),
    ("Draft a reply based on the policy for employee review.", True),
])
def test_operational_approval_is_distinct_from_reading_procedure_or_preparing_a_draft(message, expected):
    from types import SimpleNamespace

    from app.agents.agent_orchestrator import AgentType
    from app.core.intent_recognizer import UrgencyLevel
    from app.tickets.draft_decision import handoff_review

    assert read_only_policy_query(message) is expected
    recorded = record("policies-005")["result"]
    request = make_request(message, IntentCategory.DELIVERY)
    request.urgency = UrgencyLevel.LOW
    result = SimpleNamespace(primary_agent=AgentType.ORDER, service_unavailable=False, routing_confidence=1,
                             policy_violations=recorded["policy_violations"], response="discarded answer",
                             answer_handoff_declared=False, handoff_reason="quality", answer_confidence=.4)
    recorded["handoff_review"] = handoff_review(request, result, recorded["response"])
    checked = reconcile_handoff(recorded, {"snapshot": record("policies-005")["snapshot"]})
    assert checked["handoff"] is (not expected)


@pytest.mark.parametrize("declared", ["yes", "no"])
async def test_answer_declaration_survives_handoff_and_checkpoint(orchestrator, declared):
    stub_agents(orchestrator, "保证2034年6月1日送达。费用998美元。\n[[handoff:" + declared + "]]")
    result = await orchestrator.run(make_request("查阅配送政策", IntentCategory.DELIVERY))
    assert result.handoff
    assert result.answer_handoff_declared is (declared == "yes")
    assert decode_result(encode_result(result)).answer_handoff_declared is result.answer_handoff_declared


@pytest.mark.parametrize("streaming", [False, True])
async def test_answered_no_declaration_is_preserved_without_a_handoff(orchestrator, monkeypatch, streaming):
    from app.agents.agent_orchestrator import AgentType

    async def chunks(*_):
        yield "已说明政策内容。\n[[handoff:no]]"
    if streaming:
        for agent in orchestrator._pool[AgentType.ORDER]:
            monkeypatch.setattr(agent, "_stream_llm", chunks)
        events = [e async for e in orchestrator.run_stream(make_request("查阅配送政策", IntentCategory.DELIVERY))]
        result = next(e["result"] for e in events if e["type"] == "result")
    else:
        stub_agents(orchestrator, "已说明政策内容。\n[[handoff:no]]")
        result = await orchestrator.run(make_request("查阅配送政策", IntentCategory.DELIVERY))
    assert not result.handoff and result.answer_handoff_declared is False
    assert decode_result(encode_result(result)).answer_handoff_declared is False


@pytest.mark.parametrize("declared", ["yes", "no"])
async def test_stream_answer_declaration_reaches_handoff_result(orchestrator, monkeypatch, declared):
    from app.agents.agent_orchestrator import AgentType

    async def chunks(*_):
        yield "保证2034年6月1日送达。费用998美元。\n[[handoff:" + declared + "]]"
    for agent in orchestrator._pool[AgentType.ORDER]:
        monkeypatch.setattr(agent, "_stream_llm", chunks)
    events = [e async for e in orchestrator.run_stream(make_request("查阅配送政策", IntentCategory.DELIVERY))]
    result = next(e["result"] for e in events if e["type"] == "result")
    assert result.handoff and result.answer_handoff_declared is (declared == "yes")


@pytest.mark.parametrize("markers", [
    ["yes", "no"], ["no", "yes"], ["yes", "no", "no"], ["no", "no"],
])
@pytest.mark.parametrize("mode", ["plain", "stream", "tools", "stream_tools"])
async def test_repeated_declarations_preserve_any_yes_in_all_answer_paths(orchestrator, monkeypatch, markers, mode):
    from conftest import ScriptedModel, text_step
    from test_agent_tool_loop import _Tools, _turn

    from app.agents.agent_orchestrator import AgentType

    chunks = ["保证2034年6月1日送达。费用998美元。\n"] + ["[[handoff:" + m + "]]\n" for m in markers]
    request = make_request("查阅配送政策", IntentCategory.DELIVERY)
    if "tools" in mode:
        request.turn = _turn(_Tools())
        model = ScriptedModel(text_step(*chunks))
        for agent in orchestrator._pool[AgentType.ORDER]:
            agent._client = model
    elif mode == "plain":
        stub_agents(orchestrator, "".join(chunks))
    else:
        async def stream(*_):
            for chunk in chunks:
                yield chunk
        for agent in orchestrator._pool[AgentType.ORDER]:
            monkeypatch.setattr(agent, "_stream_llm", stream)
    if mode.startswith("stream"):
        events = [e async for e in orchestrator.run_stream(request)]
        result = next(e["result"] for e in events if e["type"] == "result")
        assert "handoff:" not in "".join(e["text"] for e in events if e["type"] == "delta")
    else:
        result = await orchestrator.run(request)
    assert result.handoff  # Two independent quality violations remain.
    assert result.answer_handoff_declared is ("yes" in markers)
    assert "handoff:" not in result.response


@pytest.mark.parametrize("declarations,expected", [
    ([True, False], True), ([None, True], True), ([False, False], False), ([None, False], None),
])
async def test_parallel_answer_merge_preserves_any_explicit_yes(orchestrator, monkeypatch, declarations, expected):
    from unittest.mock import AsyncMock

    from app.agents.agent_orchestrator import AgentResponse, AgentType, RoutingDecision
    from app.core.answer_policy import PolicyViolation

    responses = [AgentResponse(agent_type=kind, content="stub", success=True, confidence=.5,
                 handoff_declared=declared, escalate=declared is True,
                 policy_violations=[PolicyViolation("lead_time_commitment", "2034-06-01", "stub", "stub")])
                 for kind, declared in zip([AgentType.ORDER, AgentType.BILLING], declarations)]
    monkeypatch.setattr(orchestrator, "_execute", AsyncMock(side_effect=responses))
    result = await orchestrator.run_parallel(make_request("订单和账款", IntentCategory.DELIVERY),
        RoutingDecision(primary_agent=AgentType.ORDER, supporting_agents=[AgentType.BILLING], confidence=1, reason="test"))
    assert result.answer_handoff_declared is expected
    assert decode_result(encode_result(result)).answer_handoff_declared is expected
