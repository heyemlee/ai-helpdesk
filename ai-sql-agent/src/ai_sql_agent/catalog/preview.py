"""
预览查询的拼装：单表、只读、列名来自清单、值全部走绑定参数。

这个模块只做一件事——把「看哪张表、搜什么、按哪列筛、翻到第几页」变成一条
SQL 加一组参数。它不连数据库，也不判权限：权限在两处判，都不在这里。
调用方（`service.DataCatalog`）先把无权的表和列从清单里拿掉，所以传进来的
`columns` 已经是「这个人能看到的列」；执行器入口的授权关卡再按 SQL 的 AST
独立判一次。这里的职责是保证**请求里的任何字符串都进不了 SQL 文本**：

- 标识符（表名、列名）只接受清单里有的，按方言加双引号；
- 运算符是闭集（`OPERATORS`），对应到写死的 SQL 片段；
- 搜索词、筛选值、LIMIT、OFFSET 一律是占位符。

另一件要在这里保证的事是**取回来的东西有上限**：长文本在 SQL 里截到
`MAX_CELL_CHARS`，二进制列只取字节数。取回来再截是不够的——一页 100 行、
每格几兆的附件列，取回来的那一刻内存就已经花出去了，超时也来不及触发。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Sequence, Tuple

from ai_sql_agent.execution.sql_guard import check_readonly_sql

OPERATORS = (
    "eq",
    "ne",
    "contains",
    "starts_with",
    "gt",
    "gte",
    "lt",
    "lte",
    "is_empty",
    "not_empty",
)

MAX_FILTERS = 5
MAX_SEARCH_LENGTH = 100
MAX_VALUE_LENGTH = 200

MAX_SEARCH_COLUMNS = 12
"""搜索词最多比对多少列。宽表上对每一列都做一次 LIKE 就是一次全表多列扫描。"""

MAX_DATE_RANGE_COLUMNS = 3

MAX_CELL_CHARS = 2000
"""一个单元格最多取回多少个字符；超出的在 SQL 里就截掉，响应里标明截过。"""

_LIKE_ESCAPE = "!"
_COMPARISONS = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
_EQUALITY = {"eq": "=", "ne": "<>"}
_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1

# 拼 SQL 时占位符先写成这个哨兵，最后按方言换成 `?` / `%s`。
# 这样「哪些是占位符」在整个拼装过程中没有歧义，不靠数问号。
_PLACEHOLDER = "\x00"

NUMBER = "number"
DATE = "date"
TEXT = "text"
BINARY = "binary"
OTHER = "other"

_INTEGER_TEXT = re.compile(r"^[+-]?\d+$")
_DECIMAL_TEXT = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?$")

# PostgreSQL 里归在「其他」、但值本身很小的类型：原样取回，不必转成文本再截
_PG_COMPACT_TYPES = ("bool", "interval", "inet", "cidr", "macaddr", "oid", "bit")


@dataclass(frozen=True)
class PreviewColumn:
    """清单里的一列：名字来自真实表结构，`kind` 决定筛选值怎么比、取回时怎么截。"""

    name: str
    kind: str = OTHER
    primary_key: bool = False
    """
    只有**整组主键都在清单里**时才置真（调用方保证）。联合主键被权限挡掉一半，
    剩下那一半并不唯一，拿它当排序兜底等于没有兜底。
    """

    data_type: str = ""


@dataclass(frozen=True)
class PreviewFilter:
    column: str
    op: str
    value: Any = ""


@dataclass
class PreviewQuery:
    page: int = 1
    page_size: int = 25
    search: str = ""
    filters: List[PreviewFilter] = field(default_factory=list)
    sort_column: str = ""
    sort_descending: bool = False


class PreviewInputError(ValueError):
    """请求本身不成立（列不在清单里、值不是数字……）。对应 HTTP 400。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Statement:
    sql: str
    params: Tuple[Any, ...]


