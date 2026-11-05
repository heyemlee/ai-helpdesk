"""
标识符大小写：策略里写的名字、库里存的名字、SQL 里写的名字，三者怎么算同一个。

2026-10-04 复现的缺陷：拒绝规则里只要带一个大写字母，关卡上就永远对不上。

    deny_tables = ["shop_db.Orders"]      →  SELECT * FROM Orders   照样放行
    deny_columns = ["….supplier.Unit_Cost"] →  SELECT unit_cost …     照样放行

目录那一层看不到这张表（它用原名去问策略），查询却查得出来（关卡把名字转成了
小写再问）。同一条规则在两处给出两个答案，而且松的那一处是真正执行 SQL 的地方。

修法不是「全部转小写」：PostgreSQL 里 `"Orders"` 与 `orders` 是两张表，
全转小写会让一条只放行 `orders` 的规则把 `"Orders"` 也放出去。规则见
`ai_sql_agent.auth.identifiers`，一句话：**拒绝从宽，放行从严**，大小写怎么算跟着数据库走。

这组用例锁三件事：
1. 被拒的表和列，换任何大小写、加不加引号，**既预览不了也查不出来**；
2. PostgreSQL 的引号语义：`"Orders"` 和 `orders` 分得开；
3. 目录、检索裁剪、SQL 关卡给出同一个答案。
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from ai_sql_agent.auth import DataPolicy, authorization_scope, check_sql_access
from ai_sql_agent.auth.identifiers import (
    bind_dialect,
    fold,
    referenced_name,
    stored_name,
)
from ai_sql_agent.auth.objects import IntersectionPolicy, RowFilter
from ai_sql_agent.auth.sql_analysis import analyze_sql
from ai_sql_agent.catalog import CatalogError, DataCatalog, PreviewFilter, PreviewQuery
from ai_sql_agent.catalog.preview import PreviewColumn, build_page_statement
from ai_sql_agent.catalog.service import _allows_column, _allows_table
from ai_sql_agent.core import reload_settings
from ai_sql_agent.execution import MCPExecutionRequest, MCPRouter, SQLiteMCPExecutor
from ai_sql_agent.pipeline.objects import DatabaseConfig
from ai_sql_agent.retrieval.access import filter_schema_graph
from ai_sql_agent.retrieval.access import bind_dialect as retrieval_bind
from ai_sql_agent.semantic import DatabaseSemantics, SemanticLayer


def allowed(policy, sql, dialect="sqlite", database="shop_db"):
    return check_sql_access(policy, database=database, sql=sql, dialect=dialect).allowed


class NameRulesTest(unittest.TestCase):
    """规范名：每种数据库各自的规则。"""

    def test_sqlite_names_are_case_insensitive_quoted_or_not(self):
        for name, quoted in (("Orders", False), ("Orders", True), ("ORDERS", True)):
            self.assertEqual(referenced_name(name, quoted=quoted, dialect="sqlite"), "orders")
        self.assertEqual(stored_name("Orders", "sqlite"), "orders")

    def test_postgres_folds_unquoted_and_keeps_quoted(self):
        self.assertEqual(referenced_name("Orders", quoted=False, dialect="postgres"), "orders")
        self.assertEqual(referenced_name("Orders", quoted=True, dialect="postgres"), "Orders")
        self.assertEqual(referenced_name("Orders", quoted=True, dialect="postgresql"), "Orders")
        # 表结构里读出来的名字就是存储名，不再折
        self.assertEqual(stored_name("Orders", "postgres"), "Orders")

    def test_only_ascii_letters_fold(self):
        # SQLite 与 PostgreSQL（UTF-8）折叠标识符时都只动 ASCII：Ärger 与 ärger 是两个名字
        self.assertEqual(fold("Ärger_ID"), "Ärger_id")
        self.assertEqual(fold("订单表"), "订单表")

    def test_the_parser_reports_canonical_names(self):
        postgres = analyze_sql('SELECT o.Amount, "X"."Total" FROM Orders o JOIN "Orders" AS "X" ON 1 = 1', "postgres")
        self.assertEqual(sorted(table.name for table in postgres.tables), ["Orders", "orders"])
        self.assertEqual(
            sorted((column.name, column.candidates[0].name) for column in postgres.columns),
            [("Total", "Orders"), ("amount", "orders")],
        )
        sqlite = analyze_sql('SELECT o.Amount, "X"."Total" FROM Orders o JOIN "Orders" AS "X" ON 1 = 1', "sqlite")
        self.assertEqual({table.name for table in sqlite.tables}, {"orders"})
        self.assertEqual(sorted(column.name for column in sqlite.columns), ["amount", "total"])


class ReportedBypassTest(unittest.TestCase):
    """当初复现出来的那四条，外加同一条规则的各种写法。"""

    POLICY = DataPolicy(
        allow_tables=("shop_db.*",),
        deny_tables=("shop_db.Orders",),
        deny_columns=("shop_db.supplier.Unit_Cost",),
    )

    def test_a_capitalised_deny_rule_is_enforced_for_every_spelling(self):
        for sql in (
            "SELECT * FROM Orders",
            'SELECT id FROM "Orders"',
            "SELECT id FROM orders",
            "SELECT id FROM ORDERS",
            'SELECT id FROM "oRdErS" AS o',
            "SELECT s.supplier_name FROM supplier s JOIN orders o ON o.supplier_id = s.supplier_id",
            "SELECT supplier_name FROM supplier WHERE supplier_id IN (SELECT supplier_id FROM Orders)",
        ):
            decision = check_sql_access(self.POLICY, database="shop_db", sql=sql, dialect="sqlite")
            self.assertFalse(decision.allowed, sql)
            self.assertEqual(decision.denied_tables, ("orders",), sql)

    def test_a_capitalised_column_rule_is_enforced_for_every_spelling(self):
        for sql in (
            "SELECT unit_cost FROM supplier",
            "SELECT Unit_Cost FROM supplier",
            'SELECT "UNIT_COST" FROM supplier',
            "SELECT s.unit_cost FROM Supplier AS s",
            "SELECT supplier_name FROM supplier ORDER BY UNIT_COST",
            "SELECT supplier_name FROM supplier WHERE unit_cost > 10",
            "SELECT * FROM supplier",
            "SELECT s.* FROM SUPPLIER s",
        ):
            self.assertFalse(allowed(self.POLICY, sql), sql)
        # 不碰那一列的查询不受影响
        self.assertTrue(allowed(self.POLICY, "SELECT supplier_name, COUNT(*) FROM Supplier GROUP BY 1"))

    def test_rules_in_any_case_deny_names_in_any_case(self):
        for rule in ("shop_db.orders", "shop_db.ORDERS", "SHOP_DB.Orders", "shop_db.O*", "*.ORDERS"):
            policy = DataPolicy(allow_tables=("*",), deny_tables=(rule,))
            for sql in ("SELECT 1 FROM orders", 'SELECT 1 FROM "Orders"', "SELECT 1 FROM ORDERS"):
                self.assertFalse(allowed(policy, sql), (rule, sql))
        for rule in ("*.*.unit_cost", "shop_db.SUPPLIER.Unit_Cost", "*.Supplier.UNIT*"):
            policy = DataPolicy(allow_tables=("*",), deny_columns=(rule,))
            self.assertFalse(allowed(policy, "SELECT Unit_Cost FROM supplier"), rule)
            self.assertFalse(allowed(policy, "SELECT * FROM supplier"), rule)

    def test_sqlite_allow_rules_are_case_insensitive_too(self):
        # 那里 Orders 和 orders 本来就是同一张表：规则照着建表语句抄成 Orders 必须管用
        policy = DataPolicy(allow_tables=("shop_db.Orders",))
        for sql in ("SELECT id FROM orders", 'SELECT id FROM "ORDERS"', "SELECT id FROM Orders"):
            self.assertTrue(allowed(policy, sql), sql)
        self.assertFalse(allowed(policy, "SELECT id FROM supplier"))


class PostgresQuotedIdentifierTest(unittest.TestCase):
    """
    PostgreSQL：没加引号的折成小写，加了引号的区分大小写。
    `"Orders"` 与 `orders` 是两张表，策略必须分得开——至少在放行这个方向上。

    这里只验证判定逻辑（解析 + 匹配），没有连真实的 PostgreSQL。
    """

    def test_deny_is_wide_both_twins_are_refused(self):
        for rule in ("pg_db.Orders", "pg_db.orders", "pg_db.ORDERS"):
            policy = DataPolicy(allow_tables=("pg_db.*",), deny_tables=(rule,))
            for sql in ('SELECT 1 FROM "Orders"', "SELECT 1 FROM orders", "SELECT 1 FROM Orders", 'SELECT 1 FROM "ORDERS"'):
                self.assertFalse(allowed(policy, sql, "postgres", "pg_db"), (rule, sql))
            self.assertTrue(allowed(policy, "SELECT 1 FROM supplier", "postgres", "pg_db"))

    def test_allowing_the_lowercase_table_does_not_allow_its_quoted_twin(self):
        policy = DataPolicy(allow_tables=("pg_db.orders",))
        # 没加引号的 Orders / ORDERS 指的就是 orders
        for sql in ("SELECT 1 FROM orders", "SELECT 1 FROM Orders", "SELECT 1 FROM ORDERS", 'SELECT 1 FROM "orders"'):
            self.assertTrue(allowed(policy, sql, "postgres", "pg_db"), sql)
        # 「全部转小写」会在这里把另一张表放出去
        denied = check_sql_access(policy, database="pg_db", sql='SELECT 1 FROM "Orders"', dialect="postgres")
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.denied_tables, ("Orders",))
        self.assertFalse(allowed(policy, 'SELECT 1 FROM "ORDERS"', "postgres", "pg_db"))

    def test_allowing_the_quoted_table_does_not_allow_its_lowercase_twin(self):
        policy = DataPolicy(allow_tables=("pg_db.Orders",))
        self.assertTrue(allowed(policy, 'SELECT 1 FROM "Orders"', "postgres", "pg_db"))
        for sql in ("SELECT 1 FROM orders", "SELECT 1 FROM Orders"):
            self.assertFalse(allowed(policy, sql, "postgres", "pg_db"), sql)

    def test_column_denies_reach_quoted_and_unquoted_columns(self):
        policy = DataPolicy(allow_tables=("pg_db.*",), deny_columns=("pg_db.emp.Salary",))
        for sql in (
            "SELECT salary FROM emp",
            "SELECT SALARY FROM emp",
            'SELECT "Salary" FROM emp',
            'SELECT "salary" FROM "emp"',
            "SELECT * FROM emp",
        ):
            self.assertFalse(allowed(policy, sql, "postgres", "pg_db"), sql)
        self.assertTrue(allowed(policy, "SELECT name FROM emp", "postgres", "pg_db"))

    def test_quoted_aliases_that_differ_only_by_case_do_not_swap_tables(self):
        # "X" 和 x 是两个别名。把别名都转成小写的话，"X".salary 会被算到 dept 头上，
        # emp.salary 这条禁列就这样被绕过去了
        policy = DataPolicy(allow_tables=("pg_db.*",), deny_columns=("pg_db.emp.salary",))
        self.assertFalse(allowed(
            policy, 'SELECT "X".salary FROM emp AS "X" JOIN dept AS x ON x.id = "X".dept_id', "postgres", "pg_db",
        ))
        self.assertTrue(allowed(
            policy, 'SELECT x.name FROM emp AS "X" JOIN dept AS x ON x.id = "X".dept_id', "postgres", "pg_db",
        ))

    def test_schema_names_follow_the_same_rule(self):
        policy = DataPolicy(allow_tables=("pg_db.*",), deny_tables=("pg_db.Secret.*",))
        for sql in ("SELECT 1 FROM secret.orders", 'SELECT 1 FROM "Secret".orders', "SELECT 1 FROM SECRET.orders"):
            self.assertFalse(allowed(policy, sql, "postgres", "pg_db"), sql)
        self.assertTrue(allowed(policy, "SELECT 1 FROM public.orders", "postgres", "pg_db"))

    def test_preview_sql_for_a_quoted_table_is_judged_by_its_stored_name(self):
        columns = [PreviewColumn("Id", "number", primary_key=True), PreviewColumn("Note", "text")]
        statement = build_page_statement("Orders", columns, PreviewQuery(), "postgres", limit=5, offset=0)
        exact = DataPolicy(allow_tables=("pg_db.Orders",))
        twin = DataPolicy(allow_tables=("pg_db.orders",))
        self.assertTrue(allowed(exact, statement.sql, "postgres", "pg_db"), statement.sql)
        self.assertFalse(allowed(twin, statement.sql, "postgres", "pg_db"))
        # 列同理：禁了 Note（任何大小写），预览语句里引用它就过不了
        no_note = DataPolicy(allow_tables=("pg_db.*",), deny_columns=("pg_db.orders.note",))
        self.assertFalse(allowed(no_note, statement.sql, "postgres", "pg_db"))

    def test_least_privilege_plan_uses_postgres_semantics(self):
        from ai_sql_agent.ops.least_privilege import TableInventory, build_plan

        inventory = TableInventory(columns={
            "orders": ["id", "amount"], "Orders": ["Id", "Secret"], "supplier": ["id", "Unit_Cost"],
        })
        plan = build_plan(
            DataPolicy(allow_tables=("pg_db.orders", "pg_db.supplier"), deny_columns=("pg_db.supplier.unit_cost",)),
            inventory, database="pg_db", role="ro",
        )
        # 放行 orders 不等于给 "Orders" 授权；禁列换个大小写照样生效
        self.assertEqual(sorted(plan.granted_tables), ["orders", "supplier"])
        self.assertEqual(plan.denied_tables, ["Orders"])
        self.assertEqual(plan.column_grants["supplier"], ["id"])

        denied = build_plan(
            DataPolicy(allow_tables=("pg_db.*",), deny_tables=("pg_db.ORDERS",)),
            inventory, database="pg_db", role="ro",
        )
        self.assertEqual(sorted(denied.denied_tables), ["Orders", "orders"])


class OtherRulesFollowTest(unittest.TestCase):
    def test_row_filters_match_in_any_case(self):
        # 行范围是限制：对不上就等于没限制，所以和拒绝规则一样从宽
        self.assertTrue(RowFilter("erp_db.Sales_Order", "owner = 1").matches("erp_db", "sales_order"))
        policy = DataPolicy(row_filters=(RowFilter("erp_db.Sales_Order", "owner_id = 7"),))
        self.assertEqual(policy.row_predicate("erp_db", "sales_order"), "owner_id = 7")
        self.assertEqual(policy.for_dialect("postgres").row_predicate("erp_db", "SALES_ORDER"), "owner_id = 7")

    def test_intersection_binds_both_sides(self):
        service = DataPolicy(allow_tables=("pg_db.*",))
        user = DataPolicy(allow_tables=("pg_db.orders",))
        both = bind_dialect(IntersectionPolicy(service=service, user=user), "postgres")
        self.assertTrue(both.allows_table("pg_db", "orders"))
        self.assertFalse(both.allows_table("pg_db", "Orders"))
        self.assertTrue(allowed(IntersectionPolicy(service=service, user=user), "SELECT 1 FROM ORDERS", "postgres", "pg_db"))
        self.assertFalse(allowed(IntersectionPolicy(service=service, user=user), 'SELECT 1 FROM "Orders"', "postgres", "pg_db"))

    def test_binding_is_cheap_and_leaves_the_config_object_alone(self):
        policy = DataPolicy(allow_tables=("a.b",))
        self.assertIs(policy.for_dialect("sqlite"), policy)
        bound = policy.for_dialect("postgres")
        self.assertIsNot(bound, policy)
        self.assertFalse(policy.case_sensitive_allow)
        self.assertIs(bound.for_dialect("postgres"), bound)
        # 配置文件里写不出这个开关
        self.assertFalse(DataPolicy.from_dict({"allow_tables": ["*"], "case_sensitive_allow": True}).case_sensitive_allow)

    def test_custom_filters_without_for_dialect_are_used_as_they_are(self):
        class Custom:
            def allows_table(self, database, table):
                return table == "orders"

            def allows_column(self, database, table, column):
                return True

        custom = Custom()
        self.assertIs(bind_dialect(custom, "postgres"), custom)
        self.assertIs(retrieval_bind(custom, "sqlite"), custom)

    def test_retrieval_trims_the_schema_graph_by_the_same_rule(self):
        class Column:
            def __init__(self, name):
                self.column_name = name

        class Graph:
            database = "shop_db"

            def __init__(self):
                self.tables = {"Orders": object(), "supplier": object()}
                self.columns = {"Orders": [Column("id")], "supplier": [Column("supplier_name"), Column("Unit_Cost")]}
                self.relations = []

        policy = DataPolicy(
            allow_tables=("shop_db.*",), deny_tables=("shop_db.ORDERS",),
            deny_columns=("shop_db.supplier.unit_cost",),
        )
        graph = filter_schema_graph(Graph(), retrieval_bind(policy, "sqlite"))
        self.assertEqual(list(graph.tables), ["supplier"])
        self.assertEqual([column.column_name for column in graph.columns["supplier"]], ["supplier_name"])


def _mixed_case_shop(directory: Path):
    """库里的表名、列名本身就带大写：`CREATE TABLE Orders`、`Unit_Cost`。"""
    db_path = directory / "shop.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE Orders (Order_No TEXT PRIMARY KEY, Supplier_Id TEXT, Amount REAL);
        CREATE TABLE supplier (supplier_id TEXT PRIMARY KEY, supplier_name TEXT, Unit_Cost REAL);
        INSERT INTO Orders VALUES ('SO-1', 'SUP-1', 120.5), ('SO-2', 'SUP-2', 80.0);
        INSERT INTO supplier VALUES ('SUP-1', 'Harborline Hardware', 3.2), ('SUP-2', 'Canyon Crest Quartz', 48.0);
        """
    )
    conn.commit()
    conn.close()
    semantic = SemanticLayer(
        databases={"shop_db": DatabaseSemantics(name="shop_db", description="", db_path=str(db_path), tables={})},
        metric_definitions={"单位成本": "看 supplier.Unit_Cost，单位 USD。"},
        source="test",
    )
    databases = [DatabaseConfig(name="shop_db", db_path=db_path)]
    router = MCPRouter()
    router.register_executor("shop_db", SQLiteMCPExecutor("shop_db", db_path, max_rows=100))
    return DataCatalog(semantic=semantic, databases=databases, router=router), router


