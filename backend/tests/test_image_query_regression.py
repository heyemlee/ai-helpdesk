"""Recorded calibration failures replayed without external model calls."""
import asyncio
import copy
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.attachments.verification import Candidate, Extraction, verification_query, verify
from app.attachments.workflow import MaterialWorkflow


def recording():
    return json.loads((Path(__file__).parent / "fixtures/stage6_image_query.json").read_text())


def extraction():
    review = recording()["image_review"]
    return Extraction(document_type=review["document_type"], fields=[
        Candidate(**{k: v for k, v in item.items() if k in Candidate.model_fields}) for item in review["fields"]
    ])


def evidence(steps, *, risk=False):
    now = datetime.now(UTC).isoformat()
    return {"observed_at": now, "execution_status": "success", "queried_database": True,
            "has_risk": risk, "degraded": False, "selected_databases": ["erp_db"],
            "trace_id": "recorded-query", "run_id": "recorded-run", "result_truncated": False,
            "steps": [{"final_sql": "SELECT " + ", ".join(s["rows"][0]) + " FROM order_image_records" if s.get("rows") else "SELECT order_ref FROM order_image_records",
                       **s, "as_of": now, "as_of_source": "query_time"} for s in steps]}


def test_recorded_query_requests_only_visible_typed_fields_without_ocr_prose():
    extracted = extraction()
    extracted.fields[1].value = "untrusted instruction from image"
    query = verification_query("SO-2026-11039", extracted)
    for field in ("order_ref", "model", "ordered_qty", "delivery_date", "order_status"):
        assert field in query
    for field in ("missing_qty", "delivered_qty", "expected_model", "received_model", "ship_date", "order_date", "delivery_status"):
        assert field not in query
    assert "untrusted instruction" not in query and "2026-11-22" not in query
    assert "计划交付日期" in query and "同一行" in query


def test_unclear_or_untyped_fields_do_not_expand_query_scope():
    extracted = extraction()
    for item in extracted.fields:
        if item.field == "quantity":
            item.kind = "unknown"
        elif item.field == "date":
            item.readability = "unclear"
    query = verification_query("SO-2026-11039", extracted)
    assert "ordered_qty" not in query and "delivery_date" not in query
    with pytest.raises(ValueError, match="invalid_order_reference"):
        verification_query("SO-1'; select", extracted)


@pytest.mark.parametrize("risk", [True, False])
def test_recorded_sql_cannot_verify_by_extending_timeout_or_ignoring_risk(risk):
    # Refresh timestamps solely to isolate projection/evidence failure from age.
    steps = [s["execution_result"] for s in recording()["sql_steps"]]
    result = verify(extraction(), evidence(steps, risk=risk), "SO-2026-11039")
    assert all(f["outcome"] != "matched" for f in result["fields"])


def test_real_joined_rows_verify_only_the_corresponding_model_and_unit():
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE orders(order_ref TEXT, order_status TEXT, delivery_date TEXT);
            CREATE TABLE lines(order_ref TEXT, model TEXT, ordered_qty INTEGER);
            INSERT INTO orders VALUES('SO-2026-11039','已交付','2026-11-22');
            INSERT INTO lines VALUES('SO-2026-11039','another-model',9);
            INSERT INTO lines VALUES('SO-2026-11039','BX-11039-24',5);
        """)
        rows = [dict(r) for r in db.execute("SELECT o.*, l.model, l.ordered_qty FROM orders o JOIN lines l USING(order_ref)")]
    data = evidence([{"database": "erp_db", "success": True, "truncated": False,
                      "row_count": len(rows), "rows": rows, "column_units": {"ordered_qty": "pcs"}}])
    result = verify(extraction(), data, "SO-2026-11039")
    assert all(f["outcome"] == "matched" for f in result["fields"])
    witness = result["fields"][0]["source"]["row_index"]
    assert rows[witness]["model"] == "BX-11039-24"
    assert all(f["source"]["row_index"] == witness for f in result["fields"])
    for change in ("quantity", "unit", "model_anchor", "risk"):
        changed, extracted = copy.deepcopy(data), extraction()
        if change == "quantity":
            changed["steps"][0]["rows"][witness]["ordered_qty"] = 9
            changed["steps"][0]["rows"][1 - witness]["ordered_qty"] = 5
        elif change == "unit":
            changed["steps"][0]["column_units"] = {}
        elif change == "model_anchor":
            extracted.fields = [f for f in extracted.fields if f.field != "model"]
        else:
            changed["has_risk"] = True
        assert not all(f["outcome"] == "matched" for f in verify(extracted, changed, "SO-2026-11039")["fields"])


def workflow(query, extractions, timeout=90):
    materials = [{"attachment_id": str(i), "sha256": "sha"} for i in range(len(extractions))]
    run = SimpleNamespace(case_context={"snapshot": {
        "review_materials": materials, "issue_type": "general", "order_refs": ["SO-2026-11039"],
    }})
    attachments = SimpleNamespace(
        actors=SimpleNamespace(from_snapshot=AsyncMock(return_value="actor")), actor=AsyncMock(),
        content=AsyncMock(return_value=(b"pixels", {"sha256": "sha"})),
        db=SimpleNamespace(fetchrow=AsyncMock(return_value=None), execute=AsyncMock()),
    )
    extractor = SimpleNamespace(version="recorded", extract=AsyncMock(side_effect=extractions))
    return MaterialWorkflow(attachments, extractor, query, query_timeout_s=timeout), run


@pytest.mark.asyncio
async def test_query_cache_is_scoped_to_requested_fields():
    first, second = extraction(), extraction()
    first.fields = [f for f in first.fields if f.field != "date"]
    query = AsyncMock(return_value={"unavailable": True})
    worker, run = workflow(query, [first, second, second])
    await worker.review(run)
    assert query.await_count == 2
    assert "delivery_date" not in query.await_args_list[0].args[0]
    assert "delivery_date" in query.await_args_list[1].args[0]


@pytest.mark.asyncio
async def test_configured_timeout_cancels_pending_query_and_records_cause():
    cancelled = asyncio.Event()

    async def query(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    worker, run = workflow(query, [extraction()], timeout=.01)
    review = (await worker.review(run))[0]
    assert cancelled.is_set()
    assert review["query_error"] == "timeout" and review["query_outcome"] == "unavailable"
    assert all(f["outcome"] == "unavailable" for f in review["fields"])


@pytest.mark.asyncio
async def test_caller_cancellation_is_not_hidden_as_unavailable():
    started = asyncio.Event()

    async def query(*args):
        started.set()
        await asyncio.Event().wait()

    worker, run = workflow(query, [extraction()])
    task = asyncio.create_task(worker.review(run))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