def value_kind(data_type: str, semantic_role: str = "") -> str:
    """
    把库里声明的类型归成几类，决定对齐方式、筛选值的比较方式和取回时的上限。

    SQLite 把日期存成 TEXT，声明类型里看不出来；语义层标了 `time` 的字段
    按日期处理——那是人写下来的，比声明类型可靠。
    """
    declared = (data_type or "").strip().lower()
    if any(token in declared for token in ("blob", "bytea", "binary")):
        return BINARY  # 先于语义角色：二进制列不管标成什么，都不能整格取回
    if (semantic_role or "").strip().lower() == "time":
        return DATE
    if not declared or declared == "unknown":
        return OTHER
    if declared.startswith("interval") or "point" in declared:
        return OTHER  # 名字里带 int，但不是能和数字比大小的类型
    if "date" in declared or "time" in declared:
        return DATE
    if any(token in declared for token in ("int", "real", "floa", "doub", "num", "dec", "money", "serial")):
        return NUMBER
    if any(token in declared for token in ("char", "text", "clob", "string", "uuid")):
        return TEXT
    return OTHER


def supported_identifier(name: str, dialect: str) -> bool:
    """
    这个名字能不能写进预览 SQL 并且真的跑得起来。

    双引号可以转义，所以空格、中文、大小写都没问题。两种不行：

    - NUL：我们自己的占位符哨兵；
    - PostgreSQL 下的 `%`：psycopg 带参数执行时会解释 SQL 文本里的 `%`，把它写成
      `%%` 能执行，但授权关卡解析到的列名就成了另一个名字。**授权看到的 SQL 必须
      和数据库执行的是同一条**，所以不支持，不绕。

    撞上 SQL 关键字的名字（`update`、`Last Update`）不在其中：执行器的只读校验
    按语法树判，引号里的标识符不是关键字（2026-10 之前它按词扫整条语句，这样的
    名字会被拒）。最后仍然拿同一道校验试一遍，而不是在这里另列规则——放不放行
    以校验为准，目录与执行器不会各说各话。
    """
    if not name or _PLACEHOLDER in name:
        return False
    if dialect == "postgres" and "%" in name:
        return False
    return check_readonly_sql(f"SELECT {quote_identifier(name)}", dialect=dialect).allowed


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def is_sortable(column: PreviewColumn, dialect: str) -> bool:
    """能不能按这一列排序。二进制列不排；PostgreSQL 的 json、几何类型没有排序运算符。"""
    if column.kind == BINARY:
        return False
    if column.kind == OTHER and dialect == "postgres":
        return _is_compact(column)
    return True


def has_stable_order(columns: Sequence[PreviewColumn]) -> bool:
    """翻页顺序是否确定。只有整组主键可见时才是——见 `_order_by`。"""
    return any(column.primary_key for column in columns)


def search_targets(columns: Sequence[PreviewColumn]) -> Tuple[List[PreviewColumn], bool]:
    """
    搜索词比对哪几列，以及是不是因为列太多只比了前几列。

    先比声明成文本的列；一列文本都没有（类型未知、或者整张表都是数字）时退回
    其余列。二进制列从不参与：把几兆的附件转成文本去 LIKE 没有意义。
    """
    candidates = [column for column in columns if column.kind == TEXT]
    if not candidates:
        candidates = [column for column in columns if column.kind != BINARY]
    return candidates[:MAX_SEARCH_COLUMNS], len(candidates) > MAX_SEARCH_COLUMNS


def _is_compact(column: PreviewColumn) -> bool:
    return column.data_type.strip().lower().startswith(_PG_COMPACT_TYPES)


def _like_pattern(term: str, *, prefix_only: bool = False) -> str:
    escaped = "".join(
        _LIKE_ESCAPE + char if char in ("%", "_", _LIKE_ESCAPE) else char for char in term
    )
    return f"{escaped}%" if prefix_only else f"%{escaped}%"


def _as_text(column: PreviewColumn) -> str:
    return f"CAST({quote_identifier(column.name)} AS TEXT)"


def _byte_length(column: PreviewColumn, dialect: str) -> str:
    quoted = quote_identifier(column.name)
    if dialect == "postgres":
        return f"OCTET_LENGTH({quoted})"
    return f"LENGTH(CAST({quoted} AS BLOB))"


