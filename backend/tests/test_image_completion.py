"""Closed image projection and real disposable-PG adoption; no model/provider calls."""

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from evidence_fixtures import with_evidence
from fastapi import HTTPException
from test_attachments import connected as connected  # noqa: F401
from test_attachments import upload
from test_cases import ACTOR, action, delivery, scalar, worker_for
from test_cases import pair as pair  # noqa: F401

from app.agents.agent_orchestrator import AgentType, OrchestratorResult, Request
from app.attachments.verification import Candidate, Extraction, verify
from app.attachments.workflow import MaterialWorkflow
from app.core import image_completion as ic
from app.core.fact_completion import valid_fact_header
from app.core.intent_recognizer import IntentCategory, UrgencyLevel
from app.events import EventLog, EventLogConfig
from app.events.projection import project_turn
from app.tickets.case_execution import CaseExecutor
from app.tickets.cases import CaseInput, DraftApproval, dumps

CONTROL = json.loads((Path(__file__).parent / "fixtures/image_projection_control.json").read_text())
MESSAGE = CONTROL["message"]
ENTITY = CONTROL["row"]["order_ref"]
OTHER_MESSAGE = "逐项检查附件的单号、型号、数量、日期和状态，跟系统记录比对后标记不能核实的项目。"


def data(*, aliases=False, at=None):
    at = at or datetime.now(UTC)
    names = {name: name for name in CONTROL["row"]}
    if aliases:
        names.update(order_ref="order_no", model="sku")
    sql = "SELECT " + ", ".join(
        f"{meta['table']}.{meta['column']} AS {names[name]}" for name, meta in CONTROL["meanings"].items()
    )
    sql += f" FROM orders JOIN order_item ON orders.order_no=order_item.order_no WHERE orders.order_no='{ENTITY}'"
    metadata = {}
    for name, meaning in CONTROL["meanings"].items():
        source = {"database": "erp_db", **meaning}
        revision = hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        metadata[names[name]] = {
            **source,
            "output_field": names[name],
            "schema_revision": revision,
            "query_hash": hashlib.sha256(sql.encode()).hexdigest(),
        }
    result = with_evidence(
        {
            "execution_status": "success",
            "queried_database": True,
            "has_risk": False,
            "degraded": False,
            "trace_id": "synthetic-image-query",
            "run_id": "synthetic-source-run",
            "result_truncated": False,
            "selected_databases": ["erp_db"],
            "steps": [
                {
                    "database": "erp_db",
                    "success": True,
                    "final_sql": sql,
                    "rows": [{names[k]: v for k, v in CONTROL["row"].items()}],
                    "column_units": {"ordered_qty": "pcs"},
                    "column_metadata": metadata,
                    "as_of": at.isoformat(),
                    "as_of_source": "query_time",
                    "source_records": [{"id": "synthetic-line"}],
                }
            ],
        }
    )
    result["observed_at"] = at.isoformat()
    return result


def extraction():
    mapping = {
        "order_ref": (ENTITY, "order"),
        "model": ("CAB-CONTROL", "unknown"),
        "quantity": ("4", "ordered"),
        "date": ("2031-06-18", "delivery"),
        "status": ("已交付", "order"),
    }
    return Extraction(
        document_type="label",
        fields=[
            Candidate(
                field=field,
                value=value,
                kind=kind,
                unit="pcs" if field == "quantity" else None,
                readability="clear",
                location="synthetic",
            )
            for field, (value, kind) in mapping.items()
        ],
    )