class NeitherPreviewNorQueryTest(unittest.TestCase):
    """
    一份真实的 SQLite 库 + 一份规则写法和库里大小写**故意对不上**的策略。
    被拒的表和列：目录里没有、预览读不到、SQL 查不出——三条路是同一个答案。
    """

    # 库里是 Orders / Unit_Cost；规则里写成 ORDERS / unit_cost
    POLICIES = (
        DataPolicy(allow_tables=("shop_db.*",), deny_tables=("shop_db.ORDERS",), deny_columns=("shop_db.supplier.unit_cost",)),
        DataPolicy(allow_tables=("shop_db.*",), deny_tables=("shop_db.orders",), deny_columns=("shop_db.SUPPLIER.UNIT_COST",)),
        DataPolicy(allow_tables=("SHOP_DB.Supplier",), deny_columns=("*.*.Unit_cost",)),
    )

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.catalog, self.router = _mixed_case_shop(Path(self._tmpdir.name))

    def _query(self, policy, sql):
        with authorization_scope(policy, caller="t", user_id="t"):
            return self.router.execute(MCPExecutionRequest(database="shop_db", sql=sql))

    def test_denied_table_is_invisible_unpreviewable_and_unqueryable(self):
        for policy in self.POLICIES:
            payload = self.catalog.describe(policy)
            names = [table["name"] for table in payload["domains"][0]["tables"]]
            self.assertEqual(names, ["supplier"], policy)
            self.assertNotIn("Orders", json.dumps(payload))
            self.assertTrue(payload["access_limited"])

            with self.assertRaises(CatalogError) as caught:
                self.catalog.preview(policy, database="shop_db", table="Orders", query=PreviewQuery())
            self.assertEqual((caught.exception.code, caught.exception.status), ("not_found", 404))

            for sql in (
                "SELECT * FROM Orders",
                "SELECT Order_No FROM orders",
                'SELECT "Amount" FROM "ORDERS"',
                "SELECT s.supplier_name, o.Amount FROM supplier s JOIN Orders o ON o.Supplier_Id = s.supplier_id",
                "SELECT supplier_name FROM supplier WHERE supplier_id IN (SELECT supplier_id FROM oRdErS)",
            ):
                result = self._query(policy, sql)
                self.assertFalse(result.success, (policy, sql))
                self.assertIn("没有访问权限", result.error)
                self.assertEqual(result.rows, [])

    def test_denied_column_is_invisible_unpreviewable_and_unqueryable(self):
        for policy in self.POLICIES:
            payload = self.catalog.describe(policy)
            supplier = next(t for t in payload["domains"][0]["tables"] if t["name"] == "supplier")
            self.assertEqual([c["name"] for c in supplier["columns"]], ["supplier_id", "supplier_name"])
            self.assertNotIn("Unit_Cost", json.dumps(payload, ensure_ascii=False))
            # 术语表里那条提到了这一列的定义，也跟着不给
            self.assertEqual(payload["glossary"], [])

            page = self.catalog.preview(policy, database="shop_db", table="supplier", query=PreviewQuery())
            self.assertEqual([c["name"] for c in page["columns"]], ["supplier_id", "supplier_name"])
            self.assertNotIn("Unit_Cost", page["sql"])
            self.assertTrue(page["columns_hidden"])
            self.assertEqual(len(page["rows"]), 2)
            for query in (
                PreviewQuery(filters=[PreviewFilter("Unit_Cost", "gt", "1")]),
                PreviewQuery(filters=[PreviewFilter("unit_cost", "gt", "1")]),
                PreviewQuery(sort_column="UNIT_COST"),
            ):
                with self.assertRaises(CatalogError) as caught:
                    self.catalog.preview(policy, database="shop_db", table="supplier", query=query)
                self.assertEqual(caught.exception.code, "unknown_column")

            for sql in (
                "SELECT Unit_Cost FROM supplier",
                "SELECT unit_cost FROM supplier",
                'SELECT "UNIT_COST" FROM "Supplier"',
                "SELECT supplier_name FROM supplier ORDER BY unit_cost DESC",
                "SELECT supplier_name FROM supplier WHERE Unit_Cost > 10",
                "SELECT MAX(unit_cost) FROM supplier",
                "SELECT * FROM supplier",
            ):
                result = self._query(policy, sql)
                self.assertFalse(result.success, (policy, sql))
                self.assertIn("没有访问权限", result.error)

            ok = self._query(policy, "SELECT Supplier_Name FROM SUPPLIER ORDER BY 1")
            self.assertTrue(ok.success, ok.error)
            self.assertEqual(len(ok.rows), 2)

    def test_what_the_catalog_shows_is_what_the_gate_lets_through(self):
        """随机生成大小写各异的规则：目录说看得到，关卡就放；目录里没有，关卡就拒。两种方言都查。"""
        rng = random.Random(20261004)
        tables = {"Orders": ["Order_No", "Amount"], "supplier": ["supplier_id", "Unit_Cost"], "LOG": ["Seq", "note"]}

        def scramble(text):
            return "".join(rng.choice((char.lower(), char.upper())) for char in text)

        def rule(table, column=None):
            parts = [rng.choice(("db", scramble("db"), "*")), rng.choice((table, scramble(table), "*"))]
            if column is not None:
                parts.append(rng.choice((column, scramble(column))))
            return ".".join(parts)

        checked = 0
        for _ in range(400):
            policy = DataPolicy(
                allow_tables=tuple(rule(rng.choice(list(tables))) for _ in range(rng.randint(1, 2))),
                deny_tables=tuple(rule(rng.choice(list(tables))) for _ in range(rng.randint(0, 1))),
                deny_columns=tuple(
                    rule(table, rng.choice(tables[table]))
                    for table in rng.sample(list(tables), rng.randint(0, 1))
                ),
            )
            for dialect in ("sqlite", "postgres"):
                for table, columns in tables.items():
                    shown = _allows_table(policy, "db", table, dialect)
                    gate = check_sql_access(
                        policy, database="db", sql=f'SELECT 1 FROM "{table}"', dialect=dialect
                    ).allowed
                    self.assertEqual(shown, gate, (policy, dialect, table))
                    for column in columns:
                        visible = _allows_column(policy, "db", table, column, dialect)
                        readable = check_sql_access(
                            policy, database="db", sql=f'SELECT "{column}" FROM "{table}"', dialect=dialect
                        ).allowed
                        self.assertEqual(visible, readable, (policy, dialect, table, column))
                        checked += 1
        self.assertGreater(checked, 4000)


