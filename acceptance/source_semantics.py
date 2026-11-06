"""Independent source semantics for both product versions.

The pinned catalog predates the feature under test. No application renderer,
SQL lineage helper, metadata validator or model is imported. Unsupported SQL
stays unproved; a content hash is never treated as authority for its contents.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import re

from acceptance.common import ROOT, read

CATALOG = ROOT / "acceptance/fixtures/source-semantics-000e2b7.json"
CATALOG_SHA256 = "ee17340b35fdb831d2abc04aa42154f77535cf454181d84b2115d62b57092fe1"
EMPTY_POLICY_NOTICE = "本次未检索到符合所选范围和时点的已发布政策；未发布草稿不能作为政策依据。"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


@lru_cache(maxsize=1)
def catalog():
    if hashlib.sha256(CATALOG.read_bytes()).hexdigest() != CATALOG_SHA256:
        raise ValueError("Independent source-semantics catalog hash mismatch")
    return read(CATALOG)["columns"]


def labels(entry):
    return {entry["metadata"][k] for k in ("label", "label_zh", "label_en") if entry["metadata"].get(k)}


def meaning_lines(entry):
    return {f"{caption}: {entry['visible_description'][language]}" for language, caption in
            (("zh", "字段含义"), ("en", "Field meaning")) if entry["visible_description"].get(language)}


# A deliberately small lexer/parser for plain direct-column SELECTs. String
# literals and comments cannot introduce fake FROM/JOIN/projection tokens.
_TOKEN = re.compile(r"\s+|--[^\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|"
                    r"`(?:``|[^`])*`|\[(?:\]\]|[^\]])*\]|[A-Za-z_][A-Za-z_0-9]*|"
                    r"\d+(?:\.\d+)?|<=|>=|<>|!=|[.,;()*=<>+/%?\-]")
_RESERVED = {"SELECT", "DISTINCT", "FROM", "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "OUTER",
             "ON", "USING", "WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET", "AS", "UNION"}


def _tokens(sql):
    result, position = [], 0
    for match in _TOKEN.finditer(sql):
        if match.start() != position:
            raise ValueError("Unrecognized SQL token")
        position = match.end()
        value = match[0]
        if value.isspace() or value.startswith(("--", "/*")):
            continue
        if value.startswith("'"):
            result.append(("literal", value))
        elif value[0] in '\"`[':
            end = ']' if value[0] == '[' else value[0]
            result.append(("identifier", value[1:-1].replace(end * 2, end)))
        elif re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", value):
            result.append(("word", value))
        else:
            result.append(("symbol", value))
    if position != len(sql):
        raise ValueError("Incomplete SQL tokenization")
    if result and result[-1] == ("symbol", ";"):
        result.pop()
    return result


def _word(token, value):
    return token[0] == "word" and token[1].upper() == value


def _identifier(token):
    return token[0] == "identifier" or (token[0] == "word" and token[1].upper() not in _RESERVED)


def direct_columns(sql):
    """Output alias -> (table, column), or {} for an unsupported query shape.

    Recognizes unique direct columns, qualified joins, and quoted identifiers.
    Rejects expressions, stars, CTEs, nested queries, catalog qualification,
    duplicate aliases, ambiguous unqualified joins and multiple statements.
    """
    try:
        ts = _tokens(sql)
        words = [v.upper() for kind, v in ts if kind == "word"]
        if (not ts or not _word(ts[0], "SELECT") or words.count("SELECT") != 1
                or any(w in words for w in ("WITH", "UNION", "EXCEPT", "INTERSECT"))
                or ("symbol", ";") in ts or words.count("FROM") != 1):
            return {}
        start = next(i for i, t in enumerate(ts) if _word(t, "FROM"))
        selected = ts[2:start] if len(ts) > 1 and _word(ts[1], "DISTINCT") else ts[1:start]
        groups, group = [], []
        for token in selected:
            if token == ("symbol", ","):
                groups.append(group)
                group = []
            else:
                group.append(token)
        groups.append(group)
        end = next((i for i in range(start + 1, len(ts)) if ts[i][0] == "word"
                    and ts[i][1].upper() in {"WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET"}), len(ts))
        depth = 0
        for t in ts[start + 1:end]:
            depth += t == ("symbol", "(")
            depth -= t == ("symbol", ")")
            if depth < 0 or (t == ("symbol", ",") and depth == 0):
                return {}
        if depth:
            return {}
        tables = {}
        positions = [start] + [i for i in range(start + 1, end) if _word(ts[i], "JOIN")]
        for index in positions:
            if not _identifier(ts[index + 1]):
                return {}
            table = ts[index + 1][1]
            after = index + 2
            if after < len(ts) and ts[after] in {("symbol", "."), ("symbol", "(")}:
                return {}
            if after < len(ts) and _word(ts[after], "AS"):
                after += 1
                if not _identifier(ts[after]):
                    return {}
                alias = ts[after][1]
            else:
                alias = ts[after][1] if after < len(ts) and _identifier(ts[after]) else table
            if alias.casefold() in tables:
                return {}
            # The pinned four fixture databases use SQLite identifiers.
            tables[alias.casefold()] = table.casefold()
        result = {}
        for projection in groups:
            if not projection or not _identifier(projection[0]):
                return {}
            qualifier, column, used = None, projection[0][1], 1
            if len(projection) > 1 and projection[1] == ("symbol", "."):
                if len(projection) < 3 or not _identifier(projection[2]):
                    return {}
                qualifier, column, used = column.casefold(), projection[2][1], 3
            tail = projection[used:]
            if tail and _word(tail[0], "AS"):
                tail = tail[1:]
            if tail and (len(tail) != 1 or not _identifier(tail[0])):
                return {}
            output = tail[0][1] if tail else column
            table = tables.get(qualifier) if qualifier else next(iter(tables.values())) if len(tables) == 1 else None
            if table is None or output.casefold() in {k.casefold() for k in result}:
                return {}
            result[output] = (table, column.casefold())
        return result
    except (IndexError, TypeError, ValueError):
        return {}


def bound_metadata(step, output, *, outputs=None):
    """Compare exact source configuration AND independently parsed projection."""
    try:
        sql = step["final_sql"]
        mapping = direct_columns(sql)
        if output not in mapping:
            return None
        if outputs is not None and (not isinstance(outputs, list) or any(not isinstance(x, str) for x in outputs)
                                    or len(outputs) != len(set(outputs)) or set(outputs) != set(mapping)):
            return None
        table, column = mapping[output]
        database = step["database"]
        origin = ".".join((database, table, column))
        entry = catalog().get(origin)
        if not entry:
            return None
        source = {"database": database, "table": table, "column": column, **entry["metadata"]}
        expected = {**source, "output_field": output, "query_hash": digest(sql),
                    "schema_revision": digest(json.dumps(source, ensure_ascii=False, sort_keys=True))}
        if (step.get("column_metadata") or {}).get(output) != expected:
            return None
        return {"database": database, "table": table, "column": column, "origin": origin, **entry}
    except (KeyError, TypeError, AttributeError, ValueError):
        return None


def business_metadata(payload, cell, step, *, null=False):
    """Additional wire bindings needed before using a dynamic label/meaning."""
    try:
        field, si, ri = cell["field"], cell["step_index"], cell["row_index"]
        if type(si) is not int or type(ri) is not int or min(si, ri) < 0:
            return None
        row = step["rows"][ri]
        if (payload.get("execution_status") != "success" or payload.get("degraded") is not False
                or payload.get("has_risk") is not False or payload.get("result_truncated") is not False
                or not (payload.get("trace_id") or payload.get("run_id"))
                or step.get("success") is not True or step.get("truncated") is not False
                or step.get("row_count") != len(step["rows"]) or step.get("error") or step.get("risk_findings")
                or row[field] != cell["value"] or cell["database"] != step["database"]):
            return None
        meaning = bound_metadata(step, field, outputs=list(row))
        if null:
            return meaning if cell["value"] is None else None
        query_hash = digest(step["final_sql"])
        identity = [step["database"], query_hash, step["as_of"], si, ri, field, row[field]]
        if (cell.get("query_hash") != query_hash
                or cell.get("evidence_id") != "ev_" + digest(json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str))
                or cell.get("entity") != {"kind": "query_row", "query_hash": query_hash, "row_index": ri}
                or cell.get("source_records") != step.get("source_records", [])
                or cell.get("unit") != (step.get("column_units") or {}).get(field)
                or type(cell["value"]) is not type(row[field])):
            return None
        cells = [c for c in payload.get("evidence", []) if c.get("step_index") == si
                 and c.get("row_index") == ri and c.get("field") == field]
        return meaning if (len(cells) == 1 and all(cell.get(k) == v for k, v in cells[0].items())
                           and all(k not in cell or cell[k] == payload.get(k) for k in ("trace_id", "run_id", "observed_at"))
                           and ("column_metadata" not in cell or cell["column_metadata"] == step["column_metadata"][field])) else None
    except (KeyError, TypeError, AttributeError, IndexError, ValueError):
        return None


def scope_matches(left, right, stamp):
    return (all(left.get(k) == right.get(k) for k in ("product", "region", "audience"))
            and stamp(left.get("as_of")) is not None and stamp(left.get("as_of")) == stamp(right.get("as_of")))


def empty_policy_proven(scenario, observed, draft, corpus, stamp):
    """Gold absence plus this draft's successful, non-degraded empty lookup."""
    truth, scope = scenario["truth"], scenario.get("policy_scope") or {}
    if truth.get("answerable") is not False or truth.get("refusal") != "no_applicable_policy" or truth.get("policy_versions"):
        return False
    at = stamp(scope.get("as_of"))
    if at is None:
        return False
    for document in corpus["policies"]:
        policy = document["policy"]
        start, end = stamp(policy.get("effective_at")), stamp(policy.get("expires_at"))
        if (policy.get("status") == "published" and start is not None and start <= at
                and (end is None or at < end)
                and all(policy.get(k) == "*" or (scope.get(k) and policy.get(k) == scope[k])
                        for k in ("product", "region", "audience"))):
            return False
    case = observed.get("case") or {}
    runs = [r for r in observed.get("runs", []) if r.get("run_id") == draft.get("run_id")]
    if not draft.get("run_id") or len(runs) != 1 or runs[0].get("status") != "succeeded":
        return False
    result = runs[0].get("result") or {}
    state = result.get("knowledge") or {}
    if (result.get("case_applied") is not True or result.get("draft_id") != draft.get("id")
            or result.get("case_id") != case.get("id") or result.get("response") != draft.get("body")
            or result.get("service_unavailable") or result.get("knowledge_used") is not False
            or result.get("knowledge_sources") != [] or state.get("sources")
            or state.get("attempted") is not True or state.get("policy_lookup") is not True
            or state.get("lookup_status") != "empty" or state.get("degraded") is not False
            or state.get("evidence_ok") is False or not scope_matches(state.get("lookup_scope") or {}, scope, stamp)
            or not scope_matches(case.get("policy_scope") or {}, scope, stamp)
            or any(r.get("policy_revision") or r.get("policy") for r in draft.get("evidence", []))):
        return False
    for item in result.get("trajectory", []):
        trace = item.get("trace") or {}
        if item.get("tool") == "knowledge_search" and (trace.get("sources") or trace.get("degraded")
                or trace.get("lookup_status") not in {None, "empty"}
                or (trace.get("lookup_scope") and not scope_matches(trace["lookup_scope"], scope, stamp))):
            return False
    return True


def policy_dates_match(line, policy, stamp):
    match = re.fullmatch(r"生效时点：([^；]+)；失效时点：(.+)", line)
    if not match or stamp(match[1]) is None or stamp(match[1]) != stamp(policy.get("effective_at")):
        return False
    expiry = policy.get("expires_at")
    return ((match[2] == "未设置") if expiry is None else
            stamp(match[2]) is not None and stamp(match[2]) == stamp(expiry))
