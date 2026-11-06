"""Validate execution cells and render facts without a generative second pass."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime

from app.core.evidence import assess

MAX_AGE_SECONDS = 300
_ANCHORS = ("order_ref", "order_id", "order_no", "model", "sku", "expected_model", "received_model", "id")
_NUMERIC = re.compile(r"^[+-]?\d+(?:\.\d+)?$")


def collect_evidence(evidence, trajectory=()):
    """One reference order shared by the reply, API, checkpoint and review card."""
    knowledge = dict(evidence.get("knowledge") or {})
    sources = list(knowledge.get("sources") or [])
    business = dict(evidence.get("business_data") or {})
    for entry in trajectory:
        trace = entry.get("trace") or {}
        if entry.get("tool") == "knowledge_search":
            sources.extend(trace.get("sources") or [])
        elif entry.get("tool") == "order_data_query" and trace.get("attempted"):
            business = dict(trace)
    unique = {}
    for source in sources:
        key = json.dumps([source.get(k) for k in ("doc_id", "chunk_id", "policy_revision", "content", "excerpt")], sort_keys=True)
        unique.setdefault(key, source)
    knowledge["sources"] = list(unique.values())
    return {"knowledge": knowledge, "business_data": business}


def stamp(value):
    try:
        value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return value if value.tzinfo else None
    except ValueError:
        return None


def fresh(value, at=None):
    value = stamp(value)
    return bool(value and 0 <= ((at or datetime.now(UTC)) - value).total_seconds() <= MAX_AGE_SECONDS)


def verified_cells(data, *, at=None):
    try:
        return _verified_cells(data, at=at)
    except (KeyError, TypeError, IndexError, ValueError, AttributeError, OverflowError):
        return [], ["invalid_evidence"]


def _verified_cells(data, *, at=None):
    verdict = assess(data)
    issues = list(verdict.reasons)
    if not verdict.usable_as_source or verdict.execution_status != "success" or verdict.truncated:
        return [], [*issues, verdict.execution_status, *( ["truncated"] if verdict.truncated else [])]
    cells = data.get("evidence")
    if not isinstance(cells, list) or not cells:
        return [], ["missing_evidence"]
    checked = []
    for cell in cells:
        try:
            si, ri = cell["step_index"], cell["row_index"]
            if type(si) is not int or type(ri) is not int or si < 0 or ri < 0:
                raise ValueError
            step = data["steps"][si]
            row = step["rows"][ri]
            field, value = cell["field"], cell["value"]
            query_hash = hashlib.sha256(step["final_sql"].encode()).hexdigest()
            identity = [step["database"], query_hash, step["as_of"], si, ri, field, value]
            expected_id = "ev_" + hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
            if (row[field] != value or type(row[field]) is not type(value) or cell["evidence_id"] != expected_id
                    or cell["entity"] != {"kind": "query_row", "query_hash": query_hash, "row_index": ri}
                    or cell["query_hash"] != query_hash or cell["database"] != step["database"]
                    or cell.get("as_of") != step.get("as_of") or cell.get("as_of_source") != step.get("as_of_source")
                    or cell.get("unit") != (step.get("column_units") or {}).get(field)
                    or cell.get("source_records") != step.get("source_records", [])):
                raise ValueError
            reason = []
            if (not fresh(data.get("observed_at"), at) or not stamp(cell.get("as_of"))
                    or cell.get("as_of_source") not in {"query_time", "snapshot"}):
                reason.append("missing_or_stale_time")
            if not step["final_sql"] or not (cell.get("source_records") or data.get("trace_id") or data.get("run_id")):
                reason.append("missing_provenance")
            numeric = not isinstance(value, bool) and (isinstance(value, (int, float)) or bool(_NUMERIC.fullmatch(str(value))))
            if numeric and field not in _ANCHORS and not field.endswith("_id") and not cell.get("unit"):
                reason.append("missing_unit")
            if reason:
                issues.extend(reason)
            else:
                checked.append({**cell, "entity_keys": {k: row[k] for k in _ANCHORS if k in row and row[k] is not None},
                                "trace_id": data.get("trace_id", ""), "run_id": data.get("run_id", ""), "observed_at": data["observed_at"]})
        except (KeyError, TypeError, IndexError, ValueError):
            issues.append("invalid_evidence")
    # Fail closed if a wire cell was forged, omitted, or duplicated. Unit/time
    # gaps are distinct outcomes; no nearby column or global currency fills them.
    actual = {(c["step_index"], c["row_index"], c["field"]) for c in cells if isinstance(c, dict)
              and all(k in c for k in ("step_index", "row_index", "field"))}
    expected = {(si, ri, name) for si, step in enumerate(data["steps"]) for ri, row in enumerate(step["rows"])
                for name, v in row.items() if v is not None and not isinstance(v, (list, dict))}
    if actual != expected or len(actual) != len(cells) or "invalid_evidence" in issues:
        return [], ["invalid_evidence"]
    grouped = {}
    for cell in checked:
        if cell["entity_keys"]:
            key = json.dumps([cell["entity_keys"], cell["field"]], sort_keys=True)
            grouped.setdefault(key, set()).add(json.dumps([cell["value"], cell["unit"]], sort_keys=True))
    if any(len(values) > 1 for values in grouped.values()):
        return [], ["conflicting_records"]
    return checked, sorted(set(issues))


_LABELS = {
    "order_ref": ("订单", "Order"), "order_id": ("订单", "Order"), "order_no": ("订单", "Order"),
    "job_no": ("工单", "Job"), "model": ("型号", "Model"), "sku": ("型号", "SKU"),
    "amount": ("金额", "Amount"), "total_amount": ("总金额", "Total amount"), "unit_price": ("单价", "Unit price"),
    "status": ("状态", "Status"), "order_status": ("订单状态", "Order status"),
    "quantity": ("数量", "Quantity"), "missing_qty": ("缺件数量", "Missing quantity"),
    "ordered_qty": ("订购数量", "Ordered quantity"), "delivered_qty": ("送达数量", "Delivered quantity"),
    "expected_model": ("应收型号", "Expected model"), "received_model": ("实收型号", "Received model"),
    "delivery_date": ("配送日期", "Delivery date"), "ship_date": ("发货日期", "Ship date"),
    "date": ("日期", "Date"), "order_date": ("订单日期", "Order date"), "lead_time": ("交期", "Lead time"),
    "phone": ("电话", "Phone"),
}


def language_of(text):
    return "zh" if re.search(r"[\u4e00-\u9fff]", text or "") else "en"


def label(field, language):
    return display(_LABELS.get(field, (field.replace("_", " "), field.replace("_", " ")))[language == "en"])


def display(value):
    text = str(value).replace("\n", " ").replace("\r", " ")
    text = re.sub(r"([\\`*_{}\[\]<>!|~])", r"\\\1", text)
    text = re.sub(r"^([-+#])", r"\\\1", text)
    # Escape an actual Markdown list marker, not the leading digits of a
    # decimal amount. A backslash before a digit is rendered literally.
    return re.sub(r"^(\d+)\.(?=\s|$)", r"\1\\.", text)


def render_cells(cells, language="zh"):
    groups = {}
    for cell in cells:
        key = (cell["database"], cell["step_index"], cell["row_index"])
        groups.setdefault(key, []).append(cell)
    lines = []
    for index, group in enumerate(groups.values(), 1):
        first = group[0]
        anchors = " · ".join(f"{label(k, language)} {display(v)}" for k, v in first["entity_keys"].items())
        anchors = anchors or (f"查询记录 {index}" if language == "zh" else f"Query record {index}")
        when = ("数据时点" if language == "zh" else "Data as of") + " " + first["as_of"]
        lines.append(f"{anchors} [{index}]\n{when}")
        for cell in group:
            if cell["field"] in first["entity_keys"]:
                continue
            value = ("Not registered" if language == "en" else "未登记") if cell.get("null_observation") else display(cell['value'])
            lines.append(f"- {label(cell['field'], language)}: {value}"
                         f"{(' ' + display(cell['unit'])) if cell.get('unit') else ''}")
    return "\n".join(lines)


def verified_nulls(data):
    """Describe NULL only in a complete, current, attributable query row.

    NULL is an observation about a field, never a guessed business value. It
    retains the same row/query/time provenance as the row's verified anchor.
    """
    cells, issues = verified_cells(data)
    if issues:
        return []
    anchors = {(c["step_index"], c["row_index"]): c for c in cells if c.get("entity_keys")}
    result = []
    for (si, ri), anchor in anchors.items():
        step = data["steps"][si]
        for field, value in step["rows"][ri].items():
            if value is not None:
                continue
            identity = [anchor["database"], anchor["query_hash"], anchor["as_of"], si, ri, field, None]
            result.append({**anchor, "field": field, "value": None, "unit": None, "null_observation": True,
                           "evidence_id": "null_" + hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()})
    return result


_OUTCOMES = {"no_data": "查询成功，未找到记录。", "failed": "业务查询失败，请人工核实。",
             "partial_success": "查询仅部分成功，暂不据此确认业务事实。", "truncated": "结果被截断，需要完整查询后核实。",
             "missing_unit": "部分字段缺少单位，尚不能确认为业务事实。", "missing_or_stale_time": "证据时点缺失或已过期，请重新查询。",
             "missing_provenance": "缺少可追溯来源，请重新查询。", "conflicting_records": "业务记录相互冲突，请人工核实。",
             "missing_evidence": "未收到结构化业务证据，请重新查询。", "invalid_evidence": "业务证据与原始行不一致，请人工核实。"}


# Without structured evidence, only a small non-factual conversational vocabulary
# may be published. Intent routing never authorizes a business assertion.
_SAFE_CHAT = {"你好", "您好", "你好！", "您好！", "Hello!", "Hello", "Hi!", "Hi", "谢谢", "谢谢！", "Thank you!"}


def conversational_only(text, language="zh"):
    if text.strip() in _SAFE_CHAT:
        return text
    return ("请提供需要核实的问题及相关材料。业务事实需要有效的政策或业务记录支持。" if language == "zh" else
            "Please share the question and relevant records. Business facts require applicable policy or verified records.")


def rendered_images(facts):
    return [(field, fact) for field, fact in (facts or {}).items()
            if fact.get("source") == "verified_image" and fact.get("db_value") is not None
            and fresh((fact.get("business_source") or {}).get("observed_at"))]


def render_draft(knowledge, business, facts=None, language="zh"):
    """Critical values are program rendered; identifiers live in evidence details."""
    english = language == "en"
    parts = []
    if business and business.get("attempted"):
        cells, issues = business.get("evidence", []), business.get("evidence_issues", [])
        if cells:
            missing = verified_nulls(business.get("evidence_payload") or {})
            parts.append(render_cells([*cells, *missing], language))
        for issue in dict.fromkeys(issues):
            if issue in _OUTCOMES:
                parts.append(_OUTCOMES_EN.get(issue, "The evidence needs verification.") if english else _OUTCOMES[issue])
    for index, ref in enumerate(knowledge or [], 1):
        policy = {k: display(v) for k, v in (ref.get("policy") or {}).items()}
        body = ref.get("content") or ref.get("excerpt") or ""
        if body and ref.get("policy_revision"):
            heading = (f"Policy excerpt [P{index}]: {display(ref.get('title', ''))} (version {policy.get('version', '')}; "
                       f"product {policy.get('product')}; region {policy.get('region')}; audience {policy.get('audience')}; "
                       f"as of {ref.get('policy_as_of', '')})" if english else
                       f"政策摘录 [P{index}]：{display(ref.get('title', ''))}（版本 {policy.get('version', '')}；"
                       f"产品 {policy.get('product')}；区域 {policy.get('region')}；受众 {policy.get('audience')}；"
                       f"适用时点 {ref.get('policy_as_of', '')}）")
            parts.append(heading + "\n" + "\n".join("> " + line for line in body.splitlines()))
    for index, (field, fact) in enumerate(rendered_images(facts), 1):
        source = fact.get("business_source") or {}
        prefix = "Image verified" if english else "图片已核实"
        parts.append(f"{prefix} [I{index}] · {label(field, language)}: {display(fact['db_value'])}"
                     f" {display(fact.get('unit') or '')} · {display(source.get('as_of'))}")
    if not parts:
        return ("Material saved. No applicable published policy or valid business evidence is available. Please supply the product, region, audience and relevant records."
                if english else "材料已保存。目前缺少适用的已发布政策或有效业务证据，请补充适用产品、区域、受众及业务记录后核实。")
    note = ("These are source records for review. An employee must review applicable policy before confirming replacements, liability, amounts or delivery commitments."
            if english else "以上为可核对的资料。涉及补件、责任、金额或交期承诺，请由员工按适用政策审核后处理。")
    return "\n\n".join(parts) + "\n\n" + note


_OUTCOMES_EN = {
    "no_data": "The query succeeded and found no records.", "failed": "The business query failed. Please verify with an employee.",
    "partial_success": "Only part of the query succeeded; business facts remain unconfirmed.",
    "truncated": "The result was truncated. A complete query is needed.", "missing_unit": "Some fields have no confirmed unit.",
    "missing_or_stale_time": "The observation time is missing or expired. Query the records again.",
    "missing_provenance": "The record source cannot be traced. Query again.", "conflicting_records": "The business records conflict and require review.",
    "missing_evidence": "Structured evidence was not received. Query again.", "invalid_evidence": "The evidence does not match the result rows and needs review.",
}
