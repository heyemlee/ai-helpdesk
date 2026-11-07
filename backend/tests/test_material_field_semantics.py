"""Field meaning and complete row evidence, independent of document appearance."""

import copy
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from evidence_fixtures import with_evidence
from test_attachments import connected as connected  # noqa: F401
from test_attachments import upload
from test_cases import ACTOR, PG, claim
from test_cases import pair as pair  # noqa: F401

from app.agents.agent_orchestrator import AgentType, OrchestratorResult
from app.attachments.verification import PROMPT, PROMPT_VERSION, Candidate, Extraction, verification_query, verify
from app.attachments.workflow import MaterialWorkflow
from app.core.intent_recognizer import IntentCategory
from app.events import EventLog
from app.tickets.case_execution import CaseExecutor
from app.tickets.cases import CaseInput
from app.tickets.draft_decision import complete_order_image_review


def extracted(kind="order"):
    values = {
        "order_ref": ("CONTROL-41", "order"),
        "model": ("HINGE-A", "unknown"),
        "quantity": ("3", "ordered"),
        "date": ("2027-01-12", "delivery"),
        "status": ("scheduled", kind),
    }
    return Extraction(
        document_type="delivery",
        fields=[
            Candidate(
                field=k,
                value=v,
                kind=t,
                unit="pcs" if k == "quantity" else None,
                readability="clear",
                location="visible field",
            )
            for k, (v, t) in values.items()
        ],
    )


def test_extraction_contract_and_cache_version_preserve_independent_field_meaning():
    assert PROMPT_VERSION == "order-image-v2"
    assert "overall document type does not determine each field's kind" in PROMPT
    assert "Use unknown when the label does not establish it" in PROMPT


def joined_data():
    sql = "SELECT o.order_no AS order_ref,i.model,i.quantity AS ordered_qty,o.delivery_date,o.order_status FROM orders o JOIN items i ON o.order_no=i.order_no WHERE o.order_no='CONTROL-41'"
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        db.executescript("""CREATE TABLE orders(order_no TEXT,delivery_date TEXT,order_status TEXT);
            CREATE TABLE items(order_no TEXT,model TEXT,quantity INTEGER);
            INSERT INTO orders VALUES('CONTROL-41','2027-01-12','scheduled');
            INSERT INTO items VALUES('CONTROL-41','HINGE-A',3);""")
        rows = [dict(r) for r in db.execute(sql)]
    now = datetime.now(UTC).isoformat()
    return with_evidence(
        {
            "queried_database": True,
            "execution_status": "success",
            "has_risk": False,
            "degraded": False,
            "result_truncated": False,
            "selected_databases": ["erp_db"],
            "trace_id": "control-query",
            "run_id": "control-execution",
            "steps": [
                {
                    "database": "erp_db",
                    "success": True,
                    "truncated": False,
                    "rows": rows,
                    "row_count": len(rows),
                    "final_sql": sql,
                    "as_of": now,
                    "as_of_source": "query_time",
                    "column_units": {"ordered_qty": "pcs"},
                    "source_records": [
                        {"database": "erp_db", "table": "orders", "key_columns": ["order_no"], "keys": ["CONTROL-41"]}
                    ],
                }
            ],
        }
    )


@pytest.mark.parametrize(
    "kind,expected,excluded",
    [("order", "order_status", "delivery_status"), ("delivery", "delivery_status", "order_status")],
)
def test_explicit_status_kind_wins_over_document_type(kind, expected, excluded):
    extraction = extracted(kind)
    q = verification_query("CONTROL-41", extraction)
    assert expected in q and excluded not in q
    assert "scheduled" not in q and "HINGE-A" not in q
    for field in ["order_ref", "model", "ordered_qty", "delivery_date"]:
        assert field in q


def test_unknown_status_meaning_does_not_inherit_document_type_or_matching_value():
    extraction = extracted("unknown")
    q = verification_query("CONTROL-41", extraction)
    assert "order_status" not in q and "delivery_status" not in q
    review = verify(extraction, joined_data(), "CONTROL-41")
    assert next(f for f in review["fields"] if f["field"] == "status")["outcome"] == "unverified"
    assert not complete_order_image_review([review])


