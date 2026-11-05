"""
把结果列接回语义层：单位（契约 §4.6.5）与来源记录（§4.6.6）。

两件事共用同一个前提——**知道每一列是从哪张表的哪个字段来的**。SQL 的 AST 里
有这个信息，`ai_sql_agent.auth.sql_analysis` 已经为了授权把它解出来了，这里复用同一
份解析。

为什么要做：服务端不下发币种时，调用方只能在自己那边配一个全局
`SQL_AGENT_CURRENCY` 顶上。它的注释自己写着「接美国厂的库时，报给 builder 的
金额就带了错误的币种」——这是本批缺口里唯一一条已经出过业务事故的。全局常量
在「一个 agent 接多个库」或「一个库里同时有 USD 与 CNY 列」时**必然**错，
而且静默：报出去的金额看起来完全正常。

判不出来就不给，绝不猜。契约 §4.6.5 明确：缺失表示「单位未知」，调用方不得
擅自补币种符号，也不得回退到自己的全局常量。猜一个填进去比留空危险得多。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

# 保留单位的包装：对一列金额求和、取均值、取极值，结果还是那个币种。
# **COUNT 不在里面**——计数的单位是「条」，不是美元。除法、CAST、四则运算
# 同理都会改变单位，所以一概不认。
_UNIT_PRESERVING = {"sum", "avg", "min", "max", "round", "coalesce", "abs"}

MAX_SOURCE_KEYS = 50
"""单张表最多回传多少条主键。溯源要的是「能查回去」，不是把结果集抄一遍。"""


def _output_column_sources(sql: str, dialect: str) -> Dict[str, "tuple"]:
    """
    输出列名 → (表名, 原始列名)。

    只认「一路包下来底下只有一个列引用」的表达式：`amount`、`SUM(amount)`、
    `ROUND(AVG(unit_price), 2) AS avg_price` 都算；`COUNT(*)`、`a - b`、
    `amount / qty` 都不算——它们的单位不等于底下那一列的单位。
    """
    try:
        import sqlglot
        from sqlglot import exp
        from sqlglot.optimizer.scope import build_scope
    except ImportError:  # pragma: no cover
        return {}

    try:
        tree = sqlglot.parse_one(sql, read=dialect or None)
        root = build_scope(tree)
    except Exception:
        return {}
    if root is None:
        return {}

    # 别名 -> 真实表名，取最外层作用域即可：输出列是它选出来的
    sources: Dict[str, str] = {}
    for alias, source in root.sources.items():
        if isinstance(source, exp.Table) and source.name:
            sources[(alias or source.name).lower()] = source.name.lower()
            sources.setdefault(source.name.lower(), source.name.lower())

    single_table = sources and len(set(sources.values())) == 1
    only_table = next(iter(sources.values())) if single_table else ""

    mapping: Dict[str, tuple] = {}
    for projection in root.expression.selects if hasattr(root.expression, "selects") else []:
        output_name = projection.alias_or_name
        if not output_name or output_name == "*":
            continue
        column = _unwrap_single_column(projection, exp)
        if column is None:
            continue
        qualifier = (column.table or "").lower()
        table = sources.get(qualifier) if qualifier else only_table
        if not table:
            continue
        mapping[output_name] = (table, column.name)
    return mapping


def _unwrap_single_column(node, exp):
    """剥掉别名与保留单位的包装，看底下是不是**恰好一个**列引用。"""
    current = node
    while True:
        if isinstance(current, exp.Alias):
            current = current.this
            continue
        if isinstance(current, exp.Column):
            return current if not isinstance(current.this, exp.Star) else None
        name = getattr(current, "key", "")
        if name in _UNIT_PRESERVING:
            inner = list(current.find_all(exp.Column))
            if len(inner) != 1:
                return None
            candidate = inner[0]
            return candidate if not isinstance(candidate.this, exp.Star) else None
        return None


def column_units(
    semantic: Any, *, database: str, sql: str, dialect: str, columns: Sequence[str]
) -> Dict[str, str]:
    """结果列 → 单位标识。判不出来的列不出现在返回值里。"""
    if semantic is None or not columns:
        return {}
    lookup = getattr(semantic, "column_unit", None)
    if not callable(lookup):
        return {}

    mapping = _output_column_sources(sql, dialect)
    units: Dict[str, str] = {}
    for column in columns:
        source = mapping.get(column)
        if source is None:
            continue
        table, origin = source
        unit = lookup(database, table, origin)
        if unit:
            units[column] = unit
    return units


def dominant_currency(units_per_step: Sequence[Dict[str, str]]) -> Optional[str]:
    """
    本轮的币种（契约 §4.6.5 的顶层 `currency`）。

    只有当所有识别出来的币种**完全一致**时才给一个值；出现两种就返回 None，
    让调用方逐列去读 `column_units`。多币种时给一个「主要币种」正是那个全局
    常量犯的错，不能在服务端重犯一次。
    """
    found = {
        unit
        for units in units_per_step
        for unit in units.values()
        if len(unit) == 3 and unit.isupper() and unit.isalpha()
    }
    if len(found) == 1:
        return next(iter(found))
    return None


def source_records(
    semantic: Any,
    *,
    database: str,
    sql: str,
    dialect: str,
    columns: Sequence[str],
    rows: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    这批数字是从哪几条业务记录来的（契约 §4.6.6）。

    只在结果集里**真的带着主键列**时才给得出来——不带主键的聚合结果追不到记录，
    那种情况下溯源手段仍然是 `trace_id` + `final_sql`。给不出来就不给，
    不编一个近似的出来。
    """
    if semantic is None or not rows:
        return []
    key_lookup = getattr(semantic, "primary_key_columns", None)
    if not callable(key_lookup):
        return []

    mapping = _output_column_sources(sql, dialect)
    # 输出列名 → 原始列名，按表分组
    per_table: Dict[str, Dict[str, str]] = {}
    for output_name, (table, origin) in mapping.items():
        if output_name in columns:
            per_table.setdefault(table, {})[origin] = output_name

    records: List[Dict[str, Any]] = []
    for table, origin_to_output in sorted(per_table.items()):
        key_columns = key_lookup(database, table)
        if not key_columns:
            continue
        if not all(key in origin_to_output for key in key_columns):
            # 主键没全带出来，拼不出能查回去的标识
            continue
        keys: List[Any] = []
        seen = set()
        for row in rows:
            value = tuple(row.get(origin_to_output[key]) for key in key_columns)
            if value in seen:
                continue
            seen.add(value)
            keys.append(value[0] if len(value) == 1 else list(value))
            if len(keys) >= MAX_SOURCE_KEYS:
                break
        if keys:
            records.append({
                "database": database,
                "table": table,
                "key_columns": list(key_columns),
                "keys": keys,
                "truncated": len(seen) >= MAX_SOURCE_KEYS,
            })
    return records
