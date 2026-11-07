"""Archived simulated checkpoint replay, separate from new execution controls."""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from evidence_fixtures import with_evidence
from fastapi import HTTPException
from test_cases import ACTOR, PG, claim, scalar
from test_cases import pair as pair  # noqa: F401

from app.core.business_facts import render_draft, verified_cells
from app.events import EventLog, EventLogConfig
from app.runs.store import PgRunStore
from app.runs.worker import RetryRun
from app.tickets.case_execution import CaseExecutor
from app.tickets.cases import CaseInput


def recording():
    return json.loads((Path(__file__).parent / "fixtures/case_completion_checkpoint.json").read_text())


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
@pytest.mark.parametrize("fresh_execution", [False, True])
async def test_archived_checkpoint_finishes_without_model_requery(pair, fresh_execution):
    service, _ = pair
    original = recording()
    receipt = await service.accept_input(
        ACTOR,
        CaseInput(
            message_id="checkpoint-replay",
            conv_id="conversation",
            new_case=True,
            issue_type="order",
            order_refs=["SO-2026-11000"],
            message=original["original_message"],
        ),
    )
    run = await claim(service, receipt)
    proposal = copy.deepcopy(original["case_proposal"])
    if fresh_execution:
        # A new execution control with recorded SQL/rows, not a claim that the
        # archived observation is current. Old bytes stay in the fixture.
        data = proposal["business_data"]["evidence_payload"]
        now = datetime.now(UTC).isoformat()
        data["observed_at"] = now
        for step in data["steps"]:
            step["as_of"] = step["observed_at"] = now
        data = with_evidence(data)
        proposal["business_data"]["evidence_payload"] = data
        cells, issues = verified_cells(data)
        assert cells and not issues
        proposal["business_data"]["evidence"] = cells
        proposal["response"] = render_draft([], proposal["business_data"])
    store = PgRunStore(service.db, service.run_instance)
    assert await store.checkpoint(run.run_id, run.lease_owner, run.attempt, {"case_proposal": proposal})
    run = await store.get(run.run_id)
    executor = CaseExecutor(service, prepare=AsyncMock(), answer=AsyncMock(), event_log=EventLog.disabled())
    ctx = SimpleNamespace(
        run=run,
        scope=None,
        degrade=SimpleNamespace(shed=False),
        set_turn=AsyncMock(),
        publish=AsyncMock(),
        commit=AsyncMock(),
    )
    result = (await executor(ctx)).result
    assert result["case_applied"] is fresh_execution
    assert (result.get("case_rejection") == "draft_evidence_stale") is (not fresh_execution)
    assert await scalar(service.db, "SELECT count(*) FROM case_run_receipts") == 1
    assert not executor.prepare.called and not executor.answer.called


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
async def test_expired_checkpoint_without_renewal_cannot_authorize_case_completion(pair):
    service, _ = pair
    receipt = await service.accept_input(
        ACTOR, CaseInput(message_id="expired-checkpoint", conv_id="conversation", new_case=True, message="查询记录")
    )
    run = await claim(service, receipt)
    await service.db.execute("UPDATE agent_runs SET lease_until=0 WHERE run_id=$1", run.run_id)
    store = PgRunStore(service.db, service.run_instance)
    assert not await store.checkpoint(
        run.run_id, run.lease_owner, run.attempt, {"case_proposal": {"response": "saved"}}
    )
    assert (await store.get(run.run_id)).checkpoint is None
    with pytest.raises(HTTPException) as error:
        await service.complete(run, {"response": "saved"})
    assert error.value.status_code == 409 and error.value.detail["code"] == "run_lease_lost"
    assert await scalar(service.db, "SELECT count(*) FROM case_run_receipts") == 0


async def test_http_failure_retains_phase_status_and_detail_in_redacted_event_body():
    log = await EventLog.open(EventLogConfig(mode="full", instance="diagnostic"))
    try:
        run = SimpleNamespace(
            run_id="diagnostic-run",
            request_id="r",
            conv_id="c",
            attempt=1,
            message="query",
            case_context={"case_id": "case-A", "snapshot": {"policy_scope": {}}},
            checkpoint={"case_proposal": {"response": "saved"}},
        )
        service = SimpleNamespace(
            actors=None,
            complete=AsyncMock(side_effect=HTTPException(409, {"code": "run_lease_lost", "message": "raw detail"})),
        )
        executor = CaseExecutor(service, prepare=AsyncMock(), answer=AsyncMock(), event_log=log)
        ctx = SimpleNamespace(
            run=run,
            scope=None,
            degrade=SimpleNamespace(shed=False),
            set_turn=AsyncMock(),
            publish=AsyncMock(),
            commit=AsyncMock(),
        )
        with pytest.raises(RetryRun):
            await executor(ctx)
        turn_id = ctx.set_turn.call_args.args[0]
        emitted = [e.to_dict() for e in await log.store.events_of(turn_id)]
        failure = next(e for e in emitted if e["type"] == "turn_failed")
        assert failure["data"]["phase"] == "complete" and failure["data"]["http_status"] == 409
        assert failure["body"]["error_code"] == "run_lease_lost"
        assert "error_code" not in failure["data"]
        assert failure["body"]["error_detail"]["message"] == "raw detail"
        assert "raw detail" not in str(failure["data"])
        assert service.complete.await_count == 1 and not executor.answer.called
    finally:
        await log.close()
