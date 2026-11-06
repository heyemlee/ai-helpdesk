"""Format-neutral comparison shared with the 5310da9 baseline.

This is deliberately conservative. Exact semantic fields with explicit units
and entities can be adjudicated without a model; unparsed wording is a pending
human review, never an automatic pass or an unsupported feature. The release
contract scorer remains separate from this common capability comparison.
"""
from __future__ import annotations

import re
from datetime import date

from acceptance.oracle import equivalent
from acceptance.source_semantics import catalog, labels, meaning_lines

NAMES = {
    "contract_amount": r"(?:合同(?:总)?金额|合同价(?:款)?|contract\s+(?:total|amount))",
    "order_status": r"(?:订单(?:当前)?状态|order\s+status)",
    "production_status": r"(?:生产状态|production\s+status)",
    "current_stage": r"(?:当前工序|生产工序|current\s+stage)",
    "delivery_status": r"(?:配送状态|delivery\s+status)",
    "delivery_date": r"(?:计划交付日期|约定交付日期|交付日期|配送日期|delivery\s+date)",
    "part_status": r"(?:补件状态|part\s+status)",
    "quantity": r"(?:补件数量|订购数量|数量|quantity)",
    "on_hand_qty": r"(?:现有库存|现存数量|on.hand\s+quantity)",
    "allocated_qty": r"(?:已占用库存|占用数量|allocated\s+quantity)",
    "model": r"(?:柜体型号|型号|model)",
}

# A source-defined label has the same meaning in either version's text. This
# catalog is independent of the new product's metadata producer and renderer.
for _origin, _entry in catalog().items():
    _field = _origin.rsplit(".", 1)[1]
    _names = "|".join(re.escape(label) for label in sorted(labels(_entry), key=len, reverse=True))
    if _names:
        NAMES[_field] = "(?:" + NAMES.get(_field, r"(?!)") + "|" + _names + ")"


def separate_meanings(visible, scenario, corpus):
    """Known definitions are not row values; text alone cannot prove lineage.

    Only a complete catalog definition adjacent to its matching business label
    is separated, and it explicitly requires review. Unknown or appended text
    remains in the numeric/prose checks. No version-specific exception exists.
    """
    entries, result, errors = [], [], []
    entities = set(re.findall(r"SO-2026-[A-Z]?\d+|Hinge BX-\d+-24X?", visible))
    for raw in visible.splitlines():
        line = raw.strip()
        if not line:
            result.append(raw)
            continue
        if line.startswith(("字段含义:", "Field meaning:")):
            if any(line in meaning_lines(entry) for entry in entries):
                errors.append("unparsed_field_meaning_provenance")
            elif any(line in meaning_lines(entry) for entry in catalog().values()):
                errors.append("incorrect_field_meaning")
            else:
                # Do not let field-name words inside an unknown definition get
                # parsed as a new row value. Its full text still remains below.
                result.append(raw)
                errors.append("unparsed_field_meaning")
            entries = []
            continue
        entries = []
        match = re.fullmatch(r"(?:-\s*)?([^:：]+)[:：]\s*(.+)", line)
        if match:
            name = match[1].strip()
            for origin, entry in catalog().items():
                database, table, field = origin.split(".")
                if not re.fullmatch(NAMES[field], name, re.I):
                    continue
                expected = any(f["field"] == field and f.get("source") == {"database": database, "table": table}
                               for f in scenario["truth"]["facts"])
                optional = corpus and any((row.get("order_no") in entities or row.get("material_name") in entities)
                                          and field in row for row in corpus.get("records", {}).get(database, {}).get(table, []))
                if expected or optional:
                    entries.append(entry)
        result.append(raw)
    return "\n".join(result), errors


def presentation_text(text):
    """Remove only paired display delimiters, preserving their entire content."""
    for pattern in (r"(?<!\*)\*\*([^*\n]+)\*\*(?!\*)", r"(?<!_)__([^_\n]+)__(?!_)",
                    r"(?<!`)`([^`\n]+)`(?!`)", r"(?<!\*)\*([^*\n]+)\*(?!\*)"):
        text = re.sub(pattern, r"\1", text)
    return text


