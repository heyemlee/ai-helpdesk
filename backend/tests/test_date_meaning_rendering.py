"""Separate verified dates cannot become a blanket unverified-date assertion."""
import copy
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from evidence_fixtures import with_evidence
from test_request_coverage_rendering import trace
from test_request_plan import ORDER, diagnosed, payload

from app.core.business_facts import render_draft
from app.core.request_plan import make_request_plan


def dates(actual, *, restriction=""):
    planned = payload("erp_db", "orders", [{"order_no": ORDER, "delivery_date": "2032-04-17"}])
    delivered = payload("svc_db", "delivery", [{"order_no": ORDER, "delivered_date": actual, "delivery_status": "待配送"}])
    planned["steps"] += delivered["steps"]
    planned["selected_databases"] += delivered["selected_databases"]
    if restriction:
        planned["steps"][0]["final_sql"] += " " + restriction
    return rebuild(planned)


def rebuild(data):
    for step in data["steps"]:
        for metadata in step.get("column_metadata", {}).values():
            metadata["query_hash"] = hashlib.sha256(step["final_sql"].encode()).hexdigest()
    data = with_evidence(data)
    data["evidence"] = [cell for cell in data["evidence"] if cell["value"] is not None]
    return data


def render(data, language="zh"):
    coverage = diagnosed(make_request_plan(f"查询订单 {ORDER} 交付日期和配送状态。"), data)
    business = trace(data, coverage)
    return render_draft([], business, language=language), coverage, business


@pytest.mark.parametrize("actual", ["2032-04-12", None], ids=["orders003-both-dates", "orders009-real-null"])
@pytest.mark.parametrize("language", ["zh", "en"])
def test_independently_labelled_dates_keep_internal_ambiguity_without_blanket_negative(actual, language):
    data = dates(actual)
    before = copy.deepcopy(data)
    body, checked, _ = render(data, language)
    assert "2032-04-17" in body
    assert (actual or ("未登记" if language == "zh" else "Not registered")) in body
    assert ("交付日期（计划/实际口径）" if language == "zh" else "Delivery date meaning") not in body
    assert checked["roles"]["delivery_date_unspecified"]["status"] == "ambiguous"
    assert checked["planning_only"] and checked["complete"] is False and data == before


@pytest.mark.parametrize("restriction", ["LIMIT 1", "AND delivery_date>'2032-01-01'"])
def test_actual_date_query_restrictions_remain_visible_when_meanings_are_labelled(restriction):
    body, checked, _ = render(dates(None, restriction=restriction))
    assert "未登记" in body and "原问的完整目标、记录范围或口径仍未核实" in body
    assert checked["complete"] is False


@pytest.mark.parametrize("change", ["omitted", "wrong_target", "wrong_schema", "wrong_type", "stale", "no_row", "partial", "risk"])
def test_unverified_or_incomplete_date_observation_cannot_silence_needed_limitations(change):
    data = dates(None)
    step = data["steps"][1]
    if change == "omitted":
        del step["rows"][0]["delivered_date"]
        step["final_sql"] = f"SELECT order_no,delivery_status FROM delivery WHERE order_no='{ORDER}'"
    elif change == "wrong_target":
        step["rows"][0]["order_no"] = "OTHER-992"
    elif change == "wrong_schema":
        step["column_metadata"]["delivered_date"]["schema_revision"] = "wrong-revision"
    elif change == "wrong_type":
        step["rows"][0]["delivered_date"] = "yesterday"
    elif change == "stale":
        step["as_of"] = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
    elif change == "no_row":
        step.update(rows=[], row_count=0)
    elif change == "partial":
        data["execution_status"] = "partial_success"
        step.update(success=False, error="query failed", rows=[], row_count=0)
    else:
        data["has_risk"] = True
        step["risk_findings"] = [{"anomaly": "semantic_mismatch", "severity": "block", "detail": "wrong scope"}]
    body, checked, business = render(rebuild(data))
    assert checked["complete"] is False
    if change in {"partial", "risk"}:
        assert business["evidence_issues"]
        assert "2032-04-17" not in body
    else:
        assert "交付日期（计划/实际口径）" in body


def test_payload_dates_not_in_the_published_evidence_cannot_cancel_the_date_notice():
    data = dates(None)
    _, checked, business = render(data)
    business["evidence"] = [cell for cell in business["evidence"] if cell["field"] not in {"delivery_date", "delivered_date"}]
    body = render_draft([], business)
    assert "2032-04-17" not in body and "交付日期（计划/实际口径）" in body
    assert checked["complete"] is False
