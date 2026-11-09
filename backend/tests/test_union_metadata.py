"""Metadata agreement cannot grant record identity, scope or evidence exemptions."""
import copy
import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from evidence_fixtures import with_evidence
from test_attachments import connected as connected  # noqa: F401
from test_attachments import upload
from test_cases import ACTOR, PG, action, claim, scalar
from test_cases import pair as pair  # noqa: F401
from test_fact_completion import KEY, goal_for, task_for, trace, wire

from app.attachments.verification import Candidate, Extraction, verify
from app.core.business_facts import render_draft, verified_cells, verified_column_metadata
from app.core.fact_completion import bind_result, proposal
from app.core.lookup_scope import _sql_scope, dispatch_for
from app.core.metadata_sources import metadata_columns
from app.core.sql_sources import source_columns
from app.tickets.cases import CaseInput

SQL = """SELECT o.order_no AS order_ref, i.model, i.quantity AS ordered_qty,
o.delivery_date, o.order_status FROM orders o LEFT JOIN order_item i ON o.order_no=i.order_no
WHERE o.order_no='META-482'
UNION ALL
SELECT i.order_no AS other_ref, i.model AS other_model, i.quantity AS other_qty,
o.delivery_date AS other_date, o.order_status AS other_status
FROM order_item i LEFT JOIN orders o ON i.order_no=o.order_no
WHERE i.order_no='META-482' AND o.order_no IS NULL"""
ORIGINS = {"model": ("order_item", "model"), "ordered_qty": ("order_item", "quantity"),
           "delivery_date": ("orders", "delivery_date"), "order_status": ("orders", "order_status")}


def payload(*, units=True, sql=SQL):
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        db.executescript("""CREATE TABLE orders(order_no TEXT, delivery_date TEXT, order_status TEXT);
            CREATE TABLE order_item(order_no TEXT,model TEXT,quantity INT);
            INSERT INTO orders VALUES('META-482','2028-04-09','packing');
            INSERT INTO order_item VALUES('META-482','Panel-Z',7);""")
        rows = [dict(r) for r in db.execute(sql)]
    metadata = {}
    for field, (table, column) in ORIGINS.items():
        source = {"database": "erp_db", "table": table, "column": column,
                  "description": "Planned delivery date, not actual arrival." if field == "delivery_date" else f"Configured {field}."}
        metadata[field] = {**source, "output_field": field, "query_hash": hashlib.sha256(sql.encode()).hexdigest(),
                           "schema_revision": hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}
    return with_evidence({"queried_database": True, "has_risk": False, "degraded": False,
                          "trace_id": uuid.uuid4().hex, "run_id": "sql-" + uuid.uuid4().hex,
                          "execution_status": "success", "result_truncated": False,
                          "steps": [{"database": "erp_db", "success": True, "rows": rows, "final_sql": sql,
                                     "column_units": {"ordered_qty": "pcs"} if units else {}, "column_metadata": metadata}]})


def extracted():
    values = [("order_ref", "META-482", "unknown"), ("model", "Panel-Z", "unknown"),
              ("quantity", "7", "ordered"), ("date", "2028-04-09", "delivery"), ("status", "packing", "order")]
    return Extraction(document_type="order", fields=[Candidate(
        field=f, value=v, kind=k, unit="pcs" if f == "quantity" else None, readability="clear", location="control")
        for f, v, k in values])


def test_complete_metadata_renders_definition_without_gaining_record_identity():
    data = payload()
    assert dict(metadata_columns(SQL)) == ORIGINS
    assert source_columns(SQL) == ()
    step = data["steps"][0]
    assert verified_column_metadata(step, "order_ref") == {}
    cells, issues = verified_cells(data)
    assert not issues and len(cells) == 5
    assert {c["field"] for c in cells if c.get("column_metadata")} == set(ORIGINS)
    body = render_draft([], {"attempted": True, "evidence": cells}, language="en")
    assert "Ordered quantity: 7 pcs" in body
    assert "Planned delivery date, not actual arrival." in body
    assert not step["source_records"]


