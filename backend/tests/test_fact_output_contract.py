"""Canonical fact output, checked by the unchanged independent visible oracle.

All records are newly authored fictional controls. No dataset()/release corpus
or archived supplier result is read or re-stamped.
"""
import copy
import importlib
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from evidence_fixtures import with_evidence
from test_fact_completion import KEY, context, goal_for, task_for, trace, wire
from test_fact_completion import receipt_key as receipt_key  # noqa: F401

from app.core.business_facts import render_draft
from app.core.fact_completion import ORIGIN, bind_result, proposal
from app.core.lookup_scope import dispatch_for

SCHEMAS = {
    "orders": "order_no TEXT, contract_amount REAL, order_status TEXT, delivery_date TEXT, order_notes TEXT",
    "work_order": "order_no TEXT, current_stage TEXT, production_status TEXT",
    "delivery": "order_no TEXT, delivery_status TEXT, delivered_date TEXT",
}


@pytest.fixture
def independent_oracle(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    return importlib.import_module("acceptance.oracle").score


def controlled_execution(message, entity, values, records, *, reverse=False, sql_change=None):
    task = task_for(message, context(entity))
    dispatch = dispatch_for(goal_for(task, message), message)
    data = wire(dispatch, values=values, sql_change=sql_change)
    for step in data["steps"]:
        with closing(sqlite3.connect(":memory:")) as db:
            db.row_factory = sqlite3.Row
            for table, rows in records[step["database"]].items():
                db.execute(f"CREATE TABLE {table} ({SCHEMAS[table]})")
                for row in rows:
                    fields = ",".join(row)
                    placeholders = ",".join("?" for _ in row)
                    db.execute(f"INSERT INTO {table} ({fields}) VALUES ({placeholders})", list(row.values()))
            step["as_of"] = datetime.now(UTC).isoformat()
            step["rows"] = [dict(row) for row in db.execute(step["final_sql"])]
            step["observed_at"] = datetime.now(UTC).isoformat()
            step["row_count"] = len(step["rows"])
    data = with_evidence(data)
    data["as_of"] = min(step["as_of"] for step in data["steps"])
    data["observed_at"] = min(step["observed_at"] for step in data["steps"])
    data["as_of_source"] = "query_time"
    data["result_truncated"] = False
    if reverse:
        data["evidence"].reverse()
    bind_result(data, dispatch, key=KEY)
    business = trace(data)
    before = copy.deepcopy(business)
    result = proposal(task, business)
    assert business == before  # Rendering must not prune/relabel the signed source.
    return result, business


def scored_observation(result, business, entity, facts, records):
    scenario = {"id": "fictional-visible-contract", "group": "orders", "subtype": "contract",
        "truth": {"case_status": "in_progress", "control_mode": "assistant", "pending_fields": [],
            "business_resolved": False, "allowed_entities": [entity], "facts": facts,
            "policy_versions": [], "required_text": [], "forbidden_text": [], "image_fields": {},
            "source_required": True, "answerable": True, "side_effects": {"handoff": 0, "notification": 0}}}
    observed = {"completed": True, "runs": [{"status": "succeeded", "result": result}],
        "side_effects": {"handoff": 0, "notification": 0}, "invariant_violations": [],
        "case": {"status": "in_progress", "control_mode": "assistant", "pending_fields": [], "facts": {},
            "drafts": [{"id": "fictional-draft", "valid": True, "created_at": time.time(),
                "body": result["response"], "evidence": [{"source": "business_query", **business}]}]}}
    return scenario, observed, {"records": records, "policies": []}


@pytest.mark.parametrize("entity,roles,values,records,facts", [
    ("CANON-A731", "合同金额和订单状态", {"contract_amount": 2468.75, "order_status": "已签单"},
     {"erp_db": {"orders": [{"order_no": "CANON-A731", "contract_amount": 2468.75, "order_status": "已签单",
                             "order_notes": "DO-NOT-PROJECT-THIS"}]}},
     [{"entity": "CANON-A731", "field": "contract_amount", "value": 2468.75, "unit": "USD", "source": {"database": "erp_db", "table": "orders"}},
      {"entity": "CANON-A731", "field": "order_status", "value": "已签单", "unit": None, "source": {"database": "erp_db", "table": "orders"}}]),
    ("CANON-B908", "当前工序及生产状态", {"current_stage": "封边", "production_status": "生产中"},
     {"prod_db": {"work_order": [{"order_no": "CANON-B908", "current_stage": "封边", "production_status": "生产中"}]}},
     [{"entity": "CANON-B908", "field": "current_stage", "value": "封边", "unit": None, "source": {"database": "prod_db", "table": "work_order"}},
      {"entity": "CANON-B908", "field": "production_status", "value": "生产中", "unit": None, "source": {"database": "prod_db", "table": "work_order"}}]),
])
def test_independent_physical_field_coverage_and_canonical_row_reference(independent_oracle, entity, roles, values, records, facts):
    result, business = controlled_execution(f"请核对订单 {entity} 的{roles}。", entity, values, records)
    assert result and result["answer_origin"] == ORIGIN and result["answer_model_invoked"] is False
    assert result["response"] == render_draft([], business)
    assert result["response"].count("[1]") == 1 and "DO-NOT-PROJECT-THIS" not in result["response"]
    assert not {"answer_confidence", "answer_handoff_declared"} & result.keys()
    checked = independent_oracle(*scored_observation(result, business, entity, facts, records))
    assert checked["fact_correct"], checked


@pytest.mark.parametrize("reverse", [False, True])
def test_cross_source_and_repeated_rows_keep_original_group_numbers(independent_oracle, reverse):
    entity = "CANON-C417"
    records = {"erp_db": {"orders": [{"order_no": entity, "delivery_date": "2028-04-17"}]},
               "svc_db": {"delivery": [{"order_no": entity, "delivery_status": "配送中"},
                                       {"order_no": entity, "delivery_status": "配送中"}]}}
    facts = [{"entity": entity, "field": "delivery_date", "value": "2028-04-17", "unit": None,
              "source": {"database": "erp_db", "table": "orders"}},
             {"entity": entity, "field": "delivery_status", "value": "配送中", "unit": None,
              "source": {"database": "svc_db", "table": "delivery"}}]
    result, business = controlled_execution(f"请查询 {entity} 的约定交付日期及配送状态。", entity,
        {"planned_delivery_date": "2028-04-17", "delivery_status": "配送中"}, records,
        reverse=reverse, sql_change=lambda sql: sql.replace("DISTINCT ", ""))
    assert result and all(result["response"].count(f"[{n}]") == 1 for n in (1, 2, 3))
    assert "计划日期不代表实际送达日期。" not in result["response"]
    assert "字段含义:" in result["response"] and "计划送货日期" in result["response"]
    checked = independent_oracle(*scored_observation(result, business, entity, facts, records))
    assert checked["fact_correct"], checked


@pytest.mark.parametrize("mutation", ["entity", "reference", "colon", "value_reference", "unit", "value", "time", "prose"])
def test_frozen_oracle_rejects_noncanonical_or_unsupported_visible_claims(independent_oracle, mutation):
    entity = "CANON-N516"
    records = {"erp_db": {"orders": [{"order_no": entity, "contract_amount": 516.25}]}}
    facts = [{"entity": entity, "field": "contract_amount", "value": 516.25, "unit": "USD",
              "source": {"database": "erp_db", "table": "orders"}}]
    result, business = controlled_execution(f"请查询 {entity} 的合同金额。", entity, {"contract_amount": 516.25}, records)
    scenario, observed, corpus = scored_observation(result, business, entity, facts, records)
    baseline = independent_oracle(scenario, observed, corpus)
    assert baseline["fact_correct"], baseline
    draft = observed["case"]["drafts"][0]
    body = draft["body"]
    if mutation == "entity":
        body = body.replace(entity, "WRONG-ENTITY")
    elif mutation == "reference":
        body = body.replace("[1]", "[9]")
    elif mutation == "colon":
        body = body.replace("- 合同金额:", "- 合同金额：")
    elif mutation == "value_reference":
        body = body.replace("516.25 USD", "516.25 USD [1]")
    elif mutation == "unit":
        body = body.replace("516.25 USD", "516.25 CNY")
    elif mutation == "value":
        body = body.replace("516.25 USD", "999.25 USD")
    elif mutation == "time":
        body = body.replace(next(line for line in body.splitlines() if line.startswith("数据时点 ")),
                            "数据时点 1999-01-01T00:00:00Z")
    else:
        body += "\n\n各项按各自来源时点列出；草稿仍需员工审批。"
    draft["body"] = body
    rejected = independent_oracle(scenario, observed, corpus)
    assert not rejected["fact_correct"] and rejected["errors"], rejected


def test_unrequested_SQL_column_still_prevents_fact_completion():
    entity = "CANON-EXTRA"
    records = {"erp_db": {"orders": [{"order_no": entity, "contract_amount": 717.5, "order_notes": "unrequested"}]}}
    result, _ = controlled_execution(f"请查询 {entity} 的合同金额。", entity, {"contract_amount": 717.5}, records,
                                    sql_change=lambda sql: sql.replace(" FROM ", ",order_notes FROM "))
    assert result is None