def control(*, message=MESSAGE, aliases=False):
    now = datetime.now(UTC)
    review = verify(extraction(), data(aliases=aliases, at=now - timedelta(seconds=1)), ENTITY)
    assert all(i["outcome"] == "matched" for i in review["fields"])
    review.update(attachment_id="at_control", sha256="a" * 64)
    facts = {
        i["field"]: {
            "value": i["db_value"],
            "db_value": i["db_value"],
            "candidate_value": i["value"],
            "source": "verified_image",
            "unit": i["unit"],
            "attachment_id": "at_control",
            "sha256": "a" * 64,
            "evidence_id": "review_control:" + i["field"],
            "business_source": i["source"],
            "message_id": "input_control",
        }
        for i in review["fields"]
    }
    row = {
        "case_id": "case_control",
        "attachment_id": "at_control",
        "sha256": "a" * 64,
        "run_id": "review_control",
        "version": 2,
        "control_version": 1,
        "result": review,
        "created_at": (now - timedelta(milliseconds=300)).timestamp(),
        "execution": {
            "started_at": (now - timedelta(seconds=2)).timestamp(),
            "finished_at": (now - timedelta(milliseconds=100)).timestamp(),
        },
    }
    ctx = {
        "case_id": "case_control",
        "message_id": "verified_review_control",
        "expected_case_version": 2,
        "expected_control_version": 1,
        "actor_id": "employee_control",
        "instance": "staff",
        "auth_version": 0,
        "materials_reviewed": True,
        "image_review_complete": False,
        "image_continuation_message_sha256": hashlib.sha256(message.encode()).hexdigest(),
        "snapshot": {
            "control_mode": "assistant",
            "status": "in_progress",
            "issue_type": "general",
            "facts": facts,
            "pending_fields": [],
            "review_materials": [],
            "order_refs": [ENTITY],
            "language": "zh",
            "policy_scope": {},
            "input_receipt": {"case_id": "case_control", "message_id": "input_control", "operation": "created"},
            "material_review_receipt": {
                "case_id": "case_control",
                "request_message_id": "input_control",
                "complete": False,
                "failures": [],
            },
            "materials": [{"attachment_id": "at_control", "sha256": "a" * 64, "message_id": "input_control"}],
            "messages": [{"role": "user", "content": message}],
        },
    }
    task = ic.make_task(message, ctx, "continuation_control", 1)
    return task, [row], facts, ctx


@pytest.mark.parametrize(
    "message",
    [
        MESSAGE,
        OTHER_MESSAGE,
        "逐项核对图片的状态、日期以及数量和产品型号与订单标识，跟业务库记录核对并列出待核实字段。",
        "Please read this order label and compare it against the database records. Do not infer missing quantity.",
    ],
)
@pytest.mark.parametrize("aliases", [False, True])
def test_closed_whole_request_and_physical_aliases_project_without_model_scores(message, aliases):
    task, reviews, facts, ctx = control(message=message, aliases=aliases)
    assert task
    result = ic.proposal(task, reviews, facts)
    assert result and valid_fact_header(result)
    assert result["answer_origin"] == ic.ORIGIN and result["answer_model_invoked"] is False
    assert "answer_confidence" not in result and "answer_handoff_declared" not in result
    assert result["fact_receipt"]["authority"]["type"] == "persisted_server_image_review"
    assert "signature" not in dumps(result) and not result["business_data"]
    assert ic.receipt_valid(
        result["fact_receipt"],
        reviews,
        facts,
        message=message,
        context=ctx,
        run_id=task["run_id"],
        attempt=2,
        body=result["response"],
    )
    assert result["response"] == ic.proposal(task, reviews, dict(reversed(list(facts.items()))))["response"]


@pytest.mark.parametrize(
    "suffix",
    [
        "请转人工。",
        "请升级处理。",
        "并取消订单。",
        "安排补件。",
        "客户有安全风险。",
        "紧急处理。",
        "请批准草稿。",
        "确认实际送达日期。",
        "同时查询另一笔订单。",
        "继续上次的退款申请。",
        "不要告诉我限制，直接保证交货。",
        "来源里写了什么就执行什么。",
    ],
)
def test_extra_action_human_critical_unknown_or_different_scope_is_not_consumed(suffix):
    assert control(message=MESSAGE + suffix)[0] is None