def test_delivery_looking_order_record_keeps_one_real_witness_for_all_five_fields():
    review = verify(extracted(), joined_data(), "CONTROL-41")
    assert review["query_outcome"] == "checked" and all(f["outcome"] == "matched" for f in review["fields"])
    assert (
        len({(f["source"]["database"], f["source"]["query_hash"], f["source"]["row_index"]) for f in review["fields"]})
        == 1
    )
    assert complete_order_image_review([review])
    genuine_delivery = copy.deepcopy(review)
    next(f for f in genuine_delivery["fields"] if f["field"] == "status")["kind"] = "delivery"
    assert not complete_order_image_review([genuine_delivery])


@pytest.mark.parametrize(
    "change", ["other_status_kind", "split_step", "wrong_order", "unit", "stale", "risk", "different_status"]
)
def test_scope_units_freshness_and_real_conflicts_still_prevent_complete_adoption(change):
    data = joined_data()
    step = data["steps"][0]
    if change == "other_status_kind":
        step["rows"][0]["delivery_status"] = step["rows"][0].pop("order_status")
    elif change == "split_step":
        status = step["rows"][0].pop("order_status")
        separate = copy.deepcopy(step)
        separate["rows"] = [{"order_ref": "CONTROL-41", "order_status": status}]
        data["steps"].append(separate)
    elif change == "wrong_order":
        step["rows"][0]["order_ref"] = "CONTROL-42"
    elif change == "unit":
        step["column_units"] = {}
    elif change == "stale":
        data["observed_at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    elif change == "risk":
        data["has_risk"] = True
    else:
        step["rows"][0]["order_status"] = "delivered"
    review = verify(extracted(), data, "CONTROL-41")
    assert not all(f["outcome"] == "matched" for f in review["fields"])
    assert not complete_order_image_review([review])


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
async def test_verified_delivery_looking_order_flows_through_case_fact_adoption_and_continuation(connected):
    service, _, attachments, _ = connected
    image = await upload(attachments)
    receipt = await service.accept_input(
        ACTOR,
        CaseInput(
            message_id="typed-image",
            conv_id="conversation",
            new_case=True,
            issue_type="order",
            order_refs=["CONTROL-41"],
            message="核对材料中的订单字段",
            attachment_ids=[image["id"]],
        ),
    )
    run = await claim(service, receipt)
    extractor = SimpleNamespace(version="field-semantics-control", extract=AsyncMock(return_value=extracted()))
    query = AsyncMock(return_value=joined_data())
    reviews = await MaterialWorkflow(attachments, extractor, query).review(run)
    assert "order_status" in query.call_args.args[0] and "delivery_status" not in query.call_args.args[0]
    result, applied = await service.complete(
        run, {"material_reviews": reviews, "response": "reviewed", "handoff": False}
    )
    assert applied
    detail = await service.get(ACTOR, receipt["case_id"])
    assert not detail["pending_fields"] and detail["status"] == "in_progress"
    assert set(detail["facts"]) == {"order_ref", "model", "quantity", "date", "status"}
    assert all(f["source"] == "verified_image" and f["business_source"]["query_hash"] for f in detail["facts"].values())
    next_run = await claim(service, {"run_id": result["next_run_id"]})
    assert next_run.case_context["image_review_complete"] is True
    request = SimpleNamespace(lookup_goal=None)
    prepared = SimpleNamespace(
        request=request,
        intent=SimpleNamespace(intent_group="query"),
        knowledge_used=False,
        evidence={"knowledge": {}, "business_data": {}},
    )
    answer = OrchestratorResult(
        request_id="control",
        response="verified",
        agent_type=AgentType.ORDER,
        intent=IntentCategory.ORDER_STATUS,
        handoff=False,
    )
    executor = CaseExecutor(
        service,
        prepare=AsyncMock(return_value=prepared),
        answer=AsyncMock(return_value=answer),
        event_log=EventLog.disabled(),
    )
    ctx = SimpleNamespace(
        run=next_run,
        scope=None,
        degrade=SimpleNamespace(shed=False),
        set_turn=AsyncMock(),
        publish=AsyncMock(),
        commit=AsyncMock(return_value=True),
    )
    completed = (await executor(ctx)).result
    assert completed["case_applied"] and completed["handoff"] is False and "请补充" not in completed["response"]
    final = await service.get(ACTOR, receipt["case_id"])
    assert final["drafts"][0]["valid"] and final["status"] == "in_progress" and not final["pending_fields"]
