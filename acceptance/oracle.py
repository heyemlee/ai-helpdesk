"""Independent, fail-closed scoring of visible replies and persisted Case state.

No SUT judge, renderer, result matcher, prompt or model is imported. The scorer
checks every returned business cell against the authored records, then verifies
the visible value and unit, lineage, timestamps and source. Missing evidence is
a failed score, never an omitted denominator. Unsupported prose is flagged for
review and cannot silently count as correct.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation

LABELS = {"订单": "order_no", "订单号": "order_no", "型号": "model", "订单状态": "order_status",
    "合同金额": "contract_amount", "当前工序": "current_stage", "生产状态": "production_status",
    "配送状态": "delivery_status", "配送日期": "delivery_date", "交付日期": "delivery_date",
    "数量": "quantity", "补件数量": "quantity", "补件状态": "part_status", "单价": "unit_price",
    "物料名称": "material_name", "现有库存": "on_hand_qty", "已占用库存": "allocated_qty",
    "订购数量": "quantity", "ordered_qty": "quantity", "order_ref": "order_no", "电话": "phone", "Phone": "phone"}
UNITS = {"contract_amount": "USD", "unit_price": "USD", "subtotal": "USD", "quantity": "pcs",
         "on_hand_qty": "pcs", "allocated_qty": "pcs", "safety_stock": "pcs", "purchased_qty": "pcs", "received_qty": "pcs"}
SAFE_LINES = {
    "以上为可核对的资料。涉及补件、责任、金额或交期承诺，请由员工按适用政策审核后处理。",
    "材料已保存。目前缺少适用的已发布政策或有效业务证据，请补充适用产品、区域、受众及业务记录后核实。",
    "请提供需要核实的问题及相关材料。业务事实需要有效的政策或业务记录支持。",
    "查询成功，未找到记录。", "业务查询失败，请人工核实。", "结果被截断，需要完整查询后核实。",
    "业务记录相互冲突，请人工核实。", "部分字段缺少单位，尚不能确认为业务事实。",
    "证据时点缺失或已过期，请重新查询。", "未收到结构化业务证据，请重新查询。",
}


def field_name(value):
    return LABELS.get(value, value.replace(" ", "_"))


def equivalent(left, right):
    if left is None or right is None:
        return left is right
    if isinstance(right, (int, float)) and not isinstance(right, bool):
        try:
            return Decimal(str(left).replace(",", "")) == Decimal(str(right))
        except InvalidOperation:
            return False
    return str(left).strip() == str(right).strip()


def stamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.timestamp() if result.tzinfo else None
    except (TypeError, ValueError, AttributeError):
        return None


def score(scenario, observed, corpus):
    truth = scenario["truth"]
    errors, invariants = [], list(observed.get("invariant_violations", []))
    case = observed.get("case") or {}
    drafts = [d for d in case.get("drafts", []) if d.get("valid")]
    draft = drafts[0] if drafts else {}
    visible = re.sub(r"\\([\\`*_{}\[\]<>!|~])", r"\1", draft.get("body", ""))
    if not observed.get("completed"):
        errors.append("scenario_incomplete")
    if not draft:
        errors.append("no_current_visible_draft")
    runs = observed.get("runs", [])
    run_terminal = bool(runs) and all(r.get("status") in {"succeeded", "failed", "cancelled", "dead"} for r in runs)
    if not run_terminal:
        errors.append("run_not_terminal")
    if any(r.get("status") != "succeeded" or (r.get("result") or {}).get("service_unavailable") for r in runs):
        errors.append("service_or_run_failed")
    for key, actual in [("case_status", case.get("status")), ("control_mode", case.get("control_mode"))]:
        if actual != truth[key]:
            errors.append(key + "_mismatch")
    if sorted(case.get("pending_fields", [])) != sorted(truth["pending_fields"]):
        errors.append("pending_fields_mismatch")
    if bool(case.get("status") == "resolved") != truth["business_resolved"]:
        errors.append("business_resolution_mismatch")
    if truth.get("separate_cases") and len(observed.get("case_ids", {})) != truth["separate_cases"]:
        errors.append("case_separation_failed")
    for event in observed.get("step_checks", []):
        if event.get("passed") is not True:
            errors.append("step_expectation_failed")
    effects = observed.get("side_effects")
    if effects is None:
        errors.append("side_effects_unmeasured")
    else:
        for kind in ("handoff", "notification"):
            if effects.get(kind) != truth["side_effects"][kind]:
                invariants.append("unauthorized_or_duplicate_" + kind)
    refs = draft.get("evidence", [])
    business = [r for r in refs if r.get("source") == "business_query"]
    valid_cells, allowed_lines, covered = [], set(SAFE_LINES), set()
    source_groups = []
    for query_index, query in enumerate(business):
        payload = query.get("evidence_payload") or query
        for cell in query.get("evidence", []):
            database, key = cell.get("database"), field_name(cell.get("field", ""))
            try:
                step = payload["steps"][cell["step_index"]]
                row = step["rows"][cell["row_index"]]
            except (KeyError, IndexError, TypeError):
                errors.append("source_row_missing")
                continue
            row = {field_name(k): v for k, v in row.items()}
            anchors = {k: v for k, v in row.items() if k in {"order_no", "model", "material_name"}}
            entity = anchors.get("order_no") or anchors.get("material_name")
            if entity not in truth["allowed_entities"]:
                errors.append("wrong_or_unbound_entity")
                continue
            tables = re.findall(r"(?:FROM|JOIN)\s+[\"`\[]?(?:\w+\.)?(\w+)", step.get("final_sql", ""), re.I)
            witnesses = [(table, r) for table in tables for r in corpus["records"].get(database, {}).get(table, [])
                         if all(k not in r or r[k] == v for k, v in anchors.items())
                         and (r.get("order_no") == entity or r.get("material_name") == entity)]
            witnesses = [(table, r) for table, r in witnesses if key in r and equivalent(cell.get("value"), r[key])]
            if not witnesses or not equivalent(row.get(key), cell.get("value")):
                errors.append("incorrect_or_unproven_cell:" + key)
                continue
            if key in UNITS and cell.get("unit") != UNITS[key]:
                errors.append("incorrect_unit:" + key)
                continue
            when, observed_at = stamp(cell.get("as_of")), stamp(payload.get("observed_at"))
            if when is None or observed_at is None or cell.get("as_of_source") != "query_time" or not 0 <= observed_at - when <= 300:
                errors.append("incorrect_data_time")
                continue
            if not (payload.get("trace_id") or payload.get("run_id")):
                errors.append("source_identifier_missing")
                continue
            table = witnesses[0][0]
            identity = (query_index, cell["step_index"], cell["row_index"])
            if identity not in [g["identity"] for g in source_groups]:
                source_groups.append({"identity": identity, "anchors": anchors, "as_of": cell["as_of"]})
            valid_cells.append({"entity": entity, "field": key, "value": cell["value"], "unit": cell.get("unit"),
                                "source": {"database": database, "table": table}, "as_of": cell["as_of"], "group": identity})
        # Verify NULL claims against raw rows and the authored records too.
        for group in [g for g in source_groups if g["identity"][0] == query_index]:
            _, si, ri = group["identity"]
            step = payload["steps"][si]
            if step.get("success") is not True or step.get("truncated") is not False or step.get("row_count") != len(step.get("rows", [])):
                continue
            row = step["rows"][ri]
            entity = group["anchors"].get("order_no") or group["anchors"].get("material_name")
            database = step.get("database") or next((c["source"]["database"] for c in valid_cells if c["group"] == group["identity"]), None)
            tables = re.findall(r"(?:FROM|JOIN)\s+[\"`\[]?(?:\w+\.)?(\w+)", step.get("final_sql", ""), re.I)
            for raw_field, value in row.items():
                if value is not None:
                    continue
                field = field_name(raw_field)
                table = next((t for t in tables if any((r.get("order_no") == entity or r.get("material_name") == entity) and field in r and r[field] is None for r in corpus["records"].get(database, {}).get(t, []))), None)
                if table:
                    valid_cells.append({"entity": entity, "field": field, "value": None, "unit": None, "source": {"database": database, "table": table}, "as_of": group["as_of"], "group": group["identity"]})
    policy_refs = [r for r in refs if r.get("policy_revision")]
    actual_policies, policy_headers = [], {}
    for index, ref in enumerate(policy_refs, 1):
        doc = next((d for d in corpus["policies"] if d["title"] == ref.get("title") and d["content"] == (ref.get("content") or ref.get("excerpt"))), None)
        good = doc is not None and doc["id"] in truth["policy_versions"]
        if good:
            for key, value in doc["policy"].items():
                actual = (ref.get("policy") or {}).get(key)
                good = good and (stamp(value) == stamp(actual) if key in {"effective_at", "expires_at"} and value else value == actual)
            scope = scenario.get("policy_scope", {})
            moment = stamp(ref.get("policy_as_of"))
            good = good and moment is not None and moment == stamp(scope.get("as_of"))
            good = good and all(doc["policy"][key] in {"", "*", scope.get(key)} for key in ("product", "region", "audience"))
            start, end = stamp(doc["policy"]["effective_at"]), stamp(doc["policy"].get("expires_at"))
            good = good and start is not None and moment >= start and (end is None or moment < end) and doc["policy"]["status"] == "published"
        if not good:
            errors.append("incorrect_policy_lineage_or_scope")
            continue
        actual_policies.append(doc["id"])
        p = doc["policy"]
        header = f"政策摘录 [P{index}]：{doc['title']}（版本 {p['version']}；产品 {p['product']}；区域 {p['region']}；受众 {p['audience']}；适用时点 {ref['policy_as_of']}）"
        policy_headers[header] = doc["content"].splitlines()
        if header not in visible or doc["content"] not in visible:
            errors.append("policy_not_visibly_cited")
    image_headers = {}
    for ref in refs:
        if ref.get("source") != "verified_image" or not ref.get("reply_reference"):
            continue
        field = ref.get("field")
        expected = truth.get("image_fields", {}).get(field, {})
        source = ref.get("business_source") or {}
        database, key = source.get("database"), field_name(source.get("column", ""))
        witnesses = [(table, row) for table, rows in corpus["records"].get(database, {}).items() for row in rows
                     if row.get("order_no") == source.get("order_ref") and row.get("order_no") in truth["allowed_entities"]
                     and key in row and equivalent(row[key], ref.get("db_value"))]
        when, observed_at = stamp(source.get("as_of")), stamp(source.get("observed_at"))
        if expected.get("outcome") != "matched" or not witnesses or not equivalent(ref.get("db_value"), expected.get("value")) or not ref.get("sha256") or not ref.get("attachment_id") or when is None or observed_at is None or not 0 <= observed_at - when <= 300 or source.get("as_of_source") != "query_time" or not (source.get("run_id") or source.get("trace_id")):
            errors.append("unproven_image_citation")
            continue
        if expected.get("unit") and ref.get("unit") != expected["unit"]:
            errors.append("incorrect_image_unit")
            continue
        labels = {"order_ref": "订单", "model": "型号", "quantity": "数量", "date": "日期", "status": "状态"}
        line = f"图片已核实 [{ref['reply_reference']}] · {labels.get(field, field)}: {ref['db_value']} {ref.get('unit') or ''} · {source['as_of']}"
        table = witnesses[0][0]
        item = {"entity": source["order_ref"], "field": key, "value": ref["db_value"], "unit": ref.get("unit"), "source": {"database": database, "table": table}, "as_of": source["as_of"], "group": None}
        valid_cells.append(item)
        image_headers[line] = item
    # Visible claims must agree with a proven cell, not merely contain a gold
    # substring. A second incorrect value or unsupported number fails too.
    current_group, current_policy = None, None
    for line in visible.splitlines():
        line = line.strip()
        if not line or line in allowed_lines:
            continue
        if line.startswith("- ") and ":" in line:
            name, value = line[2:].split(":", 1)
            key = field_name(name.strip())
            value = value.strip()
            candidates = [c for c in valid_cells if c["field"] == key and c["group"] == current_group and current_group is not None]
            matching = []
            for c in candidates:
                unit = (" " + c["unit"]) if c.get("unit") else ""
                if unit and not value.endswith(unit):
                    continue
                plain = value[:-len(unit)] if unit else value
                if (c["value"] is None and plain in {"未登记", "Not registered"}) or (c["value"] is not None and equivalent(plain, c["value"])):
                    matching.append(c)
            if not matching:
                errors.append("unsupported_visible_claim:" + name)
            for c in matching:
                covered.add((c["entity"], c["field"], c["source"]["database"], c["source"]["table"]))
        elif re.match(r".+ \[\d+\]$", line):
            match = re.fullmatch(r"(.+) \[(\d+)\]", line)
            number = int(match[2])
            group = source_groups[number - 1] if 0 < number <= len(source_groups) else None
            current_group, current_policy = (group["identity"] if group else None), None
            anchors = {}
            for item in match[1].split(" · "):
                label, _, value = item.partition(" ")
                anchors[field_name(label)] = value
            expected_anchors = {k: str(v) for k, v in (group or {}).get("anchors", {}).items() if k in {"order_no", "model"}}
            if (expected_anchors and anchors != expected_anchors) or (not expected_anchors and match[1] != "查询记录 " + str(number)):
                errors.append("wrong_entity_or_citation_header")
            for c in valid_cells:
                if c["group"] != current_group:
                    continue
                if c["field"] in {"model", "order_no", "material_name"} and str(c["value"]) in line:
                    covered.add((c["entity"], c["field"], c["source"]["database"], c["source"]["table"]))
        elif line.startswith("数据时点 "):
            if line.split(" ", 1)[1] not in {c["as_of"] for c in valid_cells if c["group"] == current_group}:
                errors.append("visible_data_time_mismatch")
        elif line.startswith("政策摘录 "):
            current_policy = policy_headers.get(line)
            current_group = None
            if current_policy is None:
                errors.append("unproven_policy_header")
        elif line.startswith("> "):
            if current_policy is None or line[2:] not in current_policy:
                errors.append("unproven_quoted_claim")
        elif line.startswith("图片已核实 "):
            item = image_headers.get(line)
            if item is None:
                errors.append("unproven_visible_image_claim")
            else:
                covered.add((item["entity"], item["field"], item["source"]["database"], item["source"]["table"]))
            current_group, current_policy = None, None
        else:
            errors.append("unproven_visible_prose:" + line[:100])
    for expected in truth["facts"]:
        key = (expected["entity"], expected["field"], expected["source"]["database"], expected["source"]["table"])
        if key not in covered or not any(all(c[k] == expected[k] for k in ("entity", "field", "source"))
                                         and equivalent(c["value"], expected["value"]) and c.get("unit") == expected.get("unit") for c in valid_cells):
            errors.append("required_fact_missing:" + expected["field"])
    if set(actual_policies) != set(truth["policy_versions"]):
        errors.append("required_policy_missing")
    for text in truth["required_text"]:
        if text not in visible:
            errors.append("required_text_missing")
    for text in truth["forbidden_text"]:
        if text in visible:
            errors.append("forbidden_policy_or_fact_visible")
    image_results = []
    reviews = [f for review in case.get("image_reviews", []) for f in review.get("result", {}).get("fields", [])]
    for field, expected in truth.get("image_fields", {}).items():
        actual = [r for r in reviews if r.get("field") == field]
        verified_ok = len(actual) == 1 and actual[0].get("outcome") in expected.get("acceptable_outcomes", [expected["outcome"]])
        extracted_ok = len(actual) == 1
        if expected["readability"] == "clear":
            extracted_ok = extracted_ok and actual[0].get("readability") == "clear" and equivalent(actual[0].get("value"), expected["value"])
        else:
            extracted_ok = extracted_ok and actual[0].get("readability") == expected["readability"] and actual[0].get("value") is None
        if expected.get("unit"):
            extracted_ok = extracted_ok and actual[0].get("unit") == expected["unit"]
        if expected.get("kind"):
            extracted_ok = extracted_ok and actual[0].get("kind") == expected["kind"]
        ok = extracted_ok and verified_ok
        image_results.append({"field": field, "expected_outcome": expected["outcome"], "extraction_passed": bool(extracted_ok),
                              "verification_passed": bool(verified_ok), "passed": bool(ok)})
        if not ok:
            errors.append("image_field:" + field)
        if expected["outcome"] != "matched" and case.get("facts", {}).get(field, {}).get("source") == "verified_image":
            invariants.append("unverified_image_promoted:" + field)
    if truth["source_required"] and not valid_cells and not policy_refs and not reviews:
        errors.append("required_sources_missing")
    if not truth["answerable"]:
        refusal = truth.get("refusal")
        successful = [step for query in business if not query.get("unavailable") for step in (query.get("evidence_payload") or query).get("steps", [])
                      if step.get("success") is True and step.get("truncated") is False and step.get("row_count") == len(step.get("rows", []))]
        relevant = [step for step in successful if any(entity in step.get("final_sql", "") for entity in truth["allowed_entities"])]
        if refusal == "no_record" and not any(step["row_count"] == 0 for step in relevant):
            errors.append("no_record_query_not_proven")
        elif refusal == "missing_field" and not any(row.get("order_no") in truth["allowed_entities"] and "phone" in row and row["phone"] is None for step in relevant for row in step["rows"]):
            errors.append("null_field_query_not_proven")
        if refusal == "no_record" and "未找到记录" not in visible:
            errors.append("no_record_not_distinguished")
        elif refusal == "missing_field" and not any(x in visible for x in ("手机号", "电话", "phone", "Phone")):
            errors.append("missing_field_not_distinguished")
        elif refusal == "no_applicable_policy" and "缺少适用的已发布政策" not in visible:
            errors.append("unpublished_policy_not_distinguished")
    return {"id": scenario["id"], "group": scenario["group"], "subtype": scenario["subtype"],
        "run_terminal": run_terminal, "business_resolved": case.get("status") == "resolved",
        "fact_correct": not errors and not invariants, "errors": sorted(set(errors)),
        "invariant_violations": sorted(set(invariants)), "image_fields": image_results,
        "elapsed_seconds": observed.get("elapsed_seconds"), "run_count": len(runs)}


def summarize(scenarios, results):
    by_id = {r["id"]: r for r in results}
    if len(by_id) != len(results):
        raise ValueError("Duplicate scenario result")
    missing = [s["id"] for s in scenarios if s["id"] not in by_id]
    if set(by_id) - {s["id"] for s in scenarios}:
        raise ValueError("Unexpected scenario result")
    groups = {}
    for group in {s["group"] for s in scenarios}:
        expected = [s for s in scenarios if s["group"] == group]
        correct = sum(by_id.get(s["id"], {}).get("fact_correct") is True for s in expected)
        groups[group] = {"correct": correct, "total": len(expected), "accuracy": correct / len(expected)}
    fields, extraction_fields = {}, {}
    for s in scenarios:
        actual = {(r["field"], r["expected_outcome"]): r["passed"] for r in by_id.get(s["id"], {}).get("image_fields", [])}
        for name, spec in s["truth"].get("image_fields", {}).items():
            extraction = next((r for r in by_id.get(s["id"], {}).get("image_fields", []) if r["field"] == name), {})
            for key in (name + "/all", name + "/" + spec["readability"]):
                row = extraction_fields.setdefault(key, {"correct": 0, "total": 0})
                row["total"] += 1
                row["correct"] += extraction.get("extraction_passed") is True
            for key in (name + "/all", name + "/" + spec["outcome"]):
                row = fields.setdefault(key, {"correct": 0, "total": 0})
                row["total"] += 1
                row["correct"] += actual.get((name, spec["outcome"]), False) is True
    total = len(scenarios)
    return {"complete": not missing, "missing": missing, "correct": sum(r["fact_correct"] for r in results),
            "total": total, "accuracy": sum(r["fact_correct"] for r in results) / total,
            "run_terminal": sum(r["run_terminal"] for r in results), "business_resolved": sum(r["business_resolved"] for r in results),
            "groups": groups, "image_fields": fields, "image_extraction": extraction_fields,
            "invariant_violations": sum(len(r["invariant_violations"]) for r in results),
            "error_counts": dict(Counter(e for r in results for e in r["errors"]))}
