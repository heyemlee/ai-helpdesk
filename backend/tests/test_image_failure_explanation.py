"""Failed execution is not evidence of unreadable material or confirmed image facts."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from openai import BadRequestError, InternalServerError
from test_attachments import connected as connected  # noqa: F401
from test_attachments import evidence, extraction, pixels, upload
from test_cases import ACTOR, PG, action, claim, delivery, new, scalar, worker_for
from test_cases import pair as pair  # noqa: F401

from app.attachments.review_failure import extraction_failure, incomplete_image_message, review_failures
from app.attachments.workflow import MaterialWorkflow
from app.core.openai_compat import GatewayResponseError
from app.events import EventLog
from app.runs.store import PgRunStore
from app.tickets.case_execution import CaseExecutor, pending_response
from app.tickets.cases import DraftApproval

REQUEST = httpx.Request("POST", "https://controlled.invalid/responses")


@pytest.mark.parametrize("error,reason", [
    (httpx.ConnectError("private raw transport detail"), "service_unavailable"),
    (TimeoutError(), "service_unavailable"),
    (InternalServerError("failed", response=httpx.Response(502, request=REQUEST), body=None), "service_unavailable"),
    (GatewayResponseError("raw output must not reach employees"), "invalid_output"),
    (RuntimeError("service unavailable: clear image"), "incomplete"),
    (BadRequestError("bad request", response=httpx.Response(400, request=REQUEST), body=None), "incomplete"),
])
def test_only_typed_execution_errors_can_name_a_service(error, reason):
    assert extraction_failure(error) == reason


def snapshot(reason="service_unavailable"):
    return {"case_id": "controlled-case", "materials": [{"attachment_id": "a", "sha256": "s"}],
            "input_receipt": {"message_id": "original-controlled-input"},
            "pending_fields": ["image:readability"], "language": "zh",
            "material_review_receipt": {"case_id": "controlled-case", "complete": False,
                                        "request_message_id": "original-controlled-input",
                                        "failures": [{"attachment_id": "a", "sha256": "s", "reason": reason}]}}


@pytest.mark.parametrize("change", ["legacy", "unknown", "wrong_case", "wrong_attachment", "wrong_sha", "malformed"])
def test_missing_or_unbound_legacy_reason_never_claims_service_or_blur(change):
    data = snapshot()
    if change == "legacy":
        data.pop("material_review_receipt")
    elif change == "unknown":
        data["material_review_receipt"]["failures"][0]["reason"] = "TLS certificate definitely expired"
    elif change == "wrong_case":
        data["material_review_receipt"]["case_id"] = "other"
    elif change == "wrong_attachment":
        data["material_review_receipt"]["failures"][0]["attachment_id"] = "other"
    elif change == "wrong_sha":
        data["material_review_receipt"]["failures"][0]["sha256"] = "other"
    else:
        data["material_review_receipt"]["failures"] = ["service_unavailable", {"reason": ["service_unavailable"]}]
    text = pending_response(data)
    assert "核验尚未完成" in text and "原图已保留" in text
    assert "服务" not in text and "清晰" not in text and "重试" not in text


def test_real_missing_fields_are_not_hidden_by_execution_notice():
    data = snapshot("invalid_output")
    data["pending_fields"] += ["image:model", "quantity"]
    text = pending_response(data)
    assert "未得到可用结果" in text and "图片核实：缺件型号" in text and "缺件数量" in text
    assert "清晰图片" not in text
    data["language"] = "en"
    assert "did not return a usable result" in incomplete_image_message(data)
    assert "Still needed: Image review: Model, Quantity." in pending_response(data)
    assert "仍需补充" not in pending_response(data)
    assert review_failures([{"attachment_id": "a", "sha256": "s", "fields": [], "failure_reason": {}}])[0]["reason"] == "incomplete"


@pytest.mark.parametrize("change", [
    "receipt_missing", "receipt_blank", "receipt_nonstring", "receipt_wrong",
    "source_missing", "source_blank", "source_nonstring", "source_wrong",
])
def test_failure_reason_requires_an_independent_original_input_message(change):
    data = snapshot()
    if change == "receipt_missing":
        data["material_review_receipt"].pop("request_message_id")
    elif change.startswith("receipt_"):
        data["material_review_receipt"]["request_message_id"] = {
            "receipt_blank": " ", "receipt_nonstring": 42, "receipt_wrong": "unrelated-input"}[change]
    elif change == "source_missing":
        data.pop("input_receipt")
    else:
        data["input_receipt"]["message_id"] = {
            "source_blank": "", "source_nonstring": None, "source_wrong": "other-original"}[change]
    assert "服务" not in pending_response(data)
    assert "核验尚未完成" in pending_response(data)
    assert data["pending_fields"] == ["image:readability"]


async def start_failure(connected, error=None, *, extracted=None):
    a, b, attachments, provider = connected
    image = await upload(attachments)
    receipt = await a.accept_input(ACTOR, new(issue_type="general", order_refs=["SO-1"], attachment_ids=[image["id"]]))
    run = await claim(a, receipt)
    extractor = SimpleNamespace(version="controlled-failure-v1", extract=AsyncMock())
    if error is not None:
        extractor.extract.side_effect = error
    else:
        extractor.extract.return_value = extracted
    query = AsyncMock(return_value=evidence())
    workflow = MaterialWorkflow(attachments, extractor, query)
    reviews = await workflow.review(run)
    return a, b, attachments, provider, image, receipt, run, workflow, extractor, query, reviews


async def continue_case(service, run_id, workflow):
    executor = CaseExecutor(service, prepare=AsyncMock(), answer=AsyncMock(),
                            event_log=EventLog.disabled(), materials=workflow)
    worker = worker_for(service, executor)
    await worker._handle(await delivery(worker, run_id), False)
    return await service.run(run_id), executor


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
@pytest.mark.parametrize("kind", ["transport", "invalid", "unknown", "empty"])
async def test_pg_failure_reason_survives_review_snapshot_and_restart_without_new_calls(connected, kind):
    error = {"transport": httpx.ConnectError("private-secret-detail"),
             "invalid": GatewayResponseError("private-model-output"),
             "unknown": RuntimeError("service unavailable")} .get(kind)
    extracted = type(extraction())(document_type="unknown", fields=[]) if kind == "empty" else extraction()
    values = await start_failure(connected, error, extracted=extracted)
    a, b, attachments, _, image, receipt, run, workflow, extractor, query, reviews = values
    assert not reviews[0]["fields"]
    payload, applied = await a.complete(run, {"response": "review", "material_reviews": reviews})
    assert applied
    continuation = await b.run(payload["next_run_id"])
    saved = continuation.case_context["snapshot"]
    assert saved["material_review_receipt"]["failures"][0]["reason"] == {
        "transport": "service_unavailable", "invalid": "invalid_output"}.get(kind, "incomplete")
    assert saved["material_review_receipt"]["complete"] is False
    assert saved["pending_fields"] == ["image:readability"] and not saved["facts"]
    # Fresh service/executor on an independent pool uses only the durable snapshot.
    continued, executor = await continue_case(b, continuation.run_id, workflow)
    text = continued.result["response"]
    assert "原图已保留" in text and "清晰图片" not in text and "重试" not in text
    assert ("核验服务暂时不可用" in text) is (kind == "transport")
    assert ("未得到可用结果" in text) is (kind == "invalid")
    assert "private" not in text and continued.result["needs_clarification"]
    assert not continued.result["handoff"] and not continued.result["escalated"]
    executor.prepare.assert_not_awaited()
    executor.answer.assert_not_awaited()
    assert query.await_count == (1 if kind == "empty" else 0)
    assert extractor.extract.await_count == 1
    assert await scalar(a.db, "SELECT count(*) FROM attachment_extractions") == (1 if kind == "empty" else 0)
    content, _ = await attachments.content(ACTOR, image["id"])
    assert content == pixels()
    case = await b.get(ACTOR, receipt["case_id"])
    assert not case["facts"] and case["status"] == "waiting_for_info"
    assert not case["drafts"][0]["evidence"]
    assert await scalar(a.db, "SELECT count(*) FROM case_actions WHERE kind='handoff'") == 0
    assert await scalar(a.db, "SELECT count(*) FROM ticket_notifications") == 0
    # An execution-failure reason cannot authorize forged verified-image evidence.
    draft = case["drafts"][0]
    await a.db.execute("UPDATE case_drafts SET evidence=$1::jsonb WHERE id=$2",
                       json.dumps([{"source": "verified_image", "db_value": "invented"}]), draft["id"])
    with pytest.raises(HTTPException) as exc:
        await b.approve(ACTOR, case["id"], draft["id"], DraftApproval(
            expected_version=case["version"], expected_control_version=case["control_version"], body_hash=draft["body_hash"]))
    assert exc.value.detail["code"] == "draft_evidence_stale"


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
@pytest.mark.parametrize("kind", ["unclear", "missing", "conflicting"])
async def test_pg_true_material_problems_still_request_the_actual_field(connected, kind):
    extracted = extraction()
    if kind in {"unclear", "missing"}:
        model = next(f for f in extracted.fields if f.field == "model")
        model.readability = kind
        model.value = "A" if kind == "unclear" else None
    else:
        next(f for f in extracted.fields if f.field == "quantity").value = "9"
    a, b, _, _, _, _, run, workflow, extractor, query, reviews = await start_failure(connected, extracted=extracted)
    assert reviews[0]["fields"] and "failure_reason" not in reviews[0]
    payload, applied = await a.complete(run, {"response": "review", "material_reviews": reviews})
    assert applied
    result, _ = await continue_case(b, payload["next_run_id"], workflow)
    assert "请补充" in result.result["response"]
    assert ("缺件型号" if kind != "conflicting" else "缺件数量") in result.result["response"]
    assert "服务暂时不可用" not in result.result["response"]
    assert query.await_count == 1 and extractor.extract.await_count == 1


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
@pytest.mark.parametrize("legacy", [False, True])
async def test_pg_old_clarification_checkpoint_is_rendered_neutrally_or_with_bound_cause(connected, legacy):
    a, b, _, _, _, _, run, workflow, extractor, query, reviews = await start_failure(
        connected, httpx.ConnectError("unavailable"))
    payload, _ = await a.complete(run, {"response": "review", "material_reviews": reviews})
    pending = await claim(a, {"run_id": payload["next_run_id"]})
    if legacy:
        pending.case_context["snapshot"]["material_review_receipt"].pop("failures")
        await a.db.execute("UPDATE agent_runs SET case_context=$1::jsonb WHERE run_id=$2",
                           json.dumps(pending.case_context), pending.run_id)
    checkpoint = {"case_proposal": {"response": "材料已保存，请补充：图片核实：清晰图片。",
                                  "agent_type": "system", "needs_clarification": True, "handoff": False}}
    store = PgRunStore(a.db, a.run_instance)
    await store.checkpoint(pending.run_id, pending.lease_owner, pending.attempt, copy.deepcopy(checkpoint))
    await store.release(pending.run_id, pending.lease_owner, pending.attempt, outcome="retry")
    restored, _ = await continue_case(b, pending.run_id, workflow)
    assert restored.status == "succeeded" and restored.attempt == 2
    assert "清晰图片" not in restored.result["response"] and "原图已保留" in restored.result["response"]
    assert ("核验服务" in restored.result["response"]) is not legacy
    assert restored.checkpoint == checkpoint  # original saved proposal is retained, not rewritten
    assert extractor.extract.await_count == 1 and query.await_count == 0


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
@pytest.mark.parametrize("fence", ["takeover", "expired_lease", "revoked_actor"])
async def test_pg_failure_cause_does_not_bypass_case_fences(connected, fence):
    a, b, _, provider, _, receipt, run, _, _, _, reviews = await start_failure(
        connected, httpx.ConnectError("unavailable"))
    if fence == "takeover":
        c = await b.get(ACTOR, receipt["case_id"])
        await b.mutate(ACTOR, c["id"], action(c, "takeover"))
    elif fence == "expired_lease":
        await a.db.execute("UPDATE agent_runs SET lease_until=0 WHERE run_id=$1", run.run_id)
    else:
        provider.return_value = None
    if fence == "expired_lease":
        with pytest.raises(HTTPException) as exc:
            await a.complete(run, {"response": "review", "material_reviews": reviews})
        assert exc.value.detail["code"] == "run_lease_lost"
    else:
        result, applied = await a.complete(run, {"response": "review", "material_reviews": reviews})
        assert not applied and result.get("next_run_id") is None and result["draft_id"] is None
    assert await scalar(a.db, "SELECT count(*) FROM case_image_reviews") == 0
    assert await scalar(a.db, "SELECT count(*) FROM ticket_notifications") == 0


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
async def test_pg_mixed_failed_and_unclear_images_keep_the_whole_new_prompt_in_english(connected):
    a, b, attachments, _ = connected
    images = [await upload(attachments, key) for key in ("english-failed", "english-unclear")]
    body = new(issue_type="general", order_refs=["SO-1"], attachment_ids=[i["id"] for i in images])
    receipt = await a.accept_input(ACTOR, body.model_copy(update={"message": "Please verify the attached order pictures."}))
    run = await claim(a, receipt)
    unclear = extraction()
    next(f for f in unclear.fields if f.field == "model").readability = "unclear"
    extractor = SimpleNamespace(version="mixed-english-v1", extract=AsyncMock(
        side_effect=[httpx.ConnectError("private transport error"), unclear]))
    query = AsyncMock(return_value=evidence())
    workflow = MaterialWorkflow(attachments, extractor, query)
    reviews = await workflow.review(run)
    assert not reviews[0]["fields"] and reviews[0]["failure_reason"] == "service_unavailable"
    assert any(f["field"] == "model" and f["outcome"] != "matched" for f in reviews[1]["fields"])
    result, applied = await a.complete(run, {"response": "review", "material_reviews": reviews})
    assert applied
    continuation = await b.run(result["next_run_id"])
    assert continuation.case_context["snapshot"]["language"] == "en"
    assert {"image:readability", "image:model"} <= set(continuation.case_context["snapshot"]["pending_fields"])
    completed, executor = await continue_case(b, continuation.run_id, workflow)
    text = completed.result["response"]
    assert "service was unavailable" in text and "Still needed: Image review: Model" in text
    assert all(not "\u4e00" <= ch <= "\u9fff" for ch in text)
    assert "Clear image" not in text and "private" not in text
    assert extractor.extract.await_count == 2 and query.await_count == 1
    executor.prepare.assert_not_awaited()
    executor.answer.assert_not_awaited()
    assert not (await b.get(ACTOR, receipt["case_id"]))["facts"].get("model")
    assert await scalar(a.db, "SELECT count(*) FROM case_actions WHERE kind='handoff'") == 0
    assert await scalar(a.db, "SELECT count(*) FROM ticket_notifications") == 0


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("binding", ["valid", "wrong_input", "missing_original"])
async def test_pg_reason_binds_original_message_instead_of_continuation_message(connected, language, binding):
    a, b, _, _, _, receipt, run, workflow, extractor, query, reviews = await start_failure(
        connected, httpx.ConnectError("private transport detail"))
    result, applied = await a.complete(run, {"response": "review", "material_reviews": reviews})
    assert applied
    pending = await b.run(result["next_run_id"])
    snapshot = pending.case_context["snapshot"]
    original = receipt["message_id"]
    assert snapshot["input_receipt"]["message_id"] == original
    assert snapshot["material_review_receipt"]["request_message_id"] == original
    assert pending.case_context["message_id"] == "verified_" + run.run_id
    assert pending.case_context["message_id"] != original
    snapshot["language"] = language
    if binding == "wrong_input":
        snapshot["material_review_receipt"]["request_message_id"] = "unrelated-input"
    elif binding == "missing_original":
        snapshot.pop("input_receipt")
    await a.db.execute("UPDATE agent_runs SET case_context=$1::jsonb WHERE run_id=$2",
                       json.dumps(pending.case_context), pending.run_id)
    completed, executor = await continue_case(b, pending.run_id, workflow)
    text = completed.result["response"]
    service_notice = "service was unavailable" if language == "en" else "核验服务暂时不可用"
    assert (service_notice in text) is (binding == "valid")
    assert any(phrase in text for phrase in ("incomplete", "could not finish", "未完成"))
    case = await b.get(ACTOR, receipt["case_id"])
    assert case["pending_fields"] == ["image:readability"] and not case["facts"]
    assert completed.result["needs_clarification"] and not completed.result["handoff"]
    assert extractor.extract.await_count == 1 and query.await_count == 0
    executor.prepare.assert_not_awaited()
    executor.answer.assert_not_awaited()
    assert await scalar(a.db, "SELECT count(*) FROM case_actions WHERE kind='handoff'") == 0
    assert await scalar(a.db, "SELECT count(*) FROM ticket_notifications") == 0
