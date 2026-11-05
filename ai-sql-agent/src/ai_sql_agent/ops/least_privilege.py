"""
数据库侧最小授权。

应用层的授权关卡（`ai_sql_agent.auth.enforcement`）是**第二道**防线，不能是唯一一道。
只读账号只挡写、不挡读密列：一个能连库的只读账号照样 `SELECT unit_cost`。
所以同一份 `DataPolicy` 要在数据库侧再落一次——**应用层漏判也越不了权**。

这里做三件事：

1. `grant_sql()`：把 DataPolicy 翻成 REVOKE + 表级/列级 GRANT。
   PostgreSQL 的列级 GRANT 是真事：`GRANT SELECT (a, b) ON t TO role` 之后
   `SELECT c FROM t` 会直接被服务端拒。
2. `row_policies_from_policy()`：行级安全（RLS）。行范围来自 `DataPolicy.row_filters`
   （设计见 docs/authz-model.md），只为声明了非空谓词的表生成；`predicate: "*"`
   是显式放行所有行。没声明的表这里不生成，运行时按放行处理，多租户范围下
   启动检查会把它列为问题。每一条最后都经 `row_policy_sql()` 落成语句，
   它也接受运维直接写的条件。
3. `verify_least_privilege()`：**连上去真查一次**，确认该拒的真的拒了。
   生成 SQL 只是意图，执行过没执行过、执行对没执行对，得用一次真实查询回答。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

_IDENT_OK = set("abcdefghijklmnopqrstuvwxyz0123456789_")


def _quote_ident(name: str) -> str:
    """标识符转义。名字来自配置文件，不是用户输入，但便宜的防线没理由不设。"""
    cleaned = name.strip()
    if not cleaned or not set(cleaned.lower()) <= _IDENT_OK:
        raise ValueError(f"标识符只允许字母数字下划线：{name!r}")
    return f'"{cleaned}"'


@dataclass
class TableInventory:
    """库里实际有哪些表、每张表有哪些列。从 information_schema 读，不靠手写。"""

    columns: Dict[str, List[str]] = field(default_factory=dict)

    def tables(self) -> List[str]:
        return sorted(self.columns)

    @classmethod
    def from_database(cls, dsn: str, schema: str = "public") -> "TableInventory":
        import psycopg

        inventory: Dict[str, List[str]] = {}
        conn = psycopg.connect(dsn, connect_timeout=10)
        try:
            rows = conn.execute(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = %s
                ORDER BY table_name, ordinal_position
                """,
                (schema,),
            ).fetchall()
        finally:
            conn.close()
        for table, column in rows:
            inventory.setdefault(table, []).append(column)
        return cls(columns=inventory)


@dataclass
class LeastPrivilegePlan:
    role: str
    schema: str
    granted_tables: List[str]
    denied_tables: List[str]
    column_grants: Dict[str, List[str]]
    """表 → 允许读的列。只有存在禁列的表才会出现在这里。"""

    denied_columns: Dict[str, List[str]] = field(default_factory=dict)

    def statements(self) -> List[str]:
        lines = [
            f"-- 由 ai_sql_agent.ops.least_privilege 按 DataPolicy 生成，角色 {self.role}",
            f"REVOKE ALL ON ALL TABLES IN SCHEMA {_quote_ident(self.schema)} "
            f"FROM {_quote_ident(self.role)};",
        ]
        for table in self.granted_tables:
            target = f"{_quote_ident(self.schema)}.{_quote_ident(table)}"
            allowed = self.column_grants.get(table)
            if allowed is None:
                lines.append(f"GRANT SELECT ON {target} TO {_quote_ident(self.role)};")
            elif allowed:
                columns = ", ".join(_quote_ident(c) for c in allowed)
                # 列级 GRANT：没列出来的列在服务端就查不了，应用层漏判也没用
                lines.append(
                    f"GRANT SELECT ({columns}) ON {target} TO {_quote_ident(self.role)};"
                )
            else:
                lines.append(f"-- {table}: 一列都不允许，整表不授权")
        for table in self.denied_tables:
            lines.append(f"-- {table}: 策略禁止，保持不授权")
        return lines

    def to_sql(self) -> str:
        return "\n".join(self.statements()) + "\n"


def _bind_postgres(policy):
    hook = getattr(policy, "for_dialect", None)
    return hook("postgres") if callable(hook) else policy


def build_plan(
    policy,
    inventory: TableInventory,
    *,
    database: str,
    role: str,
    schema: str = "public",
) -> LeastPrivilegePlan:
    """
    把 DataPolicy 翻成这个库的授权计划。

    `database` 是策略里那个逻辑库名（`erp_db`），`schema` 是 PostgreSQL 的
    schema。两者不是一回事：策略按业务库写，授权落在物理 schema 上。
    """
    # 这份计划落在 PostgreSQL 上：inventory 里是存储名，按 PostgreSQL 的大小写规则比
    policy = _bind_postgres(policy)
    granted: List[str] = []
    denied: List[str] = []
    column_grants: Dict[str, List[str]] = {}
    denied_columns: Dict[str, List[str]] = {}

    for table in inventory.tables():
        if not policy.allows_table(database, table):
            denied.append(table)
            continue
        granted.append(table)
        columns = inventory.columns[table]
        allowed = [
            column
            for column in columns
            if policy.allows_column(database, table, column)
        ]
        if len(allowed) != len(columns):
            column_grants[table] = allowed
            denied_columns[table] = [c for c in columns if c not in set(allowed)]

    return LeastPrivilegePlan(
        role=role,
        schema=schema,
        granted_tables=granted,
        denied_tables=denied,
        column_grants=column_grants,
        denied_columns=denied_columns,
    )