def _projection(column: PreviewColumn, dialect: str) -> str:
    """
    取回这一列的表达式，带上限。

    数字和日期原样取；二进制只取字节数（页面显示成「二进制，N 字节」）；
    其余截到 `MAX_CELL_CHARS + 1` 个字符——多取的那一个用来判断是不是截过。
    """
    quoted = quote_identifier(column.name)
    cap = MAX_CELL_CHARS + 1
    if column.kind == BINARY:
        return f"{_byte_length(column, dialect)} AS {quoted}"
    if column.kind in (NUMBER, DATE):
        return quoted
    if dialect == "postgres":
        if column.kind == OTHER and _is_compact(column):
            return quoted
        return f"LEFT(CAST({quoted} AS TEXT), {cap}) AS {quoted}"
    if column.kind == TEXT:
        return f"SUBSTR({quoted}, 1, {cap}) AS {quoted}"
    # SQLite 里没声明类型的列什么都可能存：只截文本和二进制，数字原样返回
    return (
        f"CASE WHEN TYPEOF({quoted}) IN ('text', 'blob') "
        f"THEN SUBSTR({quoted}, 1, {cap}) ELSE {quoted} END AS {quoted}"
    )


def _clean_text(value: str, what: str) -> str:
    """筛选值、搜索词里不该有控制字符：NUL 在 SQLite 里会截断 LIKE，在 psycopg 里直接报错。"""
    if any(ord(char) < 32 for char in value):
        raise PreviewInputError("invalid_value", f"{what}里有不可见的控制字符。")
    return value


def _number(value: Any, column: str) -> Any:
    """
    筛选值 → 绑定用的数字。整数**原样保留**：经过 float 的话，2^53 以上的编号
    会被悄悄改掉末几位，筛出来的是另一条记录。
    """
    invalid = PreviewInputError("invalid_value", f"{column} 的筛选值必须是数字。")
    if isinstance(value, bool):
        raise invalid
    if isinstance(value, (int, float)):
        number: Any = value
    else:
        text = str(value).strip().replace(",", "")
        if _INTEGER_TEXT.match(text):
            number = int(text)
        elif _DECIMAL_TEXT.match(text):
            number = float(text)
        else:
            raise invalid
    if isinstance(number, float):
        if math.isnan(number) or math.isinf(number):
            raise invalid
        return number
    if not _INT64_MIN <= number <= _INT64_MAX:
        raise PreviewInputError("invalid_value", f"{column} 的筛选值超出了能比较的范围。")
    return number


def _date_clause(
    column: PreviewColumn, op: str, text: str, dialect: str
) -> Tuple[str, List[Any]]:
    """
    日期列的比较。只写了日期（没写时刻）时按**整天**算：

        等于 3 月 31 日  →  >= 03-31 且 < 04-01
        不晚于 3 月 31 日 →  < 04-01

    库里存的往往带时刻（`2024-03-31 10:00`）。按字符串直接比的话，「不晚于 3 月 31 日」
    会把 31 日当天的记录全丢掉，「等于 3 月 31 日」一条也匹配不上。
    """
    quoted = quote_identifier(column.name)
    declared = column.data_type.strip().lower()
    native = dialect == "postgres" and declared.startswith(("date", "timestamp"))
    # PostgreSQL 的日期/时间戳列按原生类型比（走得上索引，也不受 DateStyle 影响）；
    # 其余情况（SQLite，或者存成文本的日期）按文本比，ISO 写法的字符串序就是时间序。
    left = quoted if native else _as_text(column)

    if _DATE_ONLY.match(text):
        try:
            day = date.fromisoformat(text)
            next_day = day + timedelta(days=1)
        except (ValueError, OverflowError):
            raise PreviewInputError(
                "invalid_value", f"{column.name} 的筛选值不是一个存在的日期。"
            ) from None
        value = f"CAST({_PLACEHOLDER} AS DATE)" if native else _PLACEHOLDER
        start, end = day.isoformat(), next_day.isoformat()
        if op == "eq":
            return f"({left} >= {value} AND {left} < {value})", [start, end]
        if op == "ne":
            return f"({left} < {value} OR {left} >= {value})", [start, end]
        if op == "gte":
            return f"{left} >= {value}", [start]
        if op == "gt":
            return f"{left} >= {value}", [end]
        if op == "lt":
            return f"{left} < {value}", [start]
        return f"{left} < {value}", [end]  # lte

    if _DATE_TIME.match(text):
        try:
            datetime.fromisoformat(text.replace(" ", "T"))
        except ValueError:
            raise PreviewInputError(
                "invalid_value", f"{column.name} 的筛选值不是一个存在的时刻。"
            ) from None
        value = f"CAST({_PLACEHOLDER} AS TIMESTAMP)" if native else _PLACEHOLDER
        operator = _EQUALITY.get(op) or _COMPARISONS[op]
        return f"{left} {operator} {value}", [text]

    if op in _EQUALITY and not native:
        # 不是 ISO 写法（库里存的可能是 03/31/2024）：等值还能按原样比，比大小就没有意义了
        return f"LOWER({left}) {_EQUALITY[op]} LOWER({_PLACEHOLDER})", [text]
    raise PreviewInputError(
        "invalid_value", f"{column.name} 的筛选值请写成 YYYY-MM-DD。"
    )


