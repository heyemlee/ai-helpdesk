r"""
基于 SQL AST 的引用分析：这条 SQL 到底碰了哪些表、哪些列。

原来的实现是 `\b(?:from|join)\s+` 正则加一个把 `schema.table` 砍成 `table` 的
归一化，被复现过三条绕过：

    SELECT s.value FROM public_data p, secret_data s   逗号连接的第二张表不在正则里
    SELECT private_value FROM public_data              没有列级检查
    SELECT * FROM public_data                          通配符同上

正则拿不到的是**结构**：作用域、别名、CTE、子查询、UNION、LATERAL。所以这里换成
sqlglot 按方言解析，然后按作用域解析每个列引用的归属。

三条硬规则，都是「宁可误杀不可漏放」的方向：

1. **解析不出来的 SQL 一律拒绝。** 「提取不到表」不等于「没有越权」——
   前者是我方不知道，后者是我方知道且安全，把两者混为一谈正是旧实现的病根。
2. **列归属不确定时，按作用域里所有候选表逐个判**，任一张拒就拒。
   join 里一个不带前缀的 `salary`，两张表都要能给它放行。
3. **`SELECT *` 遇到有列级限制的表就拒。** 执行器这一层没有 schema，
   展不开通配符；展不开就不能证明它没读到禁列。

误杀的方向也如实写下来：sqlglot 的 `Scope.columns` 会把关联子查询里的列同时
算进外层作用域，所以 `SELECT (SELECT max(salary) FROM emp) FROM dept` 会拿
`salary` 去问 `dept` 一次。这是保守方向的多判一次，不是漏判。

**`Scope.columns` 本身会漏列，这里要补回来。** 它为了不把「引用 SELECT 别名」的名字
当成真实列，会丢掉几类**不带前缀**的列引用：`HAVING` / `QUALIFY` 里的、名字恰好等于
某个输出别名的 `ORDER BY` / `DISTINCT` 项（包括 `ORDER BY ssn * 1` 这种表达式）、
表值函数的实参、`Hint`。被丢掉的列从没被问过策略，`HAVING max(salary) > n` 因此能
当布尔探针把被禁列的值二分出来（复现过）。补回来的做法见
`_dropped_columns`：这些名字**既可能是别名也可能是真列**，分不清就按真列问，
任一候选拒绝就拒绝。代价是：一个输出别名恰好和被禁列同名时会被多拒一次。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from .identifiers import referenced_name

# sqlglot 方言名；执行器的 `dialect` 属性直接对得上，认不出来就用通用解析。
_KNOWN_DIALECTS = {"sqlite", "postgres", "postgresql", "mysql", "duckdb", "snowflake"}


@dataclass(frozen=True)
class TableRef:
    """一次表引用。`schema` 为空表示 SQL 里没写限定名。"""

    name: str
    schema: str = ""
    catalog: str = ""

    @property
    def qualified(self) -> str:
        return ".".join(part for part in (self.catalog, self.schema, self.name) if part)

    @property
    def identities(self) -> Tuple[str, ...]:
        """
        策略要匹配的名字集合。

        带 schema 时同时给出裸名与限定名，**两个都要被放行**才算放行：
        裸名保证老策略（`erp_db.sales_order`）继续生效，限定名让
        `secret.orders` 与 `public.orders` 在策略里终于能被区分开——
        旧实现把两者都砍成 `orders`，同名不同 schema 的表因此共用一条规则。
        """
        if self.qualified != self.name:
            return (self.name, self.qualified)
        return (self.name,)


@dataclass(frozen=True)
class ColumnRef:
    """
    一次列引用。

    `candidates` 是这个列**可能**属于的真实表。带前缀且前缀能解析时只有一张；
    不带前缀时是当前作用域里的全部真实表。判定时逐个问，任一张拒就拒。
    """

    name: str
    candidates: Tuple[TableRef, ...]


@dataclass(frozen=True)
class StarRef:
    """一次通配符引用（`*` 或 `t.*`），展开后覆盖 `tables` 里的每一张表。"""

    tables: Tuple[TableRef, ...]


@dataclass(frozen=True)
class SqlAnalysis:
    parsed: bool
    error: str = ""
    tables: Tuple[TableRef, ...] = ()
    columns: Tuple[ColumnRef, ...] = ()
    stars: Tuple[StarRef, ...] = ()
    table_functions: Tuple[str, ...] = ()
    """
    FROM 里的表值函数名（小写），如 `pragma_table_info`、`json_each`。

    它们没有表名，`tables` 里不会出现——原来授权关卡因此把
    `SELECT name FROM pragma_table_info('其他表')` 当成「不碰任何表」放行。
    关卡不判断每个函数该不该放行（那是策略的事），只把名字交出来，
    引擎内部那几个由 `enforcement` 一律拒绝。
    """
    functions: Tuple[str, ...] = ()
    """
    SQL 里调用的**全部**函数名（小写，去掉 schema 前缀与引号），含标量函数。

    关卡用它拦那些「把字符串当 SQL 或表名去执行」的函数（`query_to_xml('select …')`、
    `table_to_xml('表'::regclass)`、`dblink(…)`），和覆盖 RLS 身份的 `set_config`：
    它们读的表不出现在 AST 里，按表和列判永远看不见。
    """


# SQLite 引擎自己的对象。`enforcement` 对它们一律拒绝，`views` 扫描视图定义时也用同一个
# 判据（视图的定义里读了引擎内部对象，就当作无法判断的视图）。
ENGINE_INTERNAL_PREFIXES = ("sqlite_", "pragma_")
ENGINE_INTERNAL_NAMES = frozenset({"dbstat"})


def is_engine_internal(name: str) -> bool:
    lowered = (name or "").casefold()
    return lowered.startswith(ENGINE_INTERNAL_PREFIXES) or lowered in ENGINE_INTERNAL_NAMES


def _sqlglot_dialect(dialect: str) -> Optional[str]:
    name = (dialect or "").strip().lower()
    if name in _KNOWN_DIALECTS:
        return "postgres" if name == "postgresql" else name
    return None


def _table_ref(node) -> Optional[TableRef]:
    """
    把 sqlglot 的 Table 节点转成 TableRef；表函数之类没有名字的返回 None。

    名字已经是规范名（`_canonicalize_identifiers` 在建作用域之前换过了），这里不再动大小写。
    """
    name = (node.name or "").strip()
    if not name:
        return None
    return TableRef(
        name=name,
        schema=(node.db or "").strip(),
        catalog=(node.catalog or "").strip(),
    )


def _canonicalize_identifiers(tree, exp, dialect: str) -> None:
    """
    把语法树里的每个标识符换成它指向的规范名（规则见 `identifiers` 模块）。

    在建作用域**之前**做，表名、schema、列名、别名、列前缀就都按同一条规则落定：
    PostgreSQL 里 `Orders` 变成 `orders`、`"Orders"` 保持原样；其余方言一律小写。
    原来的做法是解析完再对每个名字 `.lower()`——那在 PostgreSQL 上把 `"Orders"`
    和 `orders` 两张表判成了同一张，别名 `"X"` 与 `x` 也会互相顶替。
    """
    for identifier in tree.find_all(exp.Identifier):
        name = identifier.this
        if isinstance(name, str):
            identifier.set(
                "this",
                referenced_name(
                    name, quoted=bool(identifier.args.get("quoted")), dialect=dialect
                ),
            )


def analyze_sql(sql: str, dialect: str = "") -> SqlAnalysis:
    """解析 SQL，返回它引用的表与列。解析失败时 `parsed=False`，调用方必须据此拒绝。"""
    text = (sql or "").strip()
    if not text:
        return SqlAnalysis(parsed=False, error="SQL 为空。")

    try:
        import sqlglot
        from sqlglot import exp
        from sqlglot.optimizer.scope import ScopeType, build_scope
    except ImportError as exc:  # pragma: no cover - 依赖缺失属于部署事故
        return SqlAnalysis(
            parsed=False,
            error=f"缺少 sqlglot，无法做授权解析（不解析即拒绝）：{exc}",
        )

    read = _sqlglot_dialect(dialect)
    try:
        statements = sqlglot.parse(text, read=read)
    except Exception as exc:
        return SqlAnalysis(parsed=False, error=f"SQL 无法解析（{type(exc).__name__}）：{exc}")

    statements = [item for item in statements if item is not None]
    if not statements:
        return SqlAnalysis(parsed=False, error="SQL 无法解析成任何语句。")
    if len(statements) > 1:
        return SqlAnalysis(parsed=False, error="一次只允许执行一条 SQL 语句。")

    tree = statements[0]
    _canonicalize_identifiers(tree, exp, read or "")
    try:
        root = build_scope(tree)
    except Exception as exc:
        return SqlAnalysis(parsed=False, error=f"无法解析 SQL 的作用域（{type(exc).__name__}）：{exc}")

    if root is None:
        # build_scope 只认 SELECT / UNION 一类的查询表达式；其余形状交给
        # sql_guard 拒绝，这里不假装看得懂。
        return SqlAnalysis(parsed=False, error="SQL 不是可分析的查询语句。")

    tables: List[TableRef] = []
    columns: List[ColumnRef] = []
    stars: List[StarRef] = []

    # PostgreSQL 里，不带前缀的标识符既可能是列也可能是**整行**：`row_to_json(e)`、
    # `SELECT e FROM t e`、`e::text`、`(e).salary` 里的 `e` 是 FROM 里的别名，
    # 取到的是那张表的全部列（含被禁列）。名字等于某个别名/表名的无前缀「列」
    # 因此按 `e.*` 算。SQLite 没有整行引用这回事。
    whole_row = read == "postgres"

    # id(scope.expression) -> (scope, 别名表, 本作用域的真实表)。补列时要沿父作用域往上找。
    states: Dict[int, tuple] = {}
    # `Scope.columns` 已经交给下面循环处理过的列节点（按 id）。
    seen_columns: Set[int] = set()

    for scope in root.traverse():
        # 这个作用域里 alias -> 真实表；值是 Scope 的（CTE / 子查询）交给它自己那一轮。
        local: Dict[str, TableRef] = {}
        for alias, source in scope.sources.items():
            if not isinstance(source, exp.Table):
                continue
            ref = _table_ref(source)
            if ref is None:
                continue
            local[alias or ref.name] = ref
            local.setdefault(ref.name, ref)
            tables.append(ref)

        scope_tables = tuple(dict.fromkeys(local.values()))
        states[id(scope.expression)] = (scope, local, scope_tables)

        for column in scope.columns:
            seen_columns.add(id(column))
            qualifier = column.table or ""
            if qualifier:
                target = local.get(qualifier)
                if target is None:
                    # 前缀在本作用域解析不了：要么是 CTE / 子查询（它的列在自己
                    # 那一轮判），要么是关联子查询引用的外层别名（外层那一轮会
                    # 收到同一个列）。两种都不是漏判，跳过。
                    continue
                candidates: Tuple[TableRef, ...] = (target,)
            else:
                candidates = scope_tables
                whole_row_target = local.get(column.name) if whole_row else None
                if whole_row_target is not None:
                    stars.append(StarRef(tables=(whole_row_target,)))
            if not candidates:
                continue
            columns.append(ColumnRef(name=column.name, candidates=candidates))

        stars.extend(_scope_stars(scope, local, scope_tables, exp))

    extra_columns, extra_stars = _dropped_columns(
        tree, states, seen_columns, exp, ScopeType, whole_row
    )
    columns.extend(extra_columns)
    stars.extend(extra_stars)

    return SqlAnalysis(
        parsed=True,
        tables=tuple(dict.fromkeys(tables)),
        columns=tuple(columns),
        stars=tuple(stars),
        table_functions=_table_functions(tree, exp),
        functions=_function_names(tree, exp),
    )


def _function_names(tree, exp) -> Tuple[str, ...]:
    """
    调用到的全部函数名，小写。`Anonymous`（sqlglot 不认识的函数，如 `query_to_xml`）的
    `.name` 就是 SQL 里写的那个名字，带不带 schema 前缀、加不加引号都一样；
    认识的函数类（`COUNT`、`CAST`…）取 `sql_name()`。
    """
    names: List[str] = []
    for node in tree.find_all(exp.Func):
        if isinstance(node, exp.Anonymous):
            name = (node.name or "").strip()
        else:
            name = node.sql_name() or ""
        name = name.lower()
        if name:
            names.append(name)
    return tuple(dict.fromkeys(names))


def _table_functions(tree, exp) -> Tuple[str, ...]:
    """
    FROM 里的表值函数名。表值函数是 `Table` 节点，但 `.this` 是函数而不是标识符，
    所以 `.name` 为空、`_table_ref` 返回 None，`tables` 里看不到它们。
    """
    names: List[str] = []
    for node in tree.find_all(exp.Table):
        func = node.this
        if not isinstance(func, exp.Func):
            continue
        # 认得的函数类（如 PostgreSQL 的 generate_series）没有 `.name`，退到 sql_name()
        name = (getattr(func, "name", "") or func.sql_name() or "").strip().lower()
        if name:
            names.append(name)
    return tuple(dict.fromkeys(names))


def _dropped_columns(
    tree, states, seen_columns, exp, ScopeType, whole_row: bool = False
) -> Tuple[List[ColumnRef], List[StarRef]]:
    """
    补回 `Scope.columns` 丢掉的列引用（原因见模块文档）。

    做法：整棵树里所有 `Column` 节点，减去任何作用域的 `columns` 里出现过的，
    剩下的就是被丢的。每一个按它所在的 SELECT（`parent_select`）找回作用域，
    再按和 `Scope.columns` 一致的规则决定它能绑到哪些表：

    - 带前缀：沿作用域往外找第一个认得这个前缀的；找不到就跳过（那是 CTE /
      派生表的别名，它的列在它自己那一轮判过了）。
    - 不带前缀：本作用域的全部真实表，**加上**外层作用域的——SQL 的名字解析
      本来就是「先内后外」，内层表里没有这一列时它绑到外层表。往外走只穿过
      子查询 / 表值函数 / 可关联的派生表，碰到 CTE、UNION 分支、普通派生表就停，
      和 `Scope.external_columns` 的边界一致。
    """
    found: List[ColumnRef] = []
    found_stars: List[StarRef] = []

    for column in tree.find_all(exp.Column):
        if id(column) in seen_columns:
            continue
        if isinstance(column.this, exp.Star):
            continue  # `t.*` 由 `_scope_stars` 处理
        home = column.parent_select
        if home is None or id(home) not in states:
            continue
        if _orders_a_set_operation(column, exp):
            continue  # `UNION … ORDER BY a` 的 a 是输出列名，不属于任何一张表

        scope = states[id(home)][0]
        candidates = _bind_dropped(column, scope, states, ScopeType)
        if candidates:
            found.append(ColumnRef(name=column.name, candidates=candidates))
        if whole_row and not column.table:
            target = _bind_whole_row(column.name, scope, states, ScopeType)
            if target is not None:
                found_stars.append(StarRef(tables=(target,)))

    return found, found_stars


def _bind_whole_row(name, scope, states, ScopeType) -> Optional[TableRef]:
    """无前缀名字是不是外到内第一个认得的 FROM 别名 / 表名（PostgreSQL 整行引用）。"""
    current = scope
    while current is not None:
        entry = states.get(id(current.expression))
        if entry is not None:
            target = entry[1].get(name)
            if target is not None:
                return target
        if not _sees_outer_scope(current, ScopeType):
            break
        current = current.parent
    return None


def _orders_a_set_operation(column, exp) -> bool:
    order = column.find_ancestor(exp.Order)
    if order is None:
        return False
    set_operation = getattr(exp, "SetOperation", None) or exp.Union
    return isinstance(order.parent, set_operation)


def _bind_dropped(column, scope, states, ScopeType) -> Tuple[TableRef, ...]:
    chain = []
    current = scope
    while current is not None:
        entry = states.get(id(current.expression))
        if entry is not None:
            chain.append(entry)
        if not _sees_outer_scope(current, ScopeType):
            break
        current = current.parent

    qualifier = column.table or ""
    if qualifier:
        for _scope, local, _tables in chain:
            target = local.get(qualifier)
            if target is not None:
                return (target,)
        return ()

    candidates: List[TableRef] = []
    for _scope, _local, scope_tables in chain:
        candidates.extend(scope_tables)
    return tuple(dict.fromkeys(candidates))


def _sees_outer_scope(scope, ScopeType) -> bool:
    kind = getattr(scope, "scope_type", None)
    if kind in (ScopeType.SUBQUERY, ScopeType.UDTF):
        return True
    if kind == ScopeType.DERIVED_TABLE:
        return bool(getattr(scope, "can_be_correlated", False))
    return False


def _scope_stars(scope, local, scope_tables, exp) -> List[StarRef]:
    """
    收集本作用域里的通配符。

    `COUNT(*)` 不读任何列，排除掉——把它算成「读了全表所有列」会让带列限制的
    角色连计数都做不了，那是纯粹的误杀。
    """
    found: List[StarRef] = []
    expression = scope.expression

    for node in expression.find_all(exp.Star):
        if node.parent_select is not expression:
            continue
        parent = node.parent
        if isinstance(parent, exp.Count):
            continue
        if isinstance(parent, exp.Column):
            continue  # `t.*` 由下面的 Column 分支处理，避免重复
        if scope_tables:
            found.append(StarRef(tables=scope_tables))

    for node in expression.find_all(exp.Column):
        if node.parent_select is not expression:
            continue
        if not isinstance(node.this, exp.Star):
            continue
        qualifier = node.table or ""
        target = local.get(qualifier) if qualifier else None
        if target is not None:
            found.append(StarRef(tables=(target,)))
        elif not qualifier and scope_tables:
            found.append(StarRef(tables=scope_tables))

    return found


def extract_referenced_tables(sql: str, dialect: str = "") -> Set[str]:
    """
    提取 SQL 引用的表名（小写裸名，去 schema 前缀）。

    **这不是授权判据**——授权走 `ai_sql_agent.auth.enforcement.authorize_sql`，它拿的是
    完整的 `TableRef`（带 schema）和列引用。这个函数只服务两处非安全用途：
    跨库静态校验（「这张表不在这个库里」）与审计日志里的表清单，两处都要的是
    「模型写了哪张表」而不是「他有没有权限」。

    解析失败时返回空集合。调用方是启发式检查，拿不到结构就跳过，不影响安全性。
    """
    analysis = analyze_sql(sql, dialect=dialect)
    if not analysis.parsed:
        return set()
    # 这里的调用方拿它和元数据里的表名（小写）比，所以统一给小写；
    # 区分大小写的判定只发生在授权那条路上。
    return {table.name.lower() for table in analysis.tables}
