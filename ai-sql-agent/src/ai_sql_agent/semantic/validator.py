from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .objects import DatabaseSemantics, SemanticLayer

SEMANTIC_ROLES = {
    "metric",
    "metric_filter",
    "dimension",
    "output_dimension",
    "filter",
    "time",
    "join_key",
    "identifier",
    "text",
    "",
}
"""
已知的字段语义角色。

取值以内置演示元数据实际在用的为准（`output_dimension` 表示「用户想看的那一列」，
与仅用于筛选的 `dimension` 区分）。这里只做拼写提示，不强制——
写了别的值不影响链路，只是少一层校验。
"""


@dataclass
class ValidationIssue:
    """一条元数据问题。"""

    level: str
    """error：会让链路答错或起不来；warn：能跑，但检索质量会掉。"""

    where: str
    """出问题的位置，如 erp_db.sales_order.order_amt。"""

    message: str

    def format(self) -> str:
        mark = "ERROR" if self.level == "error" else "WARN "
        return f"[{mark}] {self.where}: {self.message}"


@dataclass
class ValidationReport:
    issues: List[ValidationIssue] = field(default_factory=list)
    checked_databases: List[str] = field(default_factory=list)
    live_checked: List[str] = field(default_factory=list)
    """真正连上库比对过结构的库名。连不上不算失败，只是没比对。"""

    coverage: Dict[str, float] = field(default_factory=dict)
    """每个库的字段描述覆盖率：有 description 的字段占比。"""

    def add(self, level: str, where: str, message: str) -> None:
        self.issues.append(ValidationIssue(level, where, message))

    @property
    def errors(self) -> List[ValidationIssue]:
        return [issue for issue in self.issues if issue.level == "error"]

    @property
    def warnings(self) -> List[ValidationIssue]:
        return [issue for issue in self.issues if issue.level == "warn"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self) -> str:
        lines: List[str] = []
        for issue in self.issues:
            lines.append(issue.format())
        lines.append("")
        lines.append(
            f"检查了 {len(self.checked_databases)} 个库"
            + (
                f"，其中 {len(self.live_checked)} 个连上真实库比对了结构"
                if self.live_checked
                else "（没有连上任何真实库，只做了离线检查）"
            )
        )
        for name, ratio in sorted(self.coverage.items()):
            lines.append(f"  {name}: 字段描述覆盖率 {ratio:.0%}")
        lines.append(f"错误 {len(self.errors)} 项，警告 {len(self.warnings)} 项")
        return "\n".join(lines)


def validate_semantic_layer(
    layer: SemanticLayer,
    *,
    live: bool = True,
) -> ValidationReport:
    """
    检查一份语义元数据。

    分两类检查，因为两类的后果不一样：

    - 离线检查（结构、引用、角色取值）：不连库也能做，CI 里可以常跑；
    - 实库比对（表在不在、字段在不在、有没有漏写）：需要连上业务库。
      漏写的字段是最贵的一类问题——它不会报错，只会让那类问题永远答不出来，
      而现象是「模型答不上来」，看上去像模型不行。

    live=False 时跳过实库比对；连不上库不算错误，只在报告里说明没比对。
    """
    report = ValidationReport()

    for name, database in layer.databases.items():
        report.checked_databases.append(name)
        _check_database_offline(database, report)
        if live and database.has_connection():
            _check_database_live(database, report)

    _check_shared(layer, report)
    return report


