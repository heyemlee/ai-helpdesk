"""Negative controls exercise the production cell verifier and deterministic renderer."""
import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.core.business_facts import render_draft, verified_cells
from app.core.evidence import assess, normalize_delegation_result


def evidence_payload(rows=None, *, units=None, as_of=None, observed_at=None):
    clock = datetime.now(UTC).isoformat()
    rows = rows or [{"order_ref": "SO-Ab", "model": "Cab-A", "amount": 150, "status": "shipped"}]
    step = dict(database="erp_db", final_sql="SELECT * FROM orders", success=True, truncated=False,
                row_count=len(rows), rows=rows, as_of=as_of or clock, as_of_source="snapshot" if as_of else "query_time",
                source_records=[], column_units=units if units is not None else {"amount": "USD"})
    qh = hashlib.sha256(step["final_sql"].encode()).hexdigest()
    cells = []
    for ri, row in enumerate(rows):
        for field, value in row.items():
            identity = [step['database'], qh, step['as_of'], 0, ri, field, value]
            cells.append(dict(evidence_id="ev_"+hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                              entity={"kind": "query_row", "query_hash": qh, "row_index": ri}, field=field, value=value,
                              unit=step["column_units"].get(field), as_of=step["as_of"], as_of_source=step["as_of_source"],
                              database=step["database"], step_index=0, row_index=ri, query_hash=qh, source_records=[], issues=[]))
    return dict(queried_database=True, has_risk=False, degraded=False, execution_status="success", route="database_query",
                trace_id="test-trace", steps=[step], evidence=cells, observed_at=observed_at or clock,
                answer="正确金额 $150，另外赔偿 $5000，仅供参考。")


def test_answer_prose_never_becomes_evidence_and_every_entity_is_preserved():
    payload = evidence_payload()
    cells, issues = verified_cells(payload)
    assert cells and not issues
    text = render_draft([], {"attempted": True, "evidence": cells})
    assert "150" in text and "5000" not in text and 'Cab-A' in text and 'SO-Ab' in text
    assert normalize_delegation_result(payload)["evidence"] == payload["evidence"]


@pytest.mark.parametrize("change", [
    lambda d: d.pop("execution_status"),
    lambda d: d["steps"][0].update(success=False),
    lambda d: d["steps"][0].update(error="failed"),
    lambda d: d["steps"][0].update(row_count=88),
    lambda d: d["steps"][0].update(rows=[None]),
    lambda d: d["evidence"][0].update(field=[]),
    lambda d: d["evidence"][0].update(field={}),
    lambda d: d["evidence"][0].update(step_index=-1),
    lambda d: d["evidence"][0].update(value="another-order"),
    lambda d: d["evidence"].append(copy.deepcopy(d["evidence"][0])),
    lambda d: d["evidence"].pop(),
    lambda d: d.update(evidence=[None]),
    lambda d: d.update(result_truncated=True),
    lambda d: d["steps"][0].update(truncated=True),
])
def test_malformed_conflicting_or_incomplete_payload_cannot_confirm(change):
    payload = evidence_payload()
    change(payload)
    cells, issues = verified_cells(payload)
    assert not cells and issues


def test_observation_expiry_is_separate_from_historical_snapshot():
    historical = evidence_payload(as_of="2020-01-01T00:00:00Z")
    assert verified_cells(historical)[0]
    historical["observed_at"] = (datetime.now(UTC) - timedelta(seconds=301)).isoformat()
    cells, issues = verified_cells(historical)
    assert not cells and "missing_or_stale_time" in issues


def test_unit_and_trace_gaps_are_not_filled_from_global_currency():
    payload = evidence_payload(units={})
    payload["currency"] = "USD"
    cells, issues = verified_cells(payload)
    assert "missing_unit" in issues and not any(c["field"] == "amount" for c in cells)
    payload.pop("trace_id")
    assert not verified_cells(payload)[0]


def test_same_entity_conflicting_statuses_do_not_become_two_confirmations():
    d = evidence_payload([{"order_ref": "A", "model": "X", "status": "paid"},
                          {"order_ref": "A", "model": "X", "status": "unpaid"}])
    assert verified_cells(d) == ([], ["conflicting_records"])


def test_case_sensitive_models_are_different_entities():
    d = evidence_payload([{"order_ref": "A", "model": "X", "status": "paid"},
                          {"order_ref": "A", "model": "x", "status": "unpaid"}])
    assert verified_cells(d)[0]


def test_explicit_status_cannot_hide_failed_step():
    d = evidence_payload()
    d["steps"][0]["success"] = False
    assert not assess(d).usable_as_source


def test_bilingual_short_references_and_markdown_values_are_literal():
    d = evidence_payload([{"order_ref": "SO-Ab", "model": "![x](https://outside.example/x)",
                           "amount": 150, "status": "[shipped](https://outside.example/fake)"}])
    cells, issues = verified_cells(d)
    assert cells and not issues
    en = render_draft([], {"attempted": True, "evidence": cells}, language="en")
    zh = render_draft([], {"attempted": True, "evidence": cells}, language="zh")
    assert "Order SO-Ab" in en and "Amount: 150 USD" in en and "Data as of" in en
    assert "订单 SO-Ab" in zh and "金额: 150 USD" in zh
    assert "[1]" in en and "ev_" not in en and "query_hash" not in en
    assert r"\!\[x\]" in en and r"\[shipped\]" in en
    assert cells[1]["value"] == d["steps"][0]["rows"][0]["model"]


