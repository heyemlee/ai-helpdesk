"""
数据目录与只读预览。

这组用例锁的是五件事，全部是「不能悄悄变宽」的方向：

1. 预览请求里的任何字符串都进不了 SQL 文本（标识符过清单、值走绑定参数）；
2. 目录与预览遵守当前身份的表列权限，而且**执行器那道关卡仍然在路上**——
   目录这一层判错了，执行器照样拒；
3. 库或表读不到时返回状态，不返回空结果，也不顺手建一个空库；
4. 请求只能把行数与时长上限调小，调不大；
5. 取回来的东西有上限（长文本、二进制），筛选值按它本来的含义比（大整数、日期）。
"""
import json
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from ai_sql_agent.audit import AuditRecord, SQLiteAuditStore
from ai_sql_agent.auth import DataPolicy
from ai_sql_agent.catalog import CatalogError, DataCatalog, PreviewFilter, PreviewQuery
from ai_sql_agent.catalog.preview import (
    MAX_CELL_CHARS,
    MAX_SEARCH_COLUMNS,
    PreviewColumn,
    PreviewInputError,
    build_page_statement,
    build_summary_statement,
    has_stable_order,
    is_sortable,
    search_targets,
    supported_identifier,
    value_kind,
)
from ai_sql_agent.catalog.service import _cell
from ai_sql_agent.core import reload_settings
from ai_sql_agent.execution import MCPExecutionRequest, MCPRouter, SQLiteMCPExecutor
from ai_sql_agent.execution.mcp_protocol_executor import MCPProtocolExecutor
from ai_sql_agent.pipeline.objects import DatabaseConfig
from ai_sql_agent.retrieval.access import ALLOW_ALL
from ai_sql_agent.retrieval.sqlite_loader import SQLiteSchemaLoader
from ai_sql_agent.semantic import DatabaseSemantics, SemanticLayer

COLUMNS = [
    PreviewColumn("supplier_id", "text", primary_key=True),
    PreviewColumn("supplier_name", "text"),
    PreviewColumn("on_time_rate", "number"),
    PreviewColumn("joined_on", "date"),
]


class PreviewStatementTest(unittest.TestCase):
    """SQL 拼装：纯函数，不连库。"""

    def test_values_are_bound_never_inlined(self):
        hostile = "x' OR '1'='1"
        query = PreviewQuery(
            search=hostile,
            filters=[
                PreviewFilter("supplier_name", "contains", hostile),
                PreviewFilter("on_time_rate", "lt", "0.9"),
            ],
        )
        statement = build_page_statement(
            "supplier", COLUMNS, query, "sqlite", limit=26, offset=0
        )

        self.assertNotIn(hostile, statement.sql)
        self.assertNotIn("0.9", statement.sql)
        self.assertEqual(statement.sql.count("?"), len(statement.params))
        # 两个文本列各比一次搜索词 + 一个 contains + 一个数字 + LIMIT + OFFSET
        self.assertEqual(
            statement.params,
            (f"%{hostile}%", f"%{hostile}%", f"%{hostile}%", 0.9, 26, 0),
        )

    def test_postgres_uses_its_own_placeholder_and_ilike(self):
        query = PreviewQuery(search="quartz")
        statement = build_page_statement(
            "supplier", COLUMNS, query, "postgres", limit=11, offset=20
        )
        self.assertNotIn("?", statement.sql)
        self.assertEqual(statement.sql.count("%s"), len(statement.params))
        self.assertIn("ILIKE", statement.sql)
        self.assertEqual(statement.params[-2:], (11, 20))

    def test_like_wildcards_in_the_search_term_are_escaped(self):
        query = PreviewQuery(filters=[PreviewFilter("supplier_name", "contains", "50%_!")])
        statement = build_page_statement(
            "supplier", COLUMNS, query, "sqlite", limit=5, offset=0
        )
        self.assertEqual(statement.params[0], "%50!%!_!!%")
        self.assertIn("ESCAPE '!'", statement.sql)

    def test_column_must_be_in_the_visible_list(self):
        for query in (
            PreviewQuery(filters=[PreviewFilter("secret_cost", "eq", "1")]),
            PreviewQuery(sort_column="secret_cost"),
            PreviewQuery(filters=[PreviewFilter('supplier_id"; DROP TABLE supplier; --', "eq", "1")]),
        ):
            with self.assertRaises(PreviewInputError) as caught:
                build_page_statement("supplier", COLUMNS, query, "sqlite", limit=5, offset=0)
            self.assertEqual(caught.exception.code, "unknown_column")

    def test_operator_is_a_closed_set(self):
        query = PreviewQuery(filters=[PreviewFilter("supplier_name", "regexp", "a")])
        with self.assertRaises(PreviewInputError) as caught:
            build_page_statement("supplier", COLUMNS, query, "sqlite", limit=5, offset=0)
        self.assertEqual(caught.exception.code, "unknown_operator")

    def test_numeric_column_requires_a_number(self):
        for bad in ("abc", "", "nan", "inf", "1; DROP TABLE x"):
            query = PreviewQuery(filters=[PreviewFilter("on_time_rate", "gte", bad)])
            with self.assertRaises(PreviewInputError, msg=bad):
                build_page_statement("supplier", COLUMNS, query, "sqlite", limit=5, offset=0)

        query = PreviewQuery(filters=[PreviewFilter("on_time_rate", "eq", "1,200")])
        statement = build_page_statement("supplier", COLUMNS, query, "sqlite", limit=5, offset=0)
        self.assertEqual(statement.params[0], 1200)

    def test_too_many_filters_are_rejected(self):
        query = PreviewQuery(
            filters=[PreviewFilter("supplier_name", "contains", "a")] * 6
        )
        with self.assertRaises(PreviewInputError) as caught:
            build_page_statement("supplier", COLUMNS, query, "sqlite", limit=5, offset=0)
        self.assertEqual(caught.exception.code, "too_many_filters")

    def test_order_is_always_deterministic(self):
        # 不指定排序：主键兜底。指定了：主键仍然跟在后面做平局裁决。
        plain = build_page_statement("supplier", COLUMNS, PreviewQuery(), "sqlite", limit=5, offset=0)
        self.assertIn('ORDER BY "supplier"."supplier_id" ASC LIMIT', plain.sql)
        self.assertTrue(has_stable_order(COLUMNS))

        ranked = build_page_statement(
            "supplier", COLUMNS,
            PreviewQuery(sort_column="on_time_rate", sort_descending=True),
            "sqlite", limit=5, offset=0,
        )
        self.assertIn(
            'ORDER BY "supplier"."on_time_rate" DESC, "supplier"."supplier_id" ASC LIMIT', ranked.sql
        )

        # 没有（完整可见的）主键：拿全部能排序的列兜底。只用第一列的话，
        # 第一列相同的行在两页之间的先后是不确定的，翻页会漏行或重复。
        keyless = [PreviewColumn("a", "text"), PreviewColumn("b", "number"), PreviewColumn("c", "binary")]
        fallback = build_page_statement("t", keyless, PreviewQuery(), "sqlite", limit=5, offset=0)
        self.assertIn('ORDER BY "t"."a" ASC, "t"."b" ASC LIMIT', fallback.sql)
        self.assertFalse(has_stable_order(keyless))

    def test_identifiers_are_quoted(self):
        columns = [PreviewColumn('odd "name"', "text", primary_key=True)]
        statement = build_page_statement('my "table"', columns, PreviewQuery(), "sqlite", limit=1, offset=0)
        self.assertIn('"odd ""name"""', statement.sql)
        self.assertIn('FROM "my ""table"""', statement.sql)

    def test_summary_counts_and_reports_date_ranges(self):
        statement, dates = build_summary_statement(
            "supplier", COLUMNS,
            PreviewQuery(filters=[PreviewFilter("on_time_rate", "lt", "0.9")]),
            "sqlite",
        )
        self.assertEqual(dates, ["joined_on"])
        self.assertIn('COUNT(*) AS "total_rows"', statement.sql)
        self.assertIn('MIN("joined_on") AS "min_0"', statement.sql)
        self.assertEqual(statement.params, (0.9,))

    def test_value_kind(self):
        self.assertEqual(value_kind("INTEGER"), "number")
        self.assertEqual(value_kind("numeric(12,2)"), "number")
        self.assertEqual(value_kind("double precision"), "number")
        self.assertEqual(value_kind("character varying"), "text")
        self.assertEqual(value_kind("timestamp with time zone"), "date")
        self.assertEqual(value_kind("interval"), "other")
        self.assertEqual(value_kind(""), "other")
        # SQLite 把日期存成 TEXT：语义层标了 time 的按日期算
        self.assertEqual(value_kind("TEXT", "time"), "date")
        for declared in ("BLOB", "bytea", "varbinary(64)", "LONGBLOB"):
            self.assertEqual(value_kind(declared), "binary", declared)
        # 二进制列不管语义层标成什么，都不能整格取回
        self.assertEqual(value_kind("BLOB", "time"), "binary")

    def test_percent_in_identifier_is_not_supported_on_postgres(self):
        # 带参数执行时 psycopg 会解释 `%`；写成 `%%` 的话授权关卡看到的就是另一个名字
        self.assertFalse(supported_identifier("pct%", "postgres"))
        self.assertTrue(supported_identifier("pct%", "sqlite"))
        self.assertTrue(supported_identifier("客户 名称", "postgres"))
        self.assertFalse(supported_identifier("", "sqlite"))

    def test_quoted_keyword_names_are_supported_by_the_ast_guard(self):
        """
        执行器的只读校验按语法树判（不再按词扫），引号里的标识符不是关键字：
        叫 `set`、`update`、`Last Update` 的表与列照常能预览。2026-10 之前这里
        是反过来的——旧校验会把它们拒掉，目录得如实标成「预览不了」。
        """
        for name in ("set", "update", "Last Update", "call", "a;b", "into", "last_update",
                     "settings", "updated_at", "order", "group", "select"):
            self.assertTrue(supported_identifier(name, "sqlite"), name)
            self.assertTrue(supported_identifier(name, "postgres"), name)
        # 空名与 NUL（我们自己的占位符哨兵）仍然不行
        for name in ("", "a\x00b"):
            self.assertFalse(supported_identifier(name, "sqlite"), repr(name))

    # ---- 取回来的东西有上限 ----

    def test_long_values_are_cut_in_sql_not_after_the_fetch(self):
        columns = [
            PreviewColumn("id", "number", primary_key=True, data_type="INTEGER"),
            PreviewColumn("note", "text", data_type="TEXT"),
            PreviewColumn("photo", "binary", data_type="BLOB"),
            PreviewColumn("day", "date", data_type="TEXT"),
            PreviewColumn("misc", "other", data_type=""),
        ]
        cap = MAX_CELL_CHARS + 1
        sqlite_sql = build_page_statement("doc", columns, PreviewQuery(), "sqlite", limit=5, offset=0).sql
        self.assertIn(f'SUBSTR("note", 1, {cap}) AS "note"', sqlite_sql)
        # 二进制只取字节数：几兆的附件一个字节都不进应用内存
        self.assertIn('LENGTH(CAST("photo" AS BLOB)) AS "photo"', sqlite_sql)
        self.assertNotIn('SELECT "id", "note", "photo"', sqlite_sql)
        # 没声明类型的列什么都可能存：文本和二进制截，数字原样
        self.assertIn(
            f"CASE WHEN TYPEOF(\"misc\") IN ('text', 'blob') THEN SUBSTR(\"misc\", 1, {cap}) "
            'ELSE "misc" END AS "misc"',
            sqlite_sql,
        )
        self.assertIn('SELECT "id", ', sqlite_sql)
        self.assertIn(', "day", ', sqlite_sql)

        postgres = [
            PreviewColumn("id", "number", primary_key=True, data_type="bigint"),
            PreviewColumn("note", "text", data_type="text"),
            PreviewColumn("photo", "binary", data_type="bytea"),
            PreviewColumn("payload", "other", data_type="jsonb"),
            PreviewColumn("active", "other", data_type="boolean"),
        ]
        postgres_sql = build_page_statement("doc", postgres, PreviewQuery(), "postgres", limit=5, offset=0).sql
        self.assertIn(f'LEFT(CAST("note" AS TEXT), {cap}) AS "note"', postgres_sql)
        self.assertIn('OCTET_LENGTH("photo") AS "photo"', postgres_sql)
        self.assertIn(f'LEFT(CAST("payload" AS TEXT), {cap}) AS "payload"', postgres_sql)
        self.assertIn(', "active" FROM', postgres_sql)

    def test_binary_columns_can_only_be_tested_for_content(self):
        columns = [PreviewColumn("id", "number", primary_key=True), PreviewColumn("photo", "binary")]
        statement = build_page_statement(
            "doc", columns, PreviewQuery(filters=[PreviewFilter("photo", "not_empty")]),
            "sqlite", limit=5, offset=0,
        )
        self.assertIn('("photo" IS NOT NULL AND LENGTH(CAST("photo" AS BLOB)) > 0)', statement.sql)
        self.assertNotIn('CAST("photo" AS TEXT)', statement.sql)

        for query in (
            PreviewQuery(filters=[PreviewFilter("photo", "contains", "x")]),
            PreviewQuery(sort_column="photo"),
        ):
            with self.assertRaises(PreviewInputError) as caught:
                build_page_statement("doc", columns, query, "sqlite", limit=5, offset=0)
            self.assertEqual(caught.exception.code, "invalid_value")

        self.assertFalse(is_sortable(columns[1], "sqlite"))
        # 搜索词不比二进制列；一列能搜的都没有时直接说，不是悄悄返回全部
        only_binary = [PreviewColumn("photo", "binary")]
        with self.assertRaises(PreviewInputError):
            build_page_statement("doc", only_binary, PreviewQuery(search="x"), "sqlite", limit=5, offset=0)

    def test_postgres_types_without_an_ordering_are_not_sortable(self):
        self.assertFalse(is_sortable(PreviewColumn("payload", "other", data_type="jsonb"), "postgres"))
        self.assertTrue(is_sortable(PreviewColumn("active", "other", data_type="boolean"), "postgres"))
        self.assertTrue(is_sortable(PreviewColumn("anything", "other", data_type=""), "sqlite"))
        with self.assertRaises(PreviewInputError):
            build_page_statement(
                "doc", [PreviewColumn("payload", "other", data_type="jsonb")],
                PreviewQuery(sort_column="payload"), "postgres", limit=5, offset=0,
            )

    def test_search_covers_a_bounded_set_of_columns_and_says_so(self):
        wide = [PreviewColumn(f"c{index}", "text") for index in range(MAX_SEARCH_COLUMNS + 3)]
        targets, limited = search_targets(wide)
        self.assertEqual(len(targets), MAX_SEARCH_COLUMNS)
        self.assertTrue(limited)
        statement = build_page_statement("t", wide, PreviewQuery(search="x"), "sqlite", limit=5, offset=0)
        self.assertEqual(statement.sql.count(" LIKE "), MAX_SEARCH_COLUMNS)

        targets, limited = search_targets(COLUMNS)
        self.assertEqual([column.name for column in targets], ["supplier_id", "supplier_name"])
        self.assertFalse(limited)

    # ---- 筛选值按它本来的含义比 ----

    def test_integers_are_bound_exactly(self):
        # 经过 float 的话 9007199254740993 会变成 …992：筛出来的是另一条记录
        columns = [PreviewColumn("big_id", "number", primary_key=True)]
        for value in ("9007199254740993", 9007199254740993):
            statement = build_page_statement(
                "t", columns, PreviewQuery(filters=[PreviewFilter("big_id", "eq", value)]),
                "sqlite", limit=5, offset=0,
            )
            self.assertEqual(statement.params[0], 9007199254740993)
            self.assertIsInstance(statement.params[0], int)

        # 写成浮点的就是浮点：不再转成一个 SQLite 装不下的整数
        statement = build_page_statement(
            "t", columns, PreviewQuery(filters=[PreviewFilter("big_id", "lt", "1e30")]),
            "sqlite", limit=5, offset=0,
        )
        self.assertEqual(statement.params[0], 1e30)
        self.assertIsInstance(statement.params[0], float)

        for bad in ("9" * 40, "0x10", "1_000", "1e", "--1"):
            with self.assertRaises(PreviewInputError, msg=bad):
                build_page_statement(
                    "t", columns, PreviewQuery(filters=[PreviewFilter("big_id", "eq", bad)]),
                    "sqlite", limit=5, offset=0,
                )

        # 中文输入法打出来的全角数字照常认：它就是这个数
        statement = build_page_statement(
            "t", columns, PreviewQuery(filters=[PreviewFilter("big_id", "eq", "１２")]),
            "sqlite", limit=5, offset=0,
        )
        self.assertEqual(statement.params[0], 12)

    def test_a_date_without_a_time_means_the_whole_day(self):
        def clause(op, value, dialect="sqlite", data_type="TEXT"):
            columns = [PreviewColumn("id", "number", primary_key=True), PreviewColumn("at", "date", data_type=data_type)]
            statement = build_page_statement(
                "t", columns, PreviewQuery(filters=[PreviewFilter("at", op, value)]),
                dialect, limit=5, offset=0,
            )
            where = statement.sql.split(" WHERE ")[1].split(" ORDER BY ")[0]
            return where, statement.params[:-2]

        text = 'CAST("at" AS TEXT)'
        self.assertEqual(clause("eq", "2024-03-31"), (f"({text} >= ? AND {text} < ?)", ("2024-03-31", "2024-04-01")))
        self.assertEqual(clause("ne", "2024-03-31"), (f"({text} < ? OR {text} >= ?)", ("2024-03-31", "2024-04-01")))
        self.assertEqual(clause("lte", "2024-03-31"), (f"{text} < ?", ("2024-04-01",)))
        self.assertEqual(clause("gt", "2024-03-31"), (f"{text} >= ?", ("2024-04-01",)))
        self.assertEqual(clause("gte", "2024-03-31"), (f"{text} >= ?", ("2024-03-31",)))
        self.assertEqual(clause("lt", "2024-12-31"), (f"{text} < ?", ("2024-12-31",)))
        # 跨月、跨年、闰日都由日历算，不是字符串加一
        self.assertEqual(clause("lte", "2024-02-28")[1], ("2024-02-29",))
        self.assertEqual(clause("lte", "2023-12-31")[1], ("2024-01-01",))
        # 写了时刻就按时刻比
        self.assertEqual(clause("gte", "2024-03-31 10:30"), (f"{text} >= ?", ("2024-03-31 10:30",)))

        # PostgreSQL 的日期/时间戳列按原生类型比：不转文本，不受 DateStyle 影响
        self.assertEqual(
            clause("eq", "2024-03-31", "postgres", "timestamp without time zone"),
            ('("at" >= CAST(%s AS DATE) AND "at" < CAST(%s AS DATE))', ("2024-03-31", "2024-04-01")),
        )
        self.assertEqual(
            clause("lt", "2024-03-31T08:00:00", "postgres", "date"),
            ('"at" < CAST(%s AS TIMESTAMP)', ("2024-03-31T08:00:00",)),
        )

        for bad in ("2024-02-30", "2024-13-01", "9999-12-31", "2024-03-31 25:00"):
            with self.assertRaises(PreviewInputError, msg=bad):
                clause("lte", bad)
        # 不是 ISO 写法：等值还能原样比，比大小没有意义
        self.assertEqual(clause("eq", "03/31/2024"), (f"LOWER({text}) = LOWER(?)", ("03/31/2024",)))
        with self.assertRaises(PreviewInputError):
            clause("gte", "03/31/2024")
        with self.assertRaises(PreviewInputError):
            clause("eq", "03/31/2024", "postgres", "date")

    def test_control_characters_are_rejected(self):
        # NUL 在 SQLite 的 LIKE 里会把模式截成空串（于是匹配所有行），在 psycopg 里直接报错
        for query in (
            PreviewQuery(search="a\x00b"),
            PreviewQuery(filters=[PreviewFilter("supplier_name", "contains", "a\x00")]),
            PreviewQuery(filters=[PreviewFilter("supplier_name", "eq", "line\nbreak")]),
        ):
            with self.assertRaises(PreviewInputError) as caught:
                build_page_statement("supplier", COLUMNS, query, "sqlite", limit=5, offset=0)
            self.assertEqual(caught.exception.code, "invalid_value")


