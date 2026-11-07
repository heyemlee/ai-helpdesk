"""Check explicit raw-column plans before executing generated SQL.

This is a projection check, not a substitute for authorization or semantic
validation. It derives requirements from the current plan and local schema.
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, build_scope

from ai_sql_agent.auth.sql_analysis import _sqlglot_dialect

from .objects import CotStep, LocalSchema


class RawProjectionError(ValueError):
    def __init__(self, sql: str, issues: list[str]):
        self.sql = sql
        super().__init__("raw_projection_incomplete: " + "; ".join(issues))


def is_business_identifier(column):
    return bool(re.search(r"编号|单号|订单号|业务.*(?:编码|号码)|(?:business|order|invoice|account|reference).*(?:number|code|reference|identifier)", column.description, re.I)
                and not re.search(r"内部|流水|快照|surrogate|internal", column.description, re.I))


def _business_bindings(tree, schema, user_query, step):
    """Bounded detail-only check; never infer columns from a user constant.

    A real schema-described business identifier, already used by this single
    table SQL to select an explicitly mentioned target, must also be returned.
    Aggregates, distinct, set operations and joins keep their existing grain;
    their requirements remain with the plan and semantic validator.
    """
    question = user_query.rsplit("原始任务背景：", 1)[-1]
    # A simple SELECT alone does not establish a detail-list requirement.
    # Explicit value-only requests take precedence over a broad plan marker.
    detail = re.search(r"逐条|逐行|明细|列举|raw (?:rows|columns)|detail|each record", step.operation_instruction, re.I)
    value_only = re.search(r"只(?:要|需|返回|输出|汇报)|仅(?:要|需|返回|输出|汇报)|不要[^。；;]*(?:编号|单号|明细)|only\s+(?:the\s+)?(?:value|status|attribute)|without[^.;]*(?:identifier|number|details)", question, re.I)
    if (not detail or value_only or not user_query or not isinstance(tree, exp.Select) or tree.args.get("distinct")
            or tree.args.get("group") or tree.args.get("joins") or tree.args.get("with")
            or len(list(tree.find_all(exp.Select))) != 1 or list(tree.find_all(exp.AggFunc))
            or any(i.args.get("quoted") for i in tree.find_all(exp.Identifier))):
        return set()
    tables = list(tree.find_all(exp.Table))
    if len(tables) != 1 or tables[0].db or tables[0].catalog:
        return set()
    table = schema.tables.get(tables[0].name)
    where = tree.args.get("where")
    if table is None or where is None:
        return set()
    identifiers = {c.column_name.lower() for c in table.columns if is_business_identifier(c)}
    needed = set()
    for predicate in where.find_all(exp.EQ):
        column, value = predicate.this, predicate.expression
        if (isinstance(column, exp.Column) and column.name.lower() in identifiers
                and isinstance(value, exp.Literal) and value.is_string and value.this
                and re.search(r"(?<![a-zA-Z0-9_-])" + re.escape(value.this) + r"(?![a-zA-Z0-9_-])", user_query)):
            needed.add(column.name.lower())
    return needed


def projection_issues(sql: str, step: CotStep, schema: LocalSchema, dialect: str, *, user_query: str = "") -> list[str]:
    instruction = step.operation_instruction + " " + step.output_target
    raw = bool(re.search(r"原始列|原始记录|原始明细|raw (?:rows|columns)", instruction, re.I))
    # Ordinary explicit column lists have the same projection contract. Do not
    # treat column names inside a computation/aggregate as raw output demands.
    direct = r"(?:[a-z_][a-z0-9_]*\.)?[a-z_][a-z0-9_]*(?:\s+(?:AS\s+)?[a-z_][a-z0-9_]*|\s*[（(]别名\s*[:：]?\s*[a-z_][a-z0-9_]*[）)])?"
    explicit = bool(re.fullmatch(r"\s*" + direct + r"(?:\s*[,、，]\s*" + direct + r")*\s*", step.output_target, re.I))
    tables = {name.lower(): {c.column_name.lower() for c in table.columns}
              for name, table in schema.tables.items()}
    # Alias text is a header, even if it happens to name another real column.
    source_text = re.sub(r"\s+AS\s+[a-z_][a-z0-9_]*|\s*[（(]\s*别名\s*[:：]?\s*[a-z_][a-z0-9_]*[）)]", "", step.output_target, flags=re.I)
    if explicit:
        source_text = ",".join(re.match(r"\s*((?:[a-z_][a-z0-9_]*\.)?[a-z_][a-z0-9_]*)", chunk, re.I)[1].split('.')[-1]
                               for chunk in re.split(r"[,、，]", step.output_target))
    required = {name for columns in tables.values() for name in columns
                if re.search(r"(?<![a-zA-Z0-9_])" + re.escape(name) + r"(?![a-zA-Z0-9_])", source_text, re.I)}
    if not raw and (not explicit or re.search(r"汇总|求和|计数|平均|最大|最小|分组|aggregate|group by", instruction, re.I)):
        required = set()
    try:
        tree = sqlglot.parse_one(sql, read=_sqlglot_dialect(dialect))
        root = build_scope(tree)
    except sqlglot.errors.SqlglotError:
        # Syntax errors still go through the existing bounded SQL repair path.
        return []
    if root is None:
        return ["字段查询必须返回 SELECT 结果"] if required else []
    required.update(_business_bindings(tree, schema, user_query, step))
    if not required:
        return []

    def outputs(scope: Scope, depth=0) -> dict[str, set[str]]:
        if depth > 12:
            return {}
        if scope.union_scopes:
            branches = [outputs(s, depth + 1) for s in scope.union_scopes]
            # SQL set operations align by position and use the first header.
            # Every UNION branch must supply the raw column; complementary
            # NULL-filled rows cannot establish a complete row witness.
            if not isinstance(scope.expression, exp.Union):
                return branches[0]
            columns = list(branches[0])
            return {name: set.intersection(*(list(b.values())[i] if i < len(b) else set() for b in branches))
                    for i, name in enumerate(columns)}

        def source_columns(source):
            if isinstance(source, exp.Table):
                return {c: {c} for c in tables.get(source.name.lower(), set())}
            return outputs(source, depth + 1) if isinstance(source, Scope) else {}

        sources = {alias.lower(): source_columns(source) for alias, source in scope.sources.items()}
        projected = {}
        for projection in getattr(scope.expression, "selects", ()):
            direct = projection.this if isinstance(projection, exp.Alias) else projection
            if isinstance(direct, exp.Star) or (isinstance(direct, exp.Column) and direct.is_star):
                qualifier = direct.table.lower() if isinstance(direct, exp.Column) else ""
                for alias, cols in sources.items():
                    if not qualifier or qualifier == alias:
                        for name, origins in cols.items():
                            projected.setdefault(name, set()).update(origins)
            elif isinstance(direct, exp.Column):
                candidates = [sources.get(direct.table.lower(), {})] if direct.table else list(sources.values())
                # An ambiguous unqualified column cannot prove a real origin.
                matches = [c[direct.name.lower()] for c in candidates if direct.name.lower() in c]
                projected[projection.alias_or_name.lower()] = matches[0] if len(matches) == 1 else set()
            else:
                projected[projection.alias_or_name.lower()] = set()
        return projected

    projected = outputs(root)
    provided = set().union(*projected.values()) if projected else set()
    issues = []
    missing = required - provided
    if missing:
        issues.append("输出目标中的原始列未平铺返回：" + ", ".join(sorted(missing)))
    for name in sorted(required):
        alias = re.search(re.escape(name) + r"\s*(?:[（(]\s*别名\s*[:：]?\s*|\s+AS\s+)([a-zA-Z_][a-zA-Z0-9_]*)", step.output_target, re.I)
        if alias and name not in projected.get(alias[1].lower(), set()):
            issues.append(f"原始列 {name} 必须使用指定别名 {alias[1]}")
    return issues