def _check_database_offline(database: DatabaseSemantics, report: ValidationReport) -> None:
    name = database.name

    if not database.description.strip():
        report.add(
            "warn",
            name,
            "没有写库的业务描述。库级检索靠它判断问题该去哪个库，"
            "多库部署时缺了它会经常路由到错误的库。",
        )
    if not database.aliases:
        report.add(
            "warn",
            name,
            "没有写库别名。用户说「订单系统」而库名叫 erp_db 时，库级检索匹配不上。",
        )
    if database.dialect not in ("sqlite", "postgres"):
        report.add(
            "error",
            name,
            f"dialect={database.dialect} 不支持，只能是 sqlite 或 postgres。",
        )
    if not database.has_connection():
        report.add(
            "error",
            name,
            "没有可用连接：sqlite 需要 connection.db_path，"
            "postgres 需要 connection.dsn 或 connection.dsn_env 指向的环境变量有值。",
        )
    if database.dsn:
        report.add(
            "warn",
            name,
            "连接串直接写在文件里了。文件要进版本库，密码不该进——"
            "改用 connection.dsn_env 指向环境变量。",
        )
    if not database.tables:
        report.add("error", name, "没有定义任何表。")
        return

    described = 0
    total = 0
    for table_name, table_meta in database.tables.items():
        where = f"{name}.{table_name}"
        if not isinstance(table_meta, dict):
            report.add("error", where, "表定义必须是键值对（description / aliases / columns）。")
            continue
        if not str(table_meta.get("description") or "").strip():
            report.add("warn", where, "表没有业务描述，表级检索只能靠表名匹配。")

        columns = table_meta.get("columns") or {}
        if not isinstance(columns, dict):
            report.add("error", where, "columns 必须是「字段名 → 说明」的键值对。")
            continue
        if not columns:
            report.add(
                "error",
                where,
                "表下面没有任何字段。字段是检索的最小单位，空表等于这张表不存在。",
            )
            continue

        for column_name, column_meta in columns.items():
            column_where = f"{where}.{column_name}"
            total += 1
            if not isinstance(column_meta, dict):
                report.add("error", column_where, "字段定义必须是键值对。")
                continue
            if str(column_meta.get("description") or "").strip():
                described += 1
            else:
                report.add(
                    "warn",
                    column_where,
                    "字段没有业务含义描述。缩写字段名（如 po_amt）没有描述时，"
                    "向量检索和术语表都救不回来。",
                )
            role = str(column_meta.get("semantic_role") or "")
            if role not in SEMANTIC_ROLES:
                report.add(
                    "warn",
                    column_where,
                    f"semantic_role={role} 不是已知取值"
                    f"（{'、'.join(sorted(r for r in SEMANTIC_ROLES if r))}）。",
                )
            aliases = column_meta.get("aliases") or []
            if not isinstance(aliases, list):
                report.add("error", column_where, "aliases 必须是列表。")

    if total:
        report.coverage[name] = described / total


def _check_database_live(database: DatabaseSemantics, report: ValidationReport) -> None:
    """连上真实库，比对元数据与实际结构。"""
    name = database.name
    try:
        actual = _load_actual_schema(database)
    except Exception as exc:
        report.add(
            "warn",
            name,
            f"连不上库，跳过结构比对（{type(exc).__name__}: {exc}）。"
            "元数据本身的检查不受影响。",
        )
        return

    report.live_checked.append(name)
    declared_tables = set(database.tables)
    actual_tables = set(actual)

    for table in sorted(declared_tables - actual_tables):
        report.add(
            "error",
            f"{name}.{table}",
            "元数据里有这张表，真实库里没有。检索会把它推给模型，生成的 SQL 必定报错。",
        )

    for table in sorted(actual_tables - declared_tables):
        report.add(
            "warn",
            f"{name}.{table}",
            "真实库里有这张表，元数据里没写。它对模型完全不存在，"
            "涉及它的问题永远答不出来——现象是「模型答不上来」，其实是元数据漏了。",
        )

    for table in sorted(declared_tables & actual_tables):
        declared_columns = set((database.tables[table] or {}).get("columns") or {})
        actual_columns = set(actual[table])
        for column in sorted(declared_columns - actual_columns):
            report.add(
                "error",
                f"{name}.{table}.{column}",
                "元数据里有这个字段，真实库里没有。模型会照着写进 SQL，执行报错。",
            )
        missing = sorted(actual_columns - declared_columns)
        if missing:
            report.add(
                "warn",
                f"{name}.{table}",
                f"真实库里这些字段没写进元数据：{'、'.join(missing[:12])}"
                + ("…" if len(missing) > 12 else "")
                + "。它们对模型不存在。",
            )