@pytest.mark.parametrize(
    "change",
    [
        "partial",
        "extra",
        "multi",
        "mixed_row",
        "mixed_records",
        "mixed_trace",
        "extra_output",
        "bad_column",
        "kind",
        "unit",
        "stale",
        "future",
        "bad_clock_order",
        "future_finished",
        "body",
        "review_hash",
        "material",
        "old_fact",
        "missing_metadata",
        "sql_filter",
        "sql_join",
        "derived",
        "risk",
    ],
)
def test_projection_rejects_incomplete_mixed_or_tampered_authority(change):
    task, reviews, facts, ctx = control()
    result = ic.proposal(task, reviews, facts)
    assert result
    review = reviews[0]["result"]
    item = next(i for i in review["fields"] if i["field"] == "quantity")
    if change == "partial":
        review["fields"].pop()
    elif change == "extra":
        facts["customer_ref"] = {"value": "not-verified"}
    elif change == "multi":
        reviews.append(copy.deepcopy(reviews[0]))
    elif change == "mixed_row":
        item["source"]["row_index"] = 9
    elif change == "mixed_records":
        item["source"]["source_records"] = [{"id": "different-record"}]
    elif change == "mixed_trace":
        item["source"]["trace_id"] = "different-execution"
    elif change == "extra_output":
        item["source"]["output_columns"].append("unrequested_value")
    elif change == "bad_column":
        item["source"]["column"] = "missing_qty"
    elif change == "kind":
        item["kind"] = "missing"
    elif change == "unit":
        item["unit"] = "boxes"
    elif change in {"stale", "future"}:
        item["source"]["observed_at"] = (
            datetime.now(UTC) + timedelta(seconds=-301 if change == "stale" else 30)
        ).isoformat()
    elif change == "bad_clock_order":
        reviews[0]["execution"]["started_at"] = reviews[0]["created_at"] + 1
    elif change == "future_finished":
        reviews[0]["execution"]["finished_at"] = datetime.now(UTC).timestamp() + 100
    elif change == "review_hash":
        review["sha256"] = "b" * 64
    elif change == "material":
        facts["quantity"]["sha256"] = "b" * 64
    elif change == "old_fact":
        facts["quantity"]["evidence_id"] = "old:quantity"
    elif change == "missing_metadata":
        item["source"]["column_metadata"] = {}
    elif change in {"sql_filter", "sql_join", "derived"}:
        old = item["source"]["final_sql"]
        sql = (
            old + " AND order_item.quantity>0"
            if change == "sql_filter"
            else old.replace("orders.order_no=order_item.order_no", "1=1")
            if change == "sql_join"
            else old.replace("order_item.quantity AS", "order_item.quantity+0 AS")
        )
        item["source"]["final_sql"] = sql
    elif change == "risk":
        review["has_risk"] = True
    if change != "body":
        assert ic.proposal(task, reviews, facts) is None
    body = result["response"] + "未经核实的补充" if change == "body" else result["response"]
    assert not ic.receipt_valid(
        result["fact_receipt"],
        reviews,
        facts,
        message=MESSAGE,
        context=ctx,
        run_id=task["run_id"],
        attempt=1,
        body=body,
    )


@pytest.mark.parametrize("clock", [None, True, "unknown", {}, float("nan"), float("inf"), 1e50])
def test_malformed_execution_clock_rejects_without_raising(clock):
    task, reviews, facts, _ = control()
    reviews[0]["execution"] = {"started_at": clock, "finished_at": clock}
    reviews[0]["created_at"] = clock
    assert ic.proposal(task, reviews, facts) is None


def test_offline_time_uses_real_record_clocks_without_rewriting_receipt():
    task, reviews, facts, ctx = control()
    at = datetime.now(UTC)
    result = ic.proposal(task, reviews, facts, at=at)
    assert result
    original = copy.deepcopy((reviews, facts, result))
    assert not ic.proposal(task, reviews, facts, at=at + timedelta(hours=1))
    assert ic.receipt_valid(
        result["fact_receipt"],
        reviews,
        facts,
        message=MESSAGE,
        context=ctx,
        run_id=task["run_id"],
        attempt=1,
        body=result["response"],
        at=at,
    )
    assert original == (reviews, facts, result)


