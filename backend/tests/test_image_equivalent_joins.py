"""Real synthetic SQL rows exercise image-only equivalent join boundaries."""

import copy
import hashlib
import itertools
import json
import sqlite3
from datetime import UTC, datetime

import pytest
import test_image_completion as original
from evidence_fixtures import with_evidence
from test_attachments import connected as connected  # noqa: F401
from test_cases import ACTOR, delivery, scalar
from test_cases import pair as pair  # noqa: F401

from app.attachments.verification import verify
from app.core import image_completion as ic
from app.tickets.cases import DraftApproval

ENTITY = original.ENTITY
JOIN_SHAPES = list(
    itertools.product(
        ["JOIN", "INNER JOIN", "LEFT JOIN", "LEFT OUTER JOIN"],
        [False, True],
        [False, True],
        [False, True],
        [False, True],
    )
)


def query_shape(join="LEFT JOIN", item_first=False, item_key=False, item_filter=False, aliases=False):
    header, detail = ("o", "i") if aliases else ("orders", "order_item")
    tables = {"orders": "orders o" if aliases else "orders", "order_item": "order_item i" if aliases else "order_item"}
    columns = {
        "order_ref": ("order_item" if item_key else "orders", "order_no"),
        "model": ("order_item", "model"),
        "quantity": ("order_item", "quantity"),
        "date": ("orders", "delivery_date"),
        "status": ("orders", "order_status"),
    }
    outputs = {
        "order_ref": "order_no" if aliases else "order_ref",
        "model": "sku" if aliases else "model",
        "quantity": "ordered_qty",
        "date": "delivery_date",
        "status": "order_status",
    }
    names = {"orders": header, "order_item": detail}
    selected = ", ".join(f"{names[t]}.{c} AS {outputs[f]}" for f, (t, c) in columns.items())
    first, second = ("order_item", "orders") if item_first else ("orders", "order_item")
    # Exercise commutative ON and WHERE forms as well as either physical key.
    on = f"{detail}.order_no={header}.order_no" if item_first else f"{header}.order_no={detail}.order_no"
    key = detail if item_filter else header
    where = f"'{ENTITY}'={key}.order_no" if aliases else f"{key}.order_no='{ENTITY}'"
    return f"SELECT {selected} FROM {tables[first]} {join} {tables[second]} ON {on} WHERE {where}", columns, outputs


