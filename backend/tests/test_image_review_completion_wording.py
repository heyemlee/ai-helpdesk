"""Execution failure wording follows the existing closed empty-review reasons."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from test_attachments import connected as connected  # noqa: F401
from test_attachments import evidence, extraction, upload
from test_case_draft_handoff import finish
from test_cases import ACTOR, PG, new, scalar
from test_cases import pair as pair  # noqa: F401

from app.attachments.verification import Candidate, Extraction
from app.attachments.workflow import MaterialWorkflow
from app.core.openai_compat import GatewayResponseError
from app.events import EventLog
from app.tickets.case_execution import CaseExecutor


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
@pytest.mark.parametrize("kind", ["transport", "invalid", "unknown", "empty", "unclear", "success"])
async def test_review_proposal_and_continuation_message_agree_with_actual_execution(connected, kind):
    a, b, attachments, _ = connected
    image = await upload(attachments)
    receipt = await a.accept_input(ACTOR, new(issue_type="general", order_refs=["SO-1"], attachment_ids=[image["id"]]))
    extracted = extraction()
    if kind == "empty":
        extracted = Extraction(document_type="unknown", fields=[])
    elif kind == "unclear":
        extracted = Extraction(document_type="unknown", fields=[
            Candidate(field="order_ref", value="", unit="", kind="unknown", readability="unclear", location="test")])
    errors = {"transport": httpx.ConnectError("controlled unavailable"),
              "invalid": GatewayResponseError("controlled invalid output"), "unknown": RuntimeError("controlled failure")}
    extractor = SimpleNamespace(version="wording-regression-v1", extract=AsyncMock(return_value=extracted))
    if kind in errors:
        extractor.extract.side_effect = errors[kind]
    query = AsyncMock(return_value=evidence())
    workflow = MaterialWorkflow(attachments, extractor, query)
    executor = CaseExecutor(a, prepare=AsyncMock(), answer=AsyncMock(), event_log=EventLog.disabled(), materials=workflow)
    first = await finish(a, executor, receipt["run_id"])
    assert first.status == "succeeded" and first.result["next_run_id"]
    failed = kind not in {"unclear", "success"}
    status = "未完成" if failed else "已完成"
    proposal = first.checkpoint["case_proposal"]
    assert proposal["response"] == f"图片核对{status}。"
    reviews = proposal["material_reviews"]
    assert bool(reviews[0]["fields"]) is not failed
    continuation = await b.run(first.result["next_run_id"])
    case = await b.get(ACTOR, receipt["case_id"])
    system = [m for m in case["messages"] if m["role"] == "system"]
    assert len(system) == 1
    assert system[0]["content"] == f"图片核对{status}；请按已确认事实和待补字段继续处理。"
    assert continuation.case_context["materials_reviewed"] is True
    assert len(case["runs"]) == 2 and len(case["image_reviews"]) == 1
    if failed:
        assert not continuation.case_context["snapshot"]["facts"]
        assert continuation.case_context["snapshot"]["pending_fields"] == ["image:readability"]
        # A fresh executor consumes the durable failure receipt without a new
        # extraction, business lookup or answer call.
        continued_executor = CaseExecutor(b, prepare=AsyncMock(), answer=AsyncMock(),
                                         event_log=EventLog.disabled(), materials=workflow)
        second = await finish(b, continued_executor, continuation.run_id)
        assert second.status == "succeeded" and "未完成" in second.result["response"]
        assert not second.result["handoff"]
        continued_executor.prepare.assert_not_awaited()
        continued_executor.answer.assert_not_awaited()
    extractor.extract.assert_awaited_once()
    assert query.await_count == (0 if kind in errors else 1)
    assert await scalar(a.db, "SELECT count(*) FROM case_actions") == 0
    assert await scalar(a.db, "SELECT count(*) FROM ticket_notifications") == 0