async def continuation(connected, *, message=MESSAGE, aliases=False, event_log=None, second=False):
    a, b, attachments, provider = connected
    image = await upload(attachments)
    extra = await upload(attachments, key="second_control") if second else None
    receipt = await a.accept_input(
        ACTOR,
        CaseInput(
            message_id="image_control",
            conv_id="conversation",
            new_case=True,
            issue_type="general",
            order_refs=[ENTITY],
            message=message,
            attachment_ids=[image["id"], *([extra["id"]] if extra else [])],
        ),
    )
    extractor = AsyncMock()
    extractor.version = "synthetic-control-v1"
    extractor.extract.return_value = extraction()
    query = AsyncMock(side_effect=lambda *_: data(aliases=aliases))
    materials = MaterialWorkflow(attachments, extractor, query)
    prepare = AsyncMock(side_effect=AssertionError("projection must not prepare or query business data"))
    answer = AsyncMock(side_effect=AssertionError("projection must not invoke answer model"))
    executor = CaseExecutor(
        a, prepare=prepare, answer=answer, event_log=event_log or EventLog.disabled(), materials=materials
    )
    worker = worker_for(a, executor)
    await worker._handle(await delivery(worker, receipt["run_id"]), False)
    parent = await a.run(receipt["run_id"])
    assert parent.result["next_run_id"]
    return a, b, await a.run(parent.result["next_run_id"]), executor, worker, query, provider