def row_policies_from_policy(
    policy, inventory: "TableInventory", *, database: str, role: str, schema: str = "public"
) -> List[str]:
    """
    从 `DataPolicy.row_filters` 生成 RLS —— **不再靠运维手写**。

    第一轮这里只有 `row_policy_sql()`（条件由人给），因为那时策略模型没有行范围
    这一维。现在有了（见 docs/authz-model.md），所以能推出来。

    只为**声明了非空谓词**的表生成：`predicate: "*"` 是显式放行所有行，
    没声明（`row_predicate` 返回 None）则什么都不生成——运行时按放行处理，
    生产模式的启动检查会把「没声明」拦下来。这两件事分工不同，别在这里替它判。
    """
    policy = _bind_postgres(policy)
    statements: List[str] = []
    for table in inventory.tables():
        if not policy.allows_table(database, table):
            continue
        predicate = policy.row_predicate(database, table)
        if not predicate:          # None（没声明）与 ""（显式全放）都不生成
            continue
        statements.append(
            row_policy_sql(
                role=role, table=table, schema=schema,
                using_expression=predicate,
                policy_name=f"{role}_{table}_rls",
            )
        )
    return statements


def row_policy_sql(
    *,
    role: str,
    table: str,
    using_expression: str,
    schema: str = "public",
    policy_name: str = "",
) -> str:
    """
    行级安全（RLS）：把一条行范围条件落成语句。

    `row_policies_from_policy()` 用它把 `DataPolicy.row_filters` 里声明的谓词逐表落下去；
    也可以直接传运维写的条件。

    典型写法：`using_expression="owner_id = current_setting('app.user_id', true)"`
    """
    target = f"{_quote_ident(schema)}.{_quote_ident(table)}"
    name = _quote_ident(policy_name or f"{role}_{table}_rls")
    return "\n".join([
        f"ALTER TABLE {target} ENABLE ROW LEVEL SECURITY;",
        f"ALTER TABLE {target} FORCE ROW LEVEL SECURITY;",
        f"DROP POLICY IF EXISTS {name} ON {target};",
        f"CREATE POLICY {name} ON {target} FOR SELECT "
        f"TO {_quote_ident(role)} USING ({using_expression});",
    ]) + "\n"


@dataclass
class PrivilegeFinding:
    kind: str
    """`table` / `column`。"""

    target: str
    expected: str
    """`denied` —— 目前只验「该拒的拒了没有」。"""

    actual: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.actual == self.expected


@dataclass
class PrivilegeReport:
    role: str
    findings: List[PrivilegeFinding] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def enforced(self) -> bool:
        return not self.errors and all(f.ok for f in self.findings)

    def problems(self) -> List[str]:
        issues = [
            f"{f.kind} {f.target}：策略说拒，数据库却放行了（{f.detail}）"
            for f in self.findings
            if not f.ok
        ]
        issues.extend(self.errors)
        return issues


def verify_least_privilege(
    dsn: str,
    plan: LeastPrivilegePlan,
    *,
    sample_limit: int = 25,
) -> PrivilegeReport:
    """
    用**受限角色自己的连接**去查一遍，确认该拒的真的拒了。

    生成 SQL 只是意图。有没有执行、执行时报没报错、后来有没有人手工
    `GRANT ALL` 回去——这些都只有真查一次才知道。启动时的只读体检
    （`audit_readonly`）验的是「能不能写」，这里验的是「能不能读到不该读的」，
    两件事互不覆盖。
    """
    import psycopg

    report = PrivilegeReport(role=plan.role)
    conn = None
    try:
        conn = psycopg.connect(dsn, connect_timeout=10, autocommit=True)
    except Exception as exc:
        report.errors.append(f"连不上受限角色（{type(exc).__name__}）：{exc}")
        return report

    try:
        for table in plan.denied_tables[:sample_limit]:
            report.findings.append(
                _probe(conn, "table", f"{plan.schema}.{table}",
                       f"SELECT 1 FROM {_quote_ident(plan.schema)}."
                       f"{_quote_ident(table)} LIMIT 1")
            )
        probed = 0
        for table, columns in plan.denied_columns.items():
            for column in columns:
                if probed >= sample_limit:
                    break
                probed += 1
                report.findings.append(
                    _probe(conn, "column", f"{plan.schema}.{table}.{column}",
                           f"SELECT {_quote_ident(column)} FROM "
                           f"{_quote_ident(plan.schema)}.{_quote_ident(table)} LIMIT 1")
                )
    finally:
        conn.close()
    return report


def _probe(conn, kind: str, target: str, sql: str) -> PrivilegeFinding:
    """跑一条**应该失败**的查询。跑通了就是数据库侧没拦住。"""
    try:
        conn.execute(sql).fetchall()
    except Exception as exc:
        return PrivilegeFinding(
            kind=kind, target=target, expected="denied", actual="denied",
            detail=type(exc).__name__,
        )
    return PrivilegeFinding(
        kind=kind, target=target, expected="denied", actual="allowed",
        detail="查询成功返回",
    )