def table_text(text):
    """Bind complete Markdown table columns; never discard an extra cell."""
    lines, result, index = text.splitlines(), [], 0

    def cells(line):
        line = line.strip()
        return [x.strip() for x in line[1:-1].split("|")] if line.startswith("|") and line.endswith("|") else []

    while index < len(lines):
        headers = cells(lines[index])
        separator = cells(lines[index + 1]) if index + 1 < len(lines) else []
        known = headers and all(any(re.fullmatch(name, header, re.I) for name in NAMES.values()) for header in headers)
        if known and len(separator) == len(headers) and all(re.fullmatch(r":?-{3,}:?", x) for x in separator):
            end = index + 2
            rows = []
            while end < len(lines) and cells(lines[end]):
                rows.append(cells(lines[end]))
                end += 1
            if rows and all(len(row) == len(headers) for row in rows):
                result.extend(f"{name}: {value}" for row in rows for name, value in zip(headers, row))
                index = end
                continue
        result.append(lines[index])
        index += 1
    return "\n".join(result)


def query_time_line(line):
    match = re.fullmatch(r"查询时间[：:]\s*(\d{4}-\d{2}-\d{2}|\d{4}年\d{1,2}月\d{1,2}日)"
                         r"(?:\s+(\d\d):(\d\d)(?::(\d\d))?\s+UTC)?[。.]?", line.strip())
    if not match:
        return False
    try:
        date(*map(int, re.findall(r"\d+", match[1])))
        return not match[2] or (int(match[2]) < 24 and int(match[3]) < 60 and int(match[4] or 0) < 60)
    except ValueError:
        return False


def related_parts(visible, allowed_entities, corpus):
    """A labeled part is related only when an independent source row joins it."""
    related = set()
    if not corpus:
        return visible, related
    rows = corpus.get("records", {}).get("svc_db", {}).get("replacement_part", [])
    lines = []
    for line in visible.splitlines():
        match = re.fullmatch(r"\s*关联配件[：:\s]+(Hinge BX-\d+-24X?)\s*", line)
        if match and any(row.get("order_no") in allowed_entities and row.get("order_no") in visible
                         and row.get("part_name") == match[1] for row in rows):
            related.add(match[1])
            line = "物料 " + match[1]
        lines.append(line)
    return "\n".join(lines), related


_NUMBER = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_DISCUSSION = re.compile(r"尚未|尚无|未核|待核|待确认|核实|未知|不详|查询|记录|未查|未提供|未说明")


def compare_value(raw, fact, *, explicit=False):
    """True/False for an explicit scalar; None for unparsed discussion.

    Units stay explicit. Dates use a year-first grammar and calendar validation.
    Failure to parse prose is not itself evidence of a wrong scalar value.
    """
    for left, right in (("“", "”"), ("‘", "’"), ('"', '"'), ("'", "'")):
        if raw.startswith(left) and raw.endswith(right) and len(raw) >= 2 and left not in raw[1:-1] and right not in raw[1:-1]:
            raw = raw[1:-1].strip()
            break
    if raw in {"未登记", "Not registered"}:
        # A closed NULL statement is different from a guessed missing number.
        # The caller must bind the field to its independent source witness.
        return (True if fact.get("source") else None) if fact["value"] is None else False
    unit = fact.get("unit")
    if unit:
        aliases = {"USD": ("USD", "美元", "US dollars", "US dollar", "US$"),
                   "pcs": ("pcs", "件", "个")}.get(unit, (unit,))
        name = "(?:" + "|".join(re.escape(x) for x in aliases) + ")"
        match = re.fullmatch(rf"(?P<before>{name})?\s*(?P<number>{_NUMBER})\s*(?P<after>{name})?\.?", raw, re.I)
        if match:
            return bool(match["before"] or match["after"]) and equivalent(match["number"], fact["value"])
        return None
    field = fact["field"]
    if field.endswith("_date") or re.fullmatch(r"\d{4}-\d\d-\d\d", str(fact["value"])):
        match = re.fullmatch(r"(\d{4})(?:-(\d{1,2})-(\d{1,2})|年\s*(\d{1,2})月\s*(\d{1,2})日)\.?", raw)
        if not match:
            return None
        try:
            normalized = date(int(match[1]), int(match[2] or match[4]), int(match[3] or match[5])).isoformat()
        except ValueError:
            return False
        return equivalent(normalized, fact["value"])
    if isinstance(fact["value"], (int, float)) and not isinstance(fact["value"], bool):
        return equivalent(raw, fact["value"]) if re.fullmatch(_NUMBER, raw) else None
    if equivalent(raw, fact["value"]):
        return True
    if _DISCUSSION.search(raw):
        return None
    known = set()
    for origin, entry in catalog().items():
        if origin.rsplit(".", 1)[1] == field:
            match = re.search(r"；选项：([^。]+)。", entry["metadata"]["description"])
            if match:
                known.update(match[1].split("、"))
    if raw in known or (explicit and re.fullmatch(r"[\w\u4e00-\u9fff-]+", raw)):
        return False
    return None