class ExecutionRequestLimitsTest(unittest.TestCase):
    """执行器：绑定参数，以及「只能收紧」的行数与时长上限。"""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmpdir.name) / "limits.db"
        conn = sqlite3.connect(cls.db_path)
        conn.execute("CREATE TABLE item (id INTEGER PRIMARY KEY, name TEXT)")
        conn.executemany(
            "INSERT INTO item (id, name) VALUES (?, ?)",
            [(index, f"item-{index}") for index in range(1, 31)],
        )
        conn.commit()
        conn.close()
        cls.executor = SQLiteMCPExecutor("limits_db", cls.db_path, max_rows=10)

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    def _run(self, **overrides):
        from ai_sql_agent.auth import authorization_scope

        request = MCPExecutionRequest(
            database="limits_db",
            sql=overrides.pop("sql", "SELECT id, name FROM item ORDER BY id"),
            **overrides,
        )
        with authorization_scope(ALLOW_ALL):
            return self.executor.execute(request)

    def test_parameters_are_bound(self):
        result = self._run(
            sql="SELECT id, name FROM item WHERE name = ? AND id > ?",
            params=("item-7", 0),
        )
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.rows, [{"id": 7, "name": "item-7"}])

    def test_bound_value_is_data_not_sql(self):
        result = self._run(
            sql="SELECT id FROM item WHERE name = ?",
            params=("x' OR '1'='1",),
        )
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.rows, [])

    def test_request_can_lower_the_row_cap(self):
        result = self._run(max_rows=3)
        self.assertEqual(result.row_count, 3)
        self.assertTrue(result.truncated)
        self.assertEqual(result.row_limit, 3)

    def test_request_cannot_raise_the_row_cap(self):
        result = self._run(max_rows=10_000)
        self.assertEqual(result.row_count, 10)
        self.assertTrue(result.truncated)
        self.assertEqual(result.row_limit, 10)

    def test_default_request_keeps_executor_limits(self):
        request = MCPExecutionRequest(database="limits_db", sql="SELECT 1")
        self.assertEqual(request.row_cap(10), 10)
        self.assertEqual(request.time_cap(30.0), 30.0)
        self.assertEqual(MCPExecutionRequest("d", "s", timeout_seconds=5).time_cap(30.0), 5.0)
        self.assertEqual(MCPExecutionRequest("d", "s", timeout_seconds=90).time_cap(30.0), 30.0)
        self.assertEqual(MCPExecutionRequest("d", "s", max_rows=0).row_cap(10), 1)

    def test_writes_stay_blocked_with_parameters(self):
        result = self._run(sql="DELETE FROM item WHERE id = ?", params=(1,))
        self.assertFalse(result.success)

    def test_protocol_executor_refuses_parameters(self):
        from ai_sql_agent.auth import authorization_scope

        executor = MCPProtocolExecutor(database="limits_db")
        self.assertFalse(executor.supports_params)
        with authorization_scope(ALLOW_ALL):
            result = executor.execute(
                MCPExecutionRequest("limits_db", "SELECT id FROM item WHERE id = ?", params=(1,))
            )
        self.assertFalse(result.success)
        self.assertIn("绑定参数", result.error)


