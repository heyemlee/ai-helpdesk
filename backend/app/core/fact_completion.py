"""Case-only scalar fact projection, bound before querying and checked at adoption.

This is a closed capability, not a general natural-language classifier. Unknown
words, scopes, grains or meanings leave the existing answer path in control.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, date, datetime
from pathlib import Path

import sqlglot
from sqlglot import exp

from app.core.business_facts import fresh, language_of, render_draft, stamp, verified_cells
from app.core.lookup_scope import digest
from app.core.sql_sources import source_columns

ORIGIN = "verified_fact_projection"
CATALOG = json.loads(Path(__file__).with_name("fact_capabilities.json").read_text())
CATALOG_REVISION = digest(CATALOG)
FIELDS = CATALOG["fields"]
_task = ContextVar("read_only_fact_task", default=None)
_PHRASES = {
    "planned_delivery_date": ("约定交付日期", "计划交付日期", "计划送货日期", "约定交期", "planned delivery date"),
    "actual_delivery_date": ("实际送达日期", "实际交付日期", "actual delivery date"),
    "delivery_status": ("当前配送状态", "配送状态", "current delivery status", "delivery status"),
    "order_status": ("当前订单状态", "订单状态", "current order status", "order status"),
    "contract_amount": ("合同金额", "contract amount"),
    "current_stage": ("当前工序", "current stage"),
    "production_status": ("当前生产状态", "生产状态", "current production status", "production status"),
}
_GUARDS = (
    "计划日期不能当成实际送达", "计划日期不是实际送达日期",
    "the planned date is not the actual delivery date",
)
# These words only connect already recognized entities and field phrases. They
# cannot introduce an action, comparison, history, source restriction or judgment.
_GLUE = re.compile(
    r"请帮我|请|帮我|核对|查询|查看|查一下|告诉我|订单号|订单|这个订单|该订单|"
    r"的|及|与|和|分别|是多少|是什么|多少|目前|现在|"
    r"\b(?:please|check|show|tell me|what|is|are|the|for|of|order|and)\b|[\s，,、。.;；:：?？]+", re.I)


@contextmanager
def bind_task(task):
    token = _task.set(copy.deepcopy(task))
    try:
        yield
    finally:
        _task.reset(token)


def current_task():
    return copy.deepcopy(_task.get())


def utc_now():
    return datetime.now(UTC)


def fact_mode(value):
    """A partial or unknown new-mode header must never become legacy input."""
    return isinstance(value, dict) and any(k in value for k in ("answer_origin", "answer_model_invoked", "fact_receipt"))


def fact_checkpoint(value):
    return isinstance(value, dict) and ("case_proposal_origin" in value or fact_mode(value.get("case_proposal")))


def valid_fact_header(value):
    return bool(isinstance(value, dict) and value.get("answer_origin") == ORIGIN
                and value.get("answer_model_invoked") is False and isinstance(value.get("fact_receipt"), dict)
                and value["fact_receipt"] and value.get("handoff") is False and value.get("escalated") is False
                and not any(value.get(k) for k in ("needs_clarification", "trajectory", "knowledge_sources", "policy_violations"))
                and not any(k in value for k in ("answer_confidence", "answer_handoff_declared", "handoff_review")))


def make_task(message, context, run_id, attempt):
    """Lock the whole request and server selection before any SQL is sent."""
    try:
        snapshot = context["snapshot"]
        refs = snapshot["order_refs"]
        receipt = snapshot["input_receipt"]
        if (not run_id or type(attempt) is not int or attempt < 1
                or len(refs) != 1 or not isinstance(refs[0], str) or not refs[0]
                or snapshot["control_mode"] != "assistant" or snapshot.get("pending_fields")
                or snapshot.get("materials") or context.get("materials_reviewed")
                or snapshot.get("issue_type") != "order"
                or receipt["case_id"] != context["case_id"] or receipt["message_id"] != context["message_id"]
                or receipt["operation"] not in {"created", "continued"}
                or not context["actor_id"] or type(context["expected_case_version"]) is not int
                or type(context["expected_control_version"]) is not int):
            return None
        text = message.strip().lower()
        constraints = []
        for guard in _GUARDS:
            if guard in text:
                text = text.replace(guard, "")
                constraints.append("planned_is_not_actual")
        entity = refs[0]
        # Exact selection only; a second identifier remains unparsed. Never
        # infer a customer/order from intent confidence or a returned row.
        text = re.sub(r"(?<![\w-])" + re.escape(entity.lower()) + r"(?![\w-])", " ", text)
        requested = []
        phrases = sorted(((phrase, role) for role, phrases in _PHRASES.items() for phrase in phrases),
                         key=lambda item: len(item[0]), reverse=True)
        for phrase, role in phrases:
            if phrase in text:
                text = text.replace(phrase, " ")
                requested.append(role)
        # No extraction of a 'known' part from an otherwise unknown request.
        if not requested or _GLUE.sub("", text):
            return None
        task = {"version": 1, "catalog_revision": CATALOG_REVISION,
                "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
                "run_id": run_id, "execution_attempt": attempt, "case_id": context["case_id"],
                "actor_id": context["actor_id"], "auth_version": context.get("auth_version", 0),
                "case_version": context["expected_case_version"],
                "control_version": context["expected_control_version"], "input_receipt": receipt,
                "entity": {"kind": "order", "order_no": entity}, "fields": sorted(set(requested)),
                "grain": "current_order_scalar_values", "business_effects": [],
                "constraints": sorted(set(constraints)), "language": snapshot.get("language") or language_of(message)}
        return {**task, "id": digest(task)}
    except (KeyError, TypeError, AttributeError):
        return None


def _payload_digest(data):
    return digest({k: v for k, v in data.items() if k != "_fact_dispatch"})


def retained_execution_task(task, business):
    """A memo may retain an earlier attempt's signed execution of this task.

    Keep its original identity and clocks. Every other obligation, including
    Run, input, actor, versions, entity and catalog, must be identical. Adoption
    still validates the persisted Run and the current worker's checkpoint.
    """
    try:
        original = business["evidence_payload"]["_fact_dispatch"]["fact_task"]
        def contract(value):
            return {k: v for k, v in value.items() if k not in {"id", "execution_attempt"}}
        if (type(original["execution_attempt"]) is int and 0 < original["execution_attempt"] <= task["execution_attempt"]
                and contract(original) == contract(task)):
            return original
    except (KeyError, TypeError, AttributeError):
        pass
    return task


def _signature(proof, key):
    return hmac.new(key.encode(), ("helpdesk-fact-dispatch-v1:" + digest(proof)).encode(), hashlib.sha256).hexdigest()


def bind_result(data, dispatch, *, key):
    """Only called after an authenticated transport return; ignore remote proofs.

    The existing service credential supplies a restart-stable, domain-separated
    receipt key. Without authenticated delegation this optional path stays off.
    """
    data.pop("_fact_dispatch", None)
    task = (dispatch or {}).get("fact_task")
    if key and task and data.get("execution_status") == "success":
        proof = {k: dispatch[k] for k in ("goal_id", "owner", "query", "nonce", "sent_at", "fact_task")}
        proof.update(received_at=datetime.now(UTC).isoformat(), payload_sha256=_payload_digest(data))
        data["_fact_dispatch"] = {**proof, "signature": _signature(proof, key)}
    return data


def _scope(step, task, roles):
    """Prove an entire current entity projection, including DISTINCT values.

    No row identity or same-snapshot claim follows from DISTINCT. Nulls and
    multiple values fail below. Filters, limits, history and derived SQL fail.
    """
    trees = sqlglot.parse(step["final_sql"])
    if len(trees) != 1 or not isinstance(trees[0], exp.Select):
        return False
    tree = trees[0]
    if (len(list(tree.find_all(exp.Select))) != 1
            or any(v is not None and k not in {"expressions", "from", "distinct", "where"}
                   for k, v in tree.args.items())
            or any(i.args.get("quoted") for i in tree.find_all(exp.Identifier))):
        return False
    if tree.args.get("distinct") and tree.args["distinct"].args.get("on"):
        return False
    tables = list(tree.find_all(exp.Table))
    if len(tables) != 1 or tables[0].db or tables[0].catalog:
        return False
    table = tables[0]
    if any(v is not None and k not in {"this", "alias", "db", "catalog"} for k, v in table.args.items()):
        return False
    alias = table.args.get("alias")
    if alias and alias.args.get("columns"):
        return False
    if any(c.table and c.table != table.alias_or_name or c.db or c.catalog for c in tree.find_all(exp.Column)):
        return False
    columns = dict(source_columns(step["final_sql"]))
    expected = {FIELDS[r]["source"]["column"] for r in roles} | {"order_no"}
    if (len(columns) != len(tree.selects) or len(columns) != len(expected)
            or {col for _, col in columns.values()} != expected
            or any(t != table.name for t, _ in columns.values())
            or columns.get("order_no") != (table.name, "order_no")):
        return False
    where = tree.args.get("where")
    predicate = where.this if where else None
    if (not isinstance(predicate, exp.EQ) or not isinstance(predicate.this, exp.Column)
            or predicate.this.name != "order_no" or not isinstance(predicate.expression, exp.Literal)
            or not predicate.expression.is_string or predicate.expression.this != task["entity"]["order_no"]):
        return False
    return all((step["database"], table.name) == (FIELDS[r]["source"]["database"], FIELDS[r]["source"]["table"])
               for r in roles)


def _typed(value, capability):
    kind = capability["type"]
    if kind == "number":
        return type(value) in {int, float} and math.isfinite(value) and value >= 0
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or re.search(r"[\r\n\x00]", value):
        return False
    if kind == "date":
        return date.fromisoformat(value).isoformat() == value
    return True


def projection(task, business, *, key=None, at=None):
    """Recompute all obligations; receipts themselves do not grant coverage."""
    at = at or utc_now()
    try:
        data = business["evidence_payload"]
        proof = dict(data["_fact_dispatch"])
        signature = proof.pop("signature")
        key = key if key is not None else os.getenv("SQL_AGENT_API_KEY", "").strip()
        if (not key or not hmac.compare_digest(signature, _signature(proof, key))
                or proof["fact_task"] != task or proof["owner"] != task["run_id"]
                or proof["payload_sha256"] != _payload_digest(data)
                or not proof["goal_id"] or not data.get("run_id") or data.get("trace_id") != proof["nonce"]
                or not fresh(proof["sent_at"], at) or not fresh(proof["received_at"], at)
                or not stamp(proof["sent_at"]) <= stamp(data["observed_at"]) <= stamp(proof["received_at"])
                or task["catalog_revision"] != CATALOG_REVISION
                or business.get("outcome") != "evidence" or business.get("evidence_issues")
                or business.get("has_risk") or business.get("degraded")):
            return None
        cells, issues = verified_cells(data, at=at)
        if issues or cells != business.get("evidence"):
            return None
        # Bind the actual outgoing request as well as the immutable task. The
        # query may include the server's Case context, but must name this entity.
        if task["entity"]["order_no"] not in proof["query"]:
            return None
        roles_by_source = {}
        for role in task["fields"]:
            source = FIELDS[role]["source"]
            roles_by_source.setdefault((source["database"], source["table"]), []).append(role)
        if len(data["steps"]) != len(roles_by_source):
            return None
        selected, seen = {}, set()
        for si, step in enumerate(data["steps"]):
            source_tables = {table for table, _ in dict(source_columns(step["final_sql"])).values()}
            if len(source_tables) != 1:
                return None
            source = (step["database"], next(iter(source_tables)))
            roles = roles_by_source.get(source)
            if (not roles or source in seen or not _scope(step, task, roles) or not step["rows"]
                    or step["as_of_source"] != "query_time" or not fresh(step["as_of"], at)
                    or not stamp(proof["sent_at"]) <= stamp(step["as_of"]) <= stamp(proof["received_at"])):
                return None
            seen.add(source)
            mapping = dict(source_columns(step["final_sql"]))
            for role in roles:
                capability = FIELDS[role]
                output = next(field for field, physical in mapping.items() if physical[1] == capability["source"]["column"])
                matching = [c for c in cells if c["step_index"] == si and c["field"] == output]
                if (len(matching) != len(step["rows"])
                        or len({digest([c["value"], c["unit"]]) for c in matching}) != 1
                        or any(row.get("order_no") != task["entity"]["order_no"] for row in step["rows"])
                        or any(not _typed(c["value"], capability) or c.get("unit") != capability["unit"]
                               or c["column_metadata"].get("schema_revision") != capability["schema_revision"]
                               for c in matching)):
                    return None
                selected[role] = matching[0]
        return selected if set(selected) == set(task["fields"]) else None
    except (KeyError, TypeError, ValueError, AttributeError, StopIteration, sqlglot.errors.SqlglotError):
        return None


def proposal(task, business):
    selected = projection(task, business)
    if selected is None:
        return None
    # Scope validation above permits exactly the requested physical columns
    # and order anchors. Keep every checked row and its original reference;
    # the shared renderer owns field labels, times, meanings and review prose.
    response = render_draft([], business, facts=None, language=task["language"])
    receipt = {"version": 1, "task": task, "dispatch_sha256": digest(business["evidence_payload"]["_fact_dispatch"]),
               "coverage": {r: c["evidence_id"] for r, c in selected.items()},
               "body_sha256": hashlib.sha256(response.encode()).hexdigest()}
    receipt["id"] = digest(receipt)
    return {"response": response, "answer_origin": ORIGIN, "answer_model_invoked": False,
            "fact_receipt": receipt, "agent_type": "system", "agent_types": [], "escalated": False,
            "handoff": False, "knowledge_used": False, "knowledge_sources": [], "knowledge": {},
            "business_data": business, "policy_violations": []}


def receipt_valid(receipt, business, *, message, context, run_id, attempt, body):
    try:
        task = receipt["task"]
        if (task["execution_attempt"] > attempt
                or task != make_task(message, context, run_id, task["execution_attempt"])):
            return False
        expected = proposal(task, business)
        return bool(expected and expected["response"] == body and expected["fact_receipt"] == receipt)
    except (TypeError, KeyError, AttributeError):
        return False
