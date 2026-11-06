"""Format-neutral comparison shared with the 5310da9 baseline.

This is deliberately conservative. Exact semantic fields with explicit units
and entities can be adjudicated without a model; unparsed wording is a pending
human review, never an automatic pass or an unsupported feature. The release
contract scorer remains separate from this common capability comparison.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

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
    "quantity": r"(?:补件数量|订购数量|数量|件数|quantity)",
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
    entities = set(re.findall(_ENTITY, visible))
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


_ORDER = r"SO-\d{4}-[A-Z]*\d+"
_PART = r"Hinge BX-\d+-\d+X?"
_ENTITY = rf"(?:{_ORDER}|{_PART})"
_NUMBER = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_DAY = r"\d{4}(?:-\d{1,2}-\d{1,2}|\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)"
_ZONE = r"(?:UTC(?:[+-]\d\d:\d\d)?|Z|[+-]\d\d:\d\d)"
_STAMP = rf"{_DAY}(?:[ T]+\d\d:\d\d(?::\d\d)?(?:\s*(?:{_ZONE}|\({_ZONE}\)|（{_ZONE}）))?)?"
_UNITS = {"USD": ("USD", "美元", "US dollars", "US dollar", "US$"), "pcs": ("pcs", "件", "个")}
# Recognizable alternative unit tokens allow a proved unit conflict to remain
# an error without using an otherwise unparsed number as evidence of falsity.
# Unknown abbreviations still require review; this is not a guessed unit map.
_OTHER_UNITS = ("CNY", "RMB", "EUR", "GBP", "JPY", "CAD", "AUD", "CHF", "HKD", "kg", "g", "cm", "mm", "m", "千克", "公斤", "米")
_TOTAL = rf"(?:本次)?补件合计\s*(?:为|是|[:：])?\s*(?P<number>{_NUMBER})\s*(?P<unit>[A-Za-z$]+|件|个)?"


def unit_key(text):
    return next((key for key, names in _UNITS.items() if text.casefold() in {n.casefold() for n in names}), text)


def unit_scalar(raw, expected_unit):
    tokens = {expected_unit, *_OTHER_UNITS, *(u for names in _UNITS.values() for u in names)}
    name = "(?:" + "|".join(re.escape(x) for x in sorted(tokens, key=len, reverse=True)) + ")"
    return re.fullmatch(rf"(?P<before>{name})?\s*(?P<number>{_NUMBER})\s*(?P<after>{name})?\.?", raw, re.I)


def unit_expressed(text, unit):
    # A unit discussion outside a parsed scalar may still bind it. We do not
    # infer the binding here, nor call the unit absent while it is unresolved.
    if re.search(r"单位|\bunits?\b", text, re.I):
        return True
    for name in _UNITS.get(unit, (unit,)):
        token = re.escape(name)
        pattern = (rf"(?<![A-Za-z]){token}(?![A-Za-z])" if re.search(r"[A-Za-z]", name) else
                   token)
        # The domain noun 补件 does not express the count unit 件 by itself.
        if re.search(pattern, text if re.search(r"[A-Za-z]", name) else text.replace("补件", ""), re.I):
            return True
    return False


def scoped_rows(text, scenario, corpus, database, table):
    """A single displayed, allowed parent is necessary for an implicit scope."""
    orders = set(re.findall(_ORDER, text))
    if len(orders) != 1 or not orders <= set(scenario["truth"]["allowed_entities"]):
        return []
    return [row for row in (corpus or {}).get("records", {}).get(database, {}).get(table, [])
            if row.get("order_no") in orders]


def list_text(text):
    # Only a separated marker at the beginning of a line is layout. The entire
    # item, including unknown labels and all following numbers, survives.
    return re.sub(r"(?m)^[ \t]*(?:\d+[.)]|\(\d+\)|（\d+）)[ \t]+(?=\S)", "", text)


def table_text(text, scenario, corpus):
    """Bind table cells without losing unknown rows, columns or units.

    Generic status headers require an independent identity join to a single
    source table. Neither the requested answer nor a matching scalar value is
    sufficient evidence for that join.
    """
    lines, result, index, errors, opaque = text.splitlines(), [], 0, [], []

    def cells(line):
        line = line.strip()
        return [x.strip() for x in line[1:-1].split("|")] if line.startswith("|") and line.endswith("|") else []

    def preserve_table(headers, raw):
        opaque.append(raw)
        errors.append("unparsed_table_header_context")
        header_claims = [header for header in headers if any(
            re.fullmatch(name + r"\s*[:：]\s*[^\n]+", header, re.I) for name in NAMES.values())]
        layout_words = {"项目", "字段", "item", "field", "查询结果", "结果", "值", "value", "result", "记录值"}
        if all(header in header_claims or header.casefold() in layout_words for header in headers):
            result.extend(header_claims)

    while index < len(lines):
        headers = cells(lines[index])
        separator = cells(lines[index + 1]) if index + 1 < len(lines) else []
        if headers and len(separator) == len(headers) and all(re.fullmatch(r":?-{3,}:?", x) for x in separator):
            end = index + 2
            rows = []
            while end < len(lines) and cells(lines[end]):
                rows.append(cells(lines[end]))
                end += 1
            if rows and all(len(row) == len(headers) for row in rows):
                layout_headers = (len(headers) == 2 and headers[0].casefold() in {"项目", "字段", "item", "field"}
                                  and headers[1].casefold() in {"查询结果", "结果", "值", "value", "result"})
                inferred_vertical = (len(headers) == 2
                                     and sum(any(re.search(name, row[0], re.I) for name in NAMES.values()) for row in rows) >= 2)
                if inferred_vertical and not layout_headers:
                    # Unknown headers may describe historical/negated examples,
                    # or contain additional assertions. Do not promote their
                    # body to current facts, or silently delete their context.
                    # A complete, independently bound assertion in a header is
                    # still checkable if its other headers are layout/closed
                    # claims. An unknown example/history title can scope even
                    # a closed scalar header, so that context stays opaque.
                    preserve_table(headers, "\n".join(lines[index:end]))
                elif layout_headers:
                    for name, value in rows:
                        if any(re.fullmatch(pattern, name, re.I) for pattern in NAMES.values()):
                            result.append(f"{name}: {value}")
                        else:
                            opaque.append(f"{name}: {value}")
                            errors.append("unparsed_table_row_context")
                else:
                    table_result, table_errors, unknown_header = [], [], False
                    for row in rows:
                        identities = []
                        for header, value in zip(headers, row):
                            if re.fullmatch(NAMES["part_name"] + r"|部件|配件", header, re.I):
                                identities.append(("part_name", value))
                            elif re.fullmatch(NAMES["order_no"], header, re.I):
                                identities.append(("order_no", value))
                        origins = []
                        for database, tables in (corpus or {}).get("records", {}).items():
                            for table in tables:
                                candidates = scoped_rows(text, scenario, corpus, database, table)
                                if identities:
                                    origins.extend((database, table) for r in candidates if all(r.get(k) == v for k, v in identities))
                        origin = origins[0] if len(origins) == 1 else None
                        for header, value in zip(headers, row):
                            unit_match = re.fullmatch(r"(.+?)[（(]([^()（）]+)[）)]", header)
                            header_unit = None
                            if unit_match and (unit_key(unit_match[2]) in _UNITS or re.fullmatch(r"[A-Z]{3}", unit_match[2])):
                                header, header_unit = unit_match[1].strip(), unit_match[2].strip()
                            fields = {field for field, name in NAMES.items() if re.fullmatch(name, header, re.I)}
                            if header == "名称":
                                # A generic identity heading is bound only by
                                # one exact independent source identifier. The
                                # cell's category/prose is preserved, not erased.
                                parts = set(re.findall(_PART, value))
                                known = {r.get("part_name") for r in (corpus or {}).get("records", {}).get("svc_db", {}).get("replacement_part", [])}
                                if len(parts) == 1 and parts <= known:
                                    fields = {"part_name"}
                                    header = sorted(labels(catalog()["svc_db.replacement_part.part_name"]))[0]
                            if origin:
                                columns = {key.rsplit(".", 1)[1] for key in catalog() if key.startswith(".".join(origin) + ".")}
                                if header == "当前状态":
                                    fields = {field for field in columns if field.endswith("_status")}
                                elif header == "申请状态":
                                    fields = {"part_status"} & columns
                                elif header in {"部件", "配件"} and "part_name" in columns:
                                    fields = {"part_name"}
                                if len(fields) == 1:
                                    field = next(iter(fields))
                                    entry = catalog().get(".".join((*origin, field)))
                                    if entry:
                                        header = sorted(labels(entry))[0]
                            if len(fields) != 1:
                                unknown_header = True
                            if header_unit:
                                if re.fullmatch(_NUMBER, value):
                                    value += " " + header_unit
                                else:
                                    scalar = unit_scalar(value, unit_key(header_unit))
                                    if scalar and any(unit_key(scalar[key]).casefold() != unit_key(header_unit).casefold()
                                                      for key in ("before", "after") if scalar[key]):
                                        table_errors.append("incorrect_claim:table_unit")
                                    aliases = _UNITS.get(unit_key(header_unit), (header_unit,))
                                    unit_pattern = "(?:" + "|".join(re.escape(u) for u in aliases) + ")"
                                    explicit = re.fullmatch(rf"(?P<before>{unit_pattern})?\s*{_NUMBER}\s*(?P<after>{unit_pattern})?", value, re.I)
                                    if not explicit or not (explicit["before"] or explicit["after"]):
                                        value += " " + header_unit
                            # A unit can be shared only with the one displayed
                            # source row and an explicit, equal, scoped total.
                            if (origin == ("svc_db", "replacement_part") and fields == {"quantity"}
                                    and len(rows) == 1 and re.fullmatch(_NUMBER, value)
                                    and len(scoped_rows(text, scenario, corpus, *origin)) == 1):
                                totals = list(re.finditer(_TOTAL + r"(?=[。；;\n]|$)", text))
                                if (len(totals) == 1 and totals[0]["unit"]
                                        and Decimal(value.replace(",", "")) == Decimal(totals[0]["number"].replace(",", ""))):
                                    value += " " + totals[0]["unit"]
                            table_result.append(f"{header}: {value}")
                    if unknown_header:
                        preserve_table(headers, "\n".join(lines[index:end]))
                    else:
                        result.extend(table_result)
                        errors.extend(table_errors)
                index = end
                continue
        result.append(lines[index])
        index += 1
    return "\n".join(result), errors, opaque


def valid_timestamp(value):
    try:
        day = re.match(_DAY, value)[0]
        date(*map(int, re.findall(r"\d+", day)))
        rest = value[len(day):]
        clock = re.search(r"(\d\d):(\d\d)(?::(\d\d))?", rest)
        zone = re.search(r"[+-](\d\d):(\d\d)", rest)
        return (not clock or (int(clock[1]) < 24 and int(clock[2]) < 60 and int(clock[3] or 0) < 60)) and (
            not zone or (int(zone[1]) <= 14 and int(zone[2]) < 60 and (int(zone[1]) < 14 or int(zone[2]) == 0)))
    except ValueError:
        return False


def separate_query_times(text):
    """Recognize bounded timestamp syntax, never claim execution provenance."""
    errors = []

    def replace(match):
        errors.append("unparsed_query_time_provenance" if valid_timestamp(match["stamp"]) else "incorrect_query_time")
        return " "

    boundary = r"(?=\s*(?:[，,。.;；\n)）]|$))"
    labeled = rf"(?:数据|本条)?查询(?:时间|时点|日期)\s*(?:为\s*[:：]?|[:：])\s*(?P<stamp>{_STAMP}){boundary}"
    text = re.sub(labeled, replace, text)
    prose = rf"(?:本条)?(?:结果|资料)\s*(?:来自)?(?:于|截至)\s*(?P<stamp>{_STAMP})\s*的?查询(?:取得)?{boundary}"
    return re.sub(prose, replace, text), errors


def annotation_text(text):
    """Separate balanced notes, preserving every character of their contents."""
    result = []
    for line in text.splitlines():
        # Notes between a label and its value must not sever the label binding.
        match = re.fullmatch(r"(.*?)[（(]([^()（）\n]*)[）)](.*)", line)
        if match:
            before = re.sub(r"\s*(?:为|是|[:：])\s*$", "", match[1]).strip()
            if any(re.fullmatch(name, before, re.I) for name in NAMES.values()) and match[3].strip():
                line = match[1] + match[3] + "\n" + match[2]
        # Split innermost notes first so nested notes also retain their text.
        while re.search(r"[（(][^()（）]*[）)]", line):
            line = re.sub(r"[（(]([^()（）]*)[）)]", r"\n\1\n", line)
        result.append(line)
    return "\n".join(result)


def related_parts(visible, scenario, corpus):
    """Display labels do not establish relationships; independent rows do."""
    rows = scoped_rows(visible, scenario, corpus, "svc_db", "replacement_part")
    errors = []
    orders = set(re.findall(_ORDER, visible))
    if len(orders) > 1 and orders <= set(scenario["truth"]["allowed_entities"]):
        rows = [r for r in (corpus or {}).get("records", {}).get("svc_db", {}).get("replacement_part", [])
                if r.get("order_no") in orders]
        # Existence in the union does not prove each displayed association.
        # A multi-parent answer needs explicit row/sentence binding review.
        if any(r.get("part_name") and r["part_name"] in visible for r in rows):
            errors.append("unparsed_association_scope")
    related = {r["part_name"] for r in rows if isinstance(r.get("part_name"), str) and r["part_name"] in visible}
    name = NAMES["part_name"] + r"|关联配件|配件|部件"
    for part in related:
        visible = re.sub(rf"(?mi)^\s*(?:{name})\s*(?:[:：]\s*|\s+)({re.escape(part)})\s*$", r"物料 \1", visible)
    return visible, related, errors


_DISCUSSION = re.compile(r"尚未|尚无|未核|待核|待确认|核实|未知|不详|查询|记录|未查|未提供|未说明")


def separate_aggregates(text, scenario, corpus):
    """Counts and totals lack execution scope in this three-argument API.

    Separating recognized syntax always adds a provenance review, with the
    complete original text retained by the caller. A corpus length or sum is
    not evidence of the query result, even if its surface numbers coincide.
    """
    errors = []

    def total(match):
        errors.append("unparsed_aggregate_provenance")
        return " "

    def count(match):
        # The supplied corpus is not an execution event or an assertion that
        # the displayed query returned every source row. Its length/sum cannot
        # prove a count/total contradiction. Preserve the claim for review.
        errors.append("unparsed_aggregate_provenance")
        if match["total"]:
            total(match)
        return " "

    boundary = r"(?=\s*(?:[。；;\n]|$))"
    count_pattern = (rf"查得\s*(?P<count>\d+)\s*条补件记录"
                     rf"(?P<total>\s*[，,]\s*合计\s*(?P<number>{_NUMBER})\s*(?P<unit>[A-Za-z$]+|件|个)?)?{boundary}")
    text = re.sub(count_pattern, count, text)
    return re.sub(_TOTAL + boundary, total, text), errors


def separate_date_references(text, scenario, corpus):
    """Partial dates require a complete antecedent with the same field role.

    Preserve uncertainty about prose scope and execution. Do not erase a
    contradictory restatement or borrow the year from an unrelated date.
    """
    errors = []

    def replace(match):
        field = "delivered_date" if match["role"].startswith("实际") else "delivery_date"
        antecedents = re.findall(NAMES[field] + rf"\s*[:：]\s*({_DAY})", text, re.I)
        expected = [f["value"] for f in scenario["truth"]["facts"] if f["field"] == field]
        for database, tables in (corpus or {}).get("records", {}).items():
            for table in tables:
                expected.extend(row[field] for row in scoped_rows(text, scenario, corpus, database, table) if field in row)
        values = {str(value) for value in expected}
        bound = []
        for day in antecedents:
            try:
                bound.append(date(*map(int, re.findall(r"\d+", day))))
            except ValueError:
                errors.append("incorrect_claim:" + field)
        if bound and len(values) == 1 and all(d.isoformat() in values for d in bound):
            if any((int(match["month"]), int(match["day"])) != (d.month, d.day) for d in bound):
                errors.append("incorrect_claim:" + field)
        errors.append("unparsed_date_reference")
        return " "

    partial = r"(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日"
    role = r"(?P<role>计划(?:日期)?|约定交付日期|实际送达(?:日期)?)"
    boundary = r"(?=\s*(?:[，,。；;\n]|$))"
    patterns = (rf"{partial}\s*是(?:上述)?{role}{boundary}", rf"{role}\s*(?:为|是|[:：])\s*{partial}{boundary}")
    for pattern in patterns:
        text = re.sub(pattern, replace, text)
    return text, errors


def compare_value(raw, fact, *, explicit=False):
    """True/False for an explicit scalar; None for unparsed discussion.

    Units stay explicit. Dates use a year-first grammar and calendar validation.
    Failure to parse prose is not itself evidence of a wrong scalar value.
    """
    for left, right in (("“", "”"), ("‘", "’"), ('"', '"'), ("'", "'")):
        if raw.startswith(left) and raw.endswith(right) and len(raw) >= 2 and left not in raw[1:-1] and right not in raw[1:-1]:
            raw = raw[1:-1].strip()
            break
    if raw in {"未登记", "未记录", "Not registered", "Not recorded"}:
        # A closed NULL statement is different from a guessed missing number.
        # The caller must bind the field to its independent source witness.
        return (True if fact.get("source") else None) if fact["value"] is None else False
    unit = fact.get("unit")
    if unit:
        match = unit_scalar(raw, unit)
        if match:
            if not equivalent(match["number"], fact["value"]):
                return False
            explicit_units = [unit_key(match[key]) for key in ("before", "after") if match[key]]
            if not explicit_units:
                return None  # The caller checks whole-answer unit completeness.
            return all(u.casefold() == unit.casefold() for u in explicit_units)
        return None
    field = fact["field"]
    if field.endswith("_date") or re.fullmatch(r"\d{4}-\d\d-\d\d", str(fact["value"])):
        match = re.fullmatch(r"(\d{4})(?:-(\d{1,2})-(\d{1,2})|\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日)\.?", raw)
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


def claim_context_known(text, start):
    """A field mention inside unknown prose is not a closed assertion.

    This deliberately does not guess polarity, history or example semantics.
    Only a clause beginning with layout/an explicit entity is handled here;
    unfamiliar prefixes remain reviewable with their full original context.
    """
    prefix = re.split(r"[，。；;\n]|(?<!\d),(?!\d)", text[:start])[-1].strip()
    return prefix in {"", "-", "•"} or bool(re.fullmatch(rf"(?:订单|Order)\s*[:：]?\s*{_ENTITY}", prefix, re.I))


def common_score(scenario, visible, corpus=None):
    original_text = visible
    facts = scenario["truth"]["facts"]
    if not facts or scenario["group"] == "policies":
        return {"status": "review_required", "fact_correct": None,
                "reason": "Policy meaning, safe abstention and procedures need independent attributed adjudication", "contract_checked": False}
    visible = list_text(presentation_text(re.sub(r"\\([\\`*_{}\[\]<>!|~])", r"\1", visible)))
    visible, table_errors, opaque_tables = table_text(visible, scenario, corpus)
    visible = re.sub(rf"订单\s+({_ORDER})\s*(?:的\s*)?当前状态(?=\s*(?:为|是|[:：]))",
                     r"订单 \1\n订单状态", visible)
    visible, errors = separate_meanings(visible, scenario, corpus)
    errors.extend(table_errors)
    visible, time_errors = separate_query_times(visible)
    errors.extend(time_errors)
    visible = annotation_text(visible)
    visible, reference_errors = separate_date_references(visible, scenario, corpus)
    errors.extend(reference_errors)
    visible, aggregate_errors = separate_aggregates(visible, scenario, corpus)
    errors.extend(aggregate_errors)
    claims = []
    # A structured display label is not execution evidence in this text-only
    # API either. Its provenance remains pending identically for both versions.
    def asof(match):
        errors.append("unparsed_query_time_provenance" if valid_timestamp(match["stamp"]) else "incorrect_query_time")
        return " "

    visible = re.sub(r"(?m)^数据时点 (?P<stamp>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d))\s*$", asof, visible)
    visible = visible.replace("以上为可核对的资料。涉及补件、责任、金额或交期承诺，请由员工按适用政策审核后处理。", "")
    allowed_entities = set(scenario["truth"]["allowed_entities"])
    visible, related, association_errors = related_parts(visible, scenario, corpus)
    errors.extend(association_errors)
    allowed_entities |= related
    entities = set(re.findall(_ENTITY, visible))
    for entity in entities - allowed_entities:
        if re.fullmatch(_ORDER, entity):
            errors.append("wrong_entity")
            continue
        parents = {row.get("order_no") for row in (corpus or {}).get("records", {}).get("svc_db", {}).get("replacement_part", [])
                   if row.get("part_name") == entity}
        active_orders = set(re.findall(_ORDER, visible)) & allowed_entities
        if parents and None not in parents and parents.isdisjoint(active_orders):
            errors.append("wrong_entity")
        else:
            # No row is not the same as a complete source disproving a join.
            errors.append("unparsed_entity_relation")
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
            if not claim_context_known(claim_text, match.start()):
                errors.append("unparsed_claim_context:" + field)
                continue
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
                continue
            fact = witnesses[0]
            raw = re.sub(r"\s*\[(?:P|I)?\d+\]\s*$", "", match["value"]).strip()
            ok = compare_value(raw, fact, explicit=match["separator"] is not None)
            if ok is None and fact.get("unit") and re.fullmatch(_NUMBER + r"\.?", raw):
                # A bare scalar may share a unit with a table header or prose
                # outside this parser's coverage. Missing binding is pending;
                # an answer with no expressed unit anywhere is incomplete.
                if unit_expressed(original_text, fact["unit"]):
                    errors.append("unparsed_unit_binding:" + field)
                else:
                    ok = False
                    errors.append("required_unit_explicitly_missing:" + field)
            if ok is None:
                errors.append("unparsed_claim:" + field)
                # Keep all unknown text for the extra-number/prose checks.
                continue
            claims.append({"entity": fact["entity"], "field": field, "source": fact.get("source"), "text": match[0], "correct": ok})
            if not ok:
                errors.append("incorrect_claim:" + field)
            remaining = remaining.replace(match[0], " ", 1)
    answered = {c["field"] for c in claims if c["correct"]}
    def identity(fact):
        source = fact.get("source") or {}
        return fact["entity"], fact["field"], source.get("database"), source.get("table")

    proved = {identity(claim) for claim in claims if claim["correct"]}
    for fact in facts:
        if identity(fact) not in proved:
            error = "unparsed_required_fact:" + fact["field"]
            if error not in errors:
                errors.append(error)
    refusals = re.findall(r"(?:目前|暂时|当前)?(?:无法|不能)(?:查询|提供|核实)([^。；;\n]+)", visible)
    for field in required_fields - answered:
        if any(re.search(NAMES.get(field, r"(?!)"), refusal, re.I) for refusal in refusals):
            errors.append("required_fact_explicitly_unanswered:" + field)
    remaining = re.sub(rf"{_ENTITY}|\[(?:P|I)?\d+\]", "", remaining)
    # An unexplained number is a coverage gap, not a proved contradiction. It
    # must block automatic success even if it is the only remaining content.
    if re.search(r"\d", remaining):
        errors.append("unbound_numeric_claim")
    # Unrecognized assertions remain reviewable rather than being guessed true
    # or marked unsupported merely because a different renderer wrote them.
    unresolved_text = "\n".join([*opaque_tables, remaining])
    remaining = re.sub(r"订单|物料|材料|资料供审核|\bOrder\b|\bMaterial\b|[\s，。；、:：;,.!?！？·-]", "", remaining, flags=re.I)
    if remaining and not re.fullmatch(r"\d+", remaining):
        errors.append("unparsed_additional_prose")
    definite = any(e.startswith(("incorrect_claim:", "required_fact_explicitly_unanswered:"))
                   or e in {"wrong_entity", "incorrect_field_meaning", "incorrect_query_time"} for e in errors)
    pending = any(e.startswith("unparsed") or e == "unbound_numeric_claim" for e in errors)
    status = "incorrect" if definite else "review_required" if pending else "correct"
    result = {"status": status, "fact_correct": False if definite else None if pending else True,
              "claims": claims, "errors": errors, "contract_checked": False}
    if pending:
        result["review_context"] = {"original_text": original_text, "unresolved_text": unresolved_text}
    return result


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
