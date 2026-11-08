"""Closed read-only image comparison using persisted server review authority.

Hashes identify records; they are not SQL-dispatch signatures. Adoption must
reload the referenced server records, material and original user input.
"""

from __future__ import annotations

import copy
import hashlib
import math
import re
from datetime import UTC, datetime

import sqlglot
from sqlglot import exp

from app.attachments.verification import candidate_columns, enforce_review_dependencies, normalized
from app.core.business_facts import fresh, render_draft, stamp, verified_column_metadata
from app.core.lookup_scope import digest
from app.core.sql_sources import source_columns

ORIGIN = "verified_image_projection"
KIND = "image_review_projection_v1"
FIELDS = frozenset({"order_ref", "model", "quantity", "date", "status"})
_PHYSICAL = {
    "order_ref": ("orders", "order_no"),
    "model": ("order_item", "model"),
    "quantity": ("order_item", "quantity"),
    "date": ("orders", "delivery_date"),
    "status": ("orders", "order_status"),
}
_KINDS = {
    "order_ref": {"order", "unknown"},
    "model": {"unknown"},
    "quantity": {"ordered"},
    "date": {"delivery"},
    "status": {"order"},
}
_MAIN = (
    r"(?:请(?:帮我)?)?(?:读取|查看)(?:这份|这张|上传的|所附的?)?订单(?:材料|图片|截图|标签|照片|表单)"
    r"(?:并|并且)(?:逐项)?(?:核对|比对)(?:业务库|系统)(?:记录)?",
    r"(?:请(?:帮我)?)?(?:把|将)?(?:这份|这张|上传的|所附的?)?订单(?:材料|图片|截图|标签|照片|表单)"
    r"(?:与|和)(?:业务库|系统)(?:记录)?(?:逐项)?(?:核对|比对)",
    r"(?:please )?(?:read|check) (?:this |the |uploaded )?order (?:image|material|label|screenshot) "
    r"and (?:compare|check) (?:it )?(?:against|with) (?:the )?(?:database|system) records",
)
_CONSTRAINTS = (
    r"(?:模糊或不一致|不清楚或不一致)的?字段请(?:明确)?(?:标记为)?待核实",
    r"(?:请)?(?:逐项列出|用表格列出)(?:核对结果|已核实字段)",
    r"(?:不要|不得)推测缺件数量",
    r"(?:mark|flag) (?:unclear or inconsistent|unreadable or conflicting) fields (?:as )?unverified",
    r"do not infer (?:the )?missing (?:parts )?quantity",
)


def _request(message):
    clauses = [c.strip() for c in re.split(r"[，,。.;；!?！？\n]+", message) if c.strip()]
    if not clauses:
        return False
    if any(re.fullmatch(pattern, clauses[0], re.I) for pattern in _MAIN):
        remaining = clauses[1:]
    else:
        selected = re.fullmatch(r"(?:请)?(?:逐项)?(?:检查|核对|读取)(?:附件|图片|订单材料|订单截图)的?(.+)", clauses[0])
        names = {
            "单号": "order_ref",
            "订单号": "order_ref",
            "订单标识": "order_ref",
            "型号": "model",
            "产品型号": "model",
            "数量": "quantity",
            "日期": "date",
            "状态": "status",
            "订单状态": "status",
        }
        fields = re.split(r"、|以及|和|及|与", selected[1]) if selected else []
        if (
            len(fields) != 5
            or {names.get(field.strip()) for field in fields} != FIELDS
            or len(clauses) < 2
            or not re.fullmatch(
                r"(?:跟|与|和)(?:业务库|系统)(?:记录)?(?:逐项)?(?:比对|核对)"
                r"(?:后|并)(?:标记|列出)(?:不能核实|无法核实|待核实)的?(?:项目|字段)",
                clauses[1],
            )
        ):
            return False
        remaining = clauses[2:]
    return all(any(re.fullmatch(pattern, clause, re.I) for pattern in _CONSTRAINTS) for clause in remaining)