def _filter_clause(
    column: PreviewColumn, item: PreviewFilter, like: str, dialect: str
) -> Tuple[str, List[Any]]:
    quoted = quote_identifier(column.name)
    op = item.op

    if column.kind == BINARY:
        # 二进制列只能问「有没有内容」，而且按字节数问，不把内容转成文本
        size = _byte_length(column, dialect)
        if op == "is_empty":
            return f"({quoted} IS NULL OR {size} = 0)", []
        if op == "not_empty":
            return f"({quoted} IS NOT NULL AND {size} > 0)", []
        raise PreviewInputError(
            "invalid_value", f"{column.name} 是二进制字段，只能按有没有内容筛选。"
        )

    if op == "is_empty":
        return f"({quoted} IS NULL OR {_as_text(column)} = '')", []
    if op == "not_empty":
        return f"({quoted} IS NOT NULL AND {_as_text(column)} <> '')", []

    raw = item.value
    text = "" if raw is None else str(raw).strip()
    if not text:
        raise PreviewInputError("invalid_value", f"{column.name} 的筛选值不能为空。")
    if len(text) > MAX_VALUE_LENGTH:
        raise PreviewInputError("invalid_value", f"{column.name} 的筛选值太长。")
    _clean_text(text, f"{column.name} 的筛选值")

    if op in ("contains", "starts_with"):
        pattern = _like_pattern(text, prefix_only=op == "starts_with")
        return (
            f"{_as_text(column)} {like} {_PLACEHOLDER} ESCAPE '{_LIKE_ESCAPE}'",
            [pattern],
        )

    if column.kind == NUMBER:
        number = _number(raw, column.name)
        operator = _EQUALITY.get(op) or _COMPARISONS[op]
        return f"{quoted} {operator} {_PLACEHOLDER}", [number]

    if column.kind == DATE:
        return _date_clause(column, op, text, dialect)

    # 其余一律按文本比：等值不分大小写（人记不住 Hardware 还是 hardware），大小比较按字符串序
    if op in _EQUALITY:
        return (
            f"LOWER({_as_text(column)}) {_EQUALITY[op]} LOWER({_PLACEHOLDER})",
            [text],
        )
    return f"{_as_text(column)} {_COMPARISONS[op]} {_PLACEHOLDER}", [text]


def _where(
    columns: Sequence[PreviewColumn], query: PreviewQuery, dialect: str
) -> Tuple[str, List[Any]]:
    by_name: Dict[str, PreviewColumn] = {column.name: column for column in columns}
    like = "ILIKE" if dialect == "postgres" else "LIKE"
    clauses: List[str] = []
    params: List[Any] = []

    search = (query.search or "").strip()
    if len(search) > MAX_SEARCH_LENGTH:
        raise PreviewInputError("invalid_value", "搜索词太长。")
    if search:
        _clean_text(search, "搜索词")
        targets, _limited = search_targets(columns)
        if not targets:
            raise PreviewInputError("invalid_value", "这张表没有可以搜索的字段。")
        pattern = _like_pattern(search)
        clauses.append(
            "("
            + " OR ".join(
                f"{_as_text(column)} {like} {_PLACEHOLDER} ESCAPE '{_LIKE_ESCAPE}'"
                for column in targets
            )
            + ")"
        )
        params.extend([pattern] * len(targets))

    if len(query.filters) > MAX_FILTERS:
        raise PreviewInputError("too_many_filters", f"最多 {MAX_FILTERS} 个筛选条件。")
    for item in query.filters:
        column = by_name.get(item.column)
        if column is None:
            # 不在清单里：不存在，或者这个人看不到。两种情况给同一个回答。
            raise PreviewInputError("unknown_column", "筛选列不在这张表的可见字段里。")
        if item.op not in OPERATORS:
            raise PreviewInputError("unknown_operator", "不支持的筛选方式。")
        clause, values = _filter_clause(column, item, like, dialect)
        clauses.append(clause)
        params.extend(values)

    if not clauses:
        return "", []
    return " WHERE " + " AND ".join(clauses), params


