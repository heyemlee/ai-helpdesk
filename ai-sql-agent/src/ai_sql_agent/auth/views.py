"""
视图遮住基表：授权关卡看到的是 `v_payroll`，数据库读的是 `payroll`。

关卡是纯函数（策略 + SQL），按名字授权。`deny_tables: ["app.payroll"]` 拦得住
`SELECT * FROM payroll`，拦不住 `SELECT * FROM v_payroll`——SQLite 的 authorizer 回调
在读视图时报告的是基表被读了，而 SQL 文本里根本没有这个名字（复现过）。
PostgreSQL 里视图以属主的权限执行，语义相同（**未在真实 PostgreSQL 上验证**）。

所以关卡需要一份它自己推不出来的信息：**哪些名字是视图、视图最终读了哪些基表**。
这个模块就是那份信息，来源是数据库自己的目录（SQLite 的 `sqlite_master`、
PostgreSQL 的 `pg_class` + `pg_get_viewdef`），视图定义用同一套 sqlglot 分析解析。

三条规则，方向都是「拿不准就拒绝」：

1. **目录是按库登记的，三种状态。** `未登记`（这个进程从没扫过这个库，例如直接用执行器的
   单元测试）：关卡按老样子判，不假装有信息。`已加载`：下面的规则生效。`失败`（扫描
   出错）：对受限角色**拒绝一切**，直到下一次扫描成功——拿不到视图信息，就不能证明
   一张表不是视图。
2. **只有受限策略才受目录约束。** 没有任何 deny 的策略（`DataPolicy.full_access()`）没有
   东西可遮，视图遮不住什么，放行。
3. **受限策略遇到下列情形一律拒绝：** 视图定义解析不了（`opaque`）；视图依赖的某张
   基表被策略拒绝；视图依赖的基表有列级限制（视图列到基表列的对应关系关卡看不见，
   和 `SELECT *` 同一个理由）；已加载目录里**没有登记**的对象（扫描之后才建的表或视图
   ——和检索索引一样，新对象要等一次 schema 重载才可见）。

目录挂在授权作用域上（`authorization_scope(..., views=...)`），不是进程全局：同一进程里
多个 Pipeline（测试里常见）各有各的目录，互不覆盖。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, Mapping, Optional, Set, Tuple

from ai_sql_agent.core import log_event
from ai_sql_agent.core.logging_setup import get_logger

from .identifiers import fold, quoted_names_are_case_sensitive
from .sql_analysis import TableRef, analyze_sql, is_engine_internal

_logger = get_logger("auth.views")

UNREGISTERED = "unregistered"
LOADED = "loaded"
FAILED = "failed"


@dataclass(frozen=True)
class ViewInfo:
    """一个视图最终读的基表（视图套视图时已展开到底）。"""

    bases: FrozenSet[TableRef]
    opaque: bool = False
    """定义解析不了 / 读了引擎内部对象 / 循环引用。关卡对不透明的视图一律拒绝。"""


@dataclass(frozen=True)
class DatabaseObjects:
    """一个库里扫到的对象。名字是规范名（SQLite 折小写，PostgreSQL 为存储名）。"""

    tables: FrozenSet[str]
    views: Mapping[str, ViewInfo]


@dataclass(frozen=True)
class CatalogState:
    status: str
    objects: Optional[DatabaseObjects] = None
    reason: str = ""


class ViewCatalog:
    """线程安全的「库名 → 对象目录」。整库原子替换，读的人永远拿到完整的一份。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: Dict[str, CatalogState] = {}

    # ---- 读 ----
    def state(self, database: str) -> CatalogState:
        with self._lock:
            return self._states.get(database, CatalogState(UNREGISTERED))

    # ---- 写 ----
    def replace(self, database: str, objects: DatabaseObjects) -> None:
        with self._lock:
            self._states[database] = CatalogState(LOADED, objects)

    def mark_failed(self, database: str, reason: str) -> None:
        with self._lock:
            self._states[database] = CatalogState(FAILED, None, reason)

    def refresh(
        self, database: str, dialect: str, *, db_path: str = "", dsn: str = ""
    ) -> CatalogState:
        """
        重新扫描一个库并替换目录。**扫描出任何错都登记为失败**而不是保留旧目录：
        旧目录可能正好缺了这次想看的那个视图，保留它就是在拿不准的时候放行。
        """
        try:
            if quoted_names_are_case_sensitive(dialect):
                objects = scan_postgres(dsn)
            else:
                objects = scan_sqlite(db_path)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            self.mark_failed(database, reason)
            log_event(
                _logger,
                logging.ERROR,
                "views.scan_failed",
                database=database,
                dialect=dialect,
                error=reason[:300],
            )
        else:
            self.replace(database, objects)
            log_event(
                _logger,
                logging.INFO,
                "views.scanned",
                database=database,
                tables=len(objects.tables),
                views=len(objects.views),
                opaque=sum(1 for info in objects.views.values() if info.opaque),
            )
        return self.state(database)


