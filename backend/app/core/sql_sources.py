"""Conservative source identity from executed SQL, independent of descriptions."""
from functools import lru_cache

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import build_scope


def _columns(sql, dialect):
    statements = [statement for statement in sqlglot.parse(sql, read=dialect) if statement is not None]
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        return {}
    tree = statements[0]
    scope = build_scope(tree)
    if scope is None or any(table.db or table.catalog for table in tree.find_all(exp.Table)):
        return {}

    def name(identifier):
        if not isinstance(identifier, exp.Identifier):
            return None
        return identifier.name if dialect == "postgres" and identifier.args.get("quoted") else identifier.name.lower()

    sources = {}
    for source in scope.sources.values():
        if not isinstance(source, exp.Table) or not isinstance(source.this, exp.Identifier):
            return {}
        alias = source.args.get("alias")
        if alias and alias.args.get("columns"):
            return {}
        key = name(alias.this if alias else source.this)
        if not key or key in sources:
            return {}
        sources[key] = name(source.this)
    if any(isinstance(p, exp.Star) or isinstance(p, exp.Column) and p.is_star for p in tree.selects):
        return {}
    result, seen = {}, set()
    for projection in tree.selects:
        output = projection.alias_or_name
        output_id = projection.args.get("alias") if isinstance(projection, exp.Alias) else projection.args.get("this")
        if dialect == "postgres" and isinstance(output_id, exp.Identifier):
            output = name(output_id)
        if output in seen:
            result.pop(output, None)
            continue
        seen.add(output)
        column = projection.this if isinstance(projection, exp.Alias) else projection
        if not isinstance(column, exp.Column) or column.db or column.catalog:
            continue
        table = sources.get(name(column.args.get("table"))) if column.table else (
            next(iter(sources.values())) if len(sources) == 1 else None)
        if table and name(column.this):
            result[output] = (table, name(column.this))
    return result


@lru_cache(maxsize=256)
def source_columns(sql):
    """The wire has no dialect tag; accept only bindings both supported dialects agree on.

    Ambiguous joins, derived columns, CTEs, stars and unknown schema mappings
    carry no physical identity here. Their execution values are still checked
    separately; absence of an optional description is not a forged value.
    """
    try:
        sqlite, postgres = _columns(sql, "sqlite"), _columns(sql, "postgres")
        return tuple((field, origin) for field, origin in sqlite.items() if postgres.get(field) == origin)
    except (sqlglot.errors.SqlglotError, ValueError, TypeError, AttributeError):
        return ()
