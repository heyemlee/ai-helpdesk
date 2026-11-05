"""
表概况：一张表的行数、各字段填了多少、取值分布、按月的记录数。

概况只做聚合，但它和预览读的是同一批数据，所以锁的是同一组「不能悄悄变宽」的事，
外加两件它自己的：

1. 请求里的任何字符串都进不了 SQL 文本，拼出来的语句过得了只读校验和授权解析；
2. 看不到的表和字段，在统计里同样不存在——不出现在结果里，也不出现在语句里；
3. 一个字段自己的筛选条件不参与它自己的分布（否则点了一个取值就换不了别的），
   而行数、填充数、合计说的是「当前这批记录」，全部条件都算；
4. 取回来的东西有上限（取值个数、月份个数、取值长度），算不出来的如实说，
   不拿空结果冒充「没有数据」；
5. 同一个人同一组条件不重复扫表，不同的人不共用缓存。
"""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from ai_sql_agent.auth import DataPolicy, check_sql_access
from ai_sql_agent.catalog import CatalogError, DataCatalog, PreviewFilter, PreviewQuery
from ai_sql_agent.catalog.preview import PreviewColumn, PreviewInputError
from ai_sql_agent.catalog.profile import (
    MAX_FACET_VALUE_CHARS,
    MAX_FACET_VALUES,
    MAX_FACETS,
    MAX_PROFILE_COLUMNS,
    MAX_TIMELINE_BUCKETS,
    build_facet_statement,
    build_stats_statement,
    build_timeline_statement,
    declared_values,
    facet_columns,
    order_facet_values,
    split_timeline,
    sum_columns,
    timeline_columns,
)
from ai_sql_agent.core import reload_settings
from ai_sql_agent.execution import MCPExecutionResult, MCPRouter, SQLiteMCPExecutor
from ai_sql_agent.execution.sql_guard import check_readonly_sql
from ai_sql_agent.pipeline.objects import DatabaseConfig
from ai_sql_agent.retrieval.access import ALLOW_ALL
from ai_sql_agent.semantic import DatabaseSemantics, SemanticLayer

COLUMNS = [
    PreviewColumn("ticket_no", "text", primary_key=True, data_type="TEXT"),
    PreviewColumn("status", "text", data_type="TEXT"),
    PreviewColumn("owner", "text", data_type="TEXT"),
    PreviewColumn("amount", "number", data_type="REAL"),
    PreviewColumn("opened_on", "date", data_type="TEXT"),
    PreviewColumn("photo", "binary", data_type="BLOB"),
]
ROLES = {"status": "filter", "owner": "dimension", "amount": "metric", "opened_on": "time"}


def _statements(query, dialect="sqlite", columns=COLUMNS):
    stats, _layout = build_stats_statement(
        "ticket", columns, query, dialect, summed=sum_columns(columns, ROLES)
    )
    by_name = {column.name: column for column in columns}
    return [
        stats,
        build_facet_statement("ticket", by_name["status"], columns, query, dialect),
        build_facet_statement("ticket", by_name["amount"], columns, query, dialect),
        build_timeline_statement("ticket", by_name["opened_on"], columns, query, dialect),
    ]


