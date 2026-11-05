"""
执行层的第二道防线：结果字节预算、SQLite 单值长度上限、数据库层授权器。

背景是两个问题：

- 结果只限行数、不限字节。`SELECT x, zeroblob(5000000)` 每行 5 MB，
  `hex(randomblob(150000000))` 是 progress handler 打不断的单次函数调用。
- `mode=ro` 不覆盖 ATTACH、VACUUM INTO，也不挡 sqlite_master / pragma_*。

`sql_guard` 是应用层这一道，它有意不管资源（见 tests/test_execution_safety.py 里
`AUDIT_RESOURCE_BOMBS` 的说明）。这个文件验的是**守卫和授权关卡都被绕过之后**，
执行器自己还剩什么：

1. 守卫放行的资源炸弹（G20 / G21 / G26 / G27），在执行器上被截断或被拒；
2. 绕过守卫与授权关卡直连执行器时，ATTACH / VACUUM INTO / sqlite_master / pragma_*
   被引擎里的授权器拒掉；
3. 正常查询、目录预览与画像、表结构读取不受影响（后两者在
   tests/test_data_catalog.py、tests/test_catalog_profile.py，随全量测试一起跑）。

量级都取安全的小数：不真的分配 GB 级内存。上限用构造参数调小，演示的是同一条
代码路径。PostgreSQL 一侧用假连接，**没有在真实 PostgreSQL 上跑过**。
"""
import inspect
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from pydantic import ValidationError

import ai_sql_agent.execution.sqlite_executor as sqlite_executor
from ai_sql_agent.auth import authorization_scope
from ai_sql_agent.core import get_settings, reload_settings
from ai_sql_agent.execution import MCPExecutionRequest, PostgresMCPExecutor, SQLiteMCPExecutor
from ai_sql_agent.execution.result_budget import (
    RowTooLarge,
    row_bytes,
    trim_to_budget,
    value_bytes,
)
from ai_sql_agent.execution.sql_guard import SqlGuardResult
from ai_sql_agent.execution.sqlite_authorizer import readonly_authorizer
from ai_sql_agent.pipeline import PipelineConfig, Text2SQLPipeline
from ai_sql_agent.retrieval.access import ALLOW_ALL
from ai_sql_agent.retrieval.schema_fingerprint import sqlite_schema_fingerprint
from ai_sql_agent.retrieval.sqlite_loader import SQLiteSchemaLoader

from test_data_catalog import _FakeConnection
from test_execution_limits_setting import _IsolatedEnvironment

MIB = 1024 * 1024
DEFAULT_BYTES = 8 * MIB

# 无界递归，只取列：行数上限与字节预算任一生效就该停
UNBOUNDED_ROWS = "WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM r) SELECT x FROM r"
# 无界递归，聚合：靠执行超时停
UNBOUNDED_COUNT = (
    "WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM r) SELECT count(*) AS n FROM r"
)
WIDE_ROWS = (
    "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n LIMIT {rows}) "
    "SELECT x, zeroblob({width}) AS b FROM n"
)