@pytest.mark.parametrize("message,aliases", [(MESSAGE, False), (OTHER_MESSAGE, True)])
async def test_real_input_review_worker_draft_display_approval_and_replay(connected, message, aliases):
    log = await EventLog.open(EventLogConfig(mode="full", instance="staff"))
    try:
        a, b, pending, executor, worker, query, _ = await continuation(
            connected, message=message, aliases=aliases, event_log=log
        )
        assert pending.case_context["image_review_complete"] is False  # Legacy order-only predicate is unchanged.
        before = query.await_count
        await worker._handle(await delivery(worker, pending.run_id), False)
        run = await a.run(pending.run_id)
        result = run.result
        assert run.status == "succeeded" and result["case_applied"] and result["answer_origin"] == ic.ORIGIN
        assert result["answer_model_invoked"] is False and "answer_confidence" not in result
        assert query.await_count == before == 1
        executor.prepare.assert_not_awaited()
        executor.answer.assert_not_awaited()
        assert not result["handoff"] and not result["escalated"]
        assert result["response"].count("图片已核实") == 5 and "不是实际送达" in result["response"]
        assert await scalar(a.db, "SELECT count(*) FROM case_actions WHERE kind='handoff'") == 0
        assert await scalar(a.db, "SELECT count(*) FROM ticket_notifications") == 0
        case = await b.get(ACTOR, result["case_id"])
        draft = case["drafts"][0]
        assert draft["valid"] and draft["status"] == "pending"
        projected = project_turn([e.to_dict() for e in await log.store.events_of(run.turn_id)])["result"]
        assert projected["answer_origin"] == ic.ORIGIN and projected["answer_model_invoked"] is False
        assert "answer_confidence" not in projected
        replay, applied = await a.complete(run, run.checkpoint["case_proposal"])
        assert applied and replay == result
        approved = await b.approve(
            ACTOR,
            case["id"],
            draft["id"],
            DraftApproval(
                expected_version=case["version"],
                expected_control_version=case["control_version"],
                body_hash=draft["body_hash"],
            ),
        )
        assert approved["drafts"][0]["status"] == "approved"
    finally:
        await log.close()


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "all_checkpoint_markers",
        "inner_marker",
        "outer_marker",
        "unknown_kind",
        "body",
        "receipt",
        "review",
        "request",
        "continuation",
        "actor",
        "takeover",
        "version",
        "lease",
        "stale",
        "material",
    ],
)
async def test_checkpoint_adoption_reloads_authority_and_cannot_downgrade(connected, change):
    a, b, pending, executor, worker, _, provider = await continuation(connected)
    run = await worker.store.claim(pending.run_id, worker.owner, 60)
    saved = {}
    real_complete = a.complete

    async def crash(current, proposal):
        saved.update(copy.deepcopy(proposal))
        raise RuntimeError("after durable checkpoint")

    a.complete = crash
    ctx = SimpleNamespace(
        run=run,
        scope=None,
        degrade=SimpleNamespace(shed=False),
        set_turn=AsyncMock(),
        publish=AsyncMock(),
        commit=AsyncMock(return_value=True),
    )
    from app.runs.worker import RetryRun

    with pytest.raises(RetryRun):
        await executor(ctx)
    a.complete = real_complete
    checkpoint = {"case_proposal": saved, "case_proposal_origin": ic.ORIGIN}
    if change in {"all_checkpoint_markers", "inner_marker"}:
        for k in ["answer_origin", "answer_model_invoked", "fact_receipt"]:
            saved.pop(k)
    if change in {"all_checkpoint_markers", "outer_marker"}:
        checkpoint.pop("case_proposal_origin")
    if change == "unknown_kind":
        saved["fact_receipt"]["kind"] = "unknown"
    elif change == "body":
        saved["response"] += "额外承诺"
    elif change == "receipt":
        saved["fact_receipt"]["coverage"]["quantity"]["kind"] = "missing"
    elif change == "review":
        await a.db.execute("UPDATE case_image_reviews SET result=jsonb_set(result,'{document_type}','\"unknown\"')")
    elif change == "request":
        await a.db.execute("UPDATE conversation_log SET content=content||'并取消订单' WHERE role='user'")
    elif change == "continuation":
        await a.db.execute("UPDATE agent_runs SET message='请转人工' WHERE run_id=$1", run.run_id)
    elif change == "actor":
        provider.return_value = None
    elif change == "takeover":
        case = await b.get(ACTOR, run.case_context["case_id"])
        await b.mutate(ACTOR, case["id"], action(case, "takeover"))
    elif change == "version":
        await a.db.execute("UPDATE ticket_case_state SET version=version+1")
    elif change == "lease":
        await a.db.execute("UPDATE agent_runs SET lease_until=0 WHERE run_id=$1", run.run_id)
    elif change == "stale":
        for f in run.case_context["snapshot"]["facts"].values():
            f["business_source"]["observed_at"] = "2000-01-01T00:00:00Z"
    elif change == "material":
        row = await a.db.fetchrow("SELECT object_key FROM attachments LIMIT 1")
        a.attachments.objects.path(row["object_key"]).write_bytes(b"tampered")
    await worker.store.checkpoint(run.run_id, worker.owner, run.attempt, checkpoint)
    run.checkpoint = checkpoint
    ctx.run = run
    if change == "lease":
        with pytest.raises(HTTPException):
            await a.complete(run, saved)
    else:
        result = (await executor(ctx)).result
        assert result["case_applied"] is (change == "none")
        assert not result["handoff"]
        if change != "none":
            assert result["draft_id"] is None and result["answer_model_invoked"] is False
            assert "answer_origin" not in result and "answer_confidence" not in result
    executor.prepare.assert_not_awaited()
    executor.answer.assert_not_awaited()
    assert await scalar(a.db, "SELECT count(*) FROM case_actions WHERE kind='handoff'") == 0
    assert await scalar(a.db, "SELECT count(*) FROM ticket_notifications") == 0


