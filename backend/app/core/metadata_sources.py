"""Physical column agreement for UNION ALL metadata, never record or scope proof.

None delegates an ordinary SELECT to its existing resolver. Compound queries
return only positions supported by every leaf, including branches with no rows.
"""
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import build_scope


def union_column_sources(sql, dialect):
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
        if len(statements) != 1:
            return {}
        tree = statements[0]
        if isinstance(tree, exp.Select):
            return None
        if dialect not in {"sqlite", "postgres", "postgresql"}:
            return {}
        if any(tree.find_all(exp.With)) or any(t.db or t.catalog for t in tree.find_all(exp.Table)):
            return {}

        def leaves(node):
            if isinstance(node, (exp.Subquery, exp.Paren)) and not node.args.get("alias"):
                return leaves(node.this)
            if isinstance(node, exp.Union) and node.args.get("distinct") is False and not node.args.get("by_name"):
                return [*leaves(node.this), *leaves(node.expression)]
            if isinstance(node, exp.Select):
                return [node]
            raise ValueError("unsupported set operand")

        def name(identifier):
            if not isinstance(identifier, exp.Identifier):
                return None
            if dialect in {"postgres", "postgresql"} and identifier.args.get("quoted"):
                return identifier.name
            return identifier.name.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))

        def positions(select):
            # Nested queries and derived sources need schema/lineage analysis
            # beyond this metadata-only capability.
            scope = build_scope(select)
            if scope is None or len(list(select.find_all(exp.Select))) != 1:
                raise ValueError("unsupported source")
            sources = {}
            for source in scope.sources.values():
                if not isinstance(source, exp.Table) or not isinstance(source.this, exp.Identifier):
                    raise ValueError("nonphysical source")
                alias = source.args.get("alias")
                if alias and alias.args.get("columns"):
                    raise ValueError("source column aliases")
                key = name(alias.this if alias else source.this)
                if not key or key in sources:
                    raise ValueError("ambiguous source")
                sources[key] = name(source.this)
            outputs, origins, seen = [], [], set()
            for projection in select.selects:
                if isinstance(projection, exp.Star) or isinstance(projection, exp.Column) and projection.is_star:
                    raise ValueError("unknown arity")
                identifier = projection.args.get("alias") if isinstance(projection, exp.Alias) else projection.args.get("this")
                canonical_output = name(identifier) if isinstance(identifier, exp.Identifier) else projection.alias_or_name
                if canonical_output in seen:
                    raise ValueError("duplicate output")
                seen.add(canonical_output)
                output = canonical_output if dialect in {"postgres", "postgresql"} else projection.alias_or_name
                column = projection.this if isinstance(projection, exp.Alias) else projection
                origin = None
                if isinstance(column, exp.Column) and not column.db and not column.catalog:
                    table = sources.get(name(column.args.get("table"))) if column.table else (
                        next(iter(sources.values())) if len(sources) == 1 else None)
                    if table and name(column.this):
                        origin = (table, name(column.this))
                outputs.append(output)
                origins.append(origin)
            return outputs, origins

        branches = [positions(leaf) for leaf in leaves(tree)]
        if len(branches) < 2 or not branches[0][0] or any(len(b[0]) != len(branches[0][0]) for b in branches):
            return {}
        return {field: origin for i, (field, origin) in enumerate(zip(*branches[0]))
                if field and origin is not None and all(origins[i] == origin for _, origins in branches[1:])}
    except (sqlglot.errors.SqlglotError, ValueError, TypeError, AttributeError, RecursionError):
        return {}


def metadata_columns(sql):
    """Optional meaning only; the existing source identity resolver is unchanged."""
    from app.core.sql_sources import source_columns

    sqlite = union_column_sources(sql, "sqlite")
    postgres = union_column_sources(sql, "postgres")
    if sqlite is None and postgres is None:
        return source_columns(sql)
    if sqlite is None or postgres is None:
        return ()
    return tuple((field, origin) for field, origin in sqlite.items() if postgres.get(field) == origin)