class ProfileStatementTest(unittest.TestCase):
    """SQL 拼装：纯函数，不连库。"""

    def test_values_are_bound_never_inlined(self):
        hostile = "x' OR '1'='1"
        query = PreviewQuery(
            search=hostile,
            filters=[
                PreviewFilter("owner", "contains", hostile),
                PreviewFilter("amount", "gte", "1200.5"),
            ],
        )
        for dialect, marker in (("sqlite", "?"), ("postgres", "%s")):
            for statement in _statements(query, dialect):
                self.assertNotIn(hostile, statement.sql)
                self.assertNotIn("1200.5", statement.sql)
                self.assertEqual(statement.sql.count(marker), len(statement.params), statement.sql)

    def test_limits_are_parameters_too(self):
        facet = _statements(PreviewQuery())[1]
        timeline = _statements(PreviewQuery())[3]
        self.assertEqual(facet.params, (MAX_FACET_VALUES + 1,))
        self.assertEqual(timeline.params, (MAX_TIMELINE_BUCKETS + 1,))
        self.assertTrue(facet.sql.endswith("LIMIT ?"))

    def test_a_field_is_not_narrowed_by_its_own_filter(self):
        query = PreviewQuery(
            search="glass",
            filters=[
                PreviewFilter("status", "eq", "open"),
                PreviewFilter("amount", "gte", "100"),
            ],
        )
        stats, status, amount, _timeline = _statements(query)

        # 行数与填充数：全部条件都算
        self.assertEqual(stats.params[-2:], ("open", 100))
        # 状态的分布：搜索词和金额条件留着，状态自己的条件不算
        self.assertNotIn("open", status.params)
        self.assertIn(100, status.params)
        self.assertIn("%glass%", status.params)
        # 金额的分布反过来
        self.assertIn("open", amount.params)
        self.assertNotIn(100, amount.params)

    def test_stats_cover_fill_range_and_totals(self):
        statement, layout = build_stats_statement(
            "ticket", COLUMNS, PreviewQuery(), "sqlite", summed=sum_columns(COLUMNS, ROLES)
        )
        self.assertIn('COUNT(*) AS "total_rows"', statement.sql)
        # 文本列里的空字符串算空，和预览的「为空」筛选是一个意思
        self.assertIn("""COUNT(NULLIF("ticket"."status", '')) AS "f_1\"""", statement.sql)
        self.assertIn('MIN("ticket"."amount") AS "lo_3"', statement.sql)
        self.assertIn('TOTAL("ticket"."amount") AS "sum_3"', statement.sql)
        self.assertIn("""MAX(NULLIF("ticket"."opened_on", '')) AS "hi_4\"""", statement.sql)
        # 二进制列只问有没有，不碰内容，也没有最小最大
        self.assertIn('COUNT("ticket"."photo") AS "f_5"', statement.sql)
        self.assertNotIn("lo_5", statement.sql)

        self.assertEqual(layout.columns, tuple(column.name for column in COLUMNS))
        self.assertEqual(layout.ranged, {"amount", "opened_on"})
        self.assertEqual(layout.summed, {"amount"})
        self.assertFalse(layout.limited)

    def test_only_declared_metrics_are_totalled(self):
        # 没标成指标的数字列（编号、年份）不给合计
        plain = [PreviewColumn("year", "number", data_type="INTEGER")]
        self.assertEqual(sum_columns(plain, {}), [])
        self.assertEqual(sum_columns(plain, {"year": "filter"}), [])
        self.assertEqual(sum_columns(plain, {"year": "metric"}), plain)
        # 标成了指标但不是数字列的也不算
        text = [PreviewColumn("note", "text", data_type="TEXT")]
        self.assertEqual(sum_columns(text, {"note": "metric"}), [])

    def test_wide_tables_are_profiled_up_to_a_limit(self):
        wide = [PreviewColumn(f"c{index}", "text", data_type="TEXT") for index in range(MAX_PROFILE_COLUMNS + 5)]
        statement, layout = build_stats_statement("wide", wide, PreviewQuery(), "sqlite")
        self.assertTrue(layout.limited)
        self.assertEqual(len(layout.columns), MAX_PROFILE_COLUMNS)
        self.assertNotIn(f'"c{MAX_PROFILE_COLUMNS}"', statement.sql)

    def test_facets_group_by_the_raw_value_and_cut_on_the_way_out(self):
        status = _statements(PreviewQuery())[1].sql
        cap = MAX_FACET_VALUE_CHARS + 1
        self.assertIn(f'SUBSTR("ticket"."status", 1, {cap})', status)
        self.assertIn('GROUP BY "ticket"."status" ORDER BY COUNT(*) DESC, "ticket"."status" ASC', status)
        self.assertIn('COUNT(*) OVER () AS "k"', status)
        # 空值不算一个取值
        self.assertIn("""("ticket"."status" IS NOT NULL AND CAST("ticket"."status" AS TEXT) <> '')""", status)

        amount = _statements(PreviewQuery())[2].sql
        self.assertIn('SELECT "ticket"."amount" AS "v"', amount)
        self.assertIn('WHERE "ticket"."amount" IS NOT NULL GROUP BY', amount)

    def test_postgres_shapes(self):
        columns = [
            PreviewColumn("id", "text", primary_key=True, data_type="uuid"),
            PreviewColumn("status", "text", data_type="character varying"),
            PreviewColumn("amount", "number", data_type="numeric(12,2)"),
            PreviewColumn("opened_on", "date", data_type="timestamp with time zone"),
            PreviewColumn("day_text", "date", data_type="text"),
        ]
        by_name = {column.name: column for column in columns}
        stats, _ = build_stats_statement(
            "ticket", columns, PreviewQuery(), "postgres", summed=[by_name["amount"]]
        )
        # uuid 归在文本里，但不能直接和 '' 比
        self.assertIn("""COUNT(NULLIF(CAST("ticket"."id" AS TEXT), '')) AS "f_0\"""", stats.sql)
        self.assertIn('SUM("ticket"."amount") AS "sum_2"', stats.sql)
        self.assertNotIn("TOTAL(", stats.sql)
        # 原生的时间戳列不和 '' 比；存成文本的日期才要
        self.assertIn('MIN("ticket"."opened_on") AS "lo_3"', stats.sql)
        self.assertIn("""MIN(NULLIF(CAST("ticket"."day_text" AS TEXT), '')) AS "lo_4\"""", stats.sql)

        native = build_timeline_statement("ticket", by_name["opened_on"], columns, PreviewQuery(), "postgres")
        self.assertIn("""TO_CHAR("ticket"."opened_on", 'YYYY-MM') AS "v\"""", native.sql)
        self.assertIn('WHERE "ticket"."opened_on" IS NOT NULL GROUP BY TO_CHAR', native.sql)
        stored = build_timeline_statement("ticket", by_name["day_text"], columns, PreviewQuery(), "postgres")
        self.assertIn('SUBSTR(CAST("ticket"."day_text" AS TEXT), 1, 7) AS "v"', stored.sql)

        facet = build_facet_statement("ticket", by_name["status"], columns, PreviewQuery(), "postgres")
        self.assertIn(f'LEFT(CAST("ticket"."status" AS TEXT), {MAX_FACET_VALUE_CHARS + 1}) AS "v"', facet.sql)
        self.assertTrue(facet.sql.endswith("LIMIT %s"))
        self.assertNotIn("TYPEOF", facet.sql)

    def test_statements_pass_the_readonly_guard_and_the_authorization_parser(self):
        """
        拼出来的语句要过两道独立的关：执行器的只读校验（按关键字扫）与授权解析（按 AST）。
        聚合别名（v / n / k / f_0 …）不能被当成列去问权限。
        """
        query = PreviewQuery(search="a", filters=[PreviewFilter("status", "eq", "open")])
        narrow = DataPolicy(allow_tables=("svc_db.ticket",), deny_columns=("svc_db.ticket.secret",))
        for dialect in ("sqlite", "postgres"):
            for statement in _statements(query, dialect):
                self.assertTrue(
                    check_readonly_sql(statement.sql, dialect=dialect).allowed, statement.sql
                )
                for policy in (ALLOW_ALL, narrow):
                    decision = check_sql_access(
                        policy, database="svc_db", sql=statement.sql, dialect=dialect
                    )
                    self.assertTrue(decision.allowed, f"{decision.reason}: {statement.sql}")

    def test_a_denied_column_is_still_stopped_at_the_gate(self):
        # 清单这一层万一判错，把禁列写进了语句：执行器入口的关卡照样拒
        policy = DataPolicy(allow_tables=("svc_db.ticket",), deny_columns=("svc_db.ticket.owner",))
        by_name = {column.name: column for column in COLUMNS}
        leaked = build_facet_statement("ticket", by_name["owner"], COLUMNS, PreviewQuery(), "sqlite")
        decision = check_sql_access(policy, database="svc_db", sql=leaked.sql, dialect="sqlite")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.denied_columns, ("ticket.owner",))

        stats, _ = build_stats_statement("ticket", COLUMNS, PreviewQuery(), "sqlite")
        self.assertFalse(
            check_sql_access(policy, database="svc_db", sql=stats.sql, dialect="sqlite").allowed
        )

    def test_filters_are_checked_like_the_preview_checks_them(self):
        for query, code in (
            (PreviewQuery(filters=[PreviewFilter("secret", "eq", "1")]), "unknown_column"),
            (PreviewQuery(filters=[PreviewFilter("status", "regexp", "a")]), "unknown_operator"),
            (PreviewQuery(filters=[PreviewFilter("amount", "gte", "abc")]), "invalid_value"),
            (PreviewQuery(filters=[PreviewFilter("status", "eq", "a")] * 6), "too_many_filters"),
        ):
            with self.assertRaises(PreviewInputError) as caught:
                _statements(query)
            self.assertEqual(caught.exception.code, code)

    def test_identifiers_are_quoted(self):
        odd = [PreviewColumn('odd "name"', "text", data_type="TEXT")]
        facet = build_facet_statement('my "table"', odd[0], odd, PreviewQuery(), "sqlite")
        self.assertIn('"my ""table"""."odd ""name"""', facet.sql)
        self.assertIn('FROM "my ""table"""', facet.sql)

    # ---- 选哪些字段、取值怎么排 ----

    def test_declared_values_are_read_from_the_value_range_convention(self):
        self.assertEqual(declared_values("待处理 / 处理中 / 已解决"), ("待处理", "处理中", "已解决"))
        self.assertEqual(declared_values("paid/partial"), ("paid", "partial"))
        self.assertEqual(declared_values("a / b / a"), ("a", "b"))
        # 写成范围的不是清单
        for text in ("0-1", "> 0", "", None, "a /", "/"):
            self.assertEqual(declared_values(text), (), text)
        self.assertEqual(declared_values("x / " + "y" * (MAX_FACET_VALUE_CHARS + 1)), ())

    def test_facet_columns_are_ranked_by_how_likely_they_are_to_be_useful(self):
        columns = [
            PreviewColumn("id", "text", primary_key=True, data_type="TEXT"),
            PreviewColumn("free_text", "text", data_type="TEXT"),
            PreviewColumn("order_ref", "text", data_type="TEXT"),
            PreviewColumn("region", "text", data_type="TEXT"),
            PreviewColumn("status", "text", data_type="TEXT"),
            PreviewColumn("is_rush", "number", data_type="INTEGER"),
            PreviewColumn("amount", "number", data_type="REAL"),
            PreviewColumn("opened_on", "date", data_type="TEXT"),
            PreviewColumn("photo", "binary", data_type="BLOB"),
            PreviewColumn("notes", "text", data_type="TEXT"),
        ]
        roles = {
            "id": "join_key", "order_ref": "join_key", "region": "dimension",
            "status": "filter", "is_rush": "filter", "amount": "metric",
            "opened_on": "time", "notes": "text",
        }
        declared = {"status": ("open", "closed")}
        documented = frozenset(roles)
        chosen = facet_columns(columns, roles, declared, documented, "sqlite")
        # 声明了取值清单的排最前，其次是筛选 / 维度，再次是关联键，最后是没写说明的文本列
        self.assertEqual(
            [column.name for column in chosen],
            ["status", "region", "is_rush", "order_ref", "free_text"],
        )
        # 单列主键、指标、日期、二进制、标成长文本的都不算
        self.assertFalse({"id", "amount", "opened_on", "photo", "notes"} & {c.name for c in chosen})

        many = [PreviewColumn(f"d{index}", "text", data_type="TEXT") for index in range(MAX_FACETS + 4)]
        self.assertEqual(
            len(facet_columns(many, {c.name: "filter" for c in many}, {}, frozenset(), "sqlite")),
            MAX_FACETS,
        )

    def test_composite_keys_and_postgres_types(self):
        # 联合主键的每一列单独看并不唯一，可以算分布
        composite = [
            PreviewColumn("order_no", "text", primary_key=True, data_type="TEXT"),
            PreviewColumn("line_no", "number", primary_key=True, data_type="INTEGER"),
        ]
        chosen = facet_columns(composite, {"order_no": "join_key"}, {}, frozenset({"order_no"}), "sqlite")
        self.assertEqual([column.name for column in chosen], ["order_no"])

        # PostgreSQL 的 json 没有分组用的比较运算符；布尔可以
        typed = [
            PreviewColumn("payload", "other", data_type="jsonb"),
            PreviewColumn("active", "other", data_type="boolean"),
        ]
        roles = {"payload": "filter", "active": "filter"}
        chosen = facet_columns(typed, roles, {}, frozenset(roles), "postgres")
        self.assertEqual([column.name for column in chosen], ["active"])

    def test_timelines_take_the_first_date_columns(self):
        dated = [PreviewColumn(f"d{index}", "date", data_type="TEXT") for index in range(4)]
        self.assertEqual([column.name for column in timeline_columns(dated)], ["d0", "d1"])

    def test_declared_order_is_used_only_when_it_matches_the_data(self):
        seen = [{"value": "closed", "count": 7}, {"value": "open", "count": 3}]
        ordered, order = order_facet_values(seen, ("open", "waiting", "closed"), True)
        self.assertEqual(order, "declared")
        self.assertEqual(
            ordered,
            [{"value": "open", "count": 3}, {"value": "waiting", "count": 0}, {"value": "closed", "count": 7}],
        )
        # 库里出现了清单里没有的取值：清单和库对不上，照实际的排
        drifted = seen + [{"value": "reopened", "count": 1}]
        self.assertEqual(order_facet_values(drifted, ("open", "closed"), True), (drifted, "count"))
        # 取值没取全时也不套清单：补出来的 0 可能是假的
        self.assertEqual(order_facet_values(seen, ("open", "closed"), False), (seen, "count"))
        self.assertEqual(order_facet_values(seen, (), True), (seen, "count"))
        # 数字取值按它的文本去对清单
        flags = [{"value": 0, "count": 9}, {"value": 1, "count": 2}]
        self.assertEqual(order_facet_values(flags, ("1", "0"), True)[0], [flags[1], flags[0]])

    def test_timeline_keeps_only_real_months(self):
        buckets, unparsed = split_timeline([
            {"v": "2026-01", "n": 4}, {"v": "2026-13", "n": 2}, {"v": "03/31/2", "n": 5},
            {"v": None, "n": 1}, {"v": "2026-02", "n": "6"},
        ])
        self.assertEqual(buckets, [{"period": "2026-01", "count": 4}, {"period": "2026-02", "count": 6}])
        self.assertEqual(unparsed, 8)