class CapitalisedDenyOverHttpTest(unittest.TestCase):
    """HTTP 这一层：Key 文件里的规则写成大写，目录和预览接口照样守得住。"""

    ENV_KEYS = (
        "AI_SQL_AUTH_MODE", "AI_SQL_API_KEYS_FILE", "AI_SQL_AUDIT_DB_PATH",
        "AI_SQL_MEMORY_DB_PATH", "AI_SQL_SEMANTIC_DIR",
    )

    @classmethod
    def setUpClass(cls):
        from ai_sql_agent.api.app import app

        cls.tmpdir = tempfile.TemporaryDirectory()
        key_file = Path(cls.tmpdir.name) / "keys.json"
        key_file.write_text(json.dumps({
            "keys": [{"key": "intern-key", "caller": "intern", "user_id": "intern", "roles": ["intern"]}],
            "policies": {
                "intern": {
                    "allow_tables": ["ERP_DB.*"],
                    "deny_tables": ["erp_db.Supplier", "ERP_DB.PAYMENT"],
                    "deny_columns": ["erp_db.Purchase_Order.PO_AMT"],
                }
            },
        }), encoding="utf-8")
        cls._saved = {key: os.environ.get(key) for key in cls.ENV_KEYS}
        os.environ.update({
            "AI_SQL_AUTH_MODE": "api_key",
            "AI_SQL_API_KEYS_FILE": str(key_file),
            "AI_SQL_AUDIT_DB_PATH": str(Path(cls.tmpdir.name) / "audit.db"),
            "AI_SQL_MEMORY_DB_PATH": str(Path(cls.tmpdir.name) / "memory.db"),
            "AI_SQL_SEMANTIC_DIR": "",
        })
        reload_settings()
        cls.app = app
        cls.client_ctx = TestClient(app)
        cls.client = cls.client_ctx.__enter__()
        cls.headers = {"X-API-Key": "intern-key"}

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

    def test_catalog_hides_what_the_capitalised_rules_deny(self):
        body = self.client.get("/catalog", headers=self.headers).json()
        self.assertEqual([domain["database"] for domain in body["domains"]], ["erp_db"])
        tables = {table["name"]: table for table in body["domains"][0]["tables"]}
        self.assertNotIn("supplier", tables)
        self.assertNotIn("payment", tables)
        self.assertNotIn("po_amt", [column["name"] for column in tables["purchase_order"]["columns"]])

    def test_preview_refuses_what_the_capitalised_rules_deny(self):
        for table in ("supplier", "payment", "Supplier", "SUPPLIER"):
            response = self.client.post(f"/catalog/erp_db/{table}/rows", headers=self.headers, json={})
            self.assertEqual(response.status_code, 404, table)

        page = self.client.post("/catalog/erp_db/purchase_order/rows", headers=self.headers, json={"page_size": 3})
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("po_amt", [column["name"] for column in page.json()["columns"]])
        self.assertNotIn("po_amt", page.json()["sql"])
        for column in ("po_amt", "PO_AMT"):
            filtered = self.client.post(
                "/catalog/erp_db/purchase_order/rows", headers=self.headers,
                json={"filters": [{"column": column, "op": "gt", "value": "1"}]},
            )
            self.assertEqual(filtered.status_code, 400, column)

    def test_the_sql_gate_refuses_the_same_data_for_this_key(self):
        from ai_sql_agent.api.security import AuthGuard  # noqa: F401 - 保证鉴权已初始化

        store = self.app.state.auth.store
        principal = store.resolve("intern-key", "")
        executor = self.app.state.pipeline.mcp_router.executors["erp_db"]
        for sql in (
            "SELECT supplier_name FROM supplier",
            "SELECT * FROM Supplier",
            "SELECT paid_amt FROM PAYMENT",
            "SELECT po_no, po_amt FROM purchase_order",
            "SELECT PO_AMT FROM Purchase_Order",
            "SELECT * FROM purchase_order",
        ):
            with authorization_scope(principal.policy, caller="intern", user_id="intern"):
                result = executor.execute(MCPExecutionRequest(database="erp_db", sql=sql))
            self.assertFalse(result.success, sql)
            self.assertIn("没有访问权限", result.error)
        with authorization_scope(principal.policy, caller="intern", user_id="intern"):
            ok = executor.execute(MCPExecutionRequest(database="erp_db", sql="SELECT po_no FROM purchase_order LIMIT 2"))
        self.assertTrue(ok.success, ok.error)


if __name__ == "__main__":
    unittest.main()