def common_score(scenario, visible, corpus=None):
    facts = scenario["truth"]["facts"]
    if not facts or scenario["group"] == "policies":
        return {"status": "review_required", "fact_correct": None,
                "reason": "Policy meaning, safe abstention and procedures need independent attributed adjudication", "contract_checked": False}
    visible = table_text(presentation_text(re.sub(r"\\([\\`*_{}\[\]<>!|~])", r"\1", visible)))
    visible = re.sub(r"(?m)^订单\s+(SO-2026-[A-Z]?\d+)\s+当前状态(?=\s*(?:为|是|[:：]))",
                     r"订单 \1\n订单状态", visible)
    visible, errors = separate_meanings(visible, scenario, corpus)
    lines = []
    for line in visible.splitlines():
        if query_time_line(line):
            # Text-only comparison cannot verify query-time provenance. This
            # complete metadata line explicitly stays pending, never a pass.
            errors.append("unparsed_query_time_provenance")
        else:
            lines.append(line)
    visible = "\n".join(lines)
    claims = []
    # Metadata is checked by the separate release contract scorer. Apply this
    # identical normalization to either product's text for common facts.
    visible = re.sub(r"(?m)^数据时点 \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)\s*$", "", visible)
    visible = visible.replace("以上为可核对的资料。涉及补件、责任、金额或交期承诺，请由员工按适用政策审核后处理。", "")
    allowed_entities = set(scenario["truth"]["allowed_entities"])
    visible, related = related_parts(visible, allowed_entities, corpus)
    allowed_entities |= related
    entities = set(re.findall(r"SO-2026-[A-Z]?\d+|Hinge BX-\d+-24X?", visible))
    if entities - allowed_entities:
        errors.append("wrong_entity")
    if not {f["entity"] for f in facts} <= entities:
        errors.append("unparsed_entity")
    remaining = visible
    required_fields = {f["field"] for f in facts}
    claim_text = "\n".join("" if line.strip().startswith(("字段含义:", "Field meaning:")) else line
                           for line in visible.splitlines())
    found = []
    for field, name in NAMES.items():
        pattern = re.compile(name + r"\s*(?P<separator>(?:仍为|为|是|=|is\b)\s*[:：]?|[:：])?\s*(?P<value>[^，。；;\n]+)", re.I)
        found.extend((field, match) for match in pattern.finditer(claim_text))
    # Prefer the complete label over a contained word such as "quantity" in
    # "On-hand quantity". Never count the same numeric phrase as two fields.
    selected = []
    for field, match in sorted(found, key=lambda pair: pair[1].start("value") - pair[1].start(), reverse=True):
        if not any(match.start() < other.start("value") and other.start() < match.start("value") for _, other in selected):
            selected.append((field, match))
    for field, name in NAMES.items():
        # Both natural sentences and line-oriented records are accepted.
        matches = [match for key, match in selected if key == field]
        if not matches:
            if field in required_fields:
                errors.append("unparsed_required_fact:" + field)
            continue
        for match in matches:
            requested = [f for f in facts if f["field"] == field]
            witnesses = requested[:]
            if not requested and corpus:
                material = field in {"on_hand_qty", "allocated_qty"}
                from acceptance.oracle import UNITS
                for database, tables in corpus.get("records", {}).items():
                    for table, rows in tables.items():
                        if field == "quantity" and (("订购数量" in match[0] and table != "order_item") or ("补件数量" in match[0] and table != "replacement_part")):
                            continue
                        for row in rows:
                            entity = row.get("material_name") if material else row.get("order_no")
                            if entity in entities and field in row:
                                witnesses.append({"entity": entity, "field": field, "value": row[field], "unit": UNITS.get(field), "source": {"database": database, "table": table}})
            end = match.start("separator") if match["separator"] is not None else match.start("value")
            name = claim_text[match.start():end].strip()
            origins = {tuple(origin.split(".")[:2]) for origin, entry in catalog().items()
                       if origin.rsplit(".", 1)[1] == field and any(name.casefold() == label.casefold() for label in labels(entry))}
            if origins:
                witnesses = [f for f in witnesses if not f.get("source")
                             or (f["source"].get("database"), f["source"].get("table")) in origins]
            values = {(str(f["value"]), f.get("unit")) for f in witnesses}
            if len(values) != 1:
                errors.append("unparsed_or_ambiguous_claim:" + field)
                remaining = remaining.replace(match[0], " ", 1)
                continue
            fact = witnesses[0]
            raw = re.sub(r"\s*\[(?:P|I)?\d+\]\s*$", "", match["value"]).strip()
            ok = compare_value(raw, fact, explicit=match["separator"] is not None)
            if ok is None:
                errors.append("unparsed_claim:" + field)
                # Keep all unknown text for the extra-number/prose checks.
                continue
            claims.append({"entity": fact["entity"], "field": field, "text": match[0], "correct": ok})
            if not ok:
                errors.append("incorrect_claim:" + field)
            remaining = remaining.replace(match[0], " ", 1)
    answered = {c["field"] for c in claims if c["correct"]}
    refusals = re.findall(r"(?:目前|暂时|当前)?(?:无法|不能)(?:查询|提供|核实)([^。；;\n]+)", visible)
    for field in required_fields - answered:
        if any(re.search(NAMES.get(field, r"(?!)"), refusal, re.I) for refusal in refusals):
            errors.append("required_fact_explicitly_unanswered:" + field)
    remaining = re.sub(r"SO-2026-[A-Z]?\d+|Hinge BX-\d+-24X?|BX-\d+-24X?|\[(?:P|I)?\d+\]", "", remaining)
    # Extra unbound numbers cannot hide behind a correct primary answer.
    if re.search(r"\d", remaining):
        errors.append("unbound_numeric_claim")
    # Unrecognized assertions remain reviewable rather than being guessed true
    # or marked unsupported merely because a different renderer wrote them.
    remaining = re.sub(r"订单|物料|材料|资料供审核|\bOrder\b|\bMaterial\b|[\s，。；、:：;,.!?！？·-]", "", remaining, flags=re.I)
    if remaining and not re.fullmatch(r"\d+", remaining):
        errors.append("unparsed_additional_prose")
    definite = any(e.startswith(("incorrect_claim:", "required_fact_explicitly_unanswered:"))
                   or e in {"wrong_entity", "unbound_numeric_claim", "incorrect_field_meaning"} for e in errors)
    pending = any(e.startswith("unparsed") for e in errors)
    status = "incorrect" if definite else "review_required" if pending else "correct"
    return {"status": status, "fact_correct": False if definite else None if pending else True,
            "claims": claims, "errors": errors, "contract_checked": False}


def adjudicate(review, evidence_sha256):
    """Only an explicit external review record can resolve pending wording.

    Tooling never authors a passing review on the user's behalf. Store the
    reviewers' rationale and independent field/scope judgments with evidence.
    """
    if review.get("evidence_sha256") != evidence_sha256 or review.get("status") != "reviewed":
        raise ValueError("Independent review does not bind this evidence")
    required_text = ["reviewed_by", "rationale"] + (["reviewer_model"] if review.get("reviewer_type") == "ai" else [])
    if (any(not isinstance(review.get(k), str) or not review[k].strip() for k in required_text) or type(review.get("fact_correct")) is not bool
            or review.get("reviewer_type") not in {"human", "ai"} or review.get("independent_of_system_under_test") is not True
            ):
        raise ValueError("An attributed review and explicit rationale are required")
    return review["fact_correct"]
