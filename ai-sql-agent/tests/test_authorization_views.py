"""
视图遮住基表：`deny_tables: ["app.payroll"]` 拦得住 `SELECT * FROM payroll`，
拦不住 `SELECT * FROM v_payroll`——SQLite 的 authorizer 回调在读视图时报告的是基表被读了，
而 SQL 文本里根本没有这个名字。

关卡自己推不出哪些名字是视图，所以由 `ai_sql_agent.auth.views.ViewCatalog` 提供
（来自数据库目录，随授权作用域交给关卡）。这里钉三件事：

1. 目录在时，受限策略读视图 = 读它的全部基表（视图套视图也展开到底）；
2. **拿不准就拒绝**：视图定义解析不了、目录里没登记的对象（扫描之后才建的）、
   目录扫描失败，对受限角色一律拒绝；没有东西可遮的策略不受影响；
3. 这个库从没登记过目录时关卡按老样子判（没有信息可依据）——这是一个**前提**，
   不是安全属性：管道启动时会为每个库登记。

PostgreSQL 的目录扫描（`scan_postgres`）没有在真实 PostgreSQL 上验证，这里只测 SQLite。
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from ai_sql_agent.auth import DataPolicy, ViewCatalog, authorization_scope, check_sql_access
from ai_sql_agent.auth.objects import IntersectionPolicy
from ai_sql_agent.auth.views import FAILED, LOADED, UNREGISTERED, scan_sqlite
from ai_sql_agent.execution import MCPExecutionRequest, SQLiteMCPExecutor
from ai_sql_agent.retrieval.access import ALLOW_ALL

SCHEMA = """
CREATE TABLE employees(id INTEGER PRIMARY KEY, name TEXT, salary INT);
INSERT INTO employees VALUES (101,'ann',9000),(102,'bob',4500);
CREATE TABLE payroll(emp INT, amount INT);
INSERT INTO payroll VALUES (101,1),(102,2);
CREATE INDEX idx_payroll_emp ON payroll(emp);

CREATE VIEW v_payroll AS SELECT emp, amount FROM payroll;
CREATE VIEW v_chain AS SELECT emp FROM v_payroll;
CREATE VIEW v_names AS SELECT id, name FROM employees;
CREATE VIEW v_join AS
  SELECT e.name, p.amount FROM employees e JOIN payroll p ON p.emp = e.id;