def _build_shop(directory: Path):
    """一个两库的小世界：shop_db 有表，gone_db 的库文件不存在。"""
    db_path = directory / "shop.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE supplier (
            supplier_id TEXT PRIMARY KEY,
            supplier_name TEXT NOT NULL,
            category TEXT,
            on_time_rate REAL,
            unit_cost REAL,
            joined_on TEXT
        );
        CREATE TABLE purchase_order (
            po_no TEXT PRIMARY KEY,
            supplier_id TEXT REFERENCES supplier(supplier_id),
            po_amt REAL
        );
        CREATE TABLE scratch (id INTEGER PRIMARY KEY, note TEXT);
        """
    )
    conn.executemany(
        "INSERT INTO supplier VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("SUP-01", "Westbridge Panel Supply", "Panels", 0.95, 12.5, "2024-01-05"),
            ("SUP-02", "Harborline Hardware", "Hardware", 0.88, 3.2, "2024-02-11"),
            ("SUP-03", "Canyon Crest Quartz", "Countertops", 0.72, 48.0, "2024-03-20"),
            ("SUP-04", "Mesa 100% Finish", "Coatings", 0.93, 9.9, "2024-03-28"),
            ("SUP-05", "Clearbrook Glass", "Glass", 0.65, None, "2024-04-30"),
        ],
    )
    conn.executemany(
        "INSERT INTO purchase_order VALUES (?, ?, ?)",
        [("PO-1", "SUP-01", 31600.0), ("PO-2", "SUP-03", 8200.5)],
    )
    conn.commit()
    conn.close()

    semantic = SemanticLayer(
        databases={
            "shop_db": DatabaseSemantics(
                name="shop_db",
                description="采购练习库",
                aliases=["采购库", "ERP"],
                db_path=str(db_path),
                tables={
                    "supplier": {
                        "description": "供应商表",
                        "aliases": ["供应商表", "supplier"],
                        "label_en": "Suppliers",
                        "columns": {
                            "supplier_id": {"description": "供应商编号", "semantic_role": "join_key"},
                            "supplier_name": {
                                "description": "供应商名称",
                                "aliases": ["供应商", "supplier name"],
                                "semantic_role": "output_dimension",
                            },
                            "on_time_rate": {"description": "准时率", "unit": "ratio", "value_range": "0-1"},
                            "unit_cost": {"description": "单位成本", "unit": "USD"},
                            "joined_on": {"description": "合作起始日", "semantic_role": "time"},
                            "retired_flag": {"description": "库里已经删掉的字段"},
                        },
                    },
                    "purchase_order": {
                        "description": "采购单表",
                        "columns": {"po_amt": {"description": "采购金额", "unit": "USD"}},
                    },
                    "ghost": {"description": "语义层写了、库里没有的表", "columns": {"id": {}}},
                },
                display={"label_zh": "采购与供应商"},
            ),
            "gone_db": DatabaseSemantics(
                name="gone_db",
                description="库文件不存在",
                db_path=str(directory / "missing.db"),
                tables={"anything": {"description": "读不到", "columns": {"id": {}}}},
            ),
        },
        cross_database_links=[
            {
                "left": "shop_db.supplier.supplier_id",
                "right": "gone_db.anything.id",
                "meaning": "同一个供应商",
            }
        ],
        metric_definitions={"准时率": "按约定交期到货的批次占比。"},
        source="test",
    )
    databases = [
        DatabaseConfig(name="shop_db", db_path=db_path),
        DatabaseConfig(name="gone_db", db_path=directory / "missing.db"),
    ]
    router = MCPRouter()
    for database in databases:
        router.register_executor(
            database.name,
            SQLiteMCPExecutor(database.name, database.db_path, max_rows=1000),
        )
    return DataCatalog(semantic=semantic, databases=databases, router=router), db_path


class CatalogServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.tmpdir.name)
        cls.catalog, cls.db_path = _build_shop(cls.directory)

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    def _domain(self, payload, name):
        return next(item for item in payload["domains"] if item["database"] == name)

    def _table(self, payload, database, table):
        return next(
            item for item in self._domain(payload, database)["tables"] if item["name"] == table
        )

    # ---- 目录 ----

    def test_catalog_merges_semantics_with_the_real_schema(self):
        payload = self.catalog.describe(ALLOW_ALL)
        shop = self._domain(payload, "shop_db")

        self.assertEqual(shop["status"], "available")
        # 别名里有 ERP → 认出系统类型；语义层写了中文名就用写的，英文名用预置的
        self.assertEqual(shop["kind"], "erp")
        self.assertEqual(shop["title"], {"zh": "采购与供应商", "en": "Orders & Purchasing"})
        self.assertEqual(shop["title_source"], "semantic")
        # 语义层登记的顺序在前，库里多出来的表排在后面
        self.assertEqual(
            [table["name"] for table in shop["tables"]],
            ["supplier", "purchase_order", "ghost", "scratch"],
        )

        supplier = self._table(payload, "shop_db", "supplier")
        self.assertEqual(supplier["label_en"], "Suppliers")
        self.assertEqual(supplier["primary_key"], ["supplier_id"])
        columns = {column["name"]: column for column in supplier["columns"]}
        self.assertEqual(columns["on_time_rate"]["unit"], "ratio")
        self.assertEqual(columns["on_time_rate"]["kind"], "number")
        self.assertEqual(columns["joined_on"]["kind"], "date")
        self.assertEqual(columns["supplier_id"]["data_type"], "TEXT")
        self.assertTrue(columns["supplier_id"]["primary_key"])
        # 库里有、语义层没写：照常列出，标明没有说明
        self.assertFalse(columns["category"]["documented"])
        self.assertEqual(columns["category"]["status"], "available")

        self.assertEqual(payload["glossary"], [{"term": "准时率", "definition": "按约定交期到货的批次占比。"}])
        self.assertFalse(payload["access_limited"])
        self.assertTrue(payload["columns_included"])

    def test_mismatch_between_semantics_and_database_is_reported(self):
        payload = self.catalog.describe(ALLOW_ALL)

        ghost = self._table(payload, "shop_db", "ghost")
        self.assertEqual(ghost["status"], "missing")
        self.assertFalse(ghost["previewable"])

        supplier = self._table(payload, "shop_db", "supplier")
        retired = next(c for c in supplier["columns"] if c["name"] == "retired_flag")
        self.assertEqual(retired["status"], "missing")

        scratch = self._table(payload, "shop_db", "scratch")
        self.assertFalse(scratch["documented"])
        self.assertEqual(scratch["status"], "available")

    def test_unreachable_database_is_a_status_not_an_empty_list(self):
        payload = self.catalog.describe(ALLOW_ALL, refresh=True)
        gone = self._domain(payload, "gone_db")

        self.assertEqual(gone["status"], "unavailable")
        self.assertEqual(gone["status_reason"], "file_not_found")
        self.assertEqual(gone["tables"], [])
        # 读目录不该顺手把库文件建出来——那样下一次看到的就是「一个空库」
        self.assertFalse((self.directory / "missing.db").exists())

        with self.assertRaises(CatalogError) as caught:
            self.catalog.preview(ALLOW_ALL, database="gone_db", table="anything", query=PreviewQuery())
        self.assertEqual((caught.exception.code, caught.exception.status), ("unavailable", 503))

    def test_no_connection_details_leak_into_the_catalog(self):
        text = json.dumps(self.catalog.describe(ALLOW_ALL), ensure_ascii=False)
        self.assertNotIn(str(self.directory), text)
        self.assertNotIn("shop.db", text)
        self.assertNotIn("db_path", text)
        self.assertNotIn("dsn", text)

    def test_foreign_keys_and_cross_database_links(self):
        payload = self.catalog.describe(ALLOW_ALL)
        po = self._table(payload, "shop_db", "purchase_order")
        supplier_id = next(c for c in po["columns"] if c["name"] == "supplier_id")
        self.assertEqual(
            supplier_id["references"],
            [{"relation": "foreign_key", "database": "shop_db", "table": "supplier",
              "column": "supplier_id", "note": ""}],
        )

        supplier = self._table(payload, "shop_db", "supplier")
        key = next(c for c in supplier["columns"] if c["name"] == "supplier_id")
        self.assertEqual(key["references"][0]["relation"], "cross_database")
        self.assertEqual(key["references"][0]["database"], "gone_db")

    # ---- 权限 ----

    def test_denied_tables_and_columns_do_not_appear(self):
        policy = DataPolicy(
            allow_tables=("shop_db.*",),
            deny_tables=("shop_db.purchase_order",),
            deny_columns=("shop_db.supplier.unit_cost",),
        )
        payload = self.catalog.describe(policy)

        self.assertEqual([d["database"] for d in payload["domains"]], ["shop_db"])
        shop = self._domain(payload, "shop_db")
        self.assertNotIn("purchase_order", [t["name"] for t in shop["tables"]])
        supplier = self._table(payload, "shop_db", "supplier")
        self.assertNotIn("unit_cost", [c["name"] for c in supplier["columns"]])
        # 只说「有东西没给你看」，不说是什么
        self.assertTrue(payload["access_limited"])
        self.assertNotIn("purchase_order", json.dumps(payload, ensure_ascii=False))
        self.assertNotIn("unit_cost", json.dumps(payload, ensure_ascii=False))
        # gone_db 对这个人不可见：连「它不可用」都不该告诉他
        self.assertNotIn("gone_db", json.dumps(payload, ensure_ascii=False))

    def test_references_to_denied_targets_are_dropped(self):
        policy = DataPolicy(allow_tables=("shop_db.purchase_order",))
        payload = self.catalog.describe(policy)
        po = self._table(payload, "shop_db", "purchase_order")
        supplier_id = next(c for c in po["columns"] if c["name"] == "supplier_id")
        self.assertEqual(supplier_id["references"], [])

    def test_preview_never_returns_denied_columns(self):
        policy = DataPolicy(
            allow_tables=("shop_db.*",), deny_columns=("shop_db.supplier.unit_cost",)
        )
        page = self.catalog.preview(policy, database="shop_db", table="supplier", query=PreviewQuery())

        names = [column["name"] for column in page["columns"]]
        self.assertNotIn("unit_cost", names)
        self.assertNotIn("unit_cost", page["sql"])
        self.assertTrue(page["columns_hidden"])
        self.assertTrue(all(len(row) == len(names) for row in page["rows"]))

        # 被禁的列也不能拿来筛选或排序——那等于用它的值做了一次查询
        for query in (
            PreviewQuery(filters=[PreviewFilter("unit_cost", "gt", "10")]),
            PreviewQuery(sort_column="unit_cost"),
        ):
            with self.assertRaises(CatalogError) as caught:
                self.catalog.preview(policy, database="shop_db", table="supplier", query=query)
            self.assertEqual(caught.exception.code, "unknown_column")

    def test_denied_table_and_unknown_table_look_the_same(self):
        policy = DataPolicy(allow_tables=("shop_db.supplier",))
        outcomes = []
        for table in ("purchase_order", "no_such_table"):
            with self.assertRaises(CatalogError) as caught:
                self.catalog.preview(policy, database="shop_db", table=table, query=PreviewQuery())
            outcomes.append((caught.exception.code, caught.exception.status, caught.exception.message))
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(outcomes[0][:2], ("not_found", 404))

    def test_executor_gate_still_decides(self):
        """
        目录这一层放行、执行器关卡拒绝 → 预览被拒。

        用一个「检索层说行、执行层说不行」的策略把两层拆开：证明预览确实从
        执行器的强制授权关卡过，而不是目录自己判完就直连数据库。
        """

        class CatalogSaysYesGateSaysNo:
            def allows_table(self, database, table):
                return True

            def allows_column(self, database, table, column):
                return True

            def allows_table_reference(self, database, names):
                return False

        with self.assertRaises(CatalogError) as caught:
            self.catalog.preview(
                CatalogSaysYesGateSaysNo(),
                database="shop_db", table="supplier", query=PreviewQuery(),
            )
        self.assertEqual((caught.exception.code, caught.exception.status), ("forbidden", 403))

    def test_preview_without_authorization_scope_is_refused_by_the_executor(self):
        # 预览自己绑定授权作用域；这里直接证明「不绑就被拒」这条底线还在
        executor = self.catalog.router.executors["shop_db"]
        result = executor.execute(
            MCPExecutionRequest("shop_db", 'SELECT "supplier_id" FROM "supplier" LIMIT ?', params=(1,))
        )
        self.assertFalse(result.success)
        self.assertIn("授权上下文", result.error)

    # ---- 预览 ----

    def test_paging_reports_total_and_whether_more_exist(self):
        first = self.catalog.preview(
            ALLOW_ALL, database="shop_db", table="supplier", query=PreviewQuery(page=1, page_size=2)
        )
        self.assertEqual([row[0] for row in first["rows"]], ["SUP-01", "SUP-02"])
        self.assertTrue(first["has_more"])
        self.assertEqual(first["total_rows"], 5)
        self.assertEqual(first["as_of_source"], "query_time")

        last = self.catalog.preview(
            ALLOW_ALL, database="shop_db", table="supplier", query=PreviewQuery(page=3, page_size=2)
        )
        self.assertEqual([row[0] for row in last["rows"]], ["SUP-05"])
        self.assertFalse(last["has_more"])
        self.assertEqual(last["offset"], 4)

    def test_date_range_describes_the_records_not_the_request(self):
        page = self.catalog.preview(
            ALLOW_ALL, database="shop_db", table="supplier",
            query=PreviewQuery(filters=[PreviewFilter("on_time_rate", "lt", "0.9")]),
        )
        self.assertEqual(page["total_rows"], 3)
        self.assertEqual(
            page["date_ranges"],
            [{"column": "joined_on", "min": "2024-02-11", "max": "2024-04-30"}],
        )
        # 读库时刻是今天，业务日期是 2024 年：两个字段，不能混成一个
        self.assertFalse(page["as_of"].startswith("2024"))

    def test_search_and_filters(self):
        def names(**query):
            page = self.catalog.preview(
                ALLOW_ALL, database="shop_db", table="supplier", query=PreviewQuery(**query)
            )
            return [row[1] for row in page["rows"]]

        self.assertEqual(names(search="quartz"), ["Canyon Crest Quartz"])
        self.assertEqual(names(filters=[PreviewFilter("category", "eq", "hardware")]), ["Harborline Hardware"])
        self.assertEqual(
            names(filters=[PreviewFilter("on_time_rate", "gte", "0.93")]),
            ["Westbridge Panel Supply", "Mesa 100% Finish"],
        )
        self.assertEqual(names(filters=[PreviewFilter("unit_cost", "is_empty")]), ["Clearbrook Glass"])
        self.assertEqual(
            names(filters=[PreviewFilter("joined_on", "gte", "2024-03-01"),
                           PreviewFilter("joined_on", "lt", "2024-04-01")]),
            ["Canyon Crest Quartz", "Mesa 100% Finish"],
        )
        # `%` 是要找的字符，不是通配符
        self.assertEqual(names(search="100%"), ["Mesa 100% Finish"])
        self.assertEqual(names(search="%"), ["Mesa 100% Finish"])
        self.assertEqual(
            names(sort_column="on_time_rate", sort_descending=True)[:2],
            ["Westbridge Panel Supply", "Mesa 100% Finish"],
        )

    def test_hostile_search_term_matches_nothing_and_changes_nothing(self):
        page = self.catalog.preview(
            ALLOW_ALL, database="shop_db", table="supplier",
            query=PreviewQuery(search="'; DROP TABLE supplier; --"),
        )
        self.assertEqual(page["rows"], [])
        self.assertEqual(page["total_rows"], 0)

        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM supplier").fetchone()[0], 5)
        finally:
            conn.close()

    def test_page_size_and_depth_are_capped(self):
        page = self.catalog.preview(
            ALLOW_ALL, database="shop_db", table="supplier", query=PreviewQuery(page_size=10_000)
        )
        self.assertEqual(page["page_size"], 100)

        with self.assertRaises(CatalogError) as caught:
            self.catalog.preview(
                ALLOW_ALL, database="shop_db", table="supplier",
                query=PreviewQuery(page=1_000, page_size=100),
            )
        self.assertEqual(caught.exception.code, "too_deep")

    def test_table_missing_from_database_cannot_be_previewed(self):
        with self.assertRaises(CatalogError) as caught:
            self.catalog.preview(ALLOW_ALL, database="shop_db", table="ghost", query=PreviewQuery())
        self.assertEqual(caught.exception.code, "not_found")

    def test_executor_without_parameter_support_is_reported(self):
        class TextOnlyExecutor:
            dialect = "sqlite"

            def execute(self, request):  # pragma: no cover - 不该被调到
                raise AssertionError("不支持参数的执行器不应该被用来预览")

        router = MCPRouter()
        router.register_executor("shop_db", TextOnlyExecutor())
        limited = DataCatalog(
            semantic=self.catalog.semantic, databases=self.catalog.databases[:1], router=router
        )
        payload = limited.describe(ALLOW_ALL)
        self.assertFalse(self._domain(payload, "shop_db")["preview_supported"])
        with self.assertRaises(CatalogError) as caught:
            limited.preview(ALLOW_ALL, database="shop_db", table="supplier", query=PreviewQuery())
        self.assertEqual((caught.exception.code, caught.exception.status), ("unsupported", 501))

    def test_cells_survive_json(self):
        self.assertEqual(_cell(Decimal("31600.00")), 31600)
        self.assertEqual(_cell(Decimal("0.1")), 0.1)
        # 浮点装不下的精度宁可给字符串，不给一个被改过的数
        self.assertEqual(_cell(Decimal("0.12345678901234567890")), "0.12345678901234567890")
        self.assertEqual(_cell(2**60), str(2**60))
        self.assertEqual(_cell(b"\x00\x01"), "<binary, 2 bytes>")
        self.assertEqual(_cell(float("nan")), "nan")
        self.assertIsNone(_cell(None))


class _SpyRouter:
    """记下经过路由的每一条语句；可以让某一类语句失败（模拟超时）。"""

    def __init__(self, inner, fail_when=None):
        self.inner = inner
        self.executors = inner.executors
        self.statements = []
        self.results = []
        self.fail_when = fail_when

    def execute(self, request):
        self.statements.append(request.sql)
        if self.fail_when and self.fail_when(request.sql):
            from ai_sql_agent.execution import MCPExecutionResult

            result = MCPExecutionResult(
                database=request.database, sql=request.sql, success=False, error="SQL 执行超时"
            )
        else:
            result = self.inner.execute(request)
        self.results.append(result)
        return result


def _catalog_from_script(directory, script, *, name="lab_db", tables=None, metric_definitions=None,
                         rows=None, clock=None, spy=False, fail_when=None):
    db_path = Path(directory) / f"{name}.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(script)
    for statement, values in rows or []:
        conn.executemany(statement, values)
    conn.commit()
    conn.close()

    semantic = SemanticLayer(
        databases={
            name: DatabaseSemantics(name=name, description="", db_path=str(db_path), tables=tables or {})
        },
        metric_definitions=metric_definitions or {},
        source="test",
    )
    databases = [DatabaseConfig(name=name, db_path=db_path)]
    router = MCPRouter()
    router.register_executor(name, SQLiteMCPExecutor(name, db_path, max_rows=1000))
    if spy or fail_when:
        router = _SpyRouter(router, fail_when)
    options = {"clock": clock} if clock is not None else {}
    return DataCatalog(semantic=semantic, databases=databases, router=router, **options), db_path


class CatalogHardeningTest(unittest.TestCase):
    """评审里逐条复现出来的问题。每条一个用例，名字就是它不该再发生的事。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.directory = Path(self._tmpdir.name)

    @staticmethod
    def _table(payload, table):
        return next(item for item in payload["domains"][0]["tables"] if item["name"] == table)

    # ---- 权限 ----

    def test_a_hidden_primary_key_is_not_named_anywhere(self):
        catalog, _ = _build_shop(self.directory)
        policy = DataPolicy(allow_tables=("shop_db.*",), deny_columns=("shop_db.*.supplier_id",))

        payload = catalog.describe(policy)
        supplier = next(
            table for table in payload["domains"][0]["tables"] if table["name"] == "supplier"
        )
        self.assertEqual(supplier["primary_key"], [])
        self.assertNotIn("supplier_id", json.dumps(payload, ensure_ascii=False))
        detail = catalog.describe_table(policy, database="shop_db", table="supplier")
        self.assertNotIn("supplier_id", json.dumps(detail, ensure_ascii=False))

        page = catalog.preview(policy, database="shop_db", table="supplier", query=PreviewQuery())
        self.assertNotIn("supplier_id", page["sql"])
        # 主键看不到：翻页顺序不再保证唯一，如实说
        self.assertFalse(page["stable_order"])
        self.assertIn('ORDER BY "supplier"."supplier_name" ASC, "supplier"."category" ASC', page["sql"])

    def test_half_of_a_composite_key_is_not_a_key(self):
        catalog, _ = _catalog_from_script(
            self.directory,
            "CREATE TABLE line (order_no TEXT, line_no INTEGER, item TEXT, PRIMARY KEY (order_no, line_no));",
            rows=[("INSERT INTO line VALUES (?, ?, ?)", [("A", 1, "x"), ("A", 2, "y"), ("B", 1, "z")])],
        )
        whole = catalog.preview(ALLOW_ALL, database="lab_db", table="line", query=PreviewQuery())
        self.assertTrue(whole["stable_order"])
        self.assertIn('ORDER BY "line"."order_no" ASC, "line"."line_no" ASC LIMIT', whole["sql"])

        policy = DataPolicy(allow_tables=("lab_db.*",), deny_columns=("lab_db.line.line_no",))
        half = catalog.preview(policy, database="lab_db", table="line", query=PreviewQuery())
        self.assertFalse(half["stable_order"])
        self.assertIn('ORDER BY "line"."order_no" ASC, "line"."item" ASC LIMIT', half["sql"])

    def test_glossary_entries_naming_hidden_fields_are_withheld(self):
        catalog, _ = _catalog_from_script(
            self.directory,
            """
            CREATE TABLE supplier (supplier_id TEXT PRIMARY KEY, on_time_rate REAL);
            CREATE TABLE project (project_code TEXT PRIMARY KEY, budget_amt REAL, actual_cost REAL);
            """,
            metric_definitions={
                "超预算": "实际成本超过预算（actual_cost > budget_amt）。",
                "准时率": "按约定交期到货的批次占比，看 supplier.on_time_rate。",
                "练习税额": "8% 是虚构的训练假设，金额单位 USD。",
            },
        )
        everything = catalog.describe(ALLOW_ALL)
        self.assertEqual(len(everything["glossary"]), 3)
        self.assertFalse(everything["access_limited"])

        buyer = catalog.describe(DataPolicy(allow_tables=("lab_db.supplier",)))
        self.assertEqual([item["term"] for item in buyer["glossary"]], ["准时率", "练习税额"])
        self.assertTrue(buyer["access_limited"])
        self.assertNotIn("budget_amt", json.dumps(buyer, ensure_ascii=False))

        no_rate = catalog.describe(
            DataPolicy(allow_tables=("lab_db.supplier",), deny_columns=("lab_db.supplier.on_time_rate",))
        )
        self.assertEqual([item["term"] for item in no_rate["glossary"]], ["练习税额"])

    def test_an_empty_database_is_not_shown_to_callers_without_access(self):
        catalog, _ = _catalog_from_script(self.directory, "CREATE TABLE t (x); DROP TABLE t;")

        allowed = catalog.describe(ALLOW_ALL)
        self.assertEqual([domain["database"] for domain in allowed["domains"]], ["lab_db"])
        self.assertEqual(allowed["domains"][0]["tables"], [])

        denied = catalog.describe(DataPolicy.deny_all())
        self.assertEqual(denied["domains"], [])
        self.assertTrue(denied["access_limited"])

    # ---- 库读不到 ----

    def test_loading_the_schema_does_not_create_a_missing_database_file(self):
        missing = self.directory / "never-existed.db"
        tables, columns, relations = SQLiteSchemaLoader(missing, database_name="x").load()
        self.assertEqual((tables, columns, relations), ({}, [], []))
        # 问答启动时走的就是 load()：它顺手建出空文件的话，目录之后看到的就是「库在，只是空的」
        self.assertFalse(missing.exists())

    def test_a_zero_byte_database_file_is_unavailable_not_empty(self):
        catalog, db_path = _catalog_from_script(
            self.directory, "CREATE TABLE t (x);",
            tables={"t": {"description": "有说明的表", "columns": {"x": {}}}},
        )
        db_path.write_bytes(b"")

        domain = catalog.describe(ALLOW_ALL, refresh=True)["domains"][0]
        self.assertEqual((domain["status"], domain["status_reason"]), ("unavailable", "empty_file"))
        self.assertEqual(domain["tables"], [])

    def test_slow_failures_stay_cached_and_are_not_repeated(self):
        from ai_sql_agent.catalog.service import _Structure

        now = [1000.0]
        catalog, _ = _catalog_from_script(self.directory, "CREATE TABLE t (x);", clock=lambda: now[0])
        loads = []

        def slow_failure(database):
            loads.append(database.name)
            now[0] += 5.0  # 连不上的库要等满连接超时才有结论
            return _Structure(problem="connection_failed")

        catalog._load_structure = slow_failure
        for _ in range(3):
            self.assertEqual(catalog.describe(ALLOW_ALL)["domains"][0]["status"], "unavailable")
        # 结论的有效期从读完算起：读了 5 秒不等于存进去就过期
        self.assertEqual(len(loads), 1)

        now[0] += 16.0
        catalog.describe(ALLOW_ALL)
        self.assertEqual(len(loads), 2)

    def test_concurrent_cold_requests_share_one_load(self):
        import threading
        import time as real_time

        catalog, _ = _catalog_from_script(self.directory, "CREATE TABLE t (x);")
        original = catalog._load_structure
        loads = []

        def slow(database):
            loads.append(database.name)
            real_time.sleep(0.15)
            return original(database)

        catalog._load_structure = slow
        results = []
        threads = [
            threading.Thread(target=lambda: results.append(catalog.describe(ALLOW_ALL)))
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(results), 8)
        self.assertEqual(len(loads), 1)

    def test_refresh_does_not_reload_more_than_every_few_seconds(self):
        now = [50.0]
        catalog, _ = _catalog_from_script(self.directory, "CREATE TABLE t (x);", clock=lambda: now[0])
        original = catalog._load_structure
        loads = []

        def counting(database):
            loads.append(database.name)
            return original(database)

        catalog._load_structure = counting
        catalog.describe(ALLOW_ALL, refresh=True)
        catalog.describe(ALLOW_ALL, refresh=True)
        catalog.describe(ALLOW_ALL, refresh=True)
        self.assertEqual(len(loads), 1)

        now[0] += 4.0
        catalog.describe(ALLOW_ALL, refresh=True)
        self.assertEqual(len(loads), 2)

    # ---- 取回来的东西有上限 ----

    def test_large_cells_never_reach_the_application_whole(self):
        long_a = "a" * (MAX_CELL_CHARS + 500) + " NEEDLE-A"
        long_z = "a" * (MAX_CELL_CHARS + 500) + " NEEDLE-Z"
        catalog, _ = _catalog_from_script(
            self.directory,
            "CREATE TABLE doc (id INTEGER PRIMARY KEY, note TEXT, photo BLOB, misc);",
            rows=[(
                "INSERT INTO doc VALUES (?, ?, zeroblob(?), ?)",
                [(1, long_a, 3_000_000, 7), (2, long_z, 0, "short"), (3, "tiny", 12, b"\x01\x02")],
            )],
            spy=True,
        )

        page = catalog.preview(ALLOW_ALL, database="lab_db", table="doc", query=PreviewQuery())
        by_id = {row[0]: row for row in page["rows"]}

        self.assertEqual(by_id[1][1], "a" * MAX_CELL_CHARS + "…")
        self.assertTrue(page["cells_truncated"])
        self.assertEqual(page["cell_limit"], MAX_CELL_CHARS)
        self.assertEqual(by_id[1][2], "<binary, 3000000 bytes>")
        self.assertEqual(by_id[2][2], "<binary, 0 bytes>")
        self.assertEqual(by_id[3][1], "tiny")
        # 没声明类型的列：数字还是数字，短文本原样，二进制只报大小
        self.assertEqual((by_id[1][3], by_id[2][3], by_id[3][3]), (7, "short", "<binary, 2 bytes>"))
        self.assertEqual(
            [(column["name"], column["kind"], column["sortable"]) for column in page["columns"]],
            [("id", "number", True), ("note", "text", True), ("photo", "binary", False), ("misc", "other", True)],
        )

        # 上限是在数据库里生效的：执行器交回来的东西里就没有那 3MB
        fetched = [value for result in catalog.router.results for row in result.rows for value in row.values()]
        self.assertFalse(any(isinstance(value, (bytes, bytearray)) and len(value) > MAX_CELL_CHARS + 1
                             for value in fetched))
        self.assertTrue(all(len(value) <= MAX_CELL_CHARS + 1 for value in fetched if isinstance(value, str)))

        # 截的只是取回来的那一份：筛选和排序看的仍然是完整的原值
        found = catalog.preview(
            ALLOW_ALL, database="lab_db", table="doc",
            query=PreviewQuery(filters=[PreviewFilter("note", "contains", "NEEDLE-Z")]),
        )
        self.assertEqual([row[0] for row in found["rows"]], [2])
        ranked = catalog.preview(
            ALLOW_ALL, database="lab_db", table="doc",
            query=PreviewQuery(sort_column="note", sort_descending=True),
        )
        self.assertEqual([row[0] for row in ranked["rows"]], [3, 2, 1])

        has_photo = catalog.preview(
            ALLOW_ALL, database="lab_db", table="doc",
            query=PreviewQuery(filters=[PreviewFilter("photo", "not_empty")]),
        )
        self.assertEqual([row[0] for row in has_photo["rows"]], [1, 3])

    # ---- 筛选值按它本来的含义比 ----

    def test_filters_compare_big_integers_and_whole_days_correctly(self):
        catalog, _ = _catalog_from_script(
            self.directory,
            "CREATE TABLE event (big_id INTEGER PRIMARY KEY, happened_at DATETIME, note TEXT);",
            rows=[(
                "INSERT INTO event VALUES (?, ?, ?)",
                [
                    (9007199254740992, "2024-03-30 09:00:00", "a"),
                    (9007199254740993, "2024-03-31 10:00:00", "b"),
                    (5, "2024-04-01 00:00:00", "c"),
                ],
            )],
        )

        def notes(*filters):
            page = catalog.preview(
                ALLOW_ALL, database="lab_db", table="event",
                query=PreviewQuery(filters=[PreviewFilter(*item) for item in filters]),
            )
            return [row[2] for row in page["rows"]]

        # 页面拿到的大整数是字符串（浏览器装不下），原样填回来筛，筛到的必须是同一条
        page = catalog.preview(ALLOW_ALL, database="lab_db", table="event", query=PreviewQuery())
        self.assertIn("9007199254740993", [row[0] for row in page["rows"]])
        self.assertEqual(notes(("big_id", "eq", "9007199254740993")), ["b"])
        self.assertEqual(notes(("big_id", "eq", "9007199254740992")), ["a"])
        self.assertEqual(notes(("big_id", "lt", "1e30")), ["c", "a", "b"])

        # 库里存的带时刻：「3 月 31 日」指的是那一整天
        self.assertEqual(notes(("happened_at", "eq", "2024-03-31")), ["b"])
        self.assertEqual(notes(("happened_at", "lte", "2024-03-31")), ["a", "b"])
        self.assertEqual(notes(("happened_at", "gt", "2024-03-31")), ["c"])
        self.assertEqual(notes(("happened_at", "gte", "2024-03-31")), ["c", "b"])
        self.assertEqual(notes(("happened_at", "ne", "2024-03-31")), ["c", "a"])

        with self.assertRaises(CatalogError) as caught:
            notes(("happened_at", "lte", "2024-02-30"))
        self.assertEqual((caught.exception.code, caught.exception.status), ("invalid_value", 400))

    def test_control_characters_in_a_search_are_a_bad_request(self):
        catalog, _ = _build_shop(self.directory)
        with self.assertRaises(CatalogError) as caught:
            catalog.preview(
                ALLOW_ALL, database="shop_db", table="supplier", query=PreviewQuery(search="\x00")
            )
        self.assertEqual((caught.exception.code, caught.exception.status), ("invalid_value", 400))

    # ---- 汇总 ----

    def test_the_row_count_is_not_recomputed_on_every_page(self):
        catalog, _ = _build_shop(self.directory)
        catalog.router = _SpyRouter(catalog.router)

        def counts():
            return sum("COUNT(*)" in sql for sql in catalog.router.statements)

        def preview(**query):
            return catalog.preview(
                ALLOW_ALL, database="shop_db", table="supplier", query=PreviewQuery(**query),
                caller="buyer", user_id="buyer",
            )

        self.assertEqual(preview(page=1, page_size=2)["total_rows"], 5)
        self.assertEqual(preview(page=2, page_size=2)["total_rows"], 5)
        self.assertEqual(preview(page=1, page_size=2, sort_column="supplier_name")["total_rows"], 5)
        self.assertEqual(counts(), 1)

        # 换了筛选条件就是另一个数；换了人也不能借别人数出来的（行级权限按人生效）
        self.assertEqual(preview(search="quartz")["total_rows"], 1)
        self.assertEqual(counts(), 2)
        catalog.preview(
            ALLOW_ALL, database="shop_db", table="supplier", query=PreviewQuery(page_size=2),
            caller="buyer", user_id="someone-else",
        )
        self.assertEqual(counts(), 3)

    def test_a_count_that_times_out_is_not_retried_on_every_click(self):
        catalog, _ = _build_shop(self.directory)
        catalog.router = _SpyRouter(catalog.router, fail_when=lambda sql: "COUNT(*)" in sql)

        for page in (1, 2, 3):
            result = catalog.preview(
                ALLOW_ALL, database="shop_db", table="supplier",
                query=PreviewQuery(page=page, page_size=2),
            )
            # 数不出总数不影响这一页：行照常给，总数如实留空
            self.assertIsNone(result["total_rows"])
            self.assertEqual(result["date_ranges"], [])
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(sum("COUNT(*)" in sql for sql in catalog.router.statements), 1)

    def test_search_reports_which_columns_it_covered(self):
        catalog, _ = _build_shop(self.directory)
        page = catalog.preview(
            ALLOW_ALL, database="shop_db", table="supplier", query=PreviewQuery(search="quartz")
        )
        self.assertEqual(page["search_columns"], ["supplier_id", "supplier_name", "category"])
        self.assertFalse(page["search_columns_limited"])
        plain = catalog.preview(ALLOW_ALL, database="shop_db", table="supplier", query=PreviewQuery())
        self.assertEqual(plain["search_columns"], [])

    def test_the_depth_limit_is_exact(self):
        catalog, _ = _build_shop(self.directory)
        # 默认最多翻到第 10000 行：第 100 页（9901–10000）还在范围内，第 101 页不在
        edge = catalog.preview(
            ALLOW_ALL, database="shop_db", table="supplier", query=PreviewQuery(page=100, page_size=100)
        )
        self.assertEqual((edge["offset"], edge["rows"]), (9900, []))
        with self.assertRaises(CatalogError) as caught:
            catalog.preview(
                ALLOW_ALL, database="shop_db", table="supplier",
                query=PreviewQuery(page=101, page_size=100),
            )
        self.assertEqual(caught.exception.code, "too_deep")

    def test_a_row_cap_below_the_page_size_does_not_skip_rows(self):
        """
        执行器的行数上限比页大小还低时，一页只给得出「上限」那么多行。页大小不跟着压下来的话，
        下一页的偏移照旧按请求的页大小算，中间那一段哪一页都翻不到——每一页看起来还都正常。
        """
        catalog, db_path = _catalog_from_script(
            self.directory,
            "CREATE TABLE item (id INTEGER PRIMARY KEY, name TEXT);",
            rows=[(
                "INSERT INTO item VALUES (?, ?)",
                [(index, f"item-{index}") for index in range(1, 13)],
            )],
        )
        catalog.router.register_executor("lab_db", SQLiteMCPExecutor("lab_db", db_path, max_rows=5))

        pages = []
        while not pages or pages[-1]["has_more"]:
            self.assertLess(len(pages), 10, "翻页没有收敛")
            pages.append(catalog.preview(
                ALLOW_ALL, database="lab_db", table="item",
                query=PreviewQuery(page=len(pages) + 1, page_size=8),
            ))

        self.assertEqual(
            [row[0] for page in pages for row in page["rows"]], list(range(1, 13))
        )
        # 实际的页大小如实报出来：页面按它算「第几到第几条」
        self.assertEqual([page["page_size"] for page in pages], [5, 5, 5])
        self.assertEqual([page["offset"] for page in pages], [0, 5, 10])
        self.assertEqual({page["total_rows"] for page in pages}, {12})

    def test_a_row_cap_at_or_above_the_page_size_leaves_the_page_size_alone(self):
        catalog, db_path = _catalog_from_script(
            self.directory,
            "CREATE TABLE item (id INTEGER PRIMARY KEY, name TEXT);",
            rows=[(
                "INSERT INTO item VALUES (?, ?)",
                [(index, f"item-{index}") for index in range(1, 13)],
            )],
        )
        for cap in (8, 9):
            with self.subTest(cap=cap):
                catalog.router.register_executor(
                    "lab_db", SQLiteMCPExecutor("lab_db", db_path, max_rows=cap)
                )
                first, second = (
                    catalog.preview(
                        ALLOW_ALL, database="lab_db", table="item",
                        query=PreviewQuery(page=page, page_size=8),
                    )
                    for page in (1, 2)
                )
                self.assertEqual(
                    (first["page_size"], len(first["rows"]), first["has_more"]), (8, 8, True)
                )
                self.assertEqual(
                    (second["offset"], [row[0] for row in second["rows"]], second["has_more"]),
                    (8, [9, 10, 11, 12], False),
                )

    # ---- 真实库里会有的名字 ----

    def test_awkward_names_are_reported_honestly_and_break_nothing_else(self):
        catalog, _ = _catalog_from_script(
            self.directory,
            """
            CREATE TABLE "set" (id INTEGER PRIMARY KEY, v TEXT);
            CREATE TABLE item (id INTEGER PRIMARY KEY, "Last Update" TEXT, name TEXT);
            CREATE TABLE "we""ird" (id INTEGER PRIMARY KEY, label TEXT);
            CREATE TABLE child (id INTEGER PRIMARY KEY, parent INTEGER REFERENCES item);
            INSERT INTO item VALUES (1, '2024-01-01', 'first');
            INSERT INTO "we""ird" VALUES (1, 'quoted');
            """,
        )
        payload = catalog.describe(ALLOW_ALL)
        domain = payload["domains"][0]
        # 一张名字里带双引号的表不该让整个库都读不出来
        self.assertEqual(domain["status"], "available")
        self.assertEqual(
            sorted(table["name"] for table in domain["tables"]), ["child", "item", "set", 'we"ird']
        )

        # 名字撞上 SQL 关键字（`set`、`Last Update`）：只读校验按语法树判，
        # 引号里的标识符照常放行，目录里是「可预览」，点进去真能取到数据
        self.assertTrue(self._table(payload, "set")["previewable"])
        empty = catalog.preview(ALLOW_ALL, database="lab_db", table="set", query=PreviewQuery())
        self.assertEqual(empty["rows"], [])

        item = self._table(payload, "item")
        self.assertTrue(item["previewable"])
        flags = {column["name"]: column["previewable"] for column in item["columns"]}
        self.assertEqual(flags, {"id": True, "Last Update": True, "name": True})
        page = catalog.preview(ALLOW_ALL, database="lab_db", table="item", query=PreviewQuery())
        self.assertEqual([column["name"] for column in page["columns"]], ["id", "Last Update", "name"])
        self.assertEqual(page["rows"], [[1, "2024-01-01", "first"]])
        self.assertEqual(page["columns_unsupported"], [])
        self.assertFalse(page["columns_hidden"])

        weird = catalog.preview(ALLOW_ALL, database="lab_db", table='we"ird', query=PreviewQuery())
        self.assertEqual(weird["rows"], [[1, "quoted"]])

        # `REFERENCES item` 不写列名 = 指向主键；不能显示成 item.None
        parent = next(c for c in self._table(payload, "child")["columns"] if c["name"] == "parent")
        self.assertEqual(
            [(ref["table"], ref["column"]) for ref in parent["references"]], [("item", "id")]
        )


