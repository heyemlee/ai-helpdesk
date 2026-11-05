"""
目录的组装与预览的执行。

目录 = 语义层（人写的业务含义）× 业务库真实表结构 × 当前身份的表列权限。
三样东西各管一件事，缺哪样都如实反映在返回里，而不是悄悄补一个默认值：

| 情况 | 返回 |
| --- | --- |
| 库连不上 / 库文件不存在 | 这个域 `status=unavailable` + 原因码，**不给空表清单** |
| 语义层写了、库里没有 | 表或字段 `status=missing` |
| 库里有、语义层没写 | 照常列出，`documented=false`（模型一样能查到它） |
| 没有权限 | 不出现；`access_limited=true` 只说「有东西没给你看」，不说是什么 |
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ai_sql_agent.auth import authorization_scope
from ai_sql_agent.auth.identifiers import bind_dialect, stored_name
from ai_sql_agent.core import get_logger, get_settings, get_trace_id, log_event
from ai_sql_agent.execution import MCPExecutionRequest, get_dialect
from ai_sql_agent.retrieval.objects import ColumnSchema
from ai_sql_agent.retrieval.postgres_loader import PostgresSchemaLoader
from ai_sql_agent.retrieval.sqlite_loader import EmptyDatabaseFile, SQLiteSchemaLoader
from ai_sql_agent.semantic.objects import display_fields

from .preview import (
    BINARY,
    MAX_CELL_CHARS,
    PreviewColumn,
    PreviewInputError,
    PreviewQuery,
    build_page_statement,
    build_summary_statement,
    has_stable_order,
    is_sortable,
    search_targets,
    supported_identifier,
    value_kind,
)
from .profile import (
    MAX_FACET_VALUE_CHARS,
    MAX_FACET_VALUES,
    MAX_TIMELINE_BUCKETS,
    StatsLayout,
    build_facet_statement,
    build_stats_statement,
    build_timeline_statement,
    declared_values,
    facet_columns,
    has_own_filters,
    order_facet_values,
    split_timeline,
    sum_columns,
    timeline_columns,
)

_logger = get_logger("catalog")

MAX_INLINE_COLUMNS = 3000
"""
`/catalog` 一次带回全部字段的上限。四个库几十张表时一次给完最省事（页面拿它
给结果列配名字与单位）；几百张表的部署超过这个数就只给到表，字段按表另取。
"""

_PROBLEM_TTL_SECONDS = 15.0
"""
库不可用的结论缓存多久。比正常结论短（恢复之后不该等满一个周期），但必须明显长于
「发现它不可用」本身要花的时间：连不上的 PostgreSQL 要等满 5 秒连接超时才有结论，
结论只留 5 秒的话存进去就已经过期，等于每个请求都去等一遍。有效期从**读完**那一刻算起。
"""

_MIN_REFRESH_INTERVAL_SECONDS = 3.0
"""
`refresh=true` 最快多久真的重读一次。这个参数谁都能带，不设下限的话，
一个调用方就能让每次请求都去连一遍业务库（连不上时每次 5 秒）。
"""

_SUMMARY_TTL_SECONDS = 15.0
"""
同一张表、同一组筛选条件下的总行数与日期范围缓存多久。翻页、换排序都不改变它；
不缓存的话，大表上每点一次「下一页」都要再数一遍全表。
"""

_SUMMARY_FAILURE_TTL_SECONDS = 60.0
"""数不出来（通常是超时）时多久之内不再试。每试一次就是一条跑满超时的全表扫描。"""

_MAX_SUMMARY_ENTRIES = 512

_PROFILE_BUDGET_FACTOR = 2.0
"""
一次概况总共最多花「单条查询超时」的几倍时间。预算用完之后还没轮到的分组直接跳过
（`skipped` 里记 `budget`）：一张慢表不该让一个请求把连接占上半分钟。
"""

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_JS_SAFE_INTEGER = 2**53

# 常见业务系统的默认业务名。语义层里写了 `label` / `label_en` / `label_zh` 就用写的；
# 没写时按库标识符认系统类型——契约 §4.5 规定库标识符形如 `erp_db`，
# 所以「下划线前那一段」是稳定的。认不出来就不给名字，页面退回显示别名或标识符。
_DOMAIN_TITLES: Dict[str, Dict[str, str]] = {
    "erp": {"en": "Orders & Purchasing", "zh": "订单与采购"},
    "pm": {"en": "Projects & Costs", "zh": "项目与成本"},
    "mes": {"en": "Production Progress", "zh": "生产进度"},
    "wms": {"en": "Materials & Inventory", "zh": "物料与库存"},
}

STATUS_AVAILABLE = "available"
STATUS_UNAVAILABLE = "unavailable"
STATUS_MISSING = "missing"


class CatalogError(Exception):
    """目录接口的业务错误。`code` 给页面做文案映射，`message` 给人和日志看。"""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass
class _Structure:
    tables: Dict[str, List[ColumnSchema]] = field(default_factory=dict)
    problem: str = ""
    """
    空串表示读到了；否则是原因码：
    not_configured / file_not_found / empty_file / driver_missing / connection_failed。
    """

    expires_at: float = 0.0
    loaded_at: float = 0.0


@dataclass
class _Readable:
    """一张表里这个人读得到、也写得进 SQL 的那一部分（见 `DataCatalog._readable`）。"""

    executor: Any
    dialect: str
    actual: List[ColumnSchema]
    allowed: List[ColumnSchema]
    """有权限看的列。"""

    shown: List[ColumnSchema]
    """有权限看、名字也写得进预览 SQL 的列。"""

    columns: List[PreviewColumn]
    """`shown` 换成拼 SQL 用的样子。"""

    meta: Any
    """语义层里这张表的那一段；没登记时为 None。"""


def _allows_table(access: Any, database: str, table: str, dialect: str = "") -> bool:
    """
    这张表这个人能不能看。

    名字的大小写按这个库的方言比，和执行器入口的授权关卡是**同一条规则**
    （`ai_sql_agent.auth.identifiers`）：策略绑到方言上，表名用数据库里存的那个名字去问。
    目录说看得到的表，预览和提问时关卡就放行；目录里没有的，关卡也不会放。

    自定义的权限过滤器（没有 `for_dialect`）不归这条规则管，它自己决定怎么比名字。
    对它保守一点：大小写不敏感的库里，原名和规范名各问一次，两个都放行才算。
    """
    bound = bind_dialect(access, dialect)
    return all(bound.allows_table(database, name) for name in {table, stored_name(table, dialect)})


def _allows_column(
    access: Any, database: str, table: str, column: str, dialect: str = ""
) -> bool:
    bound = bind_dialect(access, dialect)
    return all(
        bound.allows_column(database, table_name, column_name)
        for table_name, column_name in {
            (table, column),
            (stored_name(table, dialect), stored_name(column, dialect)),
        }
    )


def _aliases(meta: Any) -> List[str]:
    if not isinstance(meta, dict):
        return []
    aliases = meta.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    return [str(item) for item in aliases if str(item).strip()]


def _domain_kind(name: str, aliases: Sequence[str]) -> str:
    tokens = set(re.split(r"[^a-z0-9]+", name.lower()))
    tokens.update(alias.strip().lower() for alias in aliases)
    for kind in _DOMAIN_TITLES:
        if kind in tokens:
            return kind
    return ""


def _cell(value: Any) -> Any:
    """把驱动返回的值变成 JSON 里不会失真的样子。"""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        # 超过 2^53 的整数在浏览器里会被悄悄改掉末几位，宁可给字符串
        return value if abs(value) <= _JS_SAFE_INTEGER else str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            return str(value)
        if value == value.to_integral_value():
            return _cell(int(value))
        approximate = float(value)
        return approximate if Decimal(repr(approximate)) == value else str(value)
    if isinstance(value, (datetime, date, clock_time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary, {len(value)} bytes>"
    if isinstance(value, (list, tuple)):
        return [_cell(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _cell(item) for key, item in value.items()}
    return str(value)


def _preview_cell(value: Any, column: PreviewColumn) -> Tuple[Any, bool]:
    """
    预览里的一个单元格 → `(给页面的值, 是不是截过)`。

    取回来的已经是带上限的：二进制列只取了字节数，长文本在 SQL 里截到了
    `MAX_CELL_CHARS + 1` 个字符（多的那一个就是「截过」的记号）。
    """
    if value is None:
        return None, False
    if column.kind == BINARY:
        if isinstance(value, int) and not isinstance(value, bool):
            return f"<binary, {value} bytes>", False
        return "<binary>", False
    if isinstance(value, (bytes, bytearray, memoryview)):
        # 声明成文本的列里存了二进制：取回的是截过的前一段，真实长度不知道
        size = len(value)
        if size > MAX_CELL_CHARS:
            return f"<binary, more than {MAX_CELL_CHARS} bytes>", False
        return f"<binary, {size} bytes>", False
    if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
        return value[:MAX_CELL_CHARS] + "…", True
    return _cell(value), False


def _column_stats(row: Dict[str, Any], layout: StatsLayout) -> List[Dict[str, Any]]:
    """统计语句的那一行 → 每个字段一条：填了多少、最小最大、合计。"""
    stats: List[Dict[str, Any]] = []
    for index, name in enumerate(layout.columns):
        try:
            filled = int(row.get(f"f_{index}") or 0)
        except (TypeError, ValueError):
            filled = 0
        entry: Dict[str, Any] = {"name": name, "filled": filled}
        if filled and name in layout.ranged:
            lowest, highest = row.get(f"lo_{index}"), row.get(f"hi_{index}")
            if lowest is not None and highest is not None:
                entry["min"], entry["max"] = _cell(lowest), _cell(highest)
        if filled and name in layout.summed and row.get(f"sum_{index}") is not None:
            entry["sum"] = _cell(row.get(f"sum_{index}"))
        stats.append(entry)
    return stats


def _facet(name: str, result: Any, declared: Sequence[str], own: bool) -> Dict[str, Any]:
    """分组语句的结果 → 一个字段的取值分布。"""
    values: List[Dict[str, Any]] = []
    distinct: Optional[int] = None
    for row in result.rows[:MAX_FACET_VALUES]:
        value = _cell(row.get("v"))
        try:
            count = int(row.get("n") or 0)
        except (TypeError, ValueError):
            count = 0
        item: Dict[str, Any] = {"value": value, "count": count}
        if isinstance(value, str) and len(value) > MAX_FACET_VALUE_CHARS:
            # 截过的取值只能看，不能拿去做「等于」筛选——它已经不是库里的那个值了
            item["value"] = value[:MAX_FACET_VALUE_CHARS] + "…"
            item["truncated"] = True
        values.append(item)
        if distinct is None:
            try:
                distinct = int(row.get("k"))
            except (TypeError, ValueError):
                distinct = None
    # 不同取值的总数来自窗口函数；万一没拿到，就按「取回的行有没有被截」判断
    complete = (
        distinct <= len(values)
        if distinct is not None
        else not result.truncated and len(result.rows) <= MAX_FACET_VALUES
    )
    ordered, order = order_facet_values(values, declared, complete)
    return {
        "column": name,
        "values": ordered,
        "distinct": distinct if distinct is not None else (len(values) if complete else None),
        "complete": complete,
        "ordered_by": order,
        "own_filters_excluded": own,
    }


class DataCatalog:
    """数据目录、只读预览与表概况。一个进程一个实例，挂在 `app.state.catalog`。"""

    def __init__(
        self,
        *,
        semantic: Any,
        databases: Sequence[Any],
        router: Any,
        clock=time.monotonic,
        views: Any = None,
    ):
        self.semantic = semantic
        self.databases = list(databases)
        self.router = router
        # 视图目录（`ai_sql_agent.auth.views`）：预览走的是同一道授权关卡，
        # 作用域里要带上它，关卡才认得出视图。没有就不展开。
        self.views = views
        self._clock = clock
        self._structures: Dict[str, _Structure] = {}
        self._lock = threading.Lock()
        # 每个库一把锁：同一时刻只让一个请求去读它的表结构，其余的等这一次的结果
        self._load_locks: Dict[str, threading.Lock] = {}
        self._summaries: Dict[Any, Tuple[float, Optional[int], List[Dict[str, Any]]]] = {}
        # 概况的短缓存：值是整份结果，或者统计语句失败时的那个错误
        self._profiles: Dict[Any, Tuple[float, Any]] = {}
        self._links = self._index_cross_database_links()

    @classmethod
    def from_pipeline(cls, pipeline: Any) -> "DataCatalog":
        """
        从 Pipeline 取语义层、库连接与执行路由。**取的是同一批对象**，不是再读一次配置：
        目录说「有这张表」，问答就得真的查得到它，两边不能各看各的元数据。
        """
        return cls(
            semantic=pipeline.semantic,
            databases=pipeline.databases,
            router=pipeline.mcp_router,
            views=getattr(pipeline, "view_catalog", None),
        )

    # ------------------------------------------------------------------
    # 目录
    # ------------------------------------------------------------------

    def describe(self, access: Any, *, refresh: bool = False) -> Dict[str, Any]:
        """当前身份能看到的全部业务域、表与字段。"""
        domains: List[Dict[str, Any]] = []
        limited = False
        for database in self.databases:
            domain, hidden = self._domain(database, access, refresh)
            limited = limited or hidden
            if domain is not None:
                domains.append(domain)

        column_count = sum(
            len(table["columns"]) for domain in domains for table in domain["tables"]
        )
        columns_included = column_count <= MAX_INLINE_COLUMNS
        if not columns_included:
            for domain in domains:
                for table in domain["tables"]:
                    table["column_count"] = len(table.pop("columns"))

        glossary, glossary_hidden = self._glossary(access)
        return {
            "source": "demo" if getattr(self.semantic, "is_demo", False) else "semantic_layer",
            "domains": domains,
            "access_limited": limited or glossary_hidden,
            "columns_included": columns_included,
            "glossary": glossary,
        }

    def _glossary(self, access: Any) -> Tuple[List[Dict[str, str]], bool]:
        """
        业务术语表，按权限裁剪。返回 `(条目, 是否有条目因为权限没给)`。

        定义是人写的一段话，里面常常直接写着字段名（「超预算指 actual_cost > budget_amt」）。
        原样全给的话，只开了采购库的人就从这里读到了项目库的字段名。所以逐条看：
        定义里提到的每一个表名、字段名，这个人都得在某处看得到，否则整条不给。
        判不出是不是字段名的词（普通英文单词）不影响——只认目录里真有的名字。
        """
        definitions = getattr(self.semantic, "metric_definitions", {}) or {}
        if not definitions:
            return [], False

        # 名字（小写）→ 这个人在某处看不看得到它。目录里所有的库名、表名、字段名都在里面，
        # 包括他看不到的——正是要靠它们认出「这条定义提到了他不该知道的东西」。
        visible: Dict[str, bool] = {}

        def note(name: str, allowed: bool) -> None:
            key = name.lower()
            visible[key] = visible.get(key, False) or allowed

        for database in self.databases:
            documented = self._documented_tables(database.name)
            with self._lock:
                cached = self._structures.get(database.name)
            actual = cached.tables if cached is not None else {}
            database_visible = False
            for table in set(documented) | set(actual):
                table_allowed = self._allows_table(access, database.name, table)
                database_visible = database_visible or table_allowed
                note(table, table_allowed)
                meta = documented.get(table)
                documented_columns = meta.get("columns") if isinstance(meta, dict) else None
                names = set(documented_columns) if isinstance(documented_columns, dict) else set()
                names.update(column.column_name for column in actual.get(table, []))
                for column in names:
                    note(column, self._allows_column(access, database.name, table, column))
            note(database.name, database_visible)

        translations = getattr(self.semantic, "metric_definitions_en", {}) or {}
        entries: List[Dict[str, str]] = []
        hidden = False
        for term, definition in definitions.items():
            english = translations.get(term) if isinstance(translations.get(term), dict) else {}
            # 英文版和原文一起过权限：两种写法里提到的名字都得是这个人看得到的
            text = " ".join([str(term), str(definition), *[str(value) for value in english.values()]])
            mentioned = {word.lower() for word in _IDENTIFIER.findall(text)}
            if any(visible.get(word) is False for word in mentioned):
                hidden = True
                continue
            entry = {"term": str(term), "definition": str(definition)}
            if english.get("term"):
                entry["term_en"] = english["term"]
            if english.get("definition"):
                entry["definition_en"] = english["definition"]
            entries.append(entry)
        return entries, hidden

    def describe_table(self, access: Any, *, database: str, table: str) -> Dict[str, Any]:
        """单张表的字段说明。`/catalog` 没带字段时（表太多）按表取。"""
        config = self._database(database)
        if config is None:
            raise _not_found()
        structure = self._structure(config)
        documented = self._documented_tables(database)
        if structure.problem:
            if not self._possibly_visible(access, database, documented):
                raise _not_found()
            raise CatalogError("unavailable", "这个数据域暂时读不到。", status=503)
        if table not in structure.tables and table not in documented:
            raise _not_found()
        entry, _hidden = self._table(
            database,
            table,
            documented.get(table),
            structure.tables.get(table),
            access,
            get_dialect(config.dialect).name,
        )
        if entry is None:
            raise _not_found()
        return entry

    def _domain(
        self, database: Any, access: Any, refresh: bool
    ) -> Tuple[Optional[Dict[str, Any]], bool]:
        name = database.name
        documented = self._documented_tables(name)
        structure = self._structure(database, refresh=refresh)
        header = self._domain_header(database)

        if structure.problem:
            # 读不到表结构，也就没法逐表判权限。只在这个人至少看得到语义层登记的
            # 某一张表时才告诉他「这个域不可用」——否则连库名都不该让他知道。
            if not self._possibly_visible(access, name, documented):
                return None, True
            return (
                {
                    **header,
                    "status": STATUS_UNAVAILABLE,
                    "status_reason": structure.problem,
                    "tables": [],
                },
                False,
            )

        # 语义层登记的顺序在前（那是人排过的），库里多出来的表按名字排在后面
        ordered = list(documented) + sorted(
            table for table in structure.tables if table not in documented
        )
        dialect = get_dialect(database.dialect).name
        tables: List[Dict[str, Any]] = []
        hidden = False
        for table in ordered:
            entry, table_hidden = self._table(
                name, table, documented.get(table), structure.tables.get(table), access, dialect
            )
            hidden = hidden or table_hidden
            if entry is not None:
                tables.append(entry)

        if not tables and hidden:
            return None, True  # 整个域都没有权限
        if not tables and not self._possibly_visible(access, name, documented):
            # 库里一张表都没有：没有表可以拿来逐张判权限，不等于「谁都可以看到这个库」
            return None, True
        return {**header, "status": STATUS_AVAILABLE, "status_reason": "", "tables": tables}, hidden

    def _domain_header(self, database: Any) -> Dict[str, Any]:
        semantics = self.semantic.databases.get(database.name)
        aliases = list(semantics.aliases) if semantics is not None else []
        display = dict(getattr(semantics, "display", {}) or {})
        kind = _domain_kind(database.name, aliases)

        title: Dict[str, str] = {}
        for language in ("en", "zh"):
            written = display.get(f"label_{language}") or display.get("label")
            if written:
                title[language] = written
        source = "semantic" if title else "name"
        for language, text in _DOMAIN_TITLES.get(kind, {}).items():
            if language not in title:
                title[language] = text
                source = "preset" if source == "name" else source

        return {
            "database": database.name,
            "kind": kind,
            "title": title,
            "title_source": source,
            "aliases": aliases,
            "description": semantics.description if semantics is not None else "",
            "description_en": display.get("description_en", ""),
            "description_zh": display.get("description_zh", ""),
            "dialect": get_dialect(database.dialect).name,
            "documented": bool(semantics is not None and semantics.tables),
            "preview_supported": self._supports_preview(database.name),
        }

    def _table(
        self,
        database: str,
        table: str,
        meta: Any,
        actual: Optional[List[ColumnSchema]],
        access: Any,
        dialect: str = "sqlite",
    ) -> Tuple[Optional[Dict[str, Any]], bool]:
        """返回 `(表条目, 是否有东西因为权限没给)`。没权限时条目为 None。"""
        if not self._allows_table(access, database, table):
            return None, True

        meta = meta if isinstance(meta, dict) else {}
        column_meta = meta.get("columns") if isinstance(meta.get("columns"), dict) else {}
        columns: List[Dict[str, Any]] = []
        hidden = False

        actual_names = {column.column_name for column in actual or []}
        for column in actual or []:
            if not self._allows_column(access, database, table, column.column_name):
                hidden = True
                continue
            entry = self._column(
                database, table, column.column_name,
                column_meta.get(column.column_name), column, access,
            )
            entry["previewable"] = supported_identifier(column.column_name, dialect)
            columns.append(entry)
        # 语义层写了、库里没有的字段：照样列出来并标明，这是元数据与库对不上的信号
        for name, item in column_meta.items():
            if name in actual_names:
                continue
            if not self._allows_column(access, database, table, name):
                hidden = True
                continue
            columns.append(self._column(database, table, name, item, None, access))

        if actual is not None and not any(
            column["status"] == STATUS_AVAILABLE for column in columns
        ):
            return None, True  # 表给看、列一列都不给看：等于没有可看的

        return (
            {
                "name": table,
                "status": STATUS_AVAILABLE if actual is not None else STATUS_MISSING,
                "documented": bool(meta),
                "aliases": _aliases(meta),
                **display_fields(meta),
                "description": str(meta.get("description") or ""),
                # 只列看得到的主键列：主键列被权限挡掉时，连它的名字也不该从这里漏出去
                "primary_key": [
                    column.column_name
                    for column in actual or []
                    if column.is_primary_key
                    and self._allows_column(access, database, table, column.column_name)
                ],
                "previewable": (
                    actual is not None
                    and self._supports_preview(database)
                    and supported_identifier(table, dialect)
                    and any(column.get("previewable") for column in columns)
                ),
                "columns": columns,
            },
            hidden,
        )

    def _column(
        self,
        database: str,
        table: str,
        name: str,
        meta: Any,
        actual: Optional[ColumnSchema],
        access: Any,
    ) -> Dict[str, Any]:
        meta = meta if isinstance(meta, dict) else {}
        role = str(meta.get("semantic_role") or "")
        data_type = actual.data_type if actual is not None else ""
        display = display_fields(meta)
        # 语义层里叫 business_usage，接口里一直叫 usage；分语言的写法跟着接口的叫法走，
        # 没写的不出现（与 description_en / description_zh 一致）
        usage = {
            f"usage_{language}": display.pop(f"business_usage_{language}")
            for language in ("en", "zh")
            if f"business_usage_{language}" in display
        }
        return {
            "name": name,
            "status": STATUS_AVAILABLE if actual is not None else STATUS_MISSING,
            "documented": bool(meta),
            "data_type": data_type,
            "kind": value_kind(data_type, role),
            "nullable": actual.nullable if actual is not None else None,
            "primary_key": bool(actual is not None and actual.is_primary_key),
            "previewable": False,
            "aliases": _aliases(meta),
            **display,
            "description": str(meta.get("description") or ""),
            "usage": str(meta.get("business_usage") or ""),
            **usage,
            "role": role,
            "unit": str(meta.get("unit") or ""),
            "value_range": str(meta.get("value_range") or ""),
            "references": self._references(database, table, name, actual, access),
        }

    def _references(
        self,
        database: str,
        table: str,
        column: str,
        actual: Optional[ColumnSchema],
        access: Any,
    ) -> List[Dict[str, str]]:
        """这个字段连到哪：库内外键 + 语义层登记的跨库关联。对端没权限的不给。"""
        found: List[Dict[str, str]] = []

        target = getattr(actual, "foreign_key_ref", None) if actual is not None else None
        if target and "." in target:
            target_table, target_column = target.split(".", 1)
            if self._allows_column(access, database, target_table, target_column):
                found.append({
                    "relation": "foreign_key",
                    "database": database,
                    "table": target_table,
                    "column": target_column,
                    "note": "",
                })

        for other, note, note_en in self._links.get(f"{database}.{table}.{column}", []):
            other_database, other_table, other_column = other
            if self._allows_column(access, other_database, other_table, other_column):
                reference = {
                    "relation": "cross_database",
                    "database": other_database,
                    "table": other_table,
                    "column": other_column,
                    "note": note,
                }
                if note_en:
                    reference["note_en"] = note_en
                found.append(reference)
        return found

    def _index_cross_database_links(self) -> Dict[str, List[Tuple[Tuple[str, str, str], str, str]]]:
        index: Dict[str, List[Tuple[Tuple[str, str, str], str, str]]] = {}
        for link in getattr(self.semantic, "cross_database_links", []) or []:
            if not isinstance(link, dict):
                continue
            left = str(link.get("left") or "").split(".")
            right = str(link.get("right") or "").split(".")
            if len(left) != 3 or len(right) != 3:
                continue
            note = str(link.get("meaning") or "")
            note_en = str(link.get("meaning_en") or "")
            index.setdefault(".".join(left), []).append((tuple(right), note, note_en))
            index.setdefault(".".join(right), []).append((tuple(left), note, note_en))
        return index

    # ------------------------------------------------------------------
    # 预览
    # ------------------------------------------------------------------

    def _readable(self, access: Any, database: str, table: str) -> _Readable:
        """
        预览与概况的共同入口：这张表在不在、这个人看不看得到、哪些列写得进 SQL。

        两处用的是同一份判定。概况要是自己再判一遍，迟早和预览判得不一样——
        那时候页面上的分布图里就会出现一个表格里看不到的字段。
        """
        config = self._database(database)
        if config is None:
            raise _not_found()

        structure = self._structure(config)
        documented = self._documented_tables(database)
        if structure.problem:
            if not self._possibly_visible(access, database, documented):
                raise _not_found()
            raise CatalogError("unavailable", "这个数据域暂时读不到。", status=503)

        actual = structure.tables.get(table)
        if actual is None or not self._allows_table(access, database, table):
            # 不存在与没权限给同一个回答：不让人靠 404 / 403 的差别探表名
            raise _not_found()

        executor = self.router.executors.get(database)
        if executor is None or not getattr(executor, "supports_params", False):
            raise CatalogError(
                "unsupported", "当前的执行方式不支持数据预览。", status=501
            )
        dialect = get_dialect(getattr(executor, "dialect", "") or config.dialect).name
        if not supported_identifier(table, dialect):
            raise CatalogError("unsupported", "这张表的名字不支持预览。", status=501)

        allowed = [
            column
            for column in actual
            if self._allows_column(access, database, table, column.column_name)
        ]
        shown = [
            column for column in allowed if supported_identifier(column.column_name, dialect)
        ]
        if not shown:
            raise _not_found()

        # 主键只在**整组**都取得出来时才拿来当排序兜底：联合主键缺一列就不唯一了
        key_names = {column.column_name for column in actual if column.is_primary_key}
        key_complete = bool(key_names) and key_names <= {
            column.column_name for column in shown
        }
        columns = [
            PreviewColumn(
                name=column.column_name,
                kind=value_kind(
                    column.data_type,
                    self._semantic_role(documented.get(table), column.column_name),
                ),
                primary_key=key_complete and column.is_primary_key,
                data_type=column.data_type or "",
            )
            for column in shown
        ]
        return _Readable(
            executor=executor,
            dialect=dialect,
            actual=actual,
            allowed=allowed,
            shown=shown,
            columns=columns,
            meta=documented.get(table),
        )

    def preview(
        self,
        access: Any,
        *,
        database: str,
        table: str,
        query: PreviewQuery,
        caller: str = "",
        user_id: str = "",
    ) -> Dict[str, Any]:
        """
        读一页真实数据。

        SQL 由 `preview` 模块按清单拼出来，执行交给问答用的同一个路由与执行器：
        授权关卡、只读校验、行数上限、超时都是那边的，这里只把上限再收紧一档。
        """
        settings = get_settings()
        target = self._readable(access, database, table)
        executor, dialect = target.executor, target.dialect
        actual, allowed, shown, columns = (
            target.actual, target.allowed, target.shown, target.columns
        )

        page_size = max(1, min(int(query.page_size), settings.catalog_preview_max_page_size))
        # 执行器自己的行数上限是硬顶，请求只能把它调小（`MCPExecutionRequest.row_cap`）。
        # 上限比页大小还低时，一页只给得出「上限」那么多行，而下一页的偏移照旧按页大小算——
        # 中间那一段哪一页都翻不到。所以页大小先压到上限以内，偏移和「还有下一页」都按它算。
        executor_cap = getattr(executor, "max_rows", None)
        if executor_cap is not None:
            page_size = max(1, min(page_size, int(executor_cap)))
        page = max(1, int(query.page))
        offset = (page - 1) * page_size
        if offset > 0 and offset + page_size > settings.catalog_preview_max_offset:
            raise CatalogError(
                "too_deep",
                f"预览最多翻到第 {settings.catalog_preview_max_offset} 行，请加筛选条件。",
            )

        try:
            # 多要一行：执行器按 max_rows 截断时会把 truncated 置真，正好就是「还有下一页」
            page_statement = build_page_statement(
                table, columns, query, dialect, limit=page_size + 1, offset=offset
            )
            summary_statement, date_columns = build_summary_statement(
                table, columns, query, dialect
            )
        except PreviewInputError as exc:
            raise CatalogError(exc.code, exc.message) from exc

        started = time.perf_counter()
        timeout = settings.catalog_preview_timeout_seconds
        with authorization_scope(
            access,
            caller=caller,
            user_id=user_id,
            trace_id=get_trace_id(),
            views=self.views,
        ):
            result = self.router.execute(
                MCPExecutionRequest(
                    database=database,
                    sql=page_statement.sql,
                    params=page_statement.params,
                    max_rows=page_size,
                    timeout_seconds=timeout,
                )
            )
            if not result.success:
                raise self._execution_error(database, table, result.error or "")
            total_rows, date_ranges = self._summary(
                key=(
                    database,
                    table,
                    caller,
                    user_id,
                    tuple(column.name for column in columns),
                    summary_statement.sql,
                    summary_statement.params,
                ),
                database=database,
                table=table,
                statement=summary_statement,
                date_columns=date_columns,
                timeout=timeout,
            )

        rows: List[List[Any]] = []
        cells_truncated = False
        for row in result.rows[:page_size]:
            cells: List[Any] = []
            for column in columns:
                cell, cut = _preview_cell(row.get(column.name), column)
                cells_truncated = cells_truncated or cut
                cells.append(cell)
            rows.append(cells)

        searching = bool((query.search or "").strip())
        searched, search_limited = search_targets(columns) if searching else ([], False)

        log_event(
            _logger,
            logging.INFO,
            "catalog.preview",
            caller=caller,
            user_id=user_id,
            database=database,
            table=table,
            page=page,
            page_size=page_size,
            rows=len(rows),
            total_rows=total_rows,
            filters=len(query.filters),
            searched=searching,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )

        return {
            "database": database,
            "table": table,
            "columns": [
                {
                    "name": column.name,
                    "kind": column.kind,
                    "sortable": is_sortable(column, dialect),
                }
                for column in columns
            ],
            "rows": rows,
            "page": page,
            "page_size": page_size,
            "offset": offset,
            "has_more": bool(result.truncated),
            "total_rows": total_rows,
            "date_ranges": date_ranges,
            # 有字段因为权限没给。只说「有」，不说是哪些、有几个
            "columns_hidden": len(allowed) < len(actual),
            # 看得到、但名字写不进预览 SQL 的字段：名字本来就是他能看到的，如实列出来
            "columns_unsupported": [
                column.column_name for column in allowed if column not in shown
            ],
            "cells_truncated": cells_truncated,
            "cell_limit": MAX_CELL_CHARS,
            "stable_order": has_stable_order(columns),
            "search_columns": [column.name for column in searched],
            "search_columns_limited": search_limited,
            "as_of": result.as_of or None,
            "as_of_source": result.as_of_source or None,
            "dialect": dialect,
            "sql": page_statement.sql,
            "trace_id": get_trace_id(),
        }

    # ------------------------------------------------------------------
    # 概况
    # ------------------------------------------------------------------

    def profile(
        self,
        access: Any,
        *,
        database: str,
        table: str,
        query: PreviewQuery,
        breakdowns: bool = True,
        caller: str = "",
        user_id: str = "",
    ) -> Dict[str, Any]:
        """
        一张表的概况：行数、各字段填了多少、取值分布、按月的记录数。

        只做聚合，不返回任何一行记录。能统计的字段就是预览里看得到的那些
        （`_readable`），筛选条件与预览同义，执行走同一个执行器——所以这里不会
        数出一个预览里翻不到的东西。`breakdowns=False` 时只跑一条统计语句（目录页
        要的只是每张表的行数），不逐字段分组。

        结果带短缓存：同一个人在同一组条件下反复看同一张表，不每次都重扫。
        统计语句本身失败（超时、被拒）按预览同样的错误码报出来；某一个字段的分布
        算不出来只记在 `skipped` 里，其余照常返回。
        """
        settings = get_settings()
        target = self._readable(access, database, table)
        dialect, columns = target.dialect, target.columns

        column_meta = target.meta.get("columns") if isinstance(target.meta, dict) else None
        column_meta = column_meta if isinstance(column_meta, dict) else {}
        described = {
            name: item for name, item in column_meta.items() if isinstance(item, dict) and item
        }
        roles = {name: str(item.get("semantic_role") or "") for name, item in described.items()}
        declared = {name: declared_values(item.get("value_range")) for name, item in described.items()}

        try:
            stats_statement, layout = build_stats_statement(
                table, columns, query, dialect, summed=sum_columns(columns, roles)
            )
            facet_plan = [
                (column, build_facet_statement(table, column, columns, query, dialect))
                for column in facet_columns(columns, roles, declared, frozenset(described), dialect)
            ] if breakdowns else []
            timeline_plan = [
                (column, build_timeline_statement(table, column, columns, query, dialect))
                for column in timeline_columns(columns)
            ] if breakdowns else []
        except PreviewInputError as exc:
            raise CatalogError(exc.code, exc.message) from exc

        # 行级权限按用户生效：不同的人数出来的可以不一样，缓存不能互相借用
        key = (
            database, table, caller, user_id,
            tuple(column.name for column in columns),
            stats_statement.sql, stats_statement.params, bool(breakdowns),
        )
        now = self._clock()
        with self._lock:
            cached = self._profiles.get(key)
        if cached is not None and cached[0] > now:
            if isinstance(cached[1], CatalogError):
                raise CatalogError(cached[1].code, cached[1].message, cached[1].status)
            return {**cached[1], "trace_id": get_trace_id()}

        started = time.perf_counter()
        timeout = settings.catalog_preview_timeout_seconds
        # 整个概况的时间预算：一条统计加上最多八条分组，每条都跑满超时的话要等四十多秒
        deadline = now + timeout * _PROFILE_BUDGET_FACTOR
        statements = [stats_statement.sql]
        facets: List[Dict[str, Any]] = []
        timelines: List[Dict[str, Any]] = []
        skipped: List[Dict[str, str]] = []

        with authorization_scope(
            access,
            caller=caller,
            user_id=user_id,
            trace_id=get_trace_id(),
            views=self.views,
        ):
            stats = self.router.execute(
                MCPExecutionRequest(
                    database=database,
                    sql=stats_statement.sql,
                    params=stats_statement.params,
                    max_rows=1,
                    timeout_seconds=timeout,
                )
            )
            if not stats.success or not stats.rows:
                error = self._execution_error(
                    database, table, stats.error or "", event="catalog.profile_failed"
                )
                # 统计都数不出来，分组更数不出来。一段时间内不再试：每试一次就是一遍全表扫描
                self._remember_profile(key, error, _SUMMARY_FAILURE_TTL_SECONDS)
                raise error

            for part, plan, cap in (
                ("values", facet_plan, MAX_FACET_VALUES + 1),
                ("timeline", timeline_plan, MAX_TIMELINE_BUCKETS + 1),
            ):
                for column, statement in plan:
                    if self._clock() >= deadline:
                        skipped.append({"column": column.name, "part": part, "reason": "budget"})
                        continue
                    result = self.router.execute(
                        MCPExecutionRequest(
                            database=database,
                            sql=statement.sql,
                            params=statement.params,
                            max_rows=cap,
                            timeout_seconds=timeout,
                        )
                    )
                    if not result.success:
                        reason = "timeout" if "超时" in (result.error or "") else "failed"
                        skipped.append({"column": column.name, "part": part, "reason": reason})
                        log_event(
                            _logger,
                            logging.WARNING,
                            "catalog.profile_part_failed",
                            database=database,
                            table=table,
                            column=column.name,
                            part=part,
                            error=result.error or "",
                        )
                        continue
                    statements.append(statement.sql)
                    own = has_own_filters(query, column.name)
                    if part == "values":
                        facets.append(
                            _facet(column.name, result, declared.get(column.name, ()), own)
                        )
                    else:
                        buckets, unparsed = split_timeline(result.rows[:MAX_TIMELINE_BUCKETS])
                        timelines.append({
                            "column": column.name,
                            "grain": "month",
                            "buckets": buckets,
                            "unparsed": unparsed,
                            "truncated": bool(result.truncated)
                            or len(result.rows) > MAX_TIMELINE_BUCKETS,
                            "own_filters_excluded": own,
                        })

        row = stats.rows[0]
        try:
            total_rows: Optional[int] = int(row.get("total_rows"))
        except (TypeError, ValueError):
            total_rows = None
        payload = {
            "database": database,
            "table": table,
            "filtered": bool((query.search or "").strip() or query.filters),
            "total_rows": total_rows,
            "columns": _column_stats(row, layout),
            "columns_limited": layout.limited,
            # 有字段因为权限没给。只说「有」，不说是哪些、有几个
            "columns_hidden": len(target.allowed) < len(target.actual),
            "breakdowns": bool(breakdowns),
            "facets": facets,
            "timelines": timelines,
            "skipped": skipped,
            "as_of": stats.as_of or None,
            "as_of_source": stats.as_of_source or None,
            "dialect": dialect,
            "sql": statements,
        }
        self._remember_profile(
            key,
            payload,
            # 有分组没算出来（多半是超时）：留久一点，免得每次打开都把超时再跑一遍
            _SUMMARY_FAILURE_TTL_SECONDS if skipped else _SUMMARY_TTL_SECONDS,
        )

        log_event(
            _logger,
            logging.INFO,
            "catalog.profile",
            caller=caller,
            user_id=user_id,
            database=database,
            table=table,
            total_rows=total_rows,
            facets=len(facets),
            timelines=len(timelines),
            skipped=len(skipped),
            filters=len(query.filters),
            searched=bool((query.search or "").strip()),
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return {**payload, "trace_id": get_trace_id()}

    def _remember_profile(self, key: Any, value: Any, ttl: float) -> None:
        now = self._clock()
        with self._lock:
            if len(self._profiles) >= _MAX_SUMMARY_ENTRIES:
                expired = [item for item, entry in self._profiles.items() if entry[0] <= now]
                for item in expired or list(self._profiles)[: _MAX_SUMMARY_ENTRIES // 2]:
                    self._profiles.pop(item, None)
            self._profiles[key] = (now + ttl, value)

    def _summary(
        self,
        *,
        key: Any,
        database: str,
        table: str,
        statement: Any,
        date_columns: Sequence[str],
        timeout: float,
    ) -> Tuple[Optional[int], List[Dict[str, Any]]]:
        """
        总行数与日期范围，带短缓存。必须在授权上下文里调用。

        汇总是附加信息：算不出来（超时、大表）就不给总数，不让整页预览失败。
        缓存键里有调用方、用户、可见列和整条语句——行级权限按用户生效，
        不同的人数出来的行数可以不一样，不能互相借用。
        """
        now = self._clock()
        with self._lock:
            cached = self._summaries.get(key)
        if cached is not None and cached[0] > now:
            return cached[1], list(cached[2])

        summary = self.router.execute(
            MCPExecutionRequest(
                database=database,
                sql=statement.sql,
                params=statement.params,
                max_rows=1,
                timeout_seconds=timeout,
            )
        )
        total_rows: Optional[int] = None
        date_ranges: List[Dict[str, Any]] = []
        ttl = _SUMMARY_TTL_SECONDS
        if summary.success and summary.rows:
            row = summary.rows[0]
            try:
                total_rows = int(row.get("total_rows"))
            except (TypeError, ValueError):
                total_rows = None
            for index, name in enumerate(date_columns):
                earliest, latest = row.get(f"min_{index}"), row.get(f"max_{index}")
                if earliest is not None and latest is not None:
                    date_ranges.append(
                        {"column": name, "min": _cell(earliest), "max": _cell(latest)}
                    )
        else:
            ttl = _SUMMARY_FAILURE_TTL_SECONDS
            log_event(
                _logger,
                logging.WARNING,
                "catalog.summary_failed",
                database=database,
                table=table,
                error=summary.error or "",
                retry_after_seconds=ttl,
            )

        with self._lock:
            if len(self._summaries) >= _MAX_SUMMARY_ENTRIES:
                expired = [item for item, value in self._summaries.items() if value[0] <= now]
                for item in expired or list(self._summaries)[: _MAX_SUMMARY_ENTRIES // 2]:
                    self._summaries.pop(item, None)
            self._summaries[key] = (self._clock() + ttl, total_rows, list(date_ranges))
        return total_rows, date_ranges

    def _execution_error(
        self, database: str, table: str, error: str, *, event: str = "catalog.preview_failed"
    ) -> CatalogError:
        """
        执行器的失败原因 → 给页面的错误。原始报错只进日志：
        驱动的报错里可能带着主机名、账号，不该原样送到浏览器。
        """
        log_event(
            _logger,
            logging.WARNING,
            event,
            database=database,
            table=table,
            error=error,
        )
        if "没有访问权限" in error or "授权上下文" in error:
            return CatalogError("forbidden", "当前身份没有这张表的查看权限。", status=403)
        if "超时" in error:
            return CatalogError("timeout", "预览超时了，请加筛选条件缩小范围。", status=504)
        return CatalogError("failed", "这张表暂时读不出来。", status=502)

    # ------------------------------------------------------------------
    # 表结构（带短缓存）
    # ------------------------------------------------------------------

    def _database(self, name: str) -> Optional[Any]:
        for database in self.databases:
            if database.name == name:
                return database
        return None

    def _documented_tables(self, database: str) -> Dict[str, Any]:
        semantics = self.semantic.databases.get(database)
        tables = getattr(semantics, "tables", None)
        return tables if isinstance(tables, dict) else {}

    @staticmethod
    def _semantic_role(table_meta: Any, column: str) -> str:
        columns = table_meta.get("columns") if isinstance(table_meta, dict) else None
        meta = columns.get(column) if isinstance(columns, dict) else None
        return str(meta.get("semantic_role") or "") if isinstance(meta, dict) else ""

    def _possibly_visible(self, access: Any, database: str, documented: Dict[str, Any]) -> bool:
        if documented:
            return any(self._allows_table(access, database, table) for table in documented)
        # 语义层里一张表都没登记：只能问「这个库整体放没放开」
        return self._allows_table(access, database, "__any_table__")

    def _dialect_of(self, database: str) -> str:
        """这个库的方言。执行器说了算（预览走的就是它），没有执行器时看配置。"""
        executor = self.router.executors.get(database) if self.router is not None else None
        config = self._database(database)
        declared = getattr(executor, "dialect", "") or getattr(config, "dialect", "") or ""
        return get_dialect(declared).name if declared else ""

    def _allows_table(self, access: Any, database: str, table: str) -> bool:
        return _allows_table(access, database, table, self._dialect_of(database))

    def _allows_column(self, access: Any, database: str, table: str, column: str) -> bool:
        return _allows_column(access, database, table, column, self._dialect_of(database))

    def _supports_preview(self, database: str) -> bool:
        executor = self.router.executors.get(database)
        return bool(executor is not None and getattr(executor, "supports_params", False))

    def _structure(self, database: Any, *, refresh: bool = False) -> _Structure:
        cached = self._fresh_structure(database.name, refresh)
        if cached is not None:
            return cached

        with self._lock:
            gate = self._load_locks.setdefault(database.name, threading.Lock())
        with gate:
            # 等锁的这段时间里，前一个请求多半已经读完了：再看一眼，不重复去读
            cached = self._fresh_structure(database.name, refresh)
            if cached is not None:
                return cached

            loaded = self._load_structure(database)
            ttl = (
                _PROBLEM_TTL_SECONDS
                if loaded.problem
                else max(0.0, get_settings().catalog_structure_ttl_seconds)
            )
            # 有效期从读完算起，不从开始读算起：读得慢的（连不上的库要等满连接超时）
            # 按开始时刻算的话，结论存进去时就已经过期了
            loaded.loaded_at = self._clock()
            loaded.expires_at = loaded.loaded_at + ttl
            with self._lock:
                self._structures[database.name] = loaded
            return loaded

    def _fresh_structure(self, name: str, refresh: bool) -> Optional[_Structure]:
        """缓存里还能用的那一份；没有或者该重读了就返回 None。"""
        now = self._clock()
        with self._lock:
            cached = self._structures.get(name)
        if cached is None:
            return None
        if refresh:
            # 刚读过就不再读：`refresh` 是「我想看最新的」，不是「每次都替我重连一遍」
            return cached if now - cached.loaded_at < _MIN_REFRESH_INTERVAL_SECONDS else None
        return cached if cached.expires_at > now else None

    def _load_structure(self, database: Any) -> _Structure:
        try:
            if get_dialect(database.dialect).name == "postgres":
                if not database.dsn:
                    return _Structure(problem="not_configured")
                tables = PostgresSchemaLoader(
                    dsn=database.dsn, database_name=database.name
                ).load_structure()
            else:
                if not str(database.db_path or ""):
                    return _Structure(problem="not_configured")
                tables = SQLiteSchemaLoader(
                    db_path=database.db_path, database_name=database.name
                ).load_structure()
        except FileNotFoundError:
            problem = "file_not_found"
        except EmptyDatabaseFile:
            problem = "empty_file"
        except ImportError:
            problem = "driver_missing"
        except Exception as exc:
            # 原因只进日志（可能带连接信息），给页面的只有一个原因码
            log_event(
                _logger,
                logging.WARNING,
                "catalog.structure_failed",
                database=database.name,
                error=f"{type(exc).__name__}: {exc}",
            )
            problem = "connection_failed"
        else:
            return _Structure(tables=tables)

        log_event(
            _logger,
            logging.WARNING,
            "catalog.database_unavailable",
            database=database.name,
            reason=problem,
        )
        return _Structure(problem=problem)


def _not_found() -> CatalogError:
    return CatalogError("not_found", "没有这张表，或者当前身份看不到它。", status=404)