@pytest.mark.parametrize("field,value", [
    ("query_hash", "0" * 64), ("output_field", "alias_guess"), ("table", "orders"),
    ("column", "other_quantity"), ("database", "other_db"), ("schema_revision", "0" * 64),
    ("description", "Changed after signing."),
])
def test_wrong_binding_or_meaning_revision_is_discarded(field, value):
    step = payload()["steps"][0]
    step["column_metadata"]["ordered_qty"][field] = value
    assert verified_column_metadata(step, "ordered_qty") == {}


@pytest.mark.parametrize("right", [
    "NULL", "7", "'7'", "quantity+0", "SUM(quantity)", "COUNT(*)", "ABS(quantity)",
    "COALESCE(quantity,0)", "CAST(quantity AS INT)",
])
def test_every_branch_expression_is_checked_by_position(right):
    sql = ("SELECT quantity AS q, model AS m FROM order_item UNION ALL "
           f"SELECT {right} AS x, model AS y FROM order_item WHERE 1=0")
    assert dict(metadata_columns(sql)) == {"m": ("order_item", "model")}


@pytest.mark.parametrize("right", [
    "SELECT quantity AS q FROM other_item", "SELECT weight AS q FROM order_item",
    "SELECT * FROM order_item", "SELECT quantity,model FROM order_item",
    "SELECT quantity AS q FROM schema.order_item",
    "SELECT quantity AS q FROM (SELECT quantity FROM order_item) nested",
    "SELECT quantity AS q FROM order_item a JOIN order_item b ON 1=1",
    "SELECT a.quantity AS q FROM order_item a(quantity)",
])
def test_different_or_unresolved_physical_sources_are_not_descriptions(right):
    assert metadata_columns(f"SELECT quantity AS q FROM order_item UNION ALL {right}") == ()


@pytest.mark.parametrize("operator", ["UNION", "INTERSECT", "EXCEPT"])
def test_other_set_operations_are_unsupported(operator):
    select = "SELECT quantity AS q FROM order_item"
    assert metadata_columns(f"{select} {operator} {select}") == ()
    assert metadata_columns(f"({select} {operator} {select}) UNION ALL {select}") == ()


def test_nested_all_aliases_positions_and_duplicates():
    left = "SELECT quantity AS q, model AS m FROM order_item"
    right = "SELECT quantity AS renamed, model AS other FROM order_item"
    sql = f"{left} UNION ALL ({right} UNION ALL {right})"
    assert dict(metadata_columns(sql)) == {"q": ("order_item", "quantity"), "m": ("order_item", "model")}
    assert metadata_columns(f"{sql} UNION ALL SELECT model,quantity FROM order_item") == ()
    assert metadata_columns(f"{left} UNION ALL SELECT quantity AS x, model AS X FROM order_item") == ()
    assert metadata_columns(f"WITH x AS ({left}) {left} UNION ALL SELECT q,m FROM x") == ()
    assert metadata_columns(f"{left} UNION ALL {right}; {left}") == ()
    assert metadata_columns(f"{left} UNION ALL BY NAME {right}") == ()


def test_metadata_cannot_replace_numeric_units_or_change_conflict_grouping():
    data = payload(units=False)
    cells, issues = verified_cells(data)
    assert "missing_unit" in issues and not any(c["field"] == "ordered_qty" for c in cells)
    assert verified_column_metadata(data["steps"][0], "ordered_qty")
    # Stable physical columns do not prove that these are the same business row.
    sql = """SELECT a.order_no AS order_ref,a.model,b.quantity AS ordered_qty,
             o.delivery_date,o.order_status FROM order_item a CROSS JOIN order_item b CROSS JOIN orders o
             UNION ALL SELECT a.order_no,a.model,b.quantity,o.delivery_date,o.order_status
             FROM order_item a CROSS JOIN order_item b CROSS JOIN orders o"""
    assert dict(metadata_columns(sql))["ordered_qty"] == ("order_item", "quantity")
    assert source_columns(sql) == ()  # the conflict-group identity path is not widened
    assert _sql_scope({"database": "erp_db", "final_sql": sql}) is None


