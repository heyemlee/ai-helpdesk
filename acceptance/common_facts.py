"""Format-neutral comparison shared with the 5310da9 baseline.

This is deliberately conservative. Exact semantic fields with explicit units
and entities can be adjudicated without a model; unparsed wording is a pending
human review, never an automatic pass or an unsupported feature. The release
contract scorer remains separate from this common capability comparison.
"""
from __future__ import annotations

import re

from acceptance.oracle import equivalent

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


def common_score(scenario, visible, corpus=None):
    facts = scenario["truth"]["facts"]
    if not facts or scenario["group"] == "policies":
        return {"status": "review_required", "fact_correct": None,
                "reason": "Policy meaning, safe abstention and procedures need independent human adjudication", "contract_checked": False}
    errors, claims = [], []
    # Metadata is checked by the separate release contract scorer. Apply this
    # identical normalization to either product's text for common facts.
    visible = re.sub(r"(?m)^数据时点 \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)\s*$", "", visible)
    visible = visible.replace("以上为可核对的资料。涉及补件、责任、金额或交期承诺，请由员工按适用政策审核后处理。", "")
    entities = set(re.findall(r"SO-2026-[A-Z]?\d+|Hinge BX-\d+-24X?", visible))
    allowed_entities = set(scenario["truth"]["allowed_entities"])
    if entities - allowed_entities:
        errors.append("wrong_entity")
    if not {f["entity"] for f in facts} <= entities:
        errors.append("unparsed_entity")
    remaining = visible
    required_fields = {f["field"] for f in facts}
    for field, name in NAMES.items():
        # Both natural sentences and line-oriented records are accepted.
        pattern = re.compile(name + r"\s*(?::|：|为|是|=|is)?\s*([^，。；;\n]+)", re.I)
        matches = list(pattern.finditer(visible))
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
                for database, tables in corpus["records"].items():
                    for table, rows in tables.items():
                        if field == "quantity" and (("订购数量" in match[0] and table != "order_item") or ("补件数量" in match[0] and table != "replacement_part")):
                            continue
                        for row in rows:
                            entity = row.get("material_name") if material else row.get("order_no")
                            if entity in entities and field in row:
                                witnesses.append({"entity": entity, "field": field, "value": row[field], "unit": UNITS.get(field), "source": {"database": database, "table": table}})
            values = {(str(f["value"]), f.get("unit")) for f in witnesses}
            if len(values) != 1:
                errors.append("unparsed_or_ambiguous_claim:" + field)
                remaining = remaining.replace(match[0], " ", 1)
                continue
            fact = witnesses[0]
            raw = re.sub(r"\s*\[(?:P|I)?\d+\]\s*$", "", match[1]).strip()
            unit = fact.get("unit")
            if unit:
                aliases = {"USD": ("USD", "美元", "US dollars"), "pcs": ("pcs", "件", "个")}.get(unit, (unit,))
                parsed = next((raw[:-len(u)].strip() for u in aliases if raw.lower().endswith(u.lower())), None)
                ok = parsed is not None and equivalent(parsed, fact["value"])
            else:
                ok = equivalent(raw, fact["value"])
            claims.append({"entity": fact["entity"], "field": field, "text": match[0], "correct": ok})
            if not ok:
                errors.append("incorrect_claim:" + field)
            remaining = remaining.replace(match[0], " ", 1)
    remaining = re.sub(r"SO-2026-[A-Z]?\d+|Hinge BX-\d+-24X?|BX-\d+-24X?|\[(?:P|I)?\d+\]", "", remaining)
    # Extra unbound numbers cannot hide behind a correct primary answer.
    if re.search(r"\d", remaining):
        errors.append("unbound_numeric_claim")
    # Unrecognized assertions remain reviewable rather than being guessed true
    # or marked unsupported merely because a different renderer wrote them.
    remaining = re.sub(r"订单|物料|材料|资料供审核|\bOrder\b|\bMaterial\b|[\s，。；、:：;,.!?！？·-]", "", remaining, flags=re.I)
    if remaining and not re.fullmatch(r"\d+", remaining):
        errors.append("unparsed_additional_prose")
    definite = any(e.startswith("incorrect_claim:") or e in {"wrong_entity", "unbound_numeric_claim"} for e in errors)
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
        raise ValueError("Human review does not bind this evidence")
    if not review.get("reviewed_by") or not review.get("rationale") or type(review.get("fact_correct")) is not bool:
        raise ValueError("An attributed review and explicit rationale are required")
    return review["fact_correct"]