SCRIPT = """
CREATE TABLE ticket (
    ticket_no TEXT PRIMARY KEY,
    order_no TEXT NOT NULL,
    status TEXT NOT NULL,
    channel TEXT,
    owner TEXT,
    amount REAL,
    qty INTEGER,
    opened_on TEXT,
    closed_on TEXT,
    notes TEXT,
    secret_cost REAL
);
CREATE TABLE vault (id INTEGER PRIMARY KEY, code TEXT);
"""

TICKETS = [
    # ticket_no, order_no, status, channel, owner, amount, qty, opened_on, closed_on, notes, secret_cost
    ("T-01", "J-1", "open", "phone", "Ana", 100.0, 1, "2026-01-05", None, "a", 9.0),
    ("T-02", "J-1", "open", "phone", "Ana", 250.5, 2, "2026-01-20", "", "b", 9.0),
    ("T-03", "J-2", "closed", "email", "Ben", 40.0, 1, "2026-02-11", "2026-02-15", None, 9.0),
    ("T-04", "J-3", "closed", "email", "", 60.0, 5, "2026-02-28", "2026-03-02", "", 9.0),
    ("T-05", "J-3", "closed", "portal", "Cy", None, 3, "2026-03-09", "2026-03-30", "e", 9.0),
    ("T-06", "J-4", "closed", None, "Ana", 10.0, 1, "03/31/2026", "2026-04-01", "f", 9.0),
]