def executed_data(shape, *, at=None, missing=None, null=None, sql_change=None):
    sql, columns, outputs = query_shape(*shape)
    if sql_change:
        sql = sql_change(sql)
    at = at or datetime.now(UTC)
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE orders(order_no TEXT PRIMARY KEY,delivery_date TEXT,order_status TEXT)")
        db.execute("CREATE TABLE order_item(order_no TEXT,model TEXT,quantity INTEGER)")
        if missing != "header":
            db.execute(
                "INSERT INTO orders VALUES(?,?,?)",
                (ENTITY, None if null == "date" else "2031-06-18", None if null == "status" else "已交付"),
            )
        if missing != "detail":
            db.execute(
                "INSERT INTO order_item VALUES(?,?,?)",
                (ENTITY, None if null == "model" else "CAB-CONTROL", None if null == "quantity" else 4),
            )
        rows = [dict(row) for row in db.execute(sql)]
    metadata = {}
    for field, (table, column) in columns.items():
        meaning = {
            "database": "erp_db",
            "table": table,
            "column": column,
            "description": {
                "date": "计划送货日期，不是实际送达或安装日期。",
                "quantity": "订购数量，不是缺件数量。",
            }.get(field, field),
        }
        metadata[outputs[field]] = {
            **meaning,
            "output_field": outputs[field],
            "schema_revision": hashlib.sha256(
                json.dumps(meaning, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest(),
            "query_hash": hashlib.sha256(sql.encode()).hexdigest(),
        }
    result = with_evidence(
        {
            "execution_status": "success",
            "queried_database": True,
            "has_risk": False,
            "degraded": False,
            "trace_id": "synthetic-real-join",
            "run_id": "synthetic-real-join-run",
            "result_truncated": False,
            "selected_databases": ["erp_db"],
            "steps": [
                {
                    "database": "erp_db",
                    "success": True,
                    "final_sql": sql,
                    "rows": rows,
                    "column_units": {"ordered_qty": "pcs"},
                    "column_metadata": metadata,
                    "as_of": at.isoformat(),
                    "as_of_source": "query_time",
                    "source_records": [{"database": "erp_db", "table": "orders", "keys": [ENTITY]}],
                }
            ],
        }
    )
    result["observed_at"] = at.isoformat()
    return result


def joined_control(shape, **kwargs):
    task, reviews, facts, context = original.control()
    at = datetime.fromisoformat(facts["model"]["business_source"]["as_of"])
    payload = executed_data(shape, at=at, **kwargs)
    extraction = original.extraction().model_copy(update={"document_type": "order"})
    review = verify(extraction, payload, ENTITY)
    review.update(attachment_id=reviews[0]["attachment_id"], sha256=reviews[0]["sha256"])
    reviews[0]["result"] = review
    previous = copy.deepcopy(facts)
    facts.clear()
    for item in review["fields"]:
        if item["outcome"] == "matched":
            field = item["field"]
            facts[field] = {
                **previous[field],
                "value": item["db_value"],
                "db_value": item["db_value"],
                "candidate_value": item["value"],
                "business_source": item["source"],
            }
    return task, reviews, facts, context, payload


@pytest.mark.parametrize("shape", JOIN_SHAPES)
def test_equivalent_joins_use_actual_nonnull_sql_row_without_rewriting_authority(shape):
    task, reviews, facts, ctx, payload = joined_control(shape)
    assert set(facts) == ic.FIELDS and all(f["outcome"] == "matched" for f in reviews[0]["result"]["fields"])
    before = copy.deepcopy((reviews, facts))
    proposal = ic.proposal(task, reviews, facts)
    assert proposal and proposal["answer_origin"] == ic.ORIGIN and proposal["answer_model_invoked"] is False
    assert "answer_confidence" not in proposal and "routing_confidence" not in proposal
    assert ic.receipt_valid(
        proposal["fact_receipt"],
        reviews,
        facts,
        message=original.MESSAGE,
        context=ctx,
        run_id=task["run_id"],
        attempt=1,
        body=proposal["response"],
    )
    physical = facts["order_ref"]["business_source"]["column_metadata"]
    assert physical["table"] == ("order_item" if shape[2] else "orders") and physical["column"] == "order_no"
    assert all(f["business_source"]["final_sql"] == payload["steps"][0]["final_sql"] for f in facts.values())
    assert {f["business_source"]["row_index"] for f in facts.values()} == {0}
    assert before == (reviews, facts)


NEGATIVE_SQL = {
    "right": lambda s: s.replace("LEFT JOIN", "RIGHT JOIN"),
    "full": lambda s: s.replace("LEFT JOIN", "FULL JOIN"),
    "unrelated_join": lambda s: s.replace("orders.order_no=order_item.order_no", "orders.order_no=order_item.model"),
    "extra_on": lambda s: s.replace(
        " ON orders.order_no=order_item.order_no", " ON orders.order_no=order_item.order_no AND order_item.quantity>0"
    ),
    "extra_where": lambda s: s + " AND order_item.quantity>0",
    "constant_false": lambda s: s + " AND 0",
    "wrong_entity": lambda s: s.replace(ENTITY, "OTHER-ORDER"),
    "limit_zero": lambda s: s + " LIMIT 0",
    "limit_one": lambda s: s + " LIMIT 1",
    "extra_projection": lambda s: s.replace("SELECT ", "SELECT order_item.order_no AS extra_key, ", 1),
    "derived_quantity": lambda s: s.replace("order_item.quantity AS", "order_item.quantity+0 AS"),
    "duplicate_alias": lambda s: s.replace(" AS model", " AS order_ref"),
    "distinct": lambda s: s.replace("SELECT ", "SELECT DISTINCT ", 1),
    "sort": lambda s: s + " ORDER BY order_item.model",
}


@pytest.mark.parametrize("change", NEGATIVE_SQL)
def test_unrelated_or_extended_queries_never_gain_image_projection(change):
    shape = ("LEFT JOIN", False, False, False, False)
    task, reviews, facts, _, payload = joined_control(shape, sql_change=NEGATIVE_SQL[change])
    _, _, bindings = query_shape(*shape)
    assert not ic._scope(payload["steps"][0]["final_sql"], ENTITY, bindings)
    assert ic.proposal(task, reviews, facts) is None


@pytest.mark.parametrize("shape", [("LEFT JOIN", False, False, False, False), ("LEFT JOIN", True, True, True, True)])
@pytest.mark.parametrize(
    "missing,null",
    [("header", None), ("detail", None), (None, "model"), (None, "quantity"), (None, "date"), (None, "status")],
)
def test_unmatched_side_or_null_business_value_is_not_a_complete_row(shape, missing, null):
    task, reviews, facts, ctx, _ = joined_control(shape, missing=missing, null=null)
    assert set(facts) != ic.FIELDS
    assert ic.make_task(original.MESSAGE, ctx, task["run_id"], 1) is None
    assert ic.proposal(task, reviews, facts) is None


@pytest.mark.parametrize(
    "change", ["duplicate_field", "duplicate_review", "mixed_row", "null_candidate", "null_db_value"]
)
def test_equivalent_join_keeps_field_record_and_row_integrity_guards(change):
    task, reviews, facts, _, _ = joined_control(("LEFT JOIN", True, True, True, True))
    review = reviews[0]["result"]
    quantity = next(f for f in review["fields"] if f["field"] == "quantity")
    if change == "duplicate_field":
        review["fields"].append(copy.deepcopy(quantity))
    elif change == "duplicate_review":
        reviews.append(copy.deepcopy(reviews[0]))
    elif change == "mixed_row":
        quantity["source"]["row_index"] = 1
    elif change == "null_candidate":
        quantity["value"] = None
    else:
        quantity["db_value"] = None
    assert ic.proposal(task, reviews, facts) is None


@pytest.mark.parametrize(
    "shape",
    [
        ("LEFT JOIN", False, False, False, False),
        ("LEFT JOIN", True, True, True, False),
        ("INNER JOIN", True, True, False, True),
        ("LEFT OUTER JOIN", False, True, True, True),
    ],
)
async def test_actual_review_pg_adoption_display_and_approval_preserve_equivalent_source(connected, monkeypatch, shape):
    monkeypatch.setattr(original, "data", lambda **_: executed_data(shape))
    a, b, pending, executor, worker, query, _ = await original.continuation(connected)
    before = copy.deepcopy(pending.case_context["snapshot"]["facts"])
    await worker._handle(await delivery(worker, pending.run_id), False)
    run = await a.run(pending.run_id)
    assert run.result["case_applied"] and run.result["answer_origin"] == ic.ORIGIN
    assert run.result["answer_model_invoked"] is False and "answer_confidence" not in run.result
    assert run.case_context["snapshot"]["facts"] == before and query.await_count == 1
    executor.prepare.assert_not_awaited()
    executor.answer.assert_not_awaited()
    assert await scalar(a.db, "SELECT count(*) FROM case_actions WHERE kind='handoff'") == 0
    assert await scalar(a.db, "SELECT count(*) FROM ticket_notifications") == 0
    case = await b.get(ACTOR, run.result["case_id"])
    draft = case["drafts"][0]
    assert draft["valid"]
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