@pytest.mark.parametrize(
    "message,second",
    [
        (MESSAGE + "请转人工。", False),
        (MESSAGE + "请紧急升级。", False),
        (MESSAGE + "并取消订单。", False),
        ("继续处理。", False),
        (MESSAGE, True),
    ],
)
async def test_nonqualified_request_preserves_original_model_handoff_fallback(connected, message, second):
    a, _, pending, executor, worker, _, _ = await continuation(connected, message=message, second=second)
    request = Request(
        message=message,
        user_id=ACTOR.actor_id,
        conv_id="conversation",
        intent=IntentCategory.ORDER,
        urgency=UrgencyLevel.LOW,
    )
    executor.prepare.side_effect = None
    executor.prepare.return_value = SimpleNamespace(
        request=request, intent=SimpleNamespace(intent_group="order"), knowledge_used=False, evidence={}
    )
    executor.answer.side_effect = None
    executor.answer.return_value = OrchestratorResult(
        request_id=request.request_id,
        response="This requires staff review.",
        agent_type=AgentType.ORDER,
        intent=IntentCategory.ORDER,
        primary_agent=AgentType.ORDER,
        routing_confidence=1,
        handoff=True,
        handoff_reason="Explicit model handoff",
        answer_handoff_declared=True,
        answer_confidence=0.65,
    )
    await worker._handle(await delivery(worker, pending.run_id), False)
    run = await a.run(pending.run_id)
    assert run.result["case_applied"] and run.result["handoff"]
    assert "answer_origin" not in run.result and "image_projection_origin" not in run.case_context
    executor.prepare.assert_awaited_once()
    executor.answer.assert_awaited_once()
    assert await scalar(a.db, "SELECT count(*) FROM case_actions WHERE kind='handoff'") == 1
    replay, applied = await a.complete(run, run.checkpoint["case_proposal"])
    assert applied and replay == run.result
    assert await scalar(a.db, "SELECT count(*) FROM case_actions WHERE kind='handoff'") == 1


@pytest.mark.parametrize("change", ["body", "proof", "run_header", "applied_header", "review", "source_clock", "bytes"])
async def test_display_and_approval_recheck_durable_result_and_authority(connected, change):
    a, b, pending, executor, worker, _, _ = await continuation(connected)
    await worker._handle(await delivery(worker, pending.run_id), False)
    run = await a.run(pending.run_id)
    assert run.result["case_applied"]
    case = await a.get(ACTOR, run.result["case_id"])
    draft = case["drafts"][0]
    if change == "body":
        await a.db.execute("UPDATE case_drafts SET body=body||'额外内容'")
    elif change == "proof":
        await a.db.execute("UPDATE case_drafts SET evidence=evidence-0")
    elif change == "run_header":
        await a.db.execute(
            "UPDATE agent_runs SET result=result-'answer_origin'-'answer_model_invoked'-'fact_receipt' WHERE run_id=$1",
            run.run_id,
        )
    elif change == "applied_header":
        await a.db.execute(
            "UPDATE case_run_receipts SET result=result-'answer_origin'-'answer_model_invoked'-'fact_receipt' WHERE run_id=$1",
            run.run_id,
        )
    elif change == "review":
        await a.db.execute("UPDATE case_image_reviews SET result=jsonb_set(result,'{query_outcome}','\"unverified\"')")
    elif change == "source_clock":
        await a.db.execute(
            "UPDATE agent_runs SET finished_at=started_at-1 WHERE run_id=$1",
            run.result["fact_receipt"]["task"]["review_run_id"],
        )
    elif change == "bytes":
        row = await a.db.fetchrow("SELECT object_key FROM attachments LIMIT 1")
        a.attachments.objects.path(row["object_key"]).write_bytes(b"changed")
    assert not (await b.get(ACTOR, case["id"]))["drafts"][0]["valid"]
    with pytest.raises(HTTPException) as error:
        await b.approve(
            ACTOR,
            case["id"],
            draft["id"],
            DraftApproval(
                expected_version=case["version"],
                expected_control_version=case["control_version"],
                body_hash=draft["body_hash"],
            ),
        )
    assert error.value.status_code == 409