def _finish(sql: str, params: Sequence[Any], dialect: str) -> Statement:
    marker = "%s" if dialect == "postgres" else "?"
    return Statement(sql=sql.replace(_PLACEHOLDER, marker), params=tuple(params))


def _order_by(
    table: str, columns: Sequence[PreviewColumn], query: PreviewQuery, dialect: str
) -> str:
    """
    排序子句。排序必须确定，否则 OFFSET 翻页会漏行或重复：

    - 整组主键可见：用主键兜底，顺序唯一；
    - 否则：用**全部**能排序的可见列兜底。只在两行的这些列完全相同时顺序才不确定，
      而那样的两行在页面上本来也分不出来。

    列名前带表名：取回的列用同名别名包了一层截断（`SUBSTR("c", …) AS "c"`），
    ORDER BY 里的裸名字在 SQLite 和 PostgreSQL 里都会先解析成那个别名——
    排的就成了截断后的值。带上表名指的才是表里的原值。
    """
    by_name = {column.name: column for column in columns}
    owner = quote_identifier(table)
    terms: List[str] = []
    used: List[str] = []

    if query.sort_column:
        column = by_name.get(query.sort_column)
        if column is None:
            raise PreviewInputError("unknown_column", "排序列不在这张表的可见字段里。")
        if not is_sortable(column, dialect):
            raise PreviewInputError("invalid_value", f"{column.name} 这一列不支持排序。")
        direction = "DESC" if query.sort_descending else "ASC"
        terms.append(f"{owner}.{quote_identifier(column.name)} {direction}")
        used.append(column.name)

    tie_breakers = [column for column in columns if column.primary_key] or [
        column for column in columns if is_sortable(column, dialect)
    ]
    for column in tie_breakers:
        if column.name not in used:
            terms.append(f"{owner}.{quote_identifier(column.name)} ASC")
            used.append(column.name)

    return " ORDER BY " + ", ".join(terms) if terms else ""


def build_page_statement(
    table: str,
    columns: Sequence[PreviewColumn],
    query: PreviewQuery,
    dialect: str,
    *,
    limit: int,
    offset: int,
) -> Statement:
    """取一页。`limit` 由调用方给（它会多要一行，用来判断后面还有没有）。"""
    if not columns:
        raise PreviewInputError("no_columns", "这张表没有可以预览的字段。")
    where, params = _where(columns, query, dialect)
    select_list = ", ".join(_projection(column, dialect) for column in columns)
    sql = (
        f"SELECT {select_list} FROM {quote_identifier(table)}"
        f"{where}{_order_by(table, columns, query, dialect)}"
        f" LIMIT {_PLACEHOLDER} OFFSET {_PLACEHOLDER}"
    )
    return _finish(sql, [*params, int(limit), int(offset)], dialect)


def build_summary_statement(
    table: str,
    columns: Sequence[PreviewColumn],
    query: PreviewQuery,
    dialect: str,
) -> Tuple[Statement, List[str]]:
    """
    同一组筛选条件下的总行数，外加日期列的最早与最晚值。

    返回 `(语句, 日期列名)`；结果里第 i 个日期列对应 `min_i` / `max_i`。
    日期范围是给「这批记录是什么时候的」用的——读库的时刻不是业务数据的日期。
    """
    where, params = _where(columns, query, dialect)
    date_columns = [column for column in columns if column.kind == DATE][:MAX_DATE_RANGE_COLUMNS]
    parts = ['COUNT(*) AS "total_rows"']
    for index, column in enumerate(date_columns):
        quoted = quote_identifier(column.name)
        parts.append(f'MIN({quoted}) AS "min_{index}"')
        parts.append(f'MAX({quoted}) AS "max_{index}"')
    sql = f"SELECT {', '.join(parts)} FROM {quote_identifier(table)}{where}"
    return _finish(sql, params, dialect), [column.name for column in date_columns]