TABLES = {
    "ticket": {
        "description": "售后工单",
        "columns": {
            "ticket_no": {"description": "工单号", "semantic_role": "join_key"},
            "order_no": {"description": "订单号", "semantic_role": "join_key"},
            "status": {
                "description": "状态", "semantic_role": "filter",
                "value_range": "open / waiting / closed",
            },
            "channel": {"description": "渠道", "semantic_role": "dimension"},
            "owner": {"description": "负责人", "semantic_role": "filter"},
            "amount": {"description": "金额", "semantic_role": "metric", "unit": "USD"},
            "qty": {"description": "数量"},
            "opened_on": {"description": "报修日期", "semantic_role": "time"},
            "closed_on": {"description": "解决日期", "semantic_role": "time"},
            "notes": {"description": "备注", "semantic_role": "text"},
            "secret_cost": {"description": "成本", "semantic_role": "metric"},
        },
    },
}


class _SpyRouter:
    """记下经过路由的每一条语句；可以让某一类语句失败（模拟超时）。"""

    def __init__(self, inner, fail_when=None):
        self.inner = inner
        self.executors = inner.executors
        self.statements = []
        self.fail_when = fail_when

    def execute(self, request):
        self.statements.append(request.sql)
        if self.fail_when and self.fail_when(request.sql):
            return MCPExecutionResult(
                database=request.database, sql=request.sql, success=False, error="SQL 执行超时"
            )
        return self.inner.execute(request)


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _build(directory, *, rows=TICKETS, fail_when=None, clock=None, max_rows=1000):
    db_path = Path(directory) / "svc.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCRIPT)
    conn.executemany("INSERT INTO ticket VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()

    semantic = SemanticLayer(
        databases={
            "svc_db": DatabaseSemantics(name="svc_db", description="", db_path=str(db_path), tables=TABLES)
        },
        source="test",
    )
    databases = [DatabaseConfig(name="svc_db", db_path=db_path)]
    inner = MCPRouter()
    inner.register_executor("svc_db", SQLiteMCPExecutor("svc_db", db_path, max_rows=max_rows))
    router = _SpyRouter(inner, fail_when)
    options = {"clock": clock} if clock is not None else {}
    return DataCatalog(semantic=semantic, databases=databases, router=router, **options), router


class ProfileServiceTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.directory = Path(self._tmpdir.name)

    def _profile(self, catalog, access=ALLOW_ALL, **options):
        options.setdefault("query", PreviewQuery())
        return catalog.profile(access, database="svc_db", table="ticket", **options)

    @staticmethod
    def _column(profile, name):
        return next(item for item in profile["columns"] if item["name"] == name)

    @staticmethod
    def _facet(profile, name):
        return next(item for item in profile["facets"] if item["column"] == name)

    def test_counts_fill_ranges_and_totals(self):
        catalog, _router = _build(self.directory)
        profile = self._profile(catalog)

        self.assertEqual(profile["total_rows"], 6)
        self.assertFalse(profile["filtered"])
        self.assertEqual(self._column(profile, "ticket_no"), {"name": "ticket_no", "filled": 6})
        # NULL 和空字符串都算没填
        self.assertEqual(self._column(profile, "owner")["filled"], 5)
        self.assertEqual(self._column(profile, "notes")["filled"], 4)
        self.assertEqual(self._column(profile, "closed_on")["filled"], 4)
        self.assertEqual(
            self._column(profile, "amount"),
            {"name": "amount", "filled": 5, "min": 10.0, "max": 250.5, "sum": 460.5},
        )
        # 语义层没标成指标的数字列：有范围，没有合计
        self.assertEqual(self._column(profile, "qty"), {"name": "qty", "filled": 6, "min": 1, "max": 5})
        # 日期范围不把空字符串当成最早的一天
        self.assertEqual(self._column(profile, "closed_on")["min"], "2026-02-15")
        self.assertEqual(profile["dialect"], "sqlite")
        self.assertFalse(profile["columns_hidden"])
        self.assertEqual(profile["skipped"], [])

    def test_declared_values_come_back_in_their_declared_order(self):
        catalog, _router = _build(self.directory)
        status = self._facet(self._profile(catalog), "status")
        self.assertEqual(
            status["values"],
            [{"value": "open", "count": 2}, {"value": "waiting", "count": 0}, {"value": "closed", "count": 4}],
        )
        self.assertEqual((status["ordered_by"], status["complete"], status["distinct"]), ("declared", True, 2))
        self.assertFalse(status["own_filters_excluded"])

    def test_undeclared_values_are_ranked_by_count_and_empties_are_left_out(self):
        catalog, _router = _build(self.directory)
        profile = self._profile(catalog)

        channel = self._facet(profile, "channel")
        self.assertEqual(
            channel["values"],
            [{"value": "email", "count": 2}, {"value": "phone", "count": 2}, {"value": "portal", "count": 1}],
        )
        self.assertEqual(channel["ordered_by"], "count")
        owner = self._facet(profile, "owner")
        self.assertEqual([item["value"] for item in owner["values"]], ["Ana", "Ben", "Cy"])
        # 关联键也给分布（每个订单几张工单）；单列主键、指标、长文本不给
        self.assertEqual(self._facet(profile, "order_no")["values"][0], {"value": "J-1", "count": 2})
        self.assertEqual(
            [facet["column"] for facet in profile["facets"]],
            ["status", "channel", "owner", "order_no"],
        )

    def test_a_field_keeps_its_full_distribution_under_its_own_filter(self):
        catalog, _router = _build(self.directory)
        query = PreviewQuery(filters=[PreviewFilter("status", "eq", "closed")])
        profile = self._profile(catalog, query=query)

        self.assertTrue(profile["filtered"])
        self.assertEqual(profile["total_rows"], 4)
        self.assertEqual(self._column(profile, "amount")["sum"], 110.0)
        status = self._facet(profile, "status")
        self.assertTrue(status["own_filters_excluded"])
        self.assertEqual([item["count"] for item in status["values"]], [2, 0, 4])
        # 别的字段跟着这批记录变
        channel = self._facet(profile, "channel")
        self.assertFalse(channel["own_filters_excluded"])
        self.assertEqual(channel["values"], [{"value": "email", "count": 2}, {"value": "portal", "count": 1}])

    def test_timeline_counts_records_per_month(self):
        catalog, _router = _build(self.directory)
        profile = self._profile(catalog)

        opened, closed = profile["timelines"]
        self.assertEqual((opened["column"], opened["grain"]), ("opened_on", "month"))
        self.assertEqual(
            opened["buckets"],
            [{"period": "2026-01", "count": 2}, {"period": "2026-02", "count": 2}, {"period": "2026-03", "count": 1}],
        )
        # 不是 ISO 写法的那一条认不出月份：单独计数，不丢，也不硬塞进某个月
        self.assertEqual(opened["unparsed"], 1)
        self.assertFalse(opened["truncated"])
        self.assertEqual(sum(item["count"] for item in closed["buckets"]), 4)

    def test_timeline_ignores_its_own_date_filter(self):
        catalog, _router = _build(self.directory)
        query = PreviewQuery(filters=[
            PreviewFilter("opened_on", "gte", "2026-02-01"),
            PreviewFilter("opened_on", "lt", "2026-03-01"),
        ])
        profile = self._profile(catalog, query=query)
        self.assertEqual(profile["total_rows"], 2)
        opened, closed = profile["timelines"]
        self.assertTrue(opened["own_filters_excluded"])
        self.assertEqual(len(opened["buckets"]), 3)
        self.assertFalse(closed["own_filters_excluded"])
        self.assertEqual(sum(item["count"] for item in closed["buckets"]), 2)

    def test_search_narrows_everything(self):
        catalog, _router = _build(self.directory)
        profile = self._profile(catalog, query=PreviewQuery(search="Ana"))
        self.assertEqual(profile["total_rows"], 3)
        self.assertEqual([item["count"] for item in self._facet(profile, "status")["values"]], [2, 0, 1])

    def test_counts_only_runs_a_single_statement(self):
        catalog, router = _build(self.directory)
        profile = self._profile(catalog, breakdowns=False)
        self.assertEqual(profile["total_rows"], 6)
        self.assertEqual((profile["facets"], profile["timelines"]), ([], []))
        self.assertFalse(profile["breakdowns"])
        self.assertEqual(len(router.statements), 1)
        self.assertEqual(profile["sql"], router.statements)

    def test_many_values_are_cut_and_said_to_be_incomplete(self):
        rows = [
            (f"T-{index:03d}", f"J-{index % 40}", "open", None, f"owner {index % 30:02d}", 1.0, 1,
             "2026-01-01", None, None, 1.0)
            for index in range(120)
        ]
        catalog, _router = _build(self.directory, rows=rows)
        owner = self._facet(self._profile(catalog), "owner")
        self.assertEqual(len(owner["values"]), MAX_FACET_VALUES)
        self.assertFalse(owner["complete"])
        self.assertEqual(owner["distinct"], 30)
        self.assertEqual(owner["ordered_by"], "count")

    def test_long_values_are_cut_and_marked(self):
        long_name = "x" * (MAX_FACET_VALUE_CHARS + 40)
        rows = [("T-1", "J-1", "open", None, long_name, 1.0, 1, "2026-01-01", None, None, 1.0)]
        catalog, _router = _build(self.directory, rows=rows)
        value = self._facet(self._profile(catalog), "owner")["values"][0]
        self.assertEqual(len(value["value"]), MAX_FACET_VALUE_CHARS + 1)
        self.assertTrue(value["value"].endswith("…"))
        self.assertTrue(value["truncated"])

    def test_a_low_executor_cap_makes_the_distribution_incomplete_not_wrong(self):
        catalog, _router = _build(self.directory, max_rows=2)
        profile = self._profile(catalog)
        channel = self._facet(profile, "channel")
        self.assertEqual(len(channel["values"]), 2)
        self.assertFalse(channel["complete"])
        self.assertEqual(channel["distinct"], 3)
        # 声明的顺序只在取全了的时候用
        self.assertEqual(self._facet(profile, "owner")["ordered_by"], "count")
        self.assertTrue(profile["timelines"][0]["truncated"])

    # ---- 权限 ----

    def test_hidden_columns_exist_nowhere_in_the_profile(self):
        catalog, router = _build(self.directory)
        policy = DataPolicy(
            allow_tables=("svc_db.ticket",),
            deny_columns=("svc_db.ticket.secret_cost", "svc_db.ticket.owner"),
        )
        profile = self._profile(catalog, access=policy)

        text = json.dumps(profile, ensure_ascii=False)
        self.assertNotIn("secret_cost", text)
        self.assertNotIn("owner", text)
        self.assertTrue(profile["columns_hidden"])
        self.assertTrue(all("owner" not in sql and "secret_cost" not in sql for sql in router.statements))
        self.assertEqual(profile["total_rows"], 6)

        with self.assertRaises(CatalogError) as caught:
            self._profile(catalog, access=policy, query=PreviewQuery(filters=[PreviewFilter("owner", "eq", "Ana")]))
        self.assertEqual((caught.exception.code, caught.exception.status), ("unknown_column", 400))

    def test_hidden_or_missing_tables_give_the_same_answer(self):
        catalog, router = _build(self.directory)
        policy = DataPolicy(allow_tables=("svc_db.ticket",))
        for table in ("vault", "no_such_table"):
            with self.assertRaises(CatalogError) as caught:
                catalog.profile(policy, database="svc_db", table=table, query=PreviewQuery())
            self.assertEqual((caught.exception.code, caught.exception.status), ("not_found", 404), table)
        with self.assertRaises(CatalogError):
            catalog.profile(ALLOW_ALL, database="other_db", table="ticket", query=PreviewQuery())
        self.assertEqual(router.statements, [])

    def test_executor_without_parameters_is_not_supported(self):
        catalog, router = _build(self.directory)
        router.executors["svc_db"].supports_params = False
        with self.assertRaises(CatalogError) as caught:
            self._profile(catalog)
        self.assertEqual((caught.exception.code, caught.exception.status), ("unsupported", 501))

    # ---- 算不出来 ----

    def test_failed_statistics_are_an_error_not_an_empty_profile(self):
        clock = _Clock()
        catalog, router = _build(
            self.directory, fail_when=lambda sql: '"total_rows"' in sql, clock=clock
        )
        with self.assertRaises(CatalogError) as caught:
            self._profile(catalog)
        self.assertEqual((caught.exception.code, caught.exception.status), ("timeout", 504))
        self.assertEqual(len(router.statements), 1)

        # 一分钟内不再试：每试一次就是一遍跑满超时的全表扫描
        with self.assertRaises(CatalogError) as again:
            self._profile(catalog)
        self.assertEqual(again.exception.code, "timeout")
        self.assertEqual(len(router.statements), 1)

        clock.now += 61
        with self.assertRaises(CatalogError):
            self._profile(catalog)
        self.assertEqual(len(router.statements), 2)

    def test_one_failed_distribution_does_not_take_the_rest_down(self):
        catalog, router = _build(
            self.directory, fail_when=lambda sql: 'GROUP BY "ticket"."channel"' in sql
        )
        profile = self._profile(catalog)
        self.assertEqual(profile["skipped"], [{"column": "channel", "part": "values", "reason": "timeout"}])
        self.assertEqual([facet["column"] for facet in profile["facets"]], ["status", "owner", "order_no"])
        self.assertEqual(len(profile["timelines"]), 2)
        self.assertEqual(profile["total_rows"], 6)
        # 没跑成的那条语句不算在「跑过的语句」里
        self.assertEqual(len(profile["sql"]), len(router.statements) - 1)

    def test_the_time_budget_stops_further_scans(self):
        clock = _Clock()
        catalog, router = _build(self.directory, clock=clock)

        def slow(request, inner=router.inner.execute):
            clock.now += 6.0            # 每条语句都「跑了」六秒
            return inner(request)

        router.inner.execute = slow
        profile = self._profile(catalog)
        # 预算是单条超时（5 秒）的两倍：统计 + 一个分布之后就不再往下跑
        self.assertEqual(len(router.statements), 2)
        self.assertEqual([facet["column"] for facet in profile["facets"]], ["status"])
        self.assertEqual(
            {(item["column"], item["reason"]) for item in profile["skipped"]},
            {("channel", "budget"), ("owner", "budget"), ("order_no", "budget"),
             ("opened_on", "budget"), ("closed_on", "budget")},
        )

    # ---- 缓存 ----

    def test_repeated_views_do_not_rescan(self):
        clock = _Clock()
        catalog, router = _build(self.directory, clock=clock)
        first = self._profile(catalog, caller="web", user_id="ana")
        ran = len(router.statements)
        self.assertGreater(ran, 4)

        again = self._profile(catalog, caller="web", user_id="ana")
        self.assertEqual(len(router.statements), ran)
        self.assertEqual(again["facets"], first["facets"])

        # 行级权限按用户生效：换一个人要重新数
        self._profile(catalog, caller="web", user_id="ben")
        self.assertEqual(len(router.statements), ran * 2)
        # 换一组条件、或者只要行数，也是另一份
        self._profile(catalog, caller="web", user_id="ana", query=PreviewQuery(search="x"))
        self._profile(catalog, caller="web", user_id="ana", breakdowns=False)
        self.assertEqual(len(router.statements), ran * 3 + 1)

        clock.now += 16
        self._profile(catalog, caller="web", user_id="ana")
        self.assertEqual(len(router.statements), ran * 4 + 1)

    def test_a_different_set_of_visible_columns_is_a_different_cache_entry(self):
        catalog, _router = _build(self.directory)
        full = self._profile(catalog, caller="web", user_id="ana")
        narrow = self._profile(
            catalog,
            access=DataPolicy(allow_tables=("svc_db.ticket",), deny_columns=("svc_db.ticket.owner",)),
            caller="web",
            user_id="ana",
        )
        self.assertIn("owner", [item["name"] for item in full["columns"]])
        self.assertNotIn("owner", [item["name"] for item in narrow["columns"]])


class ProfileApiTest(unittest.TestCase):
    """HTTP 这一层：身份、开关、限流，以及请求体里塞不进 SQL。用的是内置演示库。"""

    ENV_KEYS = (
        "AI_SQL_AUTH_MODE",
        "AI_SQL_API_KEYS_FILE",
        "AI_SQL_AUDIT_DB_PATH",
        "AI_SQL_MEMORY_DB_PATH",
        "AI_SQL_SEMANTIC_DIR",
        "AI_SQL_CATALOG_RATE_LIMIT_PER_MINUTE",
        "AI_SQL_CATALOG_ENABLED",
        "AI_SQL_CATALOG_PROFILE_ENABLED",
    )

    @classmethod
    def setUpClass(cls):
        from ai_sql_agent.api.app import app

        cls.tmpdir = tempfile.TemporaryDirectory()
        key_file = Path(cls.tmpdir.name) / "keys.json"
        key_file.write_text(
            json.dumps({
                "keys": [
                    {"key": "admin-key", "caller": "ops", "user_id": "ops",
                     "roles": ["everything"], "is_admin": True},
                    {"key": "buyer-key", "caller": "buyer", "user_id": "buyer",
                     "roles": ["purchasing"]},
                    # 限流用例专用：令牌桶按调用方记账，和别的用例共用一把 Key 会互相饿死
                    {"key": "slow-key", "caller": "slow-profile", "user_id": "slow",
                     "roles": ["everything"], "rate_limit_per_minute": 2},
                ],
                "policies": {
                    "everything": {"allow_tables": ["*"]},
                    "purchasing": {
                        "allow_tables": ["erp_db.*"],
                        "deny_tables": ["erp_db.payment"],
                        "deny_columns": ["erp_db.supplier.on_time_rate"],
                    },
                },
            }),
            encoding="utf-8",
        )
        cls._saved = {key: os.environ.get(key) for key in cls.ENV_KEYS}
        os.environ.update({
            "AI_SQL_AUTH_MODE": "api_key",
            "AI_SQL_API_KEYS_FILE": str(key_file),
            "AI_SQL_AUDIT_DB_PATH": str(Path(cls.tmpdir.name) / "audit.db"),
            "AI_SQL_MEMORY_DB_PATH": str(Path(cls.tmpdir.name) / "memory.db"),
            # 空串 = 用内置演示库；显式写出来，本机 .env 里钉的目录就不会漏进来
            "AI_SQL_SEMANTIC_DIR": "",
            "AI_SQL_CATALOG_RATE_LIMIT_PER_MINUTE": "1000",
            "AI_SQL_CATALOG_ENABLED": "true",
            "AI_SQL_CATALOG_PROFILE_ENABLED": "true",
        })
        reload_settings()
        cls.client_ctx = TestClient(app)
        cls.client = cls.client_ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_ctx.__exit__(None, None, None)
        for key, value in cls._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reload_settings()
        cls.tmpdir.cleanup()

    @staticmethod
    def _key(value):
        return {"X-API-Key": value}

    def _post(self, path, key="admin-key", **body):
        return self.client.post(path, headers=self._key(key), json=body)

    def test_profile_requires_a_key(self):
        self.assertEqual(self.client.post("/catalog/erp_db/supplier/profile", json={}).status_code, 401)

    def test_profile_of_a_demo_table(self):
        response = self._post("/catalog/erp_db/supplier/profile")
        self.assertEqual(response.status_code, 200)
        body = response.json()

        self.assertGreater(body["total_rows"], 0)
        names = [column["name"] for column in body["columns"]]
        self.assertIn("supplier_name", names)
        self.assertTrue(all(column["filled"] <= body["total_rows"] for column in body["columns"]))
        self.assertTrue(body["trace_id"])
        self.assertTrue(all(sql.startswith("SELECT ") for sql in body["sql"]))
        # 只有聚合：响应里没有任何一行记录
        self.assertNotIn("rows", body)

        rows = self._post("/catalog/erp_db/supplier/rows", page_size=1).json()
        self.assertEqual(body["total_rows"], rows["total_rows"])

    def test_profile_and_preview_agree_under_the_same_filters(self):
        filters = [{"column": "supplier_name", "op": "contains", "value": "a"}]
        profile = self._post("/catalog/erp_db/supplier/profile", filters=filters, breakdowns=False).json()
        rows = self._post("/catalog/erp_db/supplier/rows", filters=filters, page_size=1).json()
        self.assertEqual(profile["total_rows"], rows["total_rows"])
        self.assertTrue(profile["filtered"])

    def test_profile_follows_the_same_policy_as_the_preview(self):
        allowed = self._post("/catalog/erp_db/supplier/profile", key="buyer-key")
        self.assertEqual(allowed.status_code, 200)
        self.assertNotIn("on_time_rate", allowed.text)
        self.assertTrue(allowed.json()["columns_hidden"])

        for path in ("/catalog/erp_db/payment/profile", "/catalog/pm_db/project/profile"):
            denied = self._post(path, key="buyer-key")
            self.assertEqual(denied.status_code, 404, path)
            self.assertEqual(denied.json()["detail"]["code"], "not_found")

        hidden_filter = self._post(
            "/catalog/erp_db/supplier/profile",
            key="buyer-key",
            filters=[{"column": "on_time_rate", "op": "lt", "value": "0.9"}],
        )
        self.assertEqual(hidden_filter.status_code, 400)
        self.assertEqual(hidden_filter.json()["detail"]["code"], "unknown_column")

    def test_request_body_cannot_carry_sql_or_paging(self):
        for payload in (
            {"sql": "SELECT * FROM payment"},
            {"columns": ["on_time_rate"]},
            {"group_by": "supplier_name"},
            {"page": 2},
            {"filters": [{"column": "supplier_name", "op": "eq", "value": "x", "raw": "1=1"}]},
        ):
            response = self._post("/catalog/erp_db/supplier/profile", **payload)
            self.assertEqual(response.status_code, 422, payload)

    def test_service_capabilities_say_whether_profiles_are_available(self):
        service = self.client.get("/whoami", headers=self._key("admin-key")).json()["service"]
        self.assertEqual(
            service["profile"],
            {"enabled": True, "max_facets": MAX_FACETS, "max_facet_values": MAX_FACET_VALUES, "max_timelines": 2},
        )

    def test_profiles_can_be_switched_off_without_touching_the_preview(self):
        os.environ["AI_SQL_CATALOG_PROFILE_ENABLED"] = "false"
        reload_settings()
        try:
            off = self._post("/catalog/erp_db/supplier/profile")
            self.assertEqual(off.status_code, 404)
            self.assertEqual(off.json()["detail"]["code"], "profile_disabled")
            service = self.client.get("/whoami", headers=self._key("admin-key")).json()["service"]
            self.assertFalse(service["profile"]["enabled"])
            self.assertEqual(self._post("/catalog/erp_db/supplier/rows").status_code, 200)
        finally:
            os.environ["AI_SQL_CATALOG_PROFILE_ENABLED"] = "true"
            reload_settings()

    def test_browsing_switch_covers_profiles(self):
        os.environ["AI_SQL_CATALOG_ENABLED"] = "false"
        reload_settings()
        try:
            off = self._post("/catalog/erp_db/supplier/profile")
            self.assertEqual(off.status_code, 404)
            self.assertEqual(off.json()["detail"]["code"], "disabled")
        finally:
            os.environ["AI_SQL_CATALOG_ENABLED"] = "true"
            reload_settings()

    def test_profiles_share_the_browsing_rate_limit(self):
        # 运维把这把 Key 压到每分钟 2 次：概况和预览、目录算在同一个桶里
        statuses = [
            self._post("/catalog/erp_db/supplier/profile", key="slow-key", breakdowns=False).status_code
            for _ in range(3)
        ]
        self.assertEqual(statuses, [200, 200, 429])


if __name__ == "__main__":
    unittest.main()
