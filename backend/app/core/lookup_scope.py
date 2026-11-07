"""Server-owned goal and dispatch binding for limited negative lookup claims.

This deliberately proves only a small SQL subset. Unsupported scope is a
limited tool observation, never proof that the current question has no record.
It neither validates ordinary value evidence nor overrides a human decision.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta

import sqlglot
from sqlglot import exp

_goal = ContextVar("lookup_goal", default=None)
_ENTITY_COLUMNS = {"order_id": {"order_no", "order_id", "order_ref"},
                   "item_code": {"item_code", "sku", "model"}}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def payload_digest(data):
    return digest({k: v for k, v in data.items() if k != "_lookup_dispatch"})


def make_goal(message, *, entities=None, case_refs=(), owner="", case_id=""):
    selected = {k: list(v) for k, v in (entities or {}).items() if k in _ENTITY_COLUMNS and isinstance(v, list) and v}
    if not selected and case_refs:
        selected = {"order_id": list(case_refs)}
    identity = digest([owner, case_id, message, selected]) if owner else uuid.uuid4().hex
    return {"id": identity, "message": message, "request_message": message, "queries": [message], "entities": selected,
            "owner": owner, "case_id": case_id}


def current_goal():
    return _goal.get()


@contextmanager
def bind_goal(goal):
    token = _goal.set(goal)
    try:
        yield
    finally:
        _goal.reset(token)


def dispatch_for(goal, query):
    return {"goal_id": goal["id"], "owner": goal["owner"], "query": query,
            "nonce": uuid.uuid4().hex, "sent_at": datetime.now(UTC).isoformat(),
            **({"fact_task": goal["fact_task"]} if goal.get("fact_task") else {})}


def bind_result(data, dispatch):
    """Called at the authenticated transport boundary, never from model args."""
    if dispatch and data.get("execution_status") == "no_data":
        data["_lookup_dispatch"] = {**dispatch, "received_at": datetime.now(UTC).isoformat(),
                                    "trace_matched": data.get("trace_id") == dispatch["nonce"],
                                    "payload_sha256": payload_digest(data)}
    return data


def is_empty_trace(trace):
    data = trace.get("evidence_payload")
    return isinstance(data, dict) and data.get("execution_status") == "no_data"


def _stamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def goal_matches(goal, message, owner="", case_id=""):
    return bool(isinstance(goal, dict) and goal.get("id") and goal.get("message") == message
                and (not owner or goal.get("owner") == owner)
                and (not case_id or goal.get("case_id") == case_id))


def _sql_scope(step):
    try:
        trees = sqlglot.parse(step["final_sql"])
        if len(trees) != 1 or not isinstance(trees[0], exp.Select):
            return None
        tree = trees[0]
        if (len(list(tree.find_all(exp.Select))) != 1 or tree.args.get("with") or tree.args.get("joins")
                or any(i.args.get("quoted") for i in tree.find_all(exp.Identifier))
                or tree.args.get("group") or tree.args.get("having") or tree.args.get("distinct")
                or tree.args.get("limit") or tree.args.get("offset") or list(tree.find_all(exp.AggFunc))):
            return None
        if list(tree.find_all(exp.TableSample)):
            return None
        if any(value is not None and key not in {"expressions", "from", "where", "order"}
               for key, value in tree.args.items()):
            return None
        for projection in tree.selects:
            node = projection.this if isinstance(projection, exp.Alias) else projection
            if not isinstance(node, (exp.Column, exp.Star)):
                return None
        tables = list(tree.find_all(exp.Table))
        if (len(tables) != 1 or tables[0].db or tables[0].catalog
                or not isinstance(tables[0].this, exp.Identifier)
                or any(value is not None and key not in {"this", "alias", "db", "catalog"}
                       for key, value in tables[0].args.items())):
            return None
        qualifier = tables[0].alias or tables[0].name
        if any(c.table and c.table != qualifier for c in tree.find_all(exp.Column)):
            return None
        where = tree.args.get("where")
        if where is None:
            return None
        terms = []
        operators = {exp.EQ: "=", exp.GTE: ">=", exp.GT: ">", exp.LTE: "<=", exp.LT: "<"}
        def visit(node):
            if isinstance(node, exp.Paren):
                return visit(node.this)
            if isinstance(node, exp.And):
                return visit(node.this) and visit(node.expression)
            if type(node) not in operators or not isinstance(node.this, exp.Column) or not isinstance(node.expression, exp.Literal):
                return False
            terms.append((node.this.name.lower(), operators[type(node)], node.expression.this))
            return True
        if not visit(where.this):
            return None
        return {"database": step["database"], "table": tables[0].name.lower(), "predicates": sorted(terms)}
    except (ValueError, TypeError, KeyError, AttributeError, sqlglot.errors.ParseError):
        return None


def _covers(goal, scope):
    text = goal["message"]
    if re.search(r'["`][a-z_][a-z0-9_]*["`]', text, re.I):
        return False
    databases = {v.lower() for v in re.findall(r"(?<![a-z0-9_])[a-z][a-z0-9_]*_db(?![a-z0-9_])", text, re.I)}
    if databases and databases != {scope["database"]}:
        return False
    # An unnamed/aliased source cannot be equated to a physical database here.
    if not databases and re.search(r"(?:数据[库源]|[\u4e00-\u9fff]{1,8}源|\b(?:database|source)\b)", text, re.I):
        return False
    tables = re.findall(r"(?<![a-z0-9_])[a-z][a-z0-9_]*_db\.([a-z][a-z0-9_]*)(?![a-z0-9_])", text, re.I)
    tables += re.findall(r"(?<![a-z0-9_])([a-z][a-z0-9_]*)\s*表|(?:表|\btable)\s*[:：]?\s*([a-z][a-z0-9_]*)", text, re.I)
    names = {n.lower() for item in tables for n in (item if isinstance(item, tuple) else (item,)) if n}
    if names and names != {scope["table"]}:
        return False
    expected = set()
    entities = goal.get("entities") or {}
    for kind, values in entities.items():
        if (kind not in _ENTITY_COLUMNS or not isinstance(values, list) or len(values) != 1
                or not isinstance(values[0], str)):
            return False
        matches = [p for p in scope["predicates"] if p[0] in _ENTITY_COLUMNS[kind] and p[1:] == ("=", values[0])]
        if len(matches) != 1:
            return False
        expected.add(matches[0])
    # Explicit field/operator/literal constraints support bounded time ranges
    # and custom identifiers without guessing natural-language equivalence.
    for col, op, _quote, value in re.findall(r"(?<![a-z0-9_])([a-z][a-z0-9_]*)\s*(>=|<=|=|>|<)\s*(['\"]?)([\w.:-]+)\3", text, re.I):
        expected.add((col.lower(), op, value))
    if (re.search(r"历史|截至|当时|上月|本月|最近|去年|月份|\bas of\b|snapshot|(?<![\w-])20\d\d(?:-\d\d-\d\d|年)", text, re.I)
            and not any(re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", p[2]) for p in expected)):
        return False
    if not expected:
        return False
    # Every requested constraint must be present, and an extra predicate must
    # not silently narrow the scope whose absence is being claimed.
    return expected == set(scope["predicates"])


def _query_form(text):
    # A small closed equivalence grammar for lookup phrasing, not a semantic
    # parser. Source, table, entity, time, filters and action words all remain.
    text = re.sub(r"请问|请(?:帮我|替我)?|查一下|查询|查看|查阅|查|看看|是否存在|是否有|有没有|有无|是否|存在吗|吗|的|系统中?", "", text)
    return re.sub(r"[\s，。！？?：:、,;；]+", "", text).casefold()


def request_goal(req):
    from app.runs.memo import current_run
    goal = getattr(req, "lookup_goal", None)
    run = current_run()
    return goal if (isinstance(goal, dict) and goal.get("request_message") == req.message
                    and (not run or goal.get("owner") == run.run_id)) else None


def assess_lookup(trace, goal):
    from app.core.business_facts import verified_lookup_receipt
    data = trace.get("evidence_payload") or {}
    receipt = verified_lookup_receipt(data)
    result = {"execution_verified": bool(receipt), "covers_goal": False, "reasons": [], "receipt": receipt}
    if not receipt:
        result["reasons"] = ["lookup_execution_unverified"]
        return result
    binding = data.get("_lookup_dispatch")
    if (not isinstance(goal, dict) or not goal.get("id") or not isinstance(goal.get("message"), str)
            or not isinstance(goal.get("queries"), list) or not all(isinstance(q, str) for q in goal["queries"])
            or not isinstance(goal.get("entities"), dict) or not isinstance(binding, dict)
            or not isinstance(binding.get("query"), str) or trace.get("lookup_goal") != goal
            or binding.get("goal_id") != goal["id"] or binding.get("owner") != goal.get("owner")
            or binding.get("trace_matched") is not True or binding.get("nonce") != data.get("trace_id")
            or binding.get("payload_sha256") != payload_digest(data)):
        result["reasons"] = ["lookup_goal_or_dispatch_unbound"]
        return result
    if not any(_query_form(binding.get("query", "")) == _query_form(q) for q in goal.get("queries", [])):
        result["reasons"] = ["lookup_dispatch_target_changed"]
        return result
    start, end = _stamp(binding.get("sent_at")), _stamp(binding.get("received_at"))
    scopes = [_sql_scope(s) for s in data["steps"]]
    if len(scopes) != 1 or not scopes[0] or not _covers(goal, scopes[0]):
        result["reasons"] = ["lookup_scope_unproved"]
        return result
    for step in data["steps"]:
        when = _stamp(step.get("as_of"))
        if (not start or not end or not when or step.get("as_of_source") != "query_time"
                or not start - timedelta(seconds=2) <= when <= end + timedelta(seconds=2)):
            result["reasons"] = ["lookup_observation_not_current_dispatch"]
            return result
    result.update(covers_goal=True, scope=scopes[0])
    return result


def restrict_lookup(trace, goal):
    """Keep an out-of-scope execution as a diagnostic, never as A's absence."""
    checked = assess_lookup(trace, goal)
    if checked["covers_goal"]:
        return {**trace, "lookup_goal": goal, "lookup_verified": True, "lookup_coverage": checked}
    return {"attempted": True, "outcome": "unverified", "evidence": [],
            "evidence_issues": ["lookup_scope_unproved"], "lookup_verified": False,
            "limited_lookups": [{"trace": trace, "assessment": checked}]}