def test_authenticated_complete_union_values_cannot_complete_a_fact_task(monkeypatch):
    monkeypatch.setenv("SQL_AGENT_API_KEY", KEY)
    message = "请查询 UNIT-A73 的合同金额及订单状态。"
    task = task_for(message)
    dispatch = dispatch_for(goal_for(task, message), message)
    data = wire(dispatch, sql_change=lambda sql: f"{sql} UNION ALL {sql}")
    bind_result(data, dispatch, key=KEY)
    assert data.get("_fact_dispatch")
    assert not trace(data)["evidence_issues"]
    assert proposal(task, trace(data)) is None
    assert _sql_scope(data["steps"][0]) is None


@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
@pytest.mark.parametrize("control", ["valid", "missing_unit", "stale_image", "takeover"])
async def test_fresh_image_review_and_union_continuation_keep_case_fences(connected, control):
    a, b, attachments, _ = connected
    image = await upload(attachments, key="union-" + uuid.uuid4().hex)
    receipt = await a.accept_input(ACTOR, CaseInput(
        message_id=uuid.uuid4().hex, conv_id="metadata-conversation", new_case=True, issue_type="general",
        order_refs=["META-482"], message="请核对图片中的订单记录。", attachment_ids=[image["id"]]))
    review_run = await claim(a, receipt)
    review_data = payload()  # genuine new SQLite execution, not a refreshed archive
    review = verify(extracted(), review_data, "META-482")
    assert all(f["outcome"] == "matched" for f in review["fields"])
    review.update(attachment_id=image["id"], sha256=review_run.case_context["snapshot"]["review_materials"][0]["sha256"])
    reviewed, applied = await a.complete(review_run, {"material_reviews": [review], "response": "Image checked."})
    assert applied and reviewed["next_run_id"] != review_run.run_id
    continuation = await claim(a, {"run_id": reviewed["next_run_id"]})
    data = payload(units=control != "missing_unit")  # another execution with a distinct SQL run/trace
    assert data["run_id"] != review_data["run_id"]
    business = trace(data)
    c = await b.get(ACTOR, receipt["case_id"])
    if control == "stale_image":
        stale = (datetime.now(UTC) - timedelta(seconds=301)).isoformat()
        facts = copy.deepcopy(c["facts"])
        for fact in facts.values():
            fact["business_source"]["observed_at"] = stale
        await a.db.execute("UPDATE ticket_case_state SET facts=$1::jsonb WHERE case_id=$2", json.dumps(facts), c["id"])
        continuation.case_context["snapshot"]["facts"] = facts
        await a.db.execute("UPDATE agent_runs SET case_context=$1::jsonb WHERE run_id=$2",
                           json.dumps(continuation.case_context), continuation.run_id)
    if control == "takeover":
        await b.mutate(ACTOR, c["id"], action(c, "takeover"))
    body = render_draft([], business, c["facts"], language="en")
    result, applied = await a.complete(continuation, {"response": body, "handoff": False, "business_data": business})
    assert applied is (control == "valid")
    if control == "valid":
        assert result["draft_id"] and "7 pcs" in result["response"]
        saved = await b.get(ACTOR, c["id"])
        assert saved["drafts"][0]["valid"]
        assert await a._evidence_valid(saved["drafts"][0]["evidence"], actor=ACTOR, case=saved,
                                       body=saved["drafts"][0]["body"], run_id=continuation.run_id)
    else:
        assert result["draft_id"] is None and not result["handoff"]
        assert result["case_rejection"] == ("case_control_changed" if control == "takeover" else "draft_evidence_stale")
    assert await scalar(a.db, "SELECT count(*) FROM case_actions WHERE kind='handoff'") == 0
    assert await scalar(a.db, "SELECT count(*) FROM ticket_notifications") == 0