# ---------------------------------------------------------------- 扫描


def _close_over_views(
    direct: Mapping[str, Optional[Set[TableRef]]],
) -> Dict[str, ViewInfo]:
    """
    `direct[view]` 是视图定义里直接引用的对象（可能还是视图）；展开成最终的基表。
    `None` 表示定义解析不了。循环引用按不透明处理。
    """
    memo: Dict[str, ViewInfo] = {}

    def bases(view: str, stack: Tuple[str, ...]) -> ViewInfo:
        if view in memo:
            return memo[view]
        refs = direct[view]
        if refs is None or view in stack:
            info = ViewInfo(frozenset(), opaque=True)
            memo[view] = info
            return info
        acc: Set[TableRef] = set()
        opaque = False
        for ref in refs:
            if ref.name in direct:
                sub = bases(ref.name, stack + (view,))
                acc |= sub.bases
                opaque = opaque or sub.opaque
            else:
                acc.add(ref)
        info = ViewInfo(frozenset(acc), opaque)
        memo[view] = info
        return info

    for name in direct:
        bases(name, ())
    return memo


def _direct_refs(select_sql: str, dialect: str) -> Optional[Set[TableRef]]:
    """视图的 SELECT 直接引用的表 / 视图；解析不了或读了引擎内部对象返回 None。"""
    analysis = analyze_sql(select_sql, dialect=dialect)
    if not analysis.parsed:
        return None
    for ref in analysis.tables:
        if not quoted_names_are_case_sensitive(dialect) and is_engine_internal(ref.name):
            return None
    for name in analysis.table_functions:
        if not quoted_names_are_case_sensitive(dialect) and is_engine_internal(name):
            return None
    return set(analysis.tables)


def _sqlite_view_select(create_sql: str) -> Optional[str]:
    """`CREATE VIEW v AS SELECT …` → 后面的 SELECT 文本；认不出返回 None。"""
    try:
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(create_sql, read="sqlite")
    except Exception:
        return None
    if not isinstance(tree, exp.Create) or tree.expression is None:
        return None
    try:
        return tree.expression.sql(dialect="sqlite")
    except Exception:
        return None


def scan_sqlite(db_path: str) -> DatabaseObjects:
    """读 `sqlite_master`：表名与视图定义。只读连接，库不存在就抛错（登记为失败）。"""
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"数据库文件不存在：{db_path}")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        rows = conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
        ).fetchall()
    finally:
        conn.close()

    tables: Set[str] = set()
    direct: Dict[str, Optional[Set[TableRef]]] = {}
    for kind, name, create_sql in rows:
        folded = fold(name)
        if kind == "table":
            tables.add(folded)
            continue
        select_sql = _sqlite_view_select(create_sql or "")
        direct[folded] = None if select_sql is None else _direct_refs(select_sql, "sqlite")
    return DatabaseObjects(frozenset(tables), _close_over_views(direct))


_POSTGRES_OBJECTS_SQL = """
SELECT n.nspname, c.relname, c.relkind,
       CASE WHEN c.relkind IN ('v', 'm') THEN pg_get_viewdef(c.oid, true) END
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'f', 'v', 'm')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\\_toast%'
  AND n.nspname NOT LIKE 'pg\\_temp\\_%'
"""


def scan_postgres(dsn: str) -> DatabaseObjects:
    """
    读 `pg_class`：表、外部表、分区表，以及视图和物化视图（后者也带着基表的数据）。

    **未在真实 PostgreSQL 上验证**：查询与 `pg_get_viewdef` 的输出格式来自文档，
    视图定义用 sqlglot 的 postgres 方言解析。同名视图出现在多个 schema 时
    按名字合并（基表取并集，任一不透明则不透明）。
    """
    import psycopg

    if not dsn:
        raise ValueError("缺少 DSN，无法扫描 PostgreSQL 的视图")
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(_POSTGRES_OBJECTS_SQL).fetchall()
        conn.rollback()

    tables: Set[str] = set()
    direct: Dict[str, Optional[Set[TableRef]]] = {}
    for _schema, name, kind, definition in rows:
        if kind in ("v", "m"):
            refs = None if not definition else _direct_refs(definition, "postgres")
            if name in direct and direct[name] is not None and refs is not None:
                refs = direct[name] | refs
            elif name in direct and (direct[name] is None or refs is None):
                refs = None
            direct[name] = refs
        else:
            tables.add(name)
    return DatabaseObjects(frozenset(tables), _close_over_views(direct))


__all__ = [
    "FAILED",
    "LOADED",
    "UNREGISTERED",
    "CatalogState",
    "DatabaseObjects",
    "ViewCatalog",
    "ViewInfo",
    "scan_postgres",
    "scan_sqlite",
]
