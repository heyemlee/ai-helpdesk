"""Original-request planning and coverage diagnostics, never evidence authority.

The existing cell/NULL validators and capability/adoption rules own trust.
This module neither signs a task nor changes handoff or clarification flags.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date
from pathlib import Path

import sqlglot
from sqlglot import exp

from app.core.business_facts import fresh
from app.core.fact_completion import _PHRASES, FIELDS
from app.core.sql_sources import source_columns

ROLES = {**FIELDS, **json.loads(Path(__file__).with_name("request_role_catalog.json").read_text())["roles"]}
_ORDER_REFERENCE = re.compile(
    r"该订单|这个订单|这笔订单|此订单|沿用上文编号|这笔业务|(?:它|该工单|此工单)对应记录|\b(?:this|that|selected) order\b",
    re.I,
)
_MATERIAL = re.compile(r"库存|现存|占用|物料|\b(?:inventory|stock|on.hand|allocated)\b", re.I)
_PARTS = re.compile(r"补件|补发配件|配件|\b(?:replacement|parts)\b", re.I)
_EFFECTS = re.compile(
    r"请转人工|要求人工|取消订单|安排补发|执行审批|申请退款|赔偿|\b(?:cancel|refund|escalate|human agent)\b", re.I
)
_TIME = re.compile(r"历史|截至|当时|上月|本月|去年|\bas of\b|\blast month\b", re.I)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def make_request_plan(original_message, *, entities=None, case_refs=(), fact_task=None, issue_type=""):
    """Conservative spans from the original text; Case context never wins by default."""
    text = original_message
    reused = bool(
        isinstance(fact_task, dict)
        and fact_task.get("message_sha256") == hashlib.sha256(text.encode()).hexdigest()
        and fact_task.get("fields")
        and all(r in FIELDS for r in fact_task["fields"])
    )
    targets = []

    def add(kind, value, provenance="original_text"):
        if not isinstance(value, str) or not value:
            return
        pattern = (
            re.escape(value)
            if kind == "material_name"
            else r"(?<![A-Za-z0-9_.-])" + re.escape(value) + r"(?![A-Za-z0-9_.-])"
        )
        match = re.search(pattern, text, re.I)
        if provenance == "original_text" and not match:
            return
        if match and provenance == "original_text":
            value = match[0]
        if any(t["kind"] == kind and t["value"] == value for t in targets):
            return
        targets.append(
            {
                "kind": kind,
                "value": value,
                "provenance": provenance,
                "span": [match.start(), match.end()] if match else None,
            }
        )

    if reused:
        add("order_id", (fact_task.get("entity") or {}).get("order_no"))
    for kind in ("order_id", "item_code"):
        values = (entities or {}).get(kind, []) or []
        for value in [values] if isinstance(values, str) else values:
            add(kind, value)
    for match in re.finditer(
        r"(?:订单(?:号|编号)?|\border(?: number| no\.?| id)?)\s*[:：#]?\s*([A-Za-z0-9][A-Za-z0-9_.-]{1,63})", text, re.I
    ):
        if any(c.isdigit() for c in match[1]):
            add("order_id", match[1])
    material = bool(_MATERIAL.search(text))
    if material:
        quoted = re.findall(r'[“"「\']([^”"」\']{1,160})[”"」\']', text)
        names = quoted or re.findall(
            r"(?:库存表中|物料(?:名称)?\s*[:：]?)\s*(.+?)(?=有多少|的(?:现存|现有|库存|占用)|库存|[，。？?]|$)", text
        )
        for name in names:
            add("material_name", name.strip())
        if any(t["kind"] == "material_name" for t in targets) and not re.search(
            r"编码|编号|\b(?:code|sku)\b", text, re.I
        ):
            targets[:] = [t for t in targets if t["kind"] != "item_code"]
        # An item-code entity remains an item code, never a guessed material name.
    references = list(dict.fromkeys(r for r in case_refs if isinstance(r, str) and r))
    if not material:
        for ref in references:
            add("order_id", ref)
    competing_code = re.search(r"(?<![A-Za-z0-9_.-])[A-Za-z][A-Za-z0-9]*-[A-Za-z0-9.-]+", text)
    if not targets and not material and len(references) == 1 and _ORDER_REFERENCE.search(text) and not competing_code:
        add("order_id", references[0], "case_reference")
    roles = []
    unresolved = []
    clean = re.sub(r"(?:不问|无需|不要查询|不要用|不需要)(?:实际送达日期|实际交付日期|计划交付日期)", "", text)
    rest = clean
    for phrase, role in sorted(
        ((p, r) for r, phrases in _PHRASES.items() for p in phrases), key=lambda item: len(item[0]), reverse=True
    ):
        if phrase.casefold() in rest.casefold():
            roles.append(role)
            rest = re.sub(re.escape(phrase), " ", rest, flags=re.I)
    grain = "order_values"
    if re.search(r"交付日期|送货日期|\bdelivery date\b", rest, re.I):
        roles.append("delivery_date_unspecified")
        unresolved.append("delivery_date_unspecified")
    if material:
        grain = "material_inventory"
        for role, pattern in {"on_hand_qty": r"现存|现有|\bon.hand\b", "allocated_qty": r"占用|\ballocated\b"}.items():
            if re.search(pattern, clean, re.I):
                roles.append(role)
        if "on_hand_qty" not in roles and re.search(
            r"库存(?:量|数量)?\s*(?:和|与|及|、)|\b(?:inventory|stock)\s+and\b", clean, re.I
        ):
            roles.append("inventory_quantity_unspecified")
            unresolved.append("inventory_quantity_unspecified")
    elif _PARTS.search(text):
        grain = "replacement_records"
        for role, pattern in {
            "part_name": r"名称|哪些|\bname\b",
            "part_quantity": r"数量|件数|多少|\bquantit(?:y|ies)\b",
            "part_status": r"进度|状态|到哪|\b(?:progress|status)\b",
        }.items():
            if re.search(pattern, clean, re.I):
                roles.append(role)
    elif re.search(r"电话|手机|\bphone\b", clean, re.I):
        grain = "registered_contact"
        role = (
            "delivery_phone"
            if re.search(r"配送.*电话|送货.*电话|\bdelivery phone\b", clean, re.I)
            else "registered_phone"
        )
        roles.append(role)
    if re.search(r"合计|汇总|总共|\b(?:sum|aggregate|total)\b", clean, re.I):
        grain = "aggregate"
        unresolved.append("aggregate_scope")
    if _TIME.search(clean):
        unresolved.append("time_scope")
    if references and any(t["kind"] == "order_id" and t["value"] not in references for t in targets):
        unresolved.append("case_target_conflict")
    if reused:
        roles = list(fact_task["fields"])
        grain = "current_order_scalar_values"
    filters = [
        m.group(0)
        for m in re.finditer(
            r"仓库|库位|位置|\b(?:warehouse|location)\b|\w+\s*(?:>=|<=|=|>|<)\s*[^，。\s]+", text, re.I
        )
    ]
    if filters:
        unresolved.append("filter_scope")
    if re.search(r"\b\w+_db\b|指定来源|指定数据库", text, re.I):
        unresolved.append("source_scope")
    if not reused:
        residual = clean
        for t in targets:
            residual = re.sub(re.escape(t["value"]), " ", residual, flags=re.I)
        for phrases in _PHRASES.values():
            for phrase in sorted(phrases, key=len, reverse=True):
                residual = re.sub(re.escape(phrase), " ", residual, flags=re.I)
        residual = re.sub(
            r"逐项|请提供|告诉我|请帮我|查一下|请问|查询|查看|核对|提供|请|的|及|与|和|有多少|多少|单号|订单号|订单|该|这个|这笔|此|"
            r"库存表中|物料名称|物料|材料|补发配件|配件|补件|登记联系电话|登记电话|联系电话|手机号|手机|电话|"
            r"现存|现有|已占用量|占用量|已占用|占用|库存|件数|数量|名称|进度|状态|交付日期|送货日期|"
            r"\b(?:please|show|check|provide|what|is|are|the|for|of|and|this|that|selected|order|inventory|stock|material|"
            r"on.hand|allocated|quantity|quantities|parts?|replacement|progress|status|registered|phone|number|units?)\b|"
            r"[\s，,。.;；:：、?？!！\"“”'「」]+",
            "",
            residual,
            flags=re.I,
        )
        if residual:
            unresolved.append("unparsed_request")
    lookup_entities = {kind: [t["value"] for t in targets if t["kind"] == kind] for kind in ("order_id", "item_code")}
    # A material name/code and a background order are independent, never aliases.
    if material and not re.search(r"订单|\border\b", text, re.I):
        lookup_entities.pop("order_id", None)
    lookup_entities = {k: v for k, v in lookup_entities.items() if v}
    effects = [m.group(0) for m in _EFFECTS.finditer(text)]
    plan = {
        "version": 1,
        "planning_only": True,
        "original_message_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "targets": targets,
        "lookup_entities": lookup_entities,
        "required_roles": sorted(set(roles)),
        "unresolved_roles": sorted(set(unresolved)),
        "grain": grain,
        "filters": filters,
        "context_only": {"case_order_refs": references, "issue_type": issue_type},
        "business_effects": effects,
        "existing_fact_fields_reused": reused,
    }
    return {**plan, "id": _digest(plan)}


def _role_label(role, language):
    unresolved = {
        "delivery_date_unspecified": {"zh": "交付日期（计划/实际口径）", "en": "Delivery date meaning"},
        "inventory_quantity_unspecified": {"zh": "库存数量口径", "en": "Inventory quantity meaning"},
    }
    return ROLES.get(role, unresolved.get(role, {})).get("labels", unresolved.get(role, {})).get(language, role)


def query_guidance(plan):
    """Internal model guidance; no technical descriptor is shown in the draft."""
    labels = [_role_label(r, "zh") for r in plan["required_roles"]]
    target = "；".join(t["value"] for t in plan["targets"]) or "原问未明确，不能从工单背景猜测"
    text = f"原问业务目标：{target}。原问所需字段：{'、'.join(labels) or '尚未明确'}；不得仅凭部分字段宣称整问已回答。"
    if plan["grain"] == "replacement_records":
        text += "逐条补件记录保留名称及关联订单，数量和状态不得跨记录拼接。"
    elif plan["grain"] == "material_inventory":
        text += "按原问物料及其实际计量单位核对库存和占用，不用其他业务编号替代物料。"
    elif plan["grain"] == "registered_contact":
        text += "保留原问联系角色；真实未登记、遗漏列、无记录和查询失败必须区分。"
    elif plan["grain"] == "aggregate":
        text += "保留原问汇总粒度，不为增加标识而改成明细。"
    if "delivery_date_unspecified" in plan["unresolved_roles"]:
        text += "交付日期口径未明确；计划日期和实际送达日期须按各自真实来源分别标明，不能互相替代。"
    return text


def _physical(cell):
    meta = cell.get("column_metadata") or {}
    return meta.get("database"), meta.get("table"), meta.get("column")


def _row_key(cell):
    return cell["database"], cell["query_hash"], cell["step_index"], cell["row_index"], cell["as_of"]


def _target_matches(cell, target, data, all_cells):
    step = data["steps"][cell["step_index"]]
    physical = dict(source_columns(step["final_sql"]))
    # Source-column metadata alone cannot prove which side of a join owns the
    # target row, including a self-join. Leave such targets unproved here.
    if len(list(sqlglot.parse_one(step["final_sql"]).find_all(exp.Table))) != 1:
        return False
    column = (
        "order_no" if target["kind"] == "order_id" else "material_name" if target["kind"] == "material_name" else None
    )
    if column is None:
        return False
    return any(
        _row_key(c) == _row_key(cell)
        and physical.get(c["field"]) == (_physical(cell)[1], column)
        and c["value"] == target["value"]
        for c in all_cells
    )


def _full_simple_range(data, witnesses, target):
    """Exact unlimited single-table entity query, not an inferred row count."""
    try:
        indices = {c["step_index"] for c in witnesses}
        if len(indices) != 1 or data.get("result_truncated") is not False:
            return False
        step = data["steps"][indices.pop()]
        statements = sqlglot.parse(step["final_sql"])
        if len(statements) != 1:
            return False
        tree = statements[0]
        if (
            not isinstance(tree, exp.Select)
            or len(list(tree.find_all(exp.Select))) != 1
            or any(v is not None and k not in {"expressions", "from", "where"} for k, v in tree.args.items())
            or step.get("truncated") is not False
            or step.get("row_count") != len(step["rows"])
        ):
            return False
        tables = list(tree.find_all(exp.Table))
        if len(tables) != 1:
            return False
        where = tree.args["where"].this
        key = "order_no" if target["kind"] == "order_id" else "material_name"
        return bool(
            isinstance(where, exp.EQ)
            and isinstance(where.this, exp.Column)
            and where.this.name == key
            and isinstance(where.expression, exp.Literal)
            and where.expression.is_string
            and where.expression.this == target["value"]
        )
    except (KeyError, TypeError, AttributeError, ValueError, sqlglot.errors.SqlglotError):
        return False


def _typed(role, cell, cap):
    value = cell["value"]
    if value is None:
        return cell.get("null_observation") is True and role in {
            "planned_delivery_date",
            "actual_delivery_date",
            "registered_phone",
            "delivery_phone",
        }
    if cap["type"] == "number":
        return (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
            and (not cap.get("unit") or cell.get("unit") == cap["unit"])
        )
    if not isinstance(value, str) or not value.strip():
        return False
    if cap["type"] == "date":
        try:
            return date.fromisoformat(value).isoformat() == value
        except ValueError:
            return False
    return True


def request_coverage(plan, data, cells, nulls, issues, *, outcome="", at=None):
    try:
        return _request_coverage(plan, data, cells, nulls, issues, outcome=outcome, at=at)
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, OverflowError):
        return {
            "version": 1,
            "planning_only": True,
            "plan_id": plan.get("id") if isinstance(plan, dict) else None,
            "complete": False,
            "roles": {},
            "reasons": ["diagnostic_input_unsupported"],
        }


def _request_coverage(plan, data, cells, nulls, issues, *, outcome="", at=None):
    """Consume server-validated observations without changing their usability."""
    roles = {}
    reasons = []
    all_cells = [*cells, *nulls]
    targets = plan.get("targets", [])
    target = targets[0] if len(targets) == 1 else None
    base = (
        "failed"
        if outcome in {"failed", "unavailable", "error", "model_unavailable"}
        or data.get("unavailable")
        or data.get("execution_status") == "failed"
        else (
            "no_rows"
            if data.get("execution_status") == "no_data"
            else "stale"
            if any("stale" in str(i) for i in issues)
            else "unsupported"
            if issues
            else None
        )
    )
    witnesses = []
    for role in plan.get("required_roles", []):
        cap = ROLES.get(role)
        selected = []
        status = base or ("ambiguous" if role in plan.get("unresolved_roles", []) else None)
        if status is None and cap:
            source = cap["source"]
            matches = [c for c in all_cells if _physical(c) == (source["database"], source["table"], source["column"])]
            if not matches:
                status = "unsupported" if any(c["field"] == source["column"] for c in all_cells) else "omitted"
            elif any(
                (c.get("column_metadata") or {}).get("schema_revision") != cap["schema_revision"] for c in matches
            ):
                status = "unsupported"
            elif target is None:
                status = "ambiguous"
            elif not all(_target_matches(c, target, data, cells) for c in matches):
                status = "wrong_target"
            elif any(not fresh(c.get("observed_at"), at) or not fresh(c.get("as_of"), at) for c in matches):
                status = "stale"
            elif not all(_typed(role, c, cap) for c in matches):
                status = "unsupported"
            else:
                selected = matches
                values = {_digest([c["value"], c.get("unit")]) for c in matches}
                status = (
                    "ambiguous"
                    if len(values) > 1 and plan["grain"] not in {"replacement_records", "material_inventory"}
                    else ("proven_null" if all(c.get("null_observation") is True for c in matches) else "value")
                )
        status = status or "unsupported"
        roles[role] = {
            "status": status,
            "evidence_ids": [c["evidence_id"] for c in selected],
            "witnesses": [list(_row_key(c)) for c in selected],
        }
        witnesses.extend(selected)
    if not targets:
        reasons.append("target_unresolved")
    if len(targets) > 1:
        reasons.append("multiple_targets")
    if not roles:
        reasons.append("roles_unsupported")
    if plan.get("unresolved_roles"):
        reasons.append("semantics_unresolved")
    if plan.get("business_effects"):
        reasons.append("residual_business_effects")
    if issues:
        reasons.append("execution_evidence_limited")
    if plan["grain"] in {"aggregate", "unsupported"}:
        reasons.append("grain_unsupported")
    if roles and all(v["status"] in {"value", "proven_null"} for v in roles.values()):
        if plan["grain"] in {"replacement_records", "material_inventory", "registered_contact"}:
            sets = [{tuple(w) for w in value["witnesses"]} for value in roles.values()]
            mismatch = not sets or any(s != sets[0] for s in sets)
            if plan["grain"] == "replacement_records":
                named = {_row_key(c) for c in cells if _physical(c) == ("svc_db", "replacement_part", "part_name")}
                mismatch = mismatch or not sets[0] <= named
            if mismatch:
                reasons.append("grain_mismatch")
                for value in roles.values():
                    value["status"] = "grain_mismatch"
            if not target or not _full_simple_range(data, witnesses, target):
                reasons.append("range_unproved")
    complete = bool(roles and not reasons and all(v["status"] in {"value", "proven_null"} for v in roles.values()))
    return {
        "version": 1,
        "planning_only": True,
        "plan_id": plan["id"],
        "grain": plan["grain"],
        "complete": complete,
        "roles": roles,
        "reasons": reasons,
    }


def coverage_note(coverage, language="zh"):
    if not coverage or coverage.get("complete") is True:
        return ""
    english = language == "en"
    missing = [
        _role_label(r, language)
        for r, v in coverage.get("roles", {}).items()
        if v["status"] not in {"value", "proven_null"}
    ]
    if missing:
        return "Still unverified: " + ", ".join(missing) + "." if english else "尚未核实：" + "、".join(missing) + "。"
    return (
        "The listed records do not yet establish the full requested scope."
        if english
        else "以上为已核实部分，原问的完整目标、记录范围或口径仍未核实。"
    )