class DisplayMetadataTest(unittest.TestCase):
    """给人看的名字与说明：可选的附加键，写在语义层里，不进 Prompt。"""

    PAYLOAD = {
        "name": "erp_db",
        "label_en": "Orders & Purchasing",
        "label_zh": "订单与采购",
        "description": "订单与采购域",
        "description_en": "Orders and purchasing.",
        "connection": {"dialect": "sqlite", "db_path": "x.db"},
        "tables": {
            "supplier": {
                "description": "供应商表",
                "description_en": "Suppliers we buy from.",
                "columns": {
                    "on_time_rate": {"description": "准时率", "description_en": "On-time share.", "label": "On-time rate"},
                },
            }
        },
    }

    def test_database_level_keys_survive_a_round_trip(self):
        database = DatabaseSemantics.from_dict(self.PAYLOAD)
        self.assertEqual(
            database.display,
            {"label_en": "Orders & Purchasing", "label_zh": "订单与采购", "description_en": "Orders and purchasing."},
        )
        again = DatabaseSemantics.from_dict(database.to_dict())
        self.assertEqual(again.display, database.display)
        self.assertEqual(again.tables, database.tables)

    def test_databases_without_display_keys_export_unchanged(self):
        plain = {key: value for key, value in self.PAYLOAD.items() if not key.startswith(("label", "description_"))}
        exported = DatabaseSemantics.from_dict(plain).to_dict()
        self.assertEqual(sorted(exported), ["aliases", "connection", "description", "name", "tables"])

    def test_catalog_prefers_what_the_semantic_layer_wrote(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "erp.db"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE supplier (supplier_id TEXT PRIMARY KEY, on_time_rate REAL)")
            conn.commit()
            conn.close()

            payload = {**self.PAYLOAD, "connection": {"dialect": "sqlite", "db_path": str(db_path)}}
            semantic = SemanticLayer(databases={"erp_db": DatabaseSemantics.from_dict(payload)}, source="test")
            databases = [DatabaseConfig(name="erp_db", db_path=db_path)]
            router = MCPRouter()
            router.register_executor("erp_db", SQLiteMCPExecutor("erp_db", db_path))
            domain = DataCatalog(semantic=semantic, databases=databases, router=router).describe(ALLOW_ALL)["domains"][0]

        self.assertEqual(domain["title"], {"en": "Orders & Purchasing", "zh": "订单与采购"})
        self.assertEqual(domain["title_source"], "semantic")
        self.assertEqual(domain["description_en"], "Orders and purchasing.")
        table = domain["tables"][0]
        self.assertEqual(table["description_en"], "Suppliers we buy from.")
        column = next(item for item in table["columns"] if item["name"] == "on_time_rate")
        self.assertEqual((column["label"], column["description_en"]), ("On-time rate", "On-time share."))

    # ---- 英文展示：字段用途、跨库关联备注、业务术语 ----

    SHARED = {
        "cross_database_links": [
            {
                "left": "erp_db.supplier.supplier_id",
                "right": "wms_db.batch.supplier_id",
                "meaning": "同一个供应商",
                "meaning_en": "Same supplier.",
            },
            {
                "left": "erp_db.supplier.supplier_id",
                "right": "wms_db.batch.batch_no",
                "meaning": "只有中文备注的关联",
            },
        ],
        "metric_definitions": {
            "准时率": "按约定交期到货的批次占比。",
            "良率": "合格数 / 投产数。",
            "到货批次": "仓库收到的每一批货。",
        },
        "metric_definitions_en": {
            "准时率": {
                "term": "On-time rate",
                "definition": "Share of deliveries that arrived by the promised date (on_time_rate).",
            },
            "良率": "Good units divided by units started.",
            "空的": {"term": "   "},
        },
    }

    def _two_databases(self, tmpdir):
        directory = Path(tmpdir)
        erp_path, wms_path = directory / "erp.db", directory / "wms.db"
        for path, ddl in (
            (erp_path, "CREATE TABLE supplier (supplier_id TEXT PRIMARY KEY, on_time_rate REAL)"),
            (wms_path, "CREATE TABLE batch (batch_no TEXT PRIMARY KEY, supplier_id TEXT)"),
        ):
            conn = sqlite3.connect(path)
            conn.execute(ddl)
            conn.commit()
            conn.close()

        erp = {**self.PAYLOAD, "connection": {"dialect": "sqlite", "db_path": str(erp_path)}}
        erp["tables"] = {
            "supplier": {
                "description": "供应商表",
                "columns": {
                    "supplier_id": {"description": "供应商编号"},
                    "on_time_rate": {
                        "description": "准时率",
                        "business_usage": "筛选交付不稳定的供应商",
                        "business_usage_en": "Find suppliers that deliver late.",
                    },
                },
            }
        }
        wms = {
            "name": "wms_db",
            "description": "仓储",
            "connection": {"dialect": "sqlite", "db_path": str(wms_path)},
            "tables": {
                "batch": {
                    "description": "到货批次",
                    "columns": {
                        "batch_no": {"description": "批次号"},
                        "supplier_id": {"description": "供应商编号", "business_usage": "关联供应商"},
                    },
                }
            },
        }
        semantic = SemanticLayer(
            databases={
                "erp_db": DatabaseSemantics.from_dict(erp),
                "wms_db": DatabaseSemantics.from_dict(wms),
            },
            source="test",
        )
        semantic.merge_shared(self.SHARED)
        databases = [
            DatabaseConfig(name="erp_db", db_path=erp_path),
            DatabaseConfig(name="wms_db", db_path=wms_path),
        ]
        router = MCPRouter()
        for database in databases:
            router.register_executor(database.name, SQLiteMCPExecutor(database.name, database.db_path))
        return DataCatalog(semantic=semantic, databases=databases, router=router)

    @staticmethod
    def _column_of(payload, database, table, column):
        domain = next(item for item in payload["domains"] if item["database"] == database)
        entry = next(item for item in domain["tables"] if item["name"] == table)
        return next(item for item in entry["columns"] if item["name"] == column)

    def test_english_metric_definitions_round_trip(self):
        layer = SemanticLayer(source="test")
        layer.merge_shared(self.SHARED)

        self.assertEqual(
            layer.metric_definitions_en,
            {
                "准时率": {
                    "term": "On-time rate",
                    "definition": "Share of deliveries that arrived by the promised date (on_time_rate).",
                },
                # 只写一句话 = 只给定义，名字沿用原文
                "良率": {"definition": "Good units divided by units started."},
            },
        )
        # 口径只有一份：英文版不进模型读的那张表
        self.assertEqual(set(layer.metric_definitions), {"准时率", "良率", "到货批次"})

        again = SemanticLayer(source="test")
        again.merge_shared(layer.shared_to_dict())
        self.assertEqual(again.metric_definitions_en, layer.metric_definitions_en)
        self.assertEqual(again.metric_definitions, layer.metric_definitions)
        self.assertEqual(again.cross_database_links, layer.cross_database_links)

    def test_shared_file_without_english_definitions_exports_unchanged(self):
        layer = SemanticLayer(source="test")
        layer.merge_shared({"metric_definitions": {"准时率": "按约定交期到货的批次占比。"}})
        self.assertEqual(
            sorted(layer.shared_to_dict()),
            ["cross_database_links", "metric_definitions", "metric_disambiguation"],
        )

    def test_malformed_english_definitions_are_rejected(self):
        for bad in (["准时率"], {"准时率": 3}, {"准时率": ["On-time rate"]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                SemanticLayer(source="test").merge_shared({"metric_definitions_en": bad})

    def test_catalog_carries_english_usage_notes_and_terms(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            payload = self._two_databases(tmpdir).describe(ALLOW_ALL)

        rate = self._column_of(payload, "erp_db", "supplier", "on_time_rate")
        self.assertEqual(rate["usage"], "筛选交付不稳定的供应商")
        self.assertEqual(rate["usage_en"], "Find suppliers that deliver late.")
        # 接口里只有一个叫法（usage_*），语义层的键名不原样漏出来
        self.assertNotIn("business_usage_en", rate)

        # 没写英文版的：不出现这个键，页面照原文显示并标明是原文
        plain = self._column_of(payload, "wms_db", "batch", "supplier_id")
        self.assertEqual(plain["usage"], "关联供应商")
        self.assertNotIn("usage_en", plain)

        references = {
            (ref["database"], ref["column"]): ref
            for ref in self._column_of(payload, "erp_db", "supplier", "supplier_id")["references"]
        }
        self.assertEqual(references[("wms_db", "supplier_id")]["note"], "同一个供应商")
        self.assertEqual(references[("wms_db", "supplier_id")]["note_en"], "Same supplier.")
        self.assertNotIn("note_en", references[("wms_db", "batch_no")])
        # 关联是双向登记的：从另一头看到的是同一句备注
        back = self._column_of(payload, "wms_db", "batch", "supplier_id")["references"]
        self.assertEqual([ref.get("note_en") for ref in back], ["Same supplier."])

        glossary = {entry["term"]: entry for entry in payload["glossary"]}
        self.assertEqual(glossary["准时率"]["term_en"], "On-time rate")
        self.assertIn("promised date", glossary["准时率"]["definition_en"])
        self.assertEqual(glossary["良率"]["definition_en"], "Good units divided by units started.")
        self.assertNotIn("term_en", glossary["良率"])
        self.assertEqual(sorted(glossary["到货批次"]), ["definition", "term"])
        self.assertNotIn("空的", glossary)

    def test_english_text_goes_through_the_same_permission_check(self):
        """原文没提字段名、英文版提了：看不到那个字段的人，整条术语都不给。"""
        policy = DataPolicy(
            allow_tables=["erp_db.*", "wms_db.*"], deny_columns=["erp_db.supplier.on_time_rate"]
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            payload = self._two_databases(tmpdir).describe(policy)

        self.assertEqual({entry["term"] for entry in payload["glossary"]}, {"到货批次", "良率"})
        self.assertTrue(payload["access_limited"])
        self.assertNotIn("on_time_rate", json.dumps(payload, ensure_ascii=False))

    def test_display_keys_never_reach_the_model(self):
        """同一份元数据加不加展示用的键，模型拿到的 Schema 上下文与跨库说明一字不差。"""
        import copy

        from ai_sql_agent.pipeline import PipelineConfig, Text2SQLPipeline
        from ai_sql_agent.semantic import demo_semantic_layer

        marker = "DISPLAY-ONLY-TEXT"
        plain = demo_semantic_layer()
        decorated = copy.deepcopy(plain)
        for database in decorated.databases.values():
            database.display = {"label_en": marker, "label_zh": marker, "description_en": marker}
            for meta in database.tables.values():
                meta.update(label=marker, label_en=marker, description_en=marker, description_zh=marker)
                for column_meta in (meta.get("columns") or {}).values():
                    column_meta.update(
                        label=marker, label_en=marker, label_zh=marker,
                        description_en=marker, description_zh=marker,
                        business_usage_en=marker, business_usage_zh=marker,
                    )
        for link in decorated.cross_database_links:
            link["meaning_en"] = marker
        decorated.metric_definitions_en = {
            term: {"term": marker, "definition": marker} for term in decorated.metric_definitions
        }
        self.assertTrue(decorated.cross_database_links and decorated.metric_definitions)

        # 对照组：动一个模型**会**读的键，上下文必须跟着变——证明下面的比较看得出差别
        control = copy.deepcopy(plain)
        for database in control.databases.values():
            for meta in database.tables.values():
                for column_meta in (meta.get("columns") or {}).values():
                    column_meta["description"] = f"{column_meta.get('description') or ''}{marker}"

        # 关键词由调用方给（线上是规划那一步抽的）；这里写死，免得测试去调模型
        questions = [
            ("查询准时率低于0.9的供应商名称", ["准时率", "供应商", "供应商名称"]),
            ("Which suppliers have an on-time rate below 0.9?", ["supplier", "on-time rate"]),
            ("哪些超预算项目的采购单还有未付余额", ["超预算", "项目", "采购单", "未付余额", "付款"]),
            ("Show the current process for sales orders that are in production",
             ["sales order", "current process", "work order", "in production", "生产工单", "销售订单"]),
        ]
        cwd = os.getcwd()
        saved = {"AI_SQL_SEMANTIC_DIR": os.environ.get("AI_SQL_SEMANTIC_DIR")}
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                os.chdir(tmpdir)
                os.environ["AI_SQL_SEMANTIC_DIR"] = ""
                reload_settings()
                seen = []
                for layer in (plain, decorated, control):
                    service = Text2SQLPipeline(PipelineConfig(semantic_layer=layer)).schema_retrieval_service
                    results = [
                        service.retrieve(query=question, keywords=keywords)
                        for question, keywords in questions
                    ]
                    seen.append([
                        (result.to_prompt_context(), result.to_prompt_context(compact=True), result.cross_database_notes)
                        for result in results
                    ])
            finally:
                os.chdir(cwd)
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reload_settings()

        self.assertEqual(seen[0], seen[1])
        self.assertTrue(all(context for context, _compact, _notes in seen[0]))
        self.assertTrue(any(notes for _context, _compact, notes in seen[0]), "至少有一题走到了跨库说明")
        self.assertNotIn(marker, json.dumps(seen[1], ensure_ascii=False))
        self.assertIn(marker, json.dumps(seen[2], ensure_ascii=False))


class SchemaStructureTest(unittest.TestCase):
    def test_missing_file_is_an_error_and_is_not_created(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "absent.db"
            with self.assertRaises(FileNotFoundError):
                SQLiteSchemaLoader(path, database_name="x").load_structure()
            self.assertFalse(path.exists())

    def test_structure_has_types_keys_and_foreign_keys(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _catalog, db_path = _build_shop(Path(tmpdir))
            structure = SQLiteSchemaLoader(db_path, database_name="shop_db").load_structure()

        self.assertEqual(sorted(structure), ["purchase_order", "scratch", "supplier"])
        supplier = {column.column_name: column for column in structure["supplier"]}
        self.assertTrue(supplier["supplier_id"].is_primary_key)
        self.assertFalse(supplier["supplier_name"].nullable)
        self.assertEqual(supplier["on_time_rate"].data_type, "REAL")
        po = {column.column_name: column for column in structure["purchase_order"]}
        self.assertEqual(po["supplier_id"].foreign_key_ref, "supplier.supplier_id")


class AuditExecutionStatusTest(unittest.TestCase):
    def test_execution_status_round_trips(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteAuditStore(Path(tmpdir) / "audit.db")
            store.record(AuditRecord(
                trace_id="t-1", caller="c", user_id="u", session_id="s", query="q",
                execution_status="no_data",
            ))
            self.assertEqual(store.get_by_trace("t-1")["execution_status"], "no_data")

    def test_old_database_gains_the_column(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "audit.db"
            conn = sqlite3.connect(path)
            # 这次改动之前的表结构：没有 execution_status
            conn.executescript(
                """
                CREATE TABLE ai_sql_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    caller TEXT NOT NULL, user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '', query TEXT NOT NULL,
                    route TEXT NOT NULL DEFAULT '', databases TEXT NOT NULL DEFAULT '[]',
                    tables TEXT NOT NULL DEFAULT '[]', sqls TEXT NOT NULL DEFAULT '[]',
                    denied_tables TEXT NOT NULL DEFAULT '[]',
                    success INTEGER NOT NULL DEFAULT 1, status_code INTEGER NOT NULL DEFAULT 200,
                    failed_steps INTEGER NOT NULL DEFAULT 0, repaired_steps INTEGER NOT NULL DEFAULT 0,
                    duration_ms REAL NOT NULL DEFAULT 0, prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    degraded INTEGER NOT NULL DEFAULT 0, has_risk INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );
                INSERT INTO ai_sql_audit_log (trace_id, created_at, caller, user_id, query)
                VALUES ('old-1', '2026-09-01T00:00:00+00:00', 'c', 'u', '老记录');
                """
            )
            conn.commit()
            conn.close()

            store = SQLiteAuditStore(path)
            # 老记录读出来是空串——「不知道」，不是 success
            self.assertEqual(store.get_by_trace("old-1")["execution_status"], "")
            store.record(AuditRecord(
                trace_id="new-1", caller="c", user_id="u", session_id="s", query="q",
                execution_status="success",
            ))
            self.assertEqual(store.get_by_trace("new-1")["execution_status"], "success")


class CatalogApiTest(unittest.TestCase):
    """HTTP 这一层：身份、限流、开关，以及请求体里塞不进 SQL。"""

    ENV_KEYS = (
        "AI_SQL_AUTH_MODE",
        "AI_SQL_API_KEYS_FILE",
        "AI_SQL_AUDIT_DB_PATH",
        "AI_SQL_MEMORY_DB_PATH",
        "AI_SQL_SEMANTIC_DIR",
        "AI_SQL_CATALOG_RATE_LIMIT_PER_MINUTE",
        "AI_SQL_CATALOG_ENABLED",
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
                    {"key": "portal-key", "caller": "portal", "roles": ["everything"],
                     "can_impersonate": True},
                    # 限流用例专用：令牌桶按调用方记账，和别的用例共用一把 Key 会互相饿死
                    {"key": "burst-key", "caller": "burst", "user_id": "burst",
                     "roles": ["everything"]},
                    # 运维特意压到每分钟 2 次的 Key
                    {"key": "slow-key", "caller": "slow", "user_id": "slow",
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
                "users": {"alice": {"roles": ["purchasing"]}},
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
        })
        reload_settings()
        cls.app = app
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

    def test_catalog_requires_a_key(self):
        self.assertEqual(self.client.get("/catalog").status_code, 401)
        self.assertEqual(
            self.client.post("/catalog/erp_db/supplier/rows", json={}).status_code, 401
        )

    def test_full_access_sees_the_four_demo_domains(self):
        response = self.client.get("/catalog", headers=self._key("admin-key"))
        self.assertEqual(response.status_code, 200)
        body = response.json()

        self.assertEqual(
            {domain["database"]: domain["kind"] for domain in body["domains"]},
            {"erp_db": "erp", "pm_db": "pm", "mes_db": "mes", "wms_db": "wms"},
        )
        self.assertTrue(all(domain["status"] == "available" for domain in body["domains"]))
        self.assertTrue(all(domain["tables"] for domain in body["domains"]))
        self.assertFalse(body["access_limited"])
        self.assertEqual(body["identity"]["user_id"], "ops")
        self.assertTrue(body["identity"]["is_admin"])
        self.assertEqual(body["service"]["auth_mode"], "api_key")
        self.assertEqual(body["source"], "demo")

    def test_restricted_role_sees_only_its_tables_and_columns(self):
        body = self.client.get("/catalog", headers=self._key("buyer-key")).json()

        self.assertEqual([domain["database"] for domain in body["domains"]], ["erp_db"])
        tables = {table["name"]: table for table in body["domains"][0]["tables"]}
        self.assertNotIn("payment", tables)
        self.assertNotIn(
            "on_time_rate", [column["name"] for column in tables["supplier"]["columns"]]
        )
        self.assertTrue(body["access_limited"])
        self.assertFalse(body["identity"]["is_admin"])

    def test_preview_follows_the_same_policy(self):
        allowed = self.client.post(
            "/catalog/erp_db/supplier/rows", headers=self._key("buyer-key"), json={"page_size": 2}
        )
        self.assertEqual(allowed.status_code, 200)
        body = allowed.json()
        self.assertNotIn("on_time_rate", [column["name"] for column in body["columns"]])
        self.assertEqual(len(body["rows"]), 2)
        self.assertTrue(body["columns_hidden"])

        for path in ("/catalog/erp_db/payment/rows", "/catalog/pm_db/project/rows"):
            denied = self.client.post(path, headers=self._key("buyer-key"), json={})
            self.assertEqual(denied.status_code, 404, path)
            self.assertEqual(denied.json()["detail"]["code"], "not_found")

        hidden_filter = self.client.post(
            "/catalog/erp_db/supplier/rows",
            headers=self._key("buyer-key"),
            json={"filters": [{"column": "on_time_rate", "op": "lt", "value": "0.9"}]},
        )
        self.assertEqual(hidden_filter.status_code, 400)
        self.assertEqual(hidden_filter.json()["detail"]["code"], "unknown_column")

    def test_impersonated_user_gets_the_intersection(self):
        # portal 这把 Key 全放开，alice 只有采购角色：目录按交集给
        body = self.client.get(
            "/catalog", headers=self._key("portal-key"), params={"user_id": "alice"}
        ).json()
        self.assertEqual(body["identity"]["user_id"], "alice")
        self.assertEqual([domain["database"] for domain in body["domains"]], ["erp_db"])

        denied = self.client.post(
            "/catalog/pm_db/project/rows",
            headers=self._key("portal-key"),
            json={"user_id": "alice"},
        )
        self.assertEqual(denied.status_code, 404)

    def test_personal_key_cannot_browse_as_someone_else(self):
        response = self.client.get(
            "/catalog", headers=self._key("buyer-key"), params={"user_id": "ops"}
        )
        self.assertEqual(response.status_code, 403)

    def test_request_body_cannot_carry_sql(self):
        for payload in (
            {"sql": "SELECT * FROM payment"},
            {"filters": [{"column": "supplier_name", "op": "eq", "value": "x", "raw": "1=1"}]},
            {"sort": {"column": "supplier_name", "direction": "asc; DROP TABLE supplier"}},
        ):
            response = self.client.post(
                "/catalog/erp_db/supplier/rows", headers=self._key("admin-key"), json=payload
            )
            self.assertEqual(response.status_code, 422, payload)

    def test_table_detail_endpoint(self):
        response = self.client.get("/catalog/erp_db/supplier", headers=self._key("admin-key"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("supplier_name", [column["name"] for column in response.json()["columns"]])

        self.assertEqual(
            self.client.get("/catalog/erp_db/payment", headers=self._key("buyer-key")).status_code,
            404,
        )

    def test_browsing_is_rate_limited_separately_from_queries(self):
        os.environ["AI_SQL_CATALOG_RATE_LIMIT_PER_MINUTE"] = "2"
        reload_settings()
        try:
            codes = [
                self.client.get("/catalog", headers=self._key("burst-key")).status_code
                for _ in range(4)
            ]
        finally:
            os.environ["AI_SQL_CATALOG_RATE_LIMIT_PER_MINUTE"] = "1000"
            reload_settings()
        self.assertEqual(codes[:2], [200, 200])
        self.assertIn(429, codes[2:])
        # 浏览被限流不占问答的桶：同一把 Key 的历史接口照常可用
        self.assertEqual(
            self.client.get("/history", headers=self._key("burst-key")).status_code, 200
        )

    def test_catalog_can_be_switched_off(self):
        os.environ["AI_SQL_CATALOG_ENABLED"] = "false"
        reload_settings()
        try:
            response = self.client.get("/catalog", headers=self._key("admin-key"))
        finally:
            os.environ["AI_SQL_CATALOG_ENABLED"] = "true"
            reload_settings()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "disabled")

    def test_a_throttled_key_is_throttled_for_browsing_too(self):
        # 浏览的上限是 1000/分钟，但这把 Key 自己只有 2/分钟：取更严的那个
        codes = [
            self.client.post(
                "/catalog/erp_db/supplier/rows", headers=self._key("slow-key"), json={}
            ).status_code
            for _ in range(5)
        ]
        self.assertEqual(codes[:2], [200, 200])
        self.assertIn(429, codes[2:])

    def test_whoami_resolves_the_identity_queries_will_use(self):
        self.assertEqual(self.client.get("/whoami").status_code, 401)

        body = self.client.get("/whoami", headers=self._key("buyer-key")).json()
        self.assertEqual(
            body["identity"],
            {"user_id": "buyer", "caller": "buyer", "is_admin": False,
             "can_impersonate": False, "anonymous": False},
        )
        self.assertEqual(body["service"]["auth_mode"], "api_key")
        self.assertIn("streaming", body["service"])
        self.assertEqual(body["service"]["preview"]["max_cell_chars"], MAX_CELL_CHARS)
        # 与 /catalog 里带的是同一份
        catalog = self.client.get("/catalog", headers=self._key("buyer-key")).json()
        self.assertEqual(catalog["identity"], body["identity"])
        self.assertEqual(catalog["service"], body["service"])

        # 身份解析与 /query 是同一段代码：个人 Key 不能冒充别人，门户 Key 可以指定最终用户
        self.assertEqual(
            self.client.get("/whoami", headers=self._key("buyer-key"), params={"user_id": "ops"}).status_code,
            403,
        )
        alice = self.client.get(
            "/whoami", headers=self._key("portal-key"), params={"user_id": "alice"}
        ).json()
        self.assertEqual(alice["identity"]["user_id"], "alice")

    def test_whoami_survives_browsing_being_off_or_throttled(self):
        """
        页面发问之前必须知道自己的 user_id（绑定了用户的 Key 只认它自己的那个）。
        这件事不能挂在数据浏览上：浏览关掉、或者被限流的时候，提问照样要能用。
        """
        os.environ["AI_SQL_CATALOG_ENABLED"] = "false"
        reload_settings()
        try:
            off = self.client.get("/whoami", headers=self._key("buyer-key"))
            self.assertEqual(self.client.get("/catalog", headers=self._key("buyer-key")).status_code, 404)
        finally:
            os.environ["AI_SQL_CATALOG_ENABLED"] = "true"
            reload_settings()
        self.assertEqual(off.status_code, 200)
        self.assertEqual(off.json()["identity"]["user_id"], "buyer")
        self.assertFalse(off.json()["service"]["catalog_enabled"])

        os.environ["AI_SQL_CATALOG_RATE_LIMIT_PER_MINUTE"] = "1"
        reload_settings()
        try:
            codes = [
                self.client.get("/catalog", headers=self._key("portal-key")).status_code
                for _ in range(3)
            ]
            throttled = self.client.get("/whoami", headers=self._key("portal-key"))
        finally:
            os.environ["AI_SQL_CATALOG_RATE_LIMIT_PER_MINUTE"] = "1000"
            reload_settings()
        self.assertIn(429, codes)
        self.assertEqual(throttled.status_code, 200)

    def test_bad_filter_values_are_400_not_500(self):
        for payload in (
            {"search": "a\u0000b"},
            {"filters": [{"column": "po_date", "op": "lte", "value": "2024-02-30"}]},
            {"filters": [{"column": "po_amt", "op": "gt", "value": "9" * 40}]},
        ):
            response = self.client.post(
                "/catalog/erp_db/purchase_order/rows", headers=self._key("admin-key"), json=payload
            )
            self.assertEqual(response.status_code, 400, (payload, response.text))
            self.assertEqual(response.json()["detail"]["code"], "invalid_value")

    def test_history_reports_business_outcome(self):
        self.app.state.audit.record(AuditRecord(
            trace_id="hist-no-data", caller="ops", user_id="ops", session_id="s-9",
            query="查询订单SO-NOT-FOUND的订单状态", route="database_query",
            databases=["erp_db"], sqls=[{"database": "erp_db", "sql": "SELECT 1"}],
            has_risk=True, execution_status="no_data",
        ))
        items = self.client.get("/history", headers=self._key("admin-key")).json()["items"]
        item = next(entry for entry in items if entry["trace_id"] == "hist-no-data")
        self.assertEqual(item["execution_status"], "no_data")
        self.assertTrue(item["has_risk"])
        self.assertEqual(item["session_id"], "s-9")
        self.assertEqual(item["status_code"], 200)

    def test_metrics_use_route_templates(self):
        self.client.post(
            "/catalog/erp_db/supplier/rows", headers=self._key("admin-key"), json={}
        )
        metrics = self.client.get("/metrics").text
        self.assertIn('path="/catalog/{database}/{table}/rows"', metrics)
        self.assertNotIn('path="/catalog/erp_db/supplier/rows"', metrics)

    def test_unauthenticated_paths_cannot_mint_metric_labels(self):
        # 静态文件和不存在的路径都不走鉴权：按原始路径打标签的话，谁都能把指标撑爆
        self.assertEqual(self.client.get("/static/js/main.js").status_code, 200)
        self.assertEqual(self.client.get("/static/no-such-file-8231.js").status_code, 404)
        self.assertEqual(self.client.get("/wp-admin/setup-7719.php").status_code, 404)

        metrics = self.client.get("/metrics").text
        self.assertIn('path="/static"', metrics)
        self.assertIn('path="unmatched"', metrics)
        self.assertNotIn("no-such-file-8231", metrics)
        self.assertNotIn("setup-7719", metrics)
        self.assertNotIn('path="/static/js/main.js"', metrics)


class _FakeCursor:
    def __init__(self, log, rows):
        self.log = log
        self.rows = rows
        self.itersize = None
        self.description = [type("Column", (), {"name": name})() for name in ("id", "name")]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, *args):
        self.log.append(("cursor", args))

    def fetchmany(self, size):
        self.log.append(("fetchmany", size))
        return self.rows[:size]


class _FakeConnection:
    """只记录调用。PostgreSQL 这条路在本机没有库可连，这里验证的是「怎么调驱动」。"""

    closed = False

    def __init__(self, rows=()):
        self.log = []
        self.rows = list(rows)

    def execute(self, *args):
        self.log.append(("conn", args))
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def cursor(self, name=None):
        return _FakeCursor(self.log, self.rows)

    def rollback(self):
        self.log.append(("rollback", ()))

    def close(self):
        self.closed = True


class PostgresDriverCallsTest(unittest.TestCase):
    """PostgreSQL 分支：参数怎么交给驱动、上限怎么收紧、审计多的那一列有没有对上。"""

    def _executor(self, connection):
        from ai_sql_agent.execution import PostgresMCPExecutor

        executor = PostgresMCPExecutor(
            "pg_db", "postgresql://unused", max_rows=50, max_execution_seconds=30,
            pool_max_connections=0,
        )
        executor._pool._factory = lambda: connection
        return executor

    def _execute(self, executor, **request):
        from ai_sql_agent.auth import authorization_scope

        with authorization_scope(ALLOW_ALL, user_id="alice"):
            return executor.execute(MCPExecutionRequest(database="pg_db", **request))

    def test_parameters_reach_the_driver_as_parameters(self):
        connection = _FakeConnection(rows=[(1, "a"), (2, "b"), (3, "c")])
        result = self._execute(
            self._executor(connection),
            sql='SELECT "id", "name" FROM "item" WHERE "name" ILIKE %s LIMIT %s OFFSET %s',
            params=("%a%", 3, 0),
            max_rows=2,
            timeout_seconds=5,
        )

        self.assertTrue(result.success, result.error)
        cursor_calls = [args for kind, args in connection.log if kind == "cursor"]
        self.assertEqual(len(cursor_calls), 1)
        sql, params = cursor_calls[0]
        self.assertEqual(params, ("%a%", 3, 0))
        self.assertNotIn("%a%", sql)

        # 行数与时长上限按请求收紧；多取一行用来判断是否还有
        self.assertIn(("fetchmany", 3), connection.log)
        self.assertEqual((result.row_count, result.truncated, result.row_limit), (2, True, 2))
        timeout = next(args[0] for kind, args in connection.log if kind == "conn" and "statement_timeout" in args[0])
        self.assertIn("5000", timeout)
        # 最终用户身份照旧放进事务，行级安全对预览同样生效
        self.assertTrue(any(kind == "conn" and "app.user_id" in args[0] and args[1] == ("alice",)
                            for kind, args in connection.log))

    def test_model_generated_sql_is_still_sent_without_parameters(self):
        # 不带参数时不能传第二个实参：否则 `LIKE '%x%'` 里的百分号会被驱动当成占位符
        connection = _FakeConnection(rows=[(1, "a")])
        result = self._execute(
            self._executor(connection), sql="SELECT id, name FROM item WHERE name LIKE '%a%'"
        )
        self.assertTrue(result.success, result.error)
        cursor_calls = [args for kind, args in connection.log if kind == "cursor"]
        self.assertEqual(cursor_calls, [("SELECT id, name FROM item WHERE name LIKE '%a%'",)])
        self.assertEqual(result.row_limit, 50)

    def test_generated_postgres_preview_passes_the_authorization_parser(self):
        from ai_sql_agent.auth import check_sql_access

        policy = DataPolicy(
            allow_tables=("pg_db.supplier",), deny_columns=("pg_db.supplier.unit_cost",)
        )
        query = PreviewQuery(
            search="quartz",
            filters=[PreviewFilter("on_time_rate", "lt", "0.9"), PreviewFilter("joined_on", "not_empty")],
            sort_column="on_time_rate",
        )
        page = build_page_statement("supplier", COLUMNS, query, "postgres", limit=26, offset=0)
        summary, _dates = build_summary_statement("supplier", COLUMNS, query, "postgres")
        for statement in (page, summary):
            decision = check_sql_access(policy, database="pg_db", sql=statement.sql, dialect="postgres")
            self.assertTrue(decision.allowed, decision.reason)

        # 同一条语句，换一个连这张表都不给的策略：关卡要能认出表名并拒掉
        denied = check_sql_access(
            DataPolicy(allow_tables=("pg_db.other",)), database="pg_db", sql=page.sql, dialect="postgres"
        )
        self.assertEqual(denied.denied_tables, ("supplier",))

    def test_postgres_structure_loader_shapes_catalog_rows(self):
        from unittest import mock

        from ai_sql_agent.retrieval.postgres_loader import PostgresSchemaLoader

        answers = {
            "information_schema.tables": [("supplier",), ("ai_sql_vectors_schema",)],
            "contype = 'p'": [("supplier", "supplier_id")],
            "contype = 'f'": [],
            "information_schema.columns": [
                ("supplier", "supplier_id", "text", "NO"),
                ("supplier", "on_time_rate", "numeric", "YES"),
                ("ai_sql_vectors_schema", "id", "bigint", "NO"),
                ("some_view", "x", "text", "YES"),
            ],
        }

        class Connection:
            def execute(self, sql, params=()):
                rows = next(value for key, value in answers.items() if key in sql)
                return type("Result", (), {"fetchall": lambda self: rows})()

            def close(self):
                pass

        with mock.patch("psycopg.connect", return_value=Connection()):
            structure = PostgresSchemaLoader(dsn="postgresql://unused", database_name="pg_db").load_structure()

        # 基础设施表与视图不进目录
        self.assertEqual(list(structure), ["supplier"])
        columns = {column.column_name: column for column in structure["supplier"]}
        self.assertTrue(columns["supplier_id"].is_primary_key)
        self.assertFalse(columns["supplier_id"].nullable)
        self.assertEqual(columns["on_time_rate"].data_type, "numeric")

    def test_postgres_audit_insert_matches_its_placeholders(self):
        from unittest import mock

        from ai_sql_agent.audit import PostgresAuditStore

        connection = _FakeConnection()
        with mock.patch("psycopg.connect", return_value=connection):
            store = PostgresAuditStore("postgresql://unused")
            store.record(AuditRecord(
                trace_id="t", caller="c", user_id="u", session_id="s", query="q",
                execution_status="partial_success",
            ))

        statements = [args for kind, args in connection.log if kind == "conn"]
        self.assertTrue(any("ADD COLUMN IF NOT EXISTS execution_status" in args[0] for args in statements))
        sql, params = statements[-1]
        self.assertIn("execution_status", sql)
        self.assertEqual(sql.count("%s"), len(params))
        self.assertIn("partial_success", params)


if __name__ == "__main__":
    unittest.main()