def _make_database(directory: Path, name: str = "budget.db", *, big_blob: int = 0) -> Path:
    path = directory / name
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT, n REAL);
        INSERT INTO t VALUES (1, 'a', 1.5), (2, 'b', 2.5), (3, 'c', 3.5);
        CREATE TABLE child (id INTEGER PRIMARY KEY, t_id INTEGER REFERENCES t(id));
        INSERT INTO child VALUES (10, 1), (11, 2);
        ANALYZE;  -- 生成 sqlite_stat1，授权器要拒的内部表得真的存在
        """
    )
    if big_blob:
        conn.execute("CREATE TABLE big (id INTEGER PRIMARY KEY, payload BLOB)")
        conn.execute("INSERT INTO big VALUES (1, zeroblob(?))", (big_blob,))
    conn.commit()
    conn.close()
    return path


class _DatabaseCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.directory = Path(self._tmpdir.name).resolve()

    def executor(self, path: Path, **limits) -> SQLiteMCPExecutor:
        return SQLiteMCPExecutor(database="db", db_path=path, **limits)

    @staticmethod
    def run_sql(executor, sql: str, **request):
        with authorization_scope(ALLOW_ALL):
            return executor.execute(MCPExecutionRequest(database="db", sql=sql, **request))


@contextmanager
def _gate_and_guard_bypassed():
    """
    模拟「应用层两道关都被绕过」：授权关卡与只读守卫放行一切。
    审计的 `04_sqlite_second_line_of_defence.py` 用的是同一种打补丁的方式。
    """
    with mock.patch.object(sqlite_executor, "enforce", lambda request, dialect="": None), \
            mock.patch.object(
                sqlite_executor,
                "check_readonly_sql",
                lambda sql, dialect="": SqlGuardResult(True, normalized_sql=sql),
            ):
        yield


# ---- 字节口径 ----


class ResultBudgetTest(unittest.TestCase):
    def test_value_sizes(self):
        self.assertEqual(value_bytes(None), 8)
        self.assertEqual(value_bytes(True), 8)
        self.assertEqual(value_bytes(12345678901234), 8)
        self.assertEqual(value_bytes(1.5), 8)
        self.assertEqual(value_bytes("abcd"), 4)
        self.assertEqual(value_bytes("编码"), 6)  # UTF-8：每个汉字 3 字节
        self.assertEqual(value_bytes(b"\x00" * 100), 100)
        self.assertEqual(value_bytes(bytearray(7)), 7)
        self.assertEqual(value_bytes(memoryview(b"abcdef")), 6)
        self.assertEqual(value_bytes(["ab", "c", None]), 2 + 1 + 8)
        self.assertEqual(value_bytes({"k": "vv"}), 1 + 2)
        self.assertEqual(row_bytes((1, "ab", b"xyz")), 8 + 2 + 3)

    def test_trim_keeps_a_prefix_and_reports_the_cut(self):
        rows = [(i, "x" * 92) for i in range(10)]  # 每行 8 + 92 = 100
        kept, cut = trim_to_budget(rows, 450)
        self.assertEqual((len(kept), cut), (4, True))
        self.assertEqual(kept, rows[:4])
        kept, cut = trim_to_budget(rows, 1000)
        self.assertEqual((len(kept), cut), (10, False))
        kept, cut = trim_to_budget(rows, 400)  # 刚好 4 行放满
        self.assertEqual((len(kept), cut), (4, True))

    def test_trim_accepts_dict_rows(self):
        kept, cut = trim_to_budget([{"a": 1, "b": "xx"}] * 5, 25)
        self.assertEqual((len(kept), cut), (2, True))

    def test_a_first_row_over_budget_is_an_error_not_an_empty_result(self):
        with self.assertRaises(RowTooLarge) as caught:
            trim_to_budget([("x" * 500,)], 100)
        self.assertEqual((caught.exception.size, caught.exception.limit), (500, 100))


# ---- SQLite：字节预算与单值长度上限 ----


class SqliteByteBudgetTest(_DatabaseCase):
    def test_wide_rows_are_cut_by_bytes_even_though_the_row_cap_is_far_away(self):
        # G26 的安全量级：40 行 × 300 KB，预算 1 MB。行数上限 1000 离得很远
        executor = self.executor(
            _make_database(self.directory), max_result_bytes=1_000_000, max_value_bytes=2 * MIB
        )
        result = self.run_sql(executor, WIDE_ROWS.format(rows=40, width=300_000))

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.row_count, 3)  # 3 × (300000 + 8) = 900024 ≤ 1e6，第 4 行放不下
        self.assertTrue(result.truncated)
        self.assertEqual(result.truncated_by, "bytes")
        # 契约 §4.6.3：truncated 为真时 row_limit 必须有值。按字节截时它是实际返回的行数
        self.assertEqual(result.row_limit, 3)
        # 进程内字段，不进线上载荷
        self.assertNotIn("truncated_by", result.to_dict())

    def test_the_row_cap_is_still_reported_as_rows(self):
        executor = self.executor(_make_database(self.directory), max_rows=2)
        result = self.run_sql(executor, "SELECT id FROM t ORDER BY id")

        self.assertTrue(result.success, result.error)
        self.assertEqual(
            (result.row_count, result.truncated, result.truncated_by, result.row_limit),
            (2, True, "rows", 2),
        )

    def test_results_under_both_limits_are_untouched(self):
        executor = self.executor(_make_database(self.directory), max_rows=10)
        result = self.run_sql(executor, "SELECT id, v, n FROM t ORDER BY id")

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.columns, ["id", "v", "n"])
        self.assertEqual(result.rows[0], {"id": 1, "v": "a", "n": 1.5})
        self.assertEqual(
            (result.row_count, result.truncated, result.truncated_by, result.row_limit),
            (3, False, "", 10),
        )

    def test_a_request_can_tighten_the_row_cap_but_the_budget_still_applies(self):
        executor = self.executor(
            _make_database(self.directory), max_result_bytes=1_000_000, max_value_bytes=2 * MIB
        )
        result = self.run_sql(
            executor, WIDE_ROWS.format(rows=40, width=300_000), max_rows=2
        )
        self.assertEqual(
            (result.row_count, result.truncated_by, result.row_limit), (2, "rows", 2)
        )

    def test_a_first_row_over_the_budget_is_an_error_not_a_silent_empty_page(self):
        executor = self.executor(
            _make_database(self.directory), max_result_bytes=1_000_000, max_value_bytes=2 * MIB
        )
        result = self.run_sql(executor, "SELECT zeroblob(1500000) AS b")

        self.assertFalse(result.success)
        self.assertEqual(result.rows, [])
        self.assertIn("字节上限", result.error)
        self.assertIn("SUBSTR", result.error)  # 告诉人怎么办

    def test_single_call_bombs_stop_before_they_allocate(self):
        # G20：randomblob(1e9)。G27：hex(randomblob(1.5e8))——progress handler 打不断的那种。
        # 没有长度上限时前者分配约 1 GB、后者约 420 MB；这里能在毫秒内报错说明没有分配
        executor = self.executor(
            _make_database(self.directory), max_result_bytes=1 * MIB, max_value_bytes=2 * MIB
        )
        for sql in (
            "SELECT randomblob(1000000000) AS b",
            "SELECT hex(randomblob(150000000)) AS h",
            "SELECT length(hex(randomblob(150000000))) AS n",
            "SELECT zeroblob(3000000) AS b",
        ):
            started = time.monotonic()
            result = self.run_sql(executor, sql)
            elapsed = time.monotonic() - started
            self.assertFalse(result.success, sql)
            self.assertIn("字节", result.error, sql)
            self.assertNotIn("string or blob too big", result.error, sql)  # 不把引擎原话丢给用户
            self.assertLess(elapsed, 2.0, sql)

    def test_the_connection_carries_the_configured_length_limit(self):
        executor = self.executor(_make_database(self.directory), max_value_bytes=123_456)
        conn = executor._connect()
        try:
            self.assertEqual(conn.getlimit(sqlite3.SQLITE_LIMIT_LENGTH), 123_456)
        finally:
            conn.close()

    def test_a_stored_value_longer_than_the_limit_is_refused_when_read(self):
        # 这是调高之前要知道的代价：已有的超长行读它也会报错。错误文本要让人看懂
        path = _make_database(self.directory, big_blob=3_000_000)
        executor = self.executor(path, max_value_bytes=1 * MIB)

        result = self.run_sql(executor, "SELECT payload FROM big")
        self.assertFalse(result.success)
        self.assertIn("字节", result.error)
        # 同一张库里别的表照常读
        self.assertTrue(self.run_sql(executor, "SELECT id FROM t").success)

    def test_unbounded_recursion_is_stopped_by_the_row_cap_or_the_deadline(self):
        # G21。取列的写法：行数上限先到，且不会把整条递归物化
        path = _make_database(self.directory)
        executor = self.executor(path, max_rows=500)
        started = time.monotonic()
        rows = self.run_sql(executor, UNBOUNDED_ROWS)
        self.assertTrue(rows.success, rows.error)
        self.assertEqual((rows.row_count, rows.truncated_by), (500, "rows"))
        self.assertLess(time.monotonic() - started, 5.0)

        # 聚合的写法：一行都出不来，靠执行超时
        executor = self.executor(path, max_execution_seconds=0.3)
        started = time.monotonic()
        count = self.run_sql(executor, UNBOUNDED_COUNT)
        self.assertFalse(count.success)
        self.assertIn("超时", count.error)
        self.assertLess(time.monotonic() - started, 5.0)


# ---- SQLite：数据库层授权器 ----


class SqliteAuthorizerTest(_DatabaseCase):
    """守卫与授权关卡都被绕过时，引擎里的授权器是最后一道。"""

    def setUp(self):
        super().setUp()
        self.path = _make_database(self.directory)
        self.other = _make_database(self.directory, "other.db")
        self.executor_ = self.executor(self.path)

    def execute(self, sql: str, executor=None):
        with _gate_and_guard_bypassed():
            return self.run_sql(executor or self.executor_, sql)

    def row_count(self, path: Path) -> int:
        conn = sqlite3.connect(path)
        try:
            return conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        finally:
            conn.close()

    def test_attach_is_denied_and_nothing_is_created_or_written(self):
        fresh = self.directory / "brand_new.db"
        for sql in (
            f"ATTACH '{self.other}' AS o",
            f"ATTACH DATABASE '{self.other}' AS o",
            f"ATTACH '{fresh}' AS n",
            f"ATTACH 'file:{self.directory / 'rwc.db'}?mode=rwc' AS n2",
        ):
            result = self.execute(sql)
            self.assertFalse(result.success, sql)
            self.assertIn("数据库层拒绝", result.error, sql)
        # ATTACH 不存在的路径会在磁盘上建文件。没建
        self.assertFalse(fresh.exists())
        self.assertFalse((self.directory / "rwc.db").exists())
        self.assertEqual(self.row_count(self.other), 3)

    def test_vacuum_into_cannot_copy_the_database(self):
        target = self.directory / "exfil_copy.db"
        result = self.execute(f"VACUUM INTO '{target}'")
        self.assertFalse(result.success)
        self.assertFalse(target.exists())

    def test_internal_tables_and_pragma_functions_are_denied(self):
        for sql in (
            "SELECT name, sql FROM sqlite_master",
            "SELECT name FROM sqlite_schema",
            "SELECT name FROM sqlite_temp_master",
            "SELECT * FROM sqlite_stat1",
            "SELECT name FROM pragma_table_info('t')",
            "SELECT file FROM pragma_database_list",
            "SELECT name FROM pragma_index_list('t')",
            "SELECT name, pageno, payload FROM dbstat",
            "SELECT * FROM SQLITE_MASTER",  # 大小写不影响
        ):
            result = self.execute(sql)
            self.assertFalse(result.success, sql)
            self.assertIn("数据库层拒绝", result.error, sql)

    def test_pragma_statements_are_denied(self):
        for sql in (
            "PRAGMA table_info(t)",
            "PRAGMA writable_schema = 1",
            "PRAGMA query_only = OFF",
            "PRAGMA journal_mode = DELETE",
        ):
            result = self.execute(sql)
            self.assertFalse(result.success, sql)

    def test_writes_and_ddl_are_denied_below_the_guard(self):
        for sql in (
            "INSERT INTO t VALUES (9, 'z', 0)",
            "UPDATE t SET v = 'x'",
            "DELETE FROM t",
            "DROP TABLE t",
            "CREATE TABLE evil (x)",
            "CREATE TEMP TABLE tt (x)",
            "CREATE INDEX i ON t (v)",
            "ALTER TABLE t ADD COLUMN c TEXT",
            "BEGIN",
            "SAVEPOINT s",
        ):
            result = self.execute(sql)
            self.assertFalse(result.success, sql)
        self.assertEqual(self.row_count(self.path), 3)

    def test_denied_functions_are_stopped_in_the_engine(self):
        result = self.execute("SELECT load_extension('/nonexistent/x') AS e")
        self.assertFalse(result.success)
        self.assertIn("数据库层拒绝", result.error)

    def test_the_authorizer_callback_is_a_default_deny_allowlist(self):
        ok, deny = sqlite3.SQLITE_OK, sqlite3.SQLITE_DENY
        self.assertEqual(readonly_authorizer(sqlite3.SQLITE_SELECT, None, None, None, None), ok)
        self.assertEqual(readonly_authorizer(sqlite3.SQLITE_READ, "t", "v", "main", None), ok)
        self.assertEqual(readonly_authorizer(sqlite3.SQLITE_FUNCTION, None, "lower", None, None), ok)
        self.assertEqual(readonly_authorizer(sqlite3.SQLITE_RECURSIVE, None, None, None, None), ok)
        self.assertEqual(readonly_authorizer(sqlite3.SQLITE_READ, "sqlite_master", "sql", "main", None), deny)
        self.assertEqual(readonly_authorizer(sqlite3.SQLITE_READ, "PRAGMA_table_info", "name", "main", None), deny)
        self.assertEqual(readonly_authorizer(sqlite3.SQLITE_READ, "dbstat", "name", "main", None), deny)
        self.assertEqual(readonly_authorizer(sqlite3.SQLITE_FUNCTION, None, "load_extension", None, None), deny)
        # 其它动作一律拒：不在白名单里就是不放行，不靠一份「禁止清单」
        for action in (
            sqlite3.SQLITE_ATTACH,
            sqlite3.SQLITE_DETACH,
            sqlite3.SQLITE_PRAGMA,
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_TEMP_TABLE,
            sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_TRANSACTION,
            sqlite3.SQLITE_SAVEPOINT,
        ):
            self.assertEqual(readonly_authorizer(action, "x", "y", "main", None), deny, action)

    def test_ordinary_queries_are_not_affected(self):
        for sql in (
            "SELECT id, v FROM t WHERE v LIKE 'a%' ORDER BY id",
            "SELECT t.id, c.id FROM t JOIN child c ON c.t_id = t.id",
            "SELECT v, count(*) AS n, max(n) AS m FROM t GROUP BY v HAVING count(*) >= 1",
            "SELECT id, row_number() OVER (ORDER BY id) AS rn FROM t",
            "WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM r WHERE x < 5) SELECT x FROM r",
            "SELECT * FROM json_each('[1, 2, 3]')",
            "SELECT json_extract('{\"a\": 1}', '$.a') AS a, strftime('%Y', 'now') AS y",
            "SELECT typeof(v), printf('%05d', id), coalesce(n, 0), CASE WHEN id > 1 THEN 'x' END FROM t",
            "SELECT id FROM t UNION SELECT id FROM child",
            "VALUES (1, 'a'), (2, 'b')",
            "SELECT id FROM t WHERE id IN (SELECT t_id FROM child)",
            "SELECT zeroblob(10) AS z, hex(randomblob(4)) AS h, replace(v, 'a', 'b') FROM t",
        ):
            result = self.execute(sql)
            self.assertTrue(result.success, f"{sql}: {result.error}")

    def test_the_same_queries_pass_through_the_real_guard_and_gate_too(self):
        # 不打补丁：守卫、授权关卡、授权器三层一起，正常查询照旧
        result = self.run_sql(self.executor_, "SELECT id FROM t WHERE v LIKE 'a%'")
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.rows, [{"id": 1}])

    def test_the_authorizer_belongs_to_readonly_executors_only(self):
        # 读写执行器是「有意可写」的配置（只有测试和建库脚本这么用）：不装授权器，
        # 写语句能走到引擎里执行。执行器自己从不提交，所以数据不落盘——那是它原来的行为
        writer = self.executor(self.path, readonly=False)
        result = self.execute("INSERT INTO t VALUES (9, 'z', 0)", writer)
        self.assertTrue(result.success, result.error)
        conn = writer._connect()
        try:
            self.assertEqual(conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()[0], 1)
        finally:
            conn.close()

    def test_internal_readers_use_their_own_connections(self):
        # 表结构加载、schema 指纹读 sqlite_master 与 PRAGMA。它们不经过执行器的连接，
        # 授权器装在执行器上，不能把它们拦了
        self.executor_._connect().close()  # 连接已经建过、授权器已经装过
        tables, columns, relations = SQLiteSchemaLoader(self.path).load()
        self.assertEqual(sorted(tables), ["child", "t"])
        self.assertTrue(columns)
        self.assertTrue(relations)
        self.assertEqual(len(sqlite_schema_fingerprint(str(self.path))), 64)


# ---- PostgreSQL：只能做「返回多少」这一半，且没有在真实库上跑过 ----


class PostgresByteBudgetTest(unittest.TestCase):
    """假连接：验证的是「执行器怎么处理驱动取回的行」。没有在真实 PostgreSQL 上跑过。"""

    def _executor(self, connection, **limits):
        executor = PostgresMCPExecutor(
            "pg_db", "postgresql://unused", pool_max_connections=0, **limits
        )
        executor._pool._factory = lambda: connection
        return executor

    @staticmethod
    def _run(executor, sql="SELECT id, name FROM item"):
        with authorization_scope(ALLOW_ALL):
            return executor.execute(MCPExecutionRequest(database="pg_db", sql=sql))

    def test_rows_are_cut_at_the_byte_budget(self):
        connection = _FakeConnection(rows=[(i, "x" * 1000) for i in range(20)])
        result = self._run(self._executor(connection, max_rows=50, max_result_bytes=5000))

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.row_count, 4)  # 4 × 1008 = 4032 ≤ 5000，第 5 行放不下
        self.assertEqual((result.truncated, result.truncated_by, result.row_limit), (True, "bytes", 4))
        self.assertEqual(result.rows[0], {"id": 0, "name": "x" * 1000})

    def test_the_row_cap_wins_when_it_comes_first(self):
        connection = _FakeConnection(rows=[(i, "x") for i in range(20)])
        result = self._run(self._executor(connection, max_rows=7, max_result_bytes=10 * MIB))
        self.assertEqual(
            (result.row_count, result.truncated_by, result.row_limit), (7, "rows", 7)
        )

    def test_a_first_row_over_the_budget_is_an_error(self):
        connection = _FakeConnection(rows=[(1, "x" * 10_000)])
        result = self._run(self._executor(connection, max_rows=50, max_result_bytes=5000))
        self.assertFalse(result.success)
        self.assertIn("字节上限", result.error)
        self.assertIn("left", result.error)
        # 这是结果太大，不是连接坏了：事务照常回滚
        self.assertIn(("rollback", ()), connection.log)

    def test_results_under_the_budget_are_untouched(self):
        connection = _FakeConnection(rows=[(1, "a"), (2, "b")])
        result = self._run(self._executor(connection, max_rows=50))
        self.assertEqual(
            (result.row_count, result.truncated, result.truncated_by, result.row_limit),
            (2, False, "", 50),
        )


# ---- 配置：默认值、校验、贯通 ----


class BudgetSettingsTest(_IsolatedEnvironment):
    def test_every_place_that_writes_a_default_writes_the_same_one(self):
        settings = get_settings()
        self.assertEqual(settings.max_result_bytes, DEFAULT_BYTES)
        self.assertEqual(settings.max_value_bytes, DEFAULT_BYTES)

        config = PipelineConfig()
        self.assertEqual(config.max_result_bytes, settings.max_result_bytes)
        self.assertEqual(config.max_value_bytes, settings.max_value_bytes)

        sqlite_params = inspect.signature(SQLiteMCPExecutor).parameters
        self.assertEqual(sqlite_params["max_result_bytes"].default, settings.max_result_bytes)
        self.assertEqual(sqlite_params["max_value_bytes"].default, settings.max_value_bytes)
        postgres_params = inspect.signature(PostgresMCPExecutor).parameters
        self.assertEqual(postgres_params["max_result_bytes"].default, settings.max_result_bytes)
        # PostgreSQL 没有对应的会话参数，所以没有这个形参
        self.assertNotIn("max_value_bytes", postgres_params)

    def test_the_environment_overrides_and_tiny_values_are_refused(self):
        self.configure(AI_SQL_MAX_RESULT_BYTES=65536, AI_SQL_MAX_VALUE_BYTES=32768)
        settings = get_settings()
        self.assertEqual((settings.max_result_bytes, settings.max_value_bytes), (65536, 32768))

        for key, field in (
            ("AI_SQL_MAX_RESULT_BYTES", "max_result_bytes"),
            ("AI_SQL_MAX_VALUE_BYTES", "max_value_bytes"),
        ):
            for bad in ("0", "-1", "100"):
                os.environ[key] = bad
                try:
                    with self.assertRaises(ValidationError, msg=f"{key}={bad}") as caught:
                        reload_settings()
                    self.assertIn(field, str(caught.exception))
                finally:
                    os.environ.pop(key, None)
        reload_settings()

    def test_pipeline_config_reaches_the_executors(self):
        pipeline = Text2SQLPipeline(PipelineConfig(max_result_bytes=123_456, max_value_bytes=234_567))
        for name, executor in pipeline.mcp_router.executors.items():
            self.assertEqual(executor.max_result_bytes, 123_456, name)
            self.assertEqual(executor.max_value_bytes, 234_567, name)

    def test_the_api_hands_the_settings_to_the_executors_and_logs_them(self):
        from ai_sql_agent.api.app import app

        self.configure(AI_SQL_MAX_RESULT_BYTES=65536, AI_SQL_MAX_VALUE_BYTES=32768)
        with self.assertLogs("ai_sql.api", level="INFO") as captured:
            with TestClient(app):
                for name, executor in app.state.pipeline.mcp_router.executors.items():
                    self.assertEqual(executor.max_result_bytes, 65536, name)
                    self.assertEqual(executor.max_value_bytes, 32768, name)

        startup = [
            record.ai_sql_fields for record in captured.records
            if record.getMessage() == "api.startup"
        ]
        self.assertEqual(len(startup), 1)
        self.assertEqual(
            (startup[0]["max_result_bytes"], startup[0]["max_value_bytes"]), (65536, 32768)
        )


if __name__ == "__main__":
    unittest.main()