def _load_actual_schema(database: DatabaseSemantics) -> Dict[str, Set[str]]:
    """读真实库结构，返回 {表名: {字段名}}。"""
    if database.dialect == "postgres":
        from ai_sql_agent.retrieval.postgres_loader import PostgresSchemaLoader

        loader = PostgresSchemaLoader(
            dsn=database.resolved_dsn(),
            database_name=database.name,
            sample_size=0,
        )
    else:
        from pathlib import Path

        from ai_sql_agent.retrieval.sqlite_loader import SQLiteSchemaLoader

        if not Path(str(database.db_path)).exists():
            # 文件不在就是「连不上」，交给调用方按环境问题处理。不能当成一个空库去比对：
            # 那样元数据里的每一张表都会被报成「真实库里没有」。
            raise FileNotFoundError(f"数据库文件不存在：{database.db_path}")

        loader = SQLiteSchemaLoader(
            db_path=database.db_path,
            database_name=database.name,
            sample_size=0,
        )

    _, columns, _ = loader.load()
    schema: Dict[str, Set[str]] = {}
    for column in columns:
        schema.setdefault(column.table_name, set()).add(column.column_name)
    return schema


def _check_shared(layer: SemanticLayer, report: ValidationReport) -> None:
    """检查跨库关联键与口径消歧引用的字段是否真的存在于元数据里。"""
    for index, link in enumerate(layer.cross_database_links, start=1):
        where = f"_shared.cross_database_links[{index}]"
        left = str(link.get("left") or "")
        right = str(link.get("right") or "")
        if not left or not right:
            report.add("error", where, "关联键必须同时写 left 和 right。")
            continue
        for side, reference in (("left", left), ("right", right)):
            problem = _resolve_reference(layer, reference)
            if problem:
                report.add("error", f"{where}.{side}", problem)
        if not str(link.get("meaning") or "").strip():
            report.add(
                "warn",
                where,
                "没写 meaning。跨库约束会原样注入 Prompt，"
                "一句「这两个编号指同一个工程」比字段名本身有用得多。",
            )
        if left.split(".")[0] == right.split(".")[0]:
            report.add(
                "warn",
                where,
                "两端在同一个库里。同库关联靠外键和检索就够了，"
                "写进跨库关联键只会让模型多拆一步。",
            )

    for index, item in enumerate(layer.metric_disambiguation, start=1):
        where = f"_shared.metric_disambiguation[{index}]"
        if not str(item.get("term") or "").strip():
            report.add("error", where, "缺少 term（用户会怎么说这个指标）。")
        correct = str(item.get("correct") or "")
        if not correct:
            report.add("error", where, "缺少 correct（正确字段是哪个）。")
        else:
            problem = _resolve_reference(layer, correct.split("（")[0].split("(")[0])
            if problem:
                report.add("warn", f"{where}.correct", problem)

    for term, definition in layer.metric_definitions.items():
        if not str(definition or "").strip():
            report.add("warn", f"_shared.metric_definitions.{term}", "定义为空。")


def _resolve_reference(layer: SemanticLayer, reference: str) -> Optional[str]:
    """校验 `库.表.字段` 引用；返回问题描述，没问题返回 None。"""
    parts = [part for part in reference.strip().split(".") if part]
    if len(parts) != 3:
        return f"「{reference}」格式不对，应为 库.表.字段。"
    database_name, table_name, column_name = parts
    database = layer.databases.get(database_name)
    if database is None:
        return f"库 {database_name} 不在元数据里。"
    table = database.tables.get(table_name)
    if table is None:
        return f"表 {database_name}.{table_name} 不在元数据里。"
    if column_name not in ((table or {}).get("columns") or {}):
        return f"字段 {reference} 不在元数据里。"
    return None


def coverage_summary(layer: SemanticLayer) -> List[Tuple[str, int, int]]:
    """每个库的 (库名, 表数, 字段数)，用于导出与验收时看规模。"""
    summary: List[Tuple[str, int, int]] = []
    for name, database in layer.databases.items():
        columns = sum(
            len((table or {}).get("columns") or {}) for table in database.tables.values()
        )
        summary.append((name, len(database.tables), columns))
    return summary


def format_summary(rows: Sequence[Tuple[str, int, int]]) -> str:
    return "\n".join(f"  {name}: {tables} 张表, {columns} 个字段" for name, tables, columns in rows)