-- sqlglot 的 SQLite 方言解析不了 INDEXED BY：这个视图的定义是「不透明」的
CREATE VIEW v_opaque AS SELECT emp FROM payroll INDEXED BY idx_payroll_emp;
"""

DENY_PAYROLL = DataPolicy(deny_tables=("app.payroll",))
DENY_SALARY = DataPolicy(deny_columns=("app.employees.salary",))


def _db(directory: str) -> Path:
    path = Path(directory) / "app.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
    return path


def _check(policy, sql, views):
    return check_sql_access(
        policy, database="app", sql=sql, dialect="sqlite", views=views
    )


class CatalogTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = _db(self.tmp.name)
        self.catalog = ViewCatalog()
        self.catalog.refresh("app", "sqlite", db_path=str(self.db_path))


class ScanTest(CatalogTestCase):
    def test_tables_and_views_are_told_apart(self):
        state = self.catalog.state("app")
        self.assertEqual(state.status, LOADED)
        self.assertEqual(state.objects.tables, frozenset({"employees", "payroll"}))
        self.assertEqual(
            set(state.objects.views),
            {"v_payroll", "v_chain", "v_names", "v_join", "v_opaque"},
        )

    def test_a_view_over_a_view_is_expanded_to_its_base_tables(self):
        views = self.catalog.state("app").objects.views
        self.assertEqual({ref.name for ref in views["v_chain"].bases}, {"payroll"})
        self.assertEqual({ref.name for ref in views["v_join"].bases}, {"employees", "payroll"})

    def test_an_unparseable_definition_is_opaque(self):
        views = self.catalog.state("app").objects.views
        self.assertTrue(views["v_opaque"].opaque)
        self.assertFalse(views["v_payroll"].opaque)

    def test_a_missing_database_file_is_a_failed_scan_not_an_empty_catalog(self):
        catalog = ViewCatalog()
        state = catalog.refresh("gone", "sqlite", db_path="/nonexistent/app.db")
        self.assertEqual(state.status, FAILED)
        self.assertIn("FileNotFoundError", state.reason)

    def test_names_are_folded_for_sqlite(self):
        path = Path(self.tmp.name) / "mixed.db"
        with sqlite3.connect(path) as conn:
            conn.executescript(
                "CREATE TABLE Orders(id INT); CREATE VIEW V_Orders AS SELECT id FROM Orders;"
            )
        objects = scan_sqlite(str(path))
        self.assertEqual(objects.tables, frozenset({"orders"}))
        self.assertEqual({ref.name for ref in objects.views["v_orders"].bases}, {"orders"})

    def test_a_failed_rescan_replaces_a_good_catalog(self):
        # 旧目录可能正好缺了这次想看的视图：保留它就是在拿不准的时候放行
        self.assertEqual(self.catalog.state("app").status, LOADED)
        self.catalog.refresh("app", "sqlite", db_path="/nonexistent/app.db")
        self.assertEqual(self.catalog.state("app").status, FAILED)


class ViewExpansionTest(CatalogTestCase):
    def test_a_view_over_a_denied_table_is_denied(self):
        decision = _check(DENY_PAYROLL, "SELECT * FROM v_payroll", self.catalog)
        self.assertFalse(decision.allowed)
        self.assertTrue(any("payroll" in item for item in decision.denied_tables))

    def test_a_view_over_a_view_over_a_denied_table_is_denied(self):
        decision = _check(DENY_PAYROLL, "SELECT emp FROM v_chain", self.catalog)
        self.assertFalse(decision.allowed)

    def test_a_join_view_is_denied_when_any_base_is_denied(self):
        decision = _check(DENY_PAYROLL, "SELECT name FROM v_join", self.catalog)
        self.assertFalse(decision.allowed)

    def test_the_view_hidden_inside_a_subquery_or_cte_is_found(self):
        for sql in (
            "SELECT 1 FROM employees WHERE id IN (SELECT emp FROM v_payroll)",
            "WITH x AS (SELECT * FROM v_payroll) SELECT * FROM x",
            "SELECT * FROM employees e, v_payroll p WHERE p.emp = e.id",
        ):
            with self.subTest(sql=sql):
                self.assertFalse(_check(DENY_PAYROLL, sql, self.catalog).allowed)

    def test_a_view_over_a_column_restricted_table_is_denied(self):
        # 视图 v_names 只选了 id 和 name，但视图列对应基表哪一列关卡看不见
        decision = _check(DENY_SALARY, "SELECT name FROM v_names", self.catalog)
        self.assertFalse(decision.allowed)
        self.assertTrue(any("v_names" in item for item in decision.denied_columns))

    def test_a_view_over_permitted_tables_still_works(self):
        decision = _check(DENY_PAYROLL, "SELECT name FROM v_names", self.catalog)
        self.assertTrue(decision.allowed, decision.audit_error)

    def test_ordinary_tables_are_unaffected(self):
        self.assertTrue(_check(DENY_PAYROLL, "SELECT name FROM employees", self.catalog).allowed)
        self.assertFalse(_check(DENY_PAYROLL, "SELECT * FROM payroll", self.catalog).allowed)


class FailClosedTest(CatalogTestCase):
    def test_an_opaque_view_is_refused_for_a_restricted_policy(self):
        decision = _check(DENY_PAYROLL, "SELECT emp FROM v_opaque", self.catalog)
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.unverifiable)
        self.assertIn("无法确认", decision.audit_error)
        self.assertNotIn("申请", decision.audit_error)

    def test_an_object_created_after_the_scan_is_refused_until_the_next_scan(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE VIEW v_late AS SELECT * FROM payroll")
        # 目录还是旧的：不知道 v_late 是什么，就不能证明它没读 payroll
        decision = _check(DENY_PAYROLL, "SELECT * FROM v_late", self.catalog)
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.unverifiable)
        # 重扫之后它被识别为视图，按它的基表判
        self.catalog.refresh("app", "sqlite", db_path=str(self.db_path))
        decision = _check(DENY_PAYROLL, "SELECT * FROM v_late", self.catalog)
        self.assertFalse(decision.allowed)
        self.assertFalse(decision.unverifiable)
        self.assertTrue(decision.denied_tables)

    def test_a_failed_catalog_refuses_everything_for_a_restricted_policy(self):
        self.catalog.refresh("app", "sqlite", db_path="/nonexistent/app.db")
        decision = _check(DENY_PAYROLL, "SELECT name FROM employees", self.catalog)
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.unverifiable)

    def test_queries_without_tables_are_unaffected_by_a_failed_catalog(self):
        self.catalog.refresh("app", "sqlite", db_path="/nonexistent/app.db")
        self.assertTrue(_check(DENY_PAYROLL, "SELECT 1", self.catalog).allowed)

    def test_an_unrestricted_policy_has_nothing_to_hide(self):
        for policy in (DataPolicy.full_access(), ALLOW_ALL):
            with self.subTest(policy=type(policy).__name__):
                self.assertTrue(_check(policy, "SELECT emp FROM v_opaque", self.catalog).allowed)
                self.catalog.refresh("app", "sqlite", db_path="/nonexistent/app.db")
                self.assertTrue(_check(policy, "SELECT * FROM v_payroll", self.catalog).allowed)
                self.catalog.refresh("app", "sqlite", db_path=str(self.db_path))

    def test_a_database_that_was_never_registered_is_judged_as_before(self):
        # 前提而非安全属性：管道启动时为每个库登记，直接用执行器的调用方要自己登记
        empty = ViewCatalog()
        self.assertEqual(empty.state("app").status, UNREGISTERED)
        self.assertTrue(_check(DENY_PAYROLL, "SELECT * FROM v_payroll", empty).allowed)
        self.assertTrue(_check(DENY_PAYROLL, "SELECT * FROM v_payroll", None).allowed)


class UnrestrictedDetectionTest(unittest.TestCase):
    def test_data_policy(self):
        self.assertTrue(DataPolicy.full_access().is_unrestricted())
        self.assertFalse(DataPolicy(deny_tables=("app.payroll",)).is_unrestricted())
        self.assertFalse(DataPolicy(deny_columns=("app.t.c",)).is_unrestricted())
        self.assertFalse(DataPolicy(allow_tables=("app.*",)).is_unrestricted())
        self.assertFalse(DataPolicy.deny_all().is_unrestricted())

    def test_intersection_needs_both_sides_unrestricted(self):
        free = DataPolicy.full_access()
        self.assertTrue(IntersectionPolicy(service=free, user=free).is_unrestricted())
        self.assertFalse(
            IntersectionPolicy(service=free, user=DENY_PAYROLL).is_unrestricted()
        )

    def test_a_custom_filter_that_does_not_say_is_treated_as_restricted(self):
        class Custom:
            def allows_table(self, database, table):
                return True

            def allows_column(self, database, table, column):
                return True

        catalog = ViewCatalog()
        catalog.mark_failed("app", "x")
        decision = check_sql_access(
            Custom(), database="app", sql="SELECT * FROM employees", dialect="sqlite",
            views=catalog,
        )
        self.assertFalse(decision.allowed)


class ExecutorWithCatalogTest(CatalogTestCase):
    """过真实执行器：视图以前能把被禁表的行读出来。"""

    def _run(self, views, sql):
        executor = SQLiteMCPExecutor(database="app", db_path=self.db_path)
        with authorization_scope(DENY_PAYROLL, caller="audit", user_id="audit", views=views):
            return executor.execute(MCPExecutionRequest(database="app", sql=sql))

    def test_the_view_no_longer_returns_the_denied_tables_rows(self):
        result = self._run(self.catalog, "SELECT * FROM v_payroll")
        self.assertFalse(result.success)
        self.assertEqual(result.rows, [])

    def test_without_a_catalog_in_scope_the_hole_is_still_there(self):
        # 同一条 SQL，作用域里没带目录：证明防线是目录提供的，也证明测试不是空转
        result = self._run(None, "SELECT * FROM v_payroll")
        self.assertTrue(result.success, result.error)
        self.assertEqual(len(result.rows), 2)

    def test_a_permitted_query_still_runs(self):
        result = self._run(self.catalog, "SELECT name FROM v_names ORDER BY name")
        self.assertTrue(result.success, result.error)
        self.assertEqual([row["name"] for row in result.rows], ["ann", "bob"])


class PipelineWiringTest(unittest.TestCase):
    """管道启动时为每个库登记目录，schema 轮询时重扫。"""

    @classmethod
    def setUpClass(cls):
        from ai_sql_agent.pipeline import Text2SQLPipeline

        cls.pipeline = Text2SQLPipeline()

    def test_every_database_is_registered_at_startup(self):
        for database in self.pipeline.databases:
            state = self.pipeline.view_catalog.state(database.name)
            self.assertEqual(state.status, LOADED, database.name)
            self.assertTrue(state.objects.tables, database.name)

    def test_a_schema_poll_rescans_and_heals_a_failed_entry(self):
        name = self.pipeline.databases[0].name
        self.pipeline.view_catalog.mark_failed(name, "simulated outage")
        self.assertEqual(self.pipeline.view_catalog.state(name).status, FAILED)
        self.pipeline.reload_schema(reason="poll")  # 没有变化也要重扫目录
        self.assertEqual(self.pipeline.view_catalog.state(name).status, LOADED)

    def test_the_data_catalog_hands_the_view_catalog_to_its_scopes(self):
        from ai_sql_agent.catalog.service import DataCatalog

        catalog = DataCatalog.from_pipeline(self.pipeline)
        self.assertIs(catalog.views, self.pipeline.view_catalog)


class GateSeesEveryRealReadThroughViewsTest(CatalogTestCase):
    """差分（见 test_authorization_gaps）扩到视图：SQLite 报告的每个 (表, 列) 被禁，关卡都得拒。"""

    CORPUS = [
        "SELECT * FROM v_payroll",
        "SELECT amount FROM v_payroll WHERE emp = 101",
        "SELECT emp FROM v_chain",
        "SELECT name FROM v_names",
        "SELECT * FROM v_join",
        "SELECT name FROM v_names WHERE id IN (SELECT emp FROM v_payroll)",
        "SELECT count(*) FROM v_chain",
        "WITH x AS (SELECT * FROM v_join) SELECT name FROM x",
    ]

    def _real_reads(self, sql):
        conn = sqlite3.connect(self.db_path)
        reads = set()

        def authorizer(action, arg1, arg2, _db, _source):
            if action == sqlite3.SQLITE_READ and arg1 in ("employees", "payroll"):
                reads.add((arg1, arg2 or ""))
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorizer)
        try:
            conn.execute(sql).fetchall()
        finally:
            conn.close()
        return reads

    def test_denying_any_base_table_or_column_a_view_really_reads_denies_the_query(self):
        for sql in self.CORPUS:
            reads = self._real_reads(sql)
            self.assertTrue(reads, f"这条没读到任何基表，写错了？{sql}")
            for table, column in sorted(reads):
                with self.subTest(sql=sql, table=table, column=column):
                    if column:
                        policy = DataPolicy(deny_columns=(f"app.{table}.{column}",))
                    else:
                        policy = DataPolicy(deny_tables=(f"app.{table}",))
                    decision = _check(policy, sql, self.catalog)
                    self.assertFalse(
                        decision.allowed,
                        f"SQLite 经视图读了 {table}.{column or '*'}，关卡却放行了：{sql}",
                    )


if __name__ == "__main__":
    unittest.main()