def make_task(message, context, run_id, attempt):
    """No substring extraction: every clause must describe this one comparison."""
    try:
        s = context["snapshot"]
        receipt, review = s["input_receipt"], s["material_review_receipt"]
        materials, facts = s["materials"], s["facts"]
        parent = context["message_id"].removeprefix("verified_")
        if (
            not isinstance(message, str)
            or not _request(message)
            or not run_id
            or type(attempt) is not int
            or attempt < 1
            or context.get("materials_reviewed") is not True
            or parent == context["message_id"]
            or not parent
            or s["control_mode"] != "assistant"
            or s["status"] not in {"open", "in_progress"}
            or s.get("pending_fields")
            or s.get("review_materials")
            or s["issue_type"] not in {"general", "order"}
            or receipt
            != {"case_id": context["case_id"], "message_id": review["request_message_id"], "operation": "created"}
            or review["case_id"] != context["case_id"]
            or review.get("failures")
            or len(materials) != 1
            or materials[0]["message_id"] != receipt["message_id"]
            or not materials[0]["attachment_id"]
            or not re.fullmatch(r"[0-9a-f]{64}", materials[0]["sha256"])
            or set(facts) != FIELDS
            or not context["actor_id"]
            or not context["instance"]
            or not re.fullmatch(r"[0-9a-f]{64}", context.get("image_continuation_message_sha256", ""))
            or type(context["auth_version"]) is not int
            or any(
                type(context[k]) is not int or context[k] < 1
                for k in ("expected_case_version", "expected_control_version")
            )
            or s.get("language", "zh") not in {"zh", "en"}
            or s.get("messages") != [{"role": "user", "content": message}]
        ):
            return None
        entity = facts["order_ref"]["db_value"]
        if (
            not isinstance(entity, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", entity)
            or s.get("order_refs") not in ([], [entity])
        ):
            return None
        task = {
            "version": 1,
            "run_id": run_id,
            "execution_attempt": attempt,
            "case_id": context["case_id"],
            "instance": context["instance"],
            "actor_id": context["actor_id"],
            "auth_version": context["auth_version"],
            "case_version": context["expected_case_version"],
            "control_version": context["expected_control_version"],
            "input_receipt": copy.deepcopy(receipt),
            "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
            "review_run_id": parent,
            "continuation_message_sha256": context["image_continuation_message_sha256"],
            "policy_scope": copy.deepcopy(s.get("policy_scope") or {}),
            "materials": copy.deepcopy(materials),
            "entity": entity,
            "fields": sorted(FIELDS),
            "business_effects": [],
            "language": s.get("language", "zh"),
        }
        return {**task, "id": digest(task)}
    except (KeyError, TypeError, AttributeError):
        return None


def _scope(sql, entity, bindings):
    """One actual order/item row; no derived values or unrelated join/filter."""
    try:
        trees = sqlglot.parse(sql)
        if len(trees) != 1 or not isinstance(trees[0], exp.Select):
            return False
        tree = trees[0]
        if (
            len(list(tree.find_all(exp.Select))) != 1
            or any(v is not None and k not in {"expressions", "from", "joins", "where"} for k, v in tree.args.items())
            or any(i.args.get("quoted") for i in tree.find_all(exp.Identifier))
            or len(tree.selects) != 5
            or dict(source_columns(sql)) != {bindings[f]: physical for f, physical in _PHYSICAL.items()}
        ):
            return False
        tables = list(tree.find_all(exp.Table))
        if (
            len(tables) != 2
            or {t.name for t in tables} != {"orders", "order_item"}
            or any(t.db or t.catalog for t in tables)
        ):
            return False
        aliases = {t.alias_or_name: t.name for t in tables}
        if len(aliases) != 2 or any(t.args.get("alias") and t.args["alias"].args.get("columns") for t in tables):
            return False

        def column(value):
            return (
                (aliases.get(value.table), value.name)
                if isinstance(value, exp.Column) and not value.db and not value.catalog
                else None
            )

        joins = tree.args.get("joins") or []
        if len(joins) != 1 or any(joins[0].args.get(k) for k in ("side", "kind", "using", "method")):
            return False
        on = joins[0].args.get("on")
        if not isinstance(on, exp.EQ) or {column(on.this), column(on.expression)} != {
            ("orders", "order_no"),
            ("order_item", "order_no"),
        }:
            return False
        where = tree.args["where"].this
        return bool(
            isinstance(where, exp.EQ)
            and column(where.this) == ("orders", "order_no")
            and isinstance(where.expression, exp.Literal)
            and where.expression.is_string
            and where.expression.this == entity
        )
    except (KeyError, TypeError, AttributeError, ValueError, sqlglot.errors.SqlglotError):
        return False


def utc_now():
    return datetime.now(UTC)


def _coverage(task, reviews, facts, *, at=None):
    try:
        at = at or utc_now()
        if len(reviews) != 1 or set(facts) != FIELDS:
            return None
        row = reviews[0]
        review = row["result"]
        material = task["materials"][0]
        execution = row["execution"]
        clocks = [execution["started_at"], row["created_at"], execution["finished_at"]]
        if (
            any(type(v) not in (int, float) or not math.isfinite(v) for v in clocks)
            or not clocks[0] <= clocks[1] <= clocks[2]
            or datetime.fromtimestamp(clocks[2], UTC) > at
        ):
            return None
        if (
            row["case_id"] != task["case_id"]
            or row["run_id"] != task["review_run_id"]
            or row["version"] != task["case_version"]
            or row["control_version"] != task["control_version"]
            or row["attachment_id"] != material["attachment_id"]
            or row["sha256"] != material["sha256"]
            or review["attachment_id"] != row["attachment_id"]
            or review["sha256"] != row["sha256"]
            or review["document_type"] not in {"order", "label", "delivery"}
            or review["query_outcome"] != "checked"
            or len(review["fields"]) != 5
            or {i["field"] for i in review["fields"]} != FIELDS
            or enforce_review_dependencies(copy.deepcopy(review)) != review
        ):
            return None
        bindings = {item["field"]: item["source"]["column"] for item in review["fields"]}
        if len(set(bindings.values())) != 5:
            return None
        if any(
            review.get(k)
            for k in ("has_risk", "degraded", "unavailable", "evidence_issues", "failure_reason", "query_error")
        ):
            return None
        identities, coverage = set(), {}
        for item in review["fields"]:
            field, source = item["field"], item["source"]
            if (
                item["outcome"] != "matched"
                or item["readability"] != "clear"
                or item["kind"] not in _KINDS[field]
                or not isinstance(item["db_value"], str)
                or not item["db_value"]
                or normalized(item["value"], field) != item["db_value"]
                or source["database"] != "erp_db"
                or source["order_ref"] != task["entity"]
                or source["column"] not in candidate_columns(field, item["kind"])
                or set(source["output_columns"]) != set(bindings.values())
                or item.get("unit") != source.get("unit")
                or source.get("unit") != ("pcs" if field == "quantity" else None)
                or not fresh(source["observed_at"], at)
                or not fresh(source["as_of"], at)
                or stamp(source["as_of"]) > stamp(source["observed_at"])
                or not datetime.fromtimestamp(clocks[0], UTC) <= stamp(source["as_of"])
                or not stamp(source["observed_at"]) <= datetime.fromtimestamp(clocks[1], UTC)
                or source["as_of_source"] != "query_time"
                or not _scope(source["final_sql"], task["entity"], bindings)
            ):
                return None
            metadata = source.get("column_metadata") or {}
            step = {
                "database": source["database"],
                "final_sql": source["final_sql"],
                "column_metadata": {source["column"]: metadata},
            }
            if not metadata or verified_column_metadata(step, source["column"]) != metadata:
                return None
            expected = {
                "value": item["db_value"],
                "db_value": item["db_value"],
                "candidate_value": item["value"],
                "source": "verified_image",
                "unit": item["unit"],
                "attachment_id": row["attachment_id"],
                "sha256": row["sha256"],
                "evidence_id": row["run_id"] + ":" + field,
                "business_source": source,
                "message_id": task["input_receipt"]["message_id"],
            }
            if facts[field] != expected:
                return None
            identities.add(
                digest(
                    {
                        k: source[k]
                        for k in (
                            "database",
                            "final_sql",
                            "query_hash",
                            "step_index",
                            "row_index",
                            "order_ref",
                            "trace_id",
                            "run_id",
                            "as_of",
                            "observed_at",
                            "output_columns",
                            "source_records",
                        )
                    }
                )
            )
            coverage[field] = {
                "evidence_id": expected["evidence_id"],
                "column": source["column"],
                "schema_revision": metadata["schema_revision"],
                "kind": item["kind"],
                "unit": item["unit"],
            }
        return coverage if len(identities) == 1 else None
    except (KeyError, TypeError, AttributeError, ValueError, OverflowError, OSError):
        return None


def ordered_facts(facts):
    return {field: facts[field] for field in sorted(FIELDS)}


def proposal(task, reviews, facts, *, at=None):
    if (
        not isinstance(task, dict)
        or task.get("version") != 1
        or not task.get("id")
        or digest({k: v for k, v in task.items() if k != "id"}) != task["id"]
        or task.get("fields") != sorted(FIELDS)
        or task.get("business_effects") != []
        or task.get("language") not in {"zh", "en"}
    ):
        return None
    coverage = _coverage(task, reviews, facts, at=at)
    if coverage is None:
        return None
    response = render_draft([], {}, ordered_facts(facts), language=task["language"], at=at or utc_now())
    row = reviews[0]
    authority = {
        "type": "persisted_server_image_review",
        "review_run_id": row["run_id"],
        "records": [
            {
                k: row[k]
                for k in ("case_id", "attachment_id", "sha256", "version", "control_version", "created_at", "execution")
            }
            | {"result_sha256": digest(row["result"])}
        ],
    }
    receipt = {
        "kind": KIND,
        "version": 1,
        "task": copy.deepcopy(task),
        "authority": authority,
        "coverage": coverage,
        "body_sha256": hashlib.sha256(response.encode()).hexdigest(),
    }
    receipt["id"] = digest(receipt)
    return {
        "response": response,
        "answer_origin": ORIGIN,
        "answer_model_invoked": False,
        "fact_receipt": receipt,
        "agent_type": "system",
        "agent_types": [],
        "intent": "other",
        "handoff": False,
        "escalated": False,
        "knowledge_used": False,
        "knowledge_sources": [],
        "knowledge": {},
        "business_data": {},
        "policy_violations": [],
    }


def receipt_valid(receipt, reviews, facts, *, message, context, run_id, attempt, body, at=None):
    try:
        task = receipt["task"]
        if (
            receipt["kind"] != KIND
            or type(task["execution_attempt"]) is not int
            or task["execution_attempt"] > attempt
            or task != make_task(message, context, run_id, task["execution_attempt"])
        ):
            return False
        expected = proposal(task, reviews, facts, at=at)
        return bool(expected and expected["response"] == body and expected["fact_receipt"] == receipt)
    except (KeyError, TypeError, AttributeError, ValueError):
        return False
