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


def projection_issues(sql: str, step: CotStep, schema: LocalSchema, dialect: str) -> list[str]:
    instruction = step.operation_instruction + " " + step.output_target
    if not re.search(r"原始列|原始记录|原始明细|raw (?:rows|columns)", instruction, re.I):
        return []
    tables = {name.lower(): {c.column_name.lower() for c in table.columns}
              for name, table in schema.tables.items()}
    required = {name for columns in tables.values() for name in columns
                if re.search(r"(?<![a-zA-Z0-9_])" + re.escape(name) + r"(?![a-zA-Z0-9_])", step.output_target, re.I)}
    if not required:
        return []
    try:
        tree = sqlglot.parse_one(sql, read=_sqlglot_dialect(dialect))
        root = build_scope(tree)
    except sqlglot.errors.SqlglotError:
        # Syntax errors still go through the existing bounded SQL repair path.
        return []
    if root is None:
        return ["原始列查询必须返回 SELECT 结果"]

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
                projected[projection.alias_or_name.lower()] = set().union(*(c.get(direct.name.lower(), set()) for c in candidates))
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
        alias = re.search(re.escape(name) + r"\s*[（(]\s*别名\s*[:：]?\s*([a-zA-Z_][a-zA-Z0-9_]*)", step.output_target, re.I)
        if alias and name not in projected.get(alias[1].lower(), set()):
            issues.append(f"原始列 {name} 必须使用指定别名 {alias[1]}")
    return issues