def test_image_short_references_exclude_employee_and_stale_facts():
    clock = datetime.now(UTC).isoformat()
    facts = {"order_ref": {"source": "employee_material", "value": "claimed"},
             "status": {"source": "verified_image", "db_value": "old", "business_source": {"observed_at":"2020-01-01T00:00:00Z"}},
             "model": {"source": "verified_image", "db_value": "Cab-A", "business_source": {"observed_at":clock, "as_of":clock}}}
    text = render_draft([], {}, facts, language="en")
    assert "[I1]" in text and "Cab-A" in text and "[I2]" not in text and "claimed" not in text and "old" not in text


async def test_tool_selected_facts_replace_prefetch_card_and_deduplicate_policy_refs():
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock

    from app.agents.agent_orchestrator import BaseAgent
    from app.core.business_facts import collect_evidence

    raw = evidence_payload()
    cells, issues = verified_cells(raw)
    business = {'attempted': True, 'evidence': cells, 'evidence_issues': issues, 'evidence_payload': raw}
    ref = {'doc_id': 'doc', 'chunk_id': 'chunk', 'policy_revision': 'v1', 'content': 'Shipping fee $150.'}
    evidence = {'knowledge': {'attempted': True, 'sources': [ref]}, 'business_data': {'attempted': False}}
    trajectory = [{'tool': 'knowledge_search', 'trace': {'sources': [ref]}}, {'tool': 'order_data_query', 'trace': business}]
    req = NS(fact_evidence=evidence, turn=NS(trajectory=trajectory), response_language='en', message='Order status', policy_ref_validator=AsyncMock(return_value=True))
    rendered = await BaseAgent(None, 'unused')._factual_text(req, 'Other compensation $5000, subject to review.')
    assert '150' in rendered and '5000' not in rendered
    assert req.fact_evidence['business_data']['evidence'] == cells
    assert req.fact_evidence['knowledge']['sources'] == [ref]
    assert collect_evidence(req.fact_evidence, trajectory) == req.fact_evidence


@pytest.mark.parametrize('observed_at', [None, '2020-01-01T00:00:00Z'])
def test_image_evidence_never_refreshes_age_from_snapshot(observed_at):
    from test_attachments import evidence, extraction

    from app.attachments.verification import verify

    raw = evidence()
    raw['observed_at'] = observed_at
    result = verify(extraction(), raw, 'SO-1', issue_type='missing_parts')
    assert all(field['outcome'] != 'matched' for field in result['fields'])


@pytest.mark.parametrize('unit,records', [(['USD'], []), ({'unit': 'USD'}, []), ('USD', {'id': 1}), ('USD', [None])])
def test_matching_malformed_units_or_provenance_still_cannot_confirm(unit, records):
    raw = evidence_payload()
    raw['steps'][0]['column_units']['amount'] = unit
    raw['steps'][0]['source_records'] = records
    for cell in raw['evidence']:
        cell['source_records'] = records
        if cell['field'] == 'amount':
            cell['unit'] = unit
    assert not assess(raw).usable_as_source
    assert not verified_cells(raw)[0]


def test_business_values_preserve_decimal_amounts_and_escape_list_markers():
    from app.core.business_facts import display, render_cells

    assert display(1170.25) == "1170.25"
    assert display("1. unsafe list") == r"1\. unsafe list"
    assert display("[untrusted](link)") == r"\[untrusted\](link)"
    body = render_cells([{"database": "erp_db", "step_index": 0, "row_index": 0,
                          "entity_keys": {"order_no": "SO-1"}, "as_of": "2026-11-06T12:00:00Z",
                          "field": "contract_amount", "value": 1170.25, "unit": "USD"}])
    assert "- contract amount: 1170.25 USD" in body


def test_null_field_is_distinct_from_zero_rows_and_query_failure():
    from app.core.business_facts import verified_nulls

    raw = evidence_payload(rows=[{"order_ref": "SO-Ab", "phone": None}], units={})
    raw["evidence"] = [c for c in raw["evidence"] if c["value"] is not None]
    cells, issues = verified_cells(raw)
    assert not issues and cells
    missing = verified_nulls(raw)
    assert len(missing) == 1 and missing[0]["field"] == "phone" and missing[0]["value"] is None
    assert missing[0]["query_hash"] == cells[0]["query_hash"]
    assert missing[0]["entity_keys"] == {"order_ref": "SO-Ab"}
    body = render_draft([], {"attempted": True, "evidence": cells, "evidence_payload": raw})
    assert "phone: 未登记（NULL）" in body and "订单 SO-Ab" in body
    assert "未找到记录" not in body and "查询失败" not in body
    for change in ("failure", "zero_rows", "stale", "truncated"):
        changed = copy.deepcopy(raw)
        if change == "failure":
            changed["steps"][0]["success"] = False
            changed["execution_status"] = "failed"
        elif change == "zero_rows":
            changed["steps"][0].update(rows=[], row_count=0)
            changed.update(execution_status="no_data", evidence=[])
        elif change == "stale":
            changed["observed_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        else:
            changed["steps"][0]["truncated"] = True
        assert not verified_nulls(changed), change
    empty = render_draft([], {"attempted": True, "evidence_issues": ["no_data"]})
    failed = render_draft([], {"attempted": True, "evidence_issues": ["failed"]})
    assert "未找到记录" in empty and "未登记" not in empty
    assert "查询失败" in failed and "未登记" not in failed
