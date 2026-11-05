"""
表概况的查询拼装：只聚合，不取行。

预览回答「这张表里有哪些行」，概况回答「这张表整体长什么样」——一共多少行、每个字段
填了多少、取值怎么分布、记录落在哪些月份。页面拿它画分布条和时间轴，点一下就是一个
筛选条件：不用写 SQL，也能先把一张表看个大概。

规矩和预览是同一套（见 `preview` 模块），这里一条都不放宽：

- 标识符只来自调用方给的清单（已经按权限裁过），按方言加引号；
- 请求里的搜索词与筛选值全部走绑定参数，WHERE 用的就是预览那一个 `_where`——
  同一组条件下，这里数出来的行数和预览翻出来的行必须对得上；
- 执行仍然交给问答用的执行器，授权关卡、只读校验、超时原样生效。

多出来的一条是**取回来的东西有上限**在聚合上的样子：一列最多取回
`MAX_FACET_VALUES` 个不同的取值，一条时间轴最多 `MAX_TIMELINE_BUCKETS` 个月，
取值本身截到 `MAX_FACET_VALUE_CHARS` 个字符。聚合扫的是整张表，跑多久由超时管。

**一个字段自己的筛选条件，不参与它自己那张分布图。** 按「状态 = 已交付」筛过之后，
状态这一栏如果也跟着只剩「已交付」一行，就没法换成别的状态了；所以每个字段的分布
按「其余条件都生效、它自己的不算」来数（常说的分面搜索）。行数、填充数、求和这些
说的是「当前看到的这批记录」，全部条件都算。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Dict, FrozenSet, List, Mapping, Sequence, Tuple

from .preview import (
    BINARY,
    DATE,
    NUMBER,
    OTHER,
    TEXT,
    PreviewColumn,
    PreviewQuery,
    Statement,
    _PLACEHOLDER,
    _finish,
    _is_compact,
    _where,
    quote_identifier,
)

MAX_PROFILE_COLUMNS = 80
"""一次概况最多统计多少个字段的填充数。再宽的表只统计前这么多列，响应里标明。"""

MAX_SUM_COLUMNS = 12

MAX_FACETS = 6
"""一张表最多给多少个字段算取值分布。每个字段是一条 GROUP BY，各扫一遍表。"""

MAX_FACET_VALUES = 12
"""一个字段最多取回多少个不同的取值（按出现次数从多到少）。"""

MAX_FACET_VALUE_CHARS = 120

MAX_TIMELINES = 2

MAX_TIMELINE_BUCKETS = 240
"""时间轴最多多少个月（20 年）。再长的只给最早的这么多个月，响应里标明截过。"""

# 语义层里「拿来分组、筛选」的角色。指标、时间、长文本不在其中。
_FACET_ROLES = ("filter", "dimension", "output_dimension", "metric_filter")
_KEY_ROLES = ("join_key", "identifier")

_MONTH = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")


@dataclass(frozen=True)
class StatsLayout:
    """
    统计语句的结果怎么读：第 i 个字段对应 `f_i`（填充数），在 `ranged` 里的还有
    `lo_i` / `hi_i`，在 `summed` 里的还有 `sum_i`。
    """

    columns: Tuple[str, ...]
    ranged: FrozenSet[str]
    summed: FrozenSet[str]
    limited: bool
    """字段比 `MAX_PROFILE_COLUMNS` 多，后面的没有统计。"""


def declared_values(value_range: Any) -> Tuple[str, ...]:
    """
    语义层 `value_range` 里声明的取值清单。

    约定的写法是「待处理 / 处理中 / 已解决」（`pipeline.local_clients` 抽取枚举词表时
    认的是同一个写法）。写成范围的（`0-1`、`> 0`）不是清单，返回空。
    """
    text = str(value_range or "")
    if "/" not in text:
        return ()
    parts = tuple(part.strip() for part in text.split("/"))
    if len(parts) < 2 or any(not part or len(part) > MAX_FACET_VALUE_CHARS for part in parts):
        return ()
    return tuple(dict.fromkeys(parts))


def _owned(table: str, column: PreviewColumn) -> str:
    """带表名的列引用。聚合结果的别名（`v`、`n`…）可能和真实列重名，带上表名就没有歧义。"""
    return f"{quote_identifier(table)}.{quote_identifier(column.name)}"


def _native_date(column: PreviewColumn, dialect: str) -> bool:
    declared = column.data_type.strip().lower()
    return dialect == "postgres" and declared.startswith(("date", "timestamp"))


def _text_like(column: PreviewColumn, dialect: str) -> bool:
    """这一列里会不会有空字符串——有的话「空」要连它一起算，和预览的「为空」筛选一致。"""
    if column.kind == TEXT:
        return True
    if column.kind == DATE:
        return not _native_date(column, dialect)
    # SQLite 里没声明类型的列什么都可能存；PostgreSQL 的 json、数组等拿去和 '' 比会报错
    return column.kind == OTHER and dialect != "postgres"


def _present(table: str, column: PreviewColumn, dialect: str) -> str:
    """这一列「有内容」时是它自己，空字符串当成 NULL。"""
    owned = _owned(table, column)
    if not _text_like(column, dialect):
        return owned
    if dialect == "postgres":
        # uuid、char(n) 这些归在文本里的类型不能直接和 '' 比
        return f"NULLIF(CAST({owned} AS TEXT), '')"
    return f"NULLIF({owned}, '')"


def _has_value(table: str, column: PreviewColumn, dialect: str) -> str:
    owned = _owned(table, column)
    if not _text_like(column, dialect):
        return f"{owned} IS NOT NULL"
    return f"({owned} IS NOT NULL AND CAST({owned} AS TEXT) <> '')"


def without_own_filters(query: PreviewQuery, column: str) -> PreviewQuery:
    """同一组条件，去掉 `column` 自己的筛选。搜索词留着——它不属于哪一个字段。"""
    return replace(query, filters=[item for item in query.filters if item.column != column])


def has_own_filters(query: PreviewQuery, column: str) -> bool:
    return any(item.column == column for item in query.filters)


def sum_columns(
    columns: Sequence[PreviewColumn], roles: Mapping[str, str]
) -> List[PreviewColumn]:
    """
    哪些字段给合计。只有语义层标成指标（`metric`）的数字列：
    把编号、年份、不同计量单位的数量加在一起，得到的是一个看着像回事的错数。
    """
    chosen = [
        column
        for column in columns
        if column.kind == NUMBER and (roles.get(column.name) or "") == "metric"
    ]
    return chosen[:MAX_SUM_COLUMNS]


def facet_columns(
    columns: Sequence[PreviewColumn],
    roles: Mapping[str, str],
    declared: Mapping[str, Sequence[str]],
    documented: FrozenSet[str],
    dialect: str,
) -> List[PreviewColumn]:
    """
    给哪些字段算取值分布，按「多半有用」的程度排：

    1. 语义层声明了取值清单的（状态、类型）；
    2. 角色是筛选 / 维度的；
    3. 关联键（一张明细表里「每个订单几行」）；
    4. 语义层没写说明的文本列——没有人告诉我们它是什么，只能试。

    单列主键不算：每个值恰好一行，分布没有内容。日期走时间轴，二进制与长文本不参与。
    """
    keys = [column for column in columns if column.primary_key]
    sole_key = keys[0].name if len(keys) == 1 else ""

    ranked: List[Tuple[int, int, PreviewColumn]] = []
    for position, column in enumerate(columns):
        if column.name == sole_key or column.kind in (BINARY, DATE):
            continue
        if column.kind == OTHER and dialect == "postgres" and not _is_compact(column):
            continue  # json、几何类型没有分组用的比较运算符
        role = (roles.get(column.name) or "").strip().lower()
        if declared.get(column.name):
            tier = 0
        elif role in _FACET_ROLES:
            tier = 1
        elif role in _KEY_ROLES:
            tier = 2
        elif column.name not in documented and column.kind == TEXT:
            tier = 3
        else:
            continue
        ranked.append((tier, position, column))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [column for _tier, _position, column in ranked[:MAX_FACETS]]


def timeline_columns(columns: Sequence[PreviewColumn]) -> List[PreviewColumn]:
    return [column for column in columns if column.kind == DATE][:MAX_TIMELINES]


def build_stats_statement(
    table: str,
    columns: Sequence[PreviewColumn],
    query: PreviewQuery,
    dialect: str,
    *,
    summed: Sequence[PreviewColumn] = (),
) -> Tuple[Statement, StatsLayout]:
    """
    一遍扫描：总行数、每个字段的填充数、数字与日期列的最小最大值、指标列的合计。

    平均值不在 SQL 里算（合计 ÷ 填充数页面自己除）：PostgreSQL 的 `money` 可以求和，
    没有 `avg`，为一个可有可无的数让整条语句失败不值得。
    """
    where, params = _where(columns, query, dialect)
    profiled = list(columns[:MAX_PROFILE_COLUMNS])
    summed_names = {column.name for column in summed}

    parts = ['COUNT(*) AS "total_rows"']
    ranged: List[str] = []
    for index, column in enumerate(profiled):
        if column.kind == BINARY:
            # 只问有没有，不碰内容
            parts.append(f'COUNT({_owned(table, column)}) AS "f_{index}"')
            continue
        present = _present(table, column, dialect)
        parts.append(f'COUNT({present}) AS "f_{index}"')
        if column.kind in (NUMBER, DATE):
            parts.append(f'MIN({present}) AS "lo_{index}"')
            parts.append(f'MAX({present}) AS "hi_{index}"')
            ranged.append(column.name)
        if column.name in summed_names and column.kind == NUMBER:
            # SQLite 的 SUM 在整数溢出时整条语句报错；TOTAL 按浮点加，不会
            aggregate = "SUM" if dialect == "postgres" else "TOTAL"
            parts.append(f'{aggregate}({_owned(table, column)}) AS "sum_{index}"')

    sql = f"SELECT {', '.join(parts)} FROM {quote_identifier(table)}{where}"
    layout = StatsLayout(
        columns=tuple(column.name for column in profiled),
        ranged=frozenset(ranged),
        summed=frozenset(summed_names & {column.name for column in profiled}),
        limited=len(columns) > MAX_PROFILE_COLUMNS,
    )
    return _finish(sql, params, dialect), layout


def _and(where: str, clause: str) -> str:
    return f"{where} AND {clause}" if where else f" WHERE {clause}"


def build_facet_statement(
    table: str,
    column: PreviewColumn,
    columns: Sequence[PreviewColumn],
    query: PreviewQuery,
    dialect: str,
) -> Statement:
    """
    一个字段的取值分布：出现次数最多的 `MAX_FACET_VALUES` 个取值（多要一个，用来判断
    是不是全了），每行带上不同取值的总数 `k`（窗口函数，算在 LIMIT 之前）。

    按**原值**分组，取回时才截断：截断之后再分组的话，两个只有后半段不同的长值会被
    数成一个。
    """
    where, params = _where(columns, without_own_filters(query, column.name), dialect)
    owned = _owned(table, column)
    cap = MAX_FACET_VALUE_CHARS + 1
    if column.kind == NUMBER:
        value = owned
    elif dialect == "postgres":
        value = f"LEFT(CAST({owned} AS TEXT), {cap})"
    else:
        value = (
            f"CASE WHEN TYPEOF({owned}) IN ('text', 'blob') "
            f"THEN SUBSTR({owned}, 1, {cap}) ELSE {owned} END"
        )
    sql = (
        f'SELECT {value} AS "v", COUNT(*) AS "n", COUNT(*) OVER () AS "k"'
        f" FROM {quote_identifier(table)}{_and(where, _has_value(table, column, dialect))}"
        f" GROUP BY {owned} ORDER BY COUNT(*) DESC, {owned} ASC LIMIT {_PLACEHOLDER}"
    )
    return _finish(sql, [*params, MAX_FACET_VALUES + 1], dialect)


def build_timeline_statement(
    table: str,
    column: PreviewColumn,
    columns: Sequence[PreviewColumn],
    query: PreviewQuery,
    dialect: str,
) -> Statement:
    """
    一个日期字段按月的记录数，从最早的月份起。

    PostgreSQL 的日期 / 时间戳列用 `TO_CHAR` 取月份（不受 DateStyle 影响）；存成文本的
    日期取前 7 个字符——ISO 写法下那就是 `YYYY-MM`，不是这个写法的由调用方剔出去另计。
    """
    where, params = _where(columns, without_own_filters(query, column.name), dialect)
    owned = _owned(table, column)
    if _native_date(column, dialect):
        bucket = f"TO_CHAR({owned}, 'YYYY-MM')"
    else:
        bucket = f"SUBSTR(CAST({owned} AS TEXT), 1, 7)"
    sql = (
        f'SELECT {bucket} AS "v", COUNT(*) AS "n"'
        f" FROM {quote_identifier(table)}{_and(where, _has_value(table, column, dialect))}"
        f" GROUP BY {bucket} ORDER BY {bucket} ASC LIMIT {_PLACEHOLDER}"
    )
    return _finish(sql, [*params, MAX_TIMELINE_BUCKETS + 1], dialect)


def order_facet_values(
    values: List[Dict[str, Any]], declared: Sequence[str], complete: bool
) -> Tuple[List[Dict[str, Any]], str]:
    """
    取值怎么排 → `(排好的取值, "declared" | "count")`。

    语义层声明了清单、而且库里实际出现的取值都在清单里时，按声明的顺序排（状态的先后
    是人排过的），清单里有、这批记录里没有的补一个 0。只要有一个取值不在清单里，
    就说明清单和库对不上——那就照实际的排，不拿一份对不上的清单去套。
    """
    if not declared or not complete:
        return values, "count"
    by_text = {str(item["value"]): item for item in values}
    if len(by_text) != len(values) or not set(by_text) <= set(declared):
        return values, "count"
    return [by_text.get(name) or {"value": name, "count": 0} for name in declared], "declared"


def split_timeline(rows: Sequence[Mapping[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """查询结果 → `(按月的桶, 认不出月份的记录数)`。"""
    buckets: List[Dict[str, Any]] = []
    unparsed = 0
    for row in rows:
        period = "" if row.get("v") is None else str(row.get("v"))
        try:
            count = int(row.get("n") or 0)
        except (TypeError, ValueError):
            count = 0
        if _MONTH.match(period):
            buckets.append({"period": period, "count": count})
        else:
            unparsed += count
    return buckets, unparsed
