"""
单条查询的行数上限与执行超时由配置决定
（`AI_SQL_MAX_ROWS` / `AI_SQL_MAX_EXECUTION_SECONDS`）。

这两个配置项一直在 Settings 里，启动日志（`api.startup`）也照着打，但 API 建流水线时
没有把它们交给 `PipelineConfig`，执行器拿到的是 `PipelineConfig` 自己的默认值。
于是日志写着「10000 行 / 30 秒」，实际生效的是 1000 行 / 10 秒，改环境变量什么也不发生。

锁五件事：
1. 默认值只有一组数：Settings、`PipelineConfig`、两个执行器各自写的默认值相同，
   都是原来实际生效的 1000 行 / 10 秒（数值钉在 test_settings_and_backends.py）——
   不设环境变量的部署，升级之后结果不会悄悄变大；
2. 0 和负数在读配置时就被拒掉：行数上限 0 会让每条查询都返回空结果，
   而 PostgreSQL 的 `statement_timeout = 0` 是「不限时」；
3. `PipelineConfig` 里的上限会到达执行器并且真的生效（SQLite：截断、超时中止），
   不经过 Settings 也能用；
4. API 启动时把配置交给流水线：启动日志打的数就是执行器在用的数；
5. 上限比预览的页大小还低时，翻页照样一行不漏（数据浏览用的是同一批执行器）。

PostgreSQL 分支本机没有库可连。`PostgresBranchTest` 只验证「上限交给了执行器、
执行器怎么调驱动」，用的是假连接，**没有在真实 PostgreSQL 上跑过**。
"""
import inspect
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from pydantic import ValidationError

from ai_sql_agent.auth import authorization_scope
from ai_sql_agent.core import get_settings, reload_settings
from ai_sql_agent.execution import MCPExecutionRequest, PostgresMCPExecutor, SQLiteMCPExecutor
from ai_sql_agent.pipeline import PipelineConfig, Text2SQLPipeline
from ai_sql_agent.pipeline.objects import DatabaseConfig
from ai_sql_agent.retrieval.access import ALLOW_ALL

from test_data_catalog import _FakeConnection

ENV_KEYS = (
    "AI_SQL_MAX_ROWS", "AI_SQL_MAX_EXECUTION_SECONDS",
    "AI_SQL_MEMORY_DB_PATH", "AI_SQL_AUDIT_DB_PATH", "AI_SQL_AUTH_MODE", "AI_SQL_SEMANTIC_DIR",
)

# 一条跑不完的查询。递归有上界：超时万一没生效，用例是失败，不是把整个测试进程挂住
RUNAWAY_SQL = (
    "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 100000000) "
    "SELECT COUNT(*) AS total FROM n"
)


class _IsolatedEnvironment(unittest.TestCase):
    """每个用例在自己的临时目录里跑：工作目录、环境变量用完都还原。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmpdir.name).resolve()
        self._cwd = os.getcwd()
        self._saved = {key: os.environ.get(key) for key in ENV_KEYS}
        os.chdir(self.directory)
        self.addCleanup(self._restore)
        self.configure(
            AI_SQL_MAX_ROWS=None,
            AI_SQL_MAX_EXECUTION_SECONDS=None,
            AI_SQL_AUTH_MODE="disabled",
            AI_SQL_SEMANTIC_DIR="",
            AI_SQL_MEMORY_DB_PATH=self.directory / "memory.db",
            AI_SQL_AUDIT_DB_PATH=self.directory / "audit.db",
        )

    def _restore(self):
        os.chdir(self._cwd)
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reload_settings()
        self._tmpdir.cleanup()

    def configure(self, **values):
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        reload_settings()


def _limits(holder):
    return holder.max_rows, holder.max_execution_seconds


def _execute(router, database, sql):
    # 执行器入口有强制授权关卡；这组用例验的是上限，权限维度用全放开策略排除掉
    with authorization_scope(ALLOW_ALL):
        return router.execute(MCPExecutionRequest(database=database, sql=sql))


class DefaultLimitsTest(_IsolatedEnvironment):
    def test_every_place_that_writes_a_default_writes_the_same_one(self):
        # 默认值写在四个地方。这次的问题就是其中两处对不上，而且只有一处生效。
        # 具体的数（1000 行 / 10 秒）钉在 tests/test_settings_and_backends.py
        expected = _limits(get_settings())
        self.assertEqual(_limits(PipelineConfig()), expected)
        for executor in (SQLiteMCPExecutor, PostgresMCPExecutor):
            parameters = inspect.signature(executor).parameters
            self.assertEqual(
                (parameters["max_rows"].default, parameters["max_execution_seconds"].default),
                expected,
                executor.__name__,
            )

    def test_zero_and_negative_limits_are_refused_when_settings_load(self):
        for key, field, bad in (
            ("AI_SQL_MAX_ROWS", "max_rows", "0"),
            ("AI_SQL_MAX_ROWS", "max_rows", "-1"),
            ("AI_SQL_MAX_EXECUTION_SECONDS", "max_execution_seconds", "0"),
            ("AI_SQL_MAX_EXECUTION_SECONDS", "max_execution_seconds", "-2.5"),
        ):
            os.environ[key] = bad
            try:
                with self.assertRaises(ValidationError, msg=f"{key}={bad}") as caught:
                    reload_settings()
                self.assertIn(field, str(caught.exception))
            finally:
                os.environ.pop(key, None)


class PipelineConfigReachesTheExecutorsTest(_IsolatedEnvironment):
    """SQLite 这条路是真跑的：演示库、进程内执行器，不经过 Settings。"""

    def test_the_row_cap_truncates_at_the_configured_number(self):
        pipeline = Text2SQLPipeline(PipelineConfig(max_rows=5))
        for name, executor in pipeline.mcp_router.executors.items():
            self.assertEqual(executor.max_rows, 5, name)

        # 演示库的 purchase_order 有 12 行
        result = _execute(
            pipeline.mcp_router, "erp_db", "SELECT po_no FROM purchase_order ORDER BY po_no"
        )
        self.assertTrue(result.success, result.error)
        self.assertEqual((result.row_count, result.truncated, result.row_limit), (5, True, 5))

    def test_the_timeout_aborts_at_the_configured_number(self):
        pipeline = Text2SQLPipeline(PipelineConfig(max_execution_seconds=0.2))
        for name, executor in pipeline.mcp_router.executors.items():
            self.assertEqual(executor.max_execution_seconds, 0.2, name)

        started = time.monotonic()
        result = _execute(pipeline.mcp_router, "erp_db", RUNAWAY_SQL)
        elapsed = time.monotonic() - started

        self.assertFalse(result.success)
        self.assertIn("超时", result.error)
        self.assertIn("0.2", result.error)
        # 默认是 10 秒：远早于它停下来，才说明停它的是配置的那个数
        self.assertLess(elapsed, 5.0)


class ApiWiringTest(_IsolatedEnvironment):
    def test_the_startup_log_reports_the_limits_the_executors_use(self):
        from ai_sql_agent.api.app import app

        self.configure(AI_SQL_MAX_ROWS=5, AI_SQL_MAX_EXECUTION_SECONDS=2.5)
        with self.assertLogs("ai_sql.api", level="INFO") as captured:
            with TestClient(app) as client:
                executors = app.state.pipeline.mcp_router.executors
                self.assertEqual(sorted(executors), ["erp_db", "mes_db", "pm_db", "wms_db"])
                for name, executor in executors.items():
                    self.assertEqual(_limits(executor), (5, 2.5), name)

                truncated = _execute(
                    app.state.pipeline.mcp_router,
                    "erp_db",
                    "SELECT po_no FROM purchase_order ORDER BY po_no",
                )
                self.assertEqual(
                    (truncated.row_count, truncated.truncated, truncated.row_limit), (5, True, 5)
                )

                # 再走一遍 /query 的处理路径（进程内的 TestClient，模型是 Mock）：
                # 响应里每一步报出来的上限也是配置的那个数
                response = client.post(
                    "/query",
                    json={
                        "user_id": "u_limits",
                        "session_id": "s_limits",
                        "query": "查询准时率低于0.9的供应商名称",
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)
                steps = [step for step in response.json()["steps"] if step["success"]]
                self.assertTrue(steps, response.text)
                self.assertEqual({step["row_limit"] for step in steps}, {5})

        startup = [
            record.ai_sql_fields for record in captured.records
            if record.getMessage() == "api.startup"
        ]
        self.assertEqual(len(startup), 1)
        self.assertEqual((startup[0]["max_rows"], startup[0]["max_execution_seconds"]), (5, 2.5))

    def test_a_cap_below_the_preview_page_size_still_pages_through_every_row(self):
        from ai_sql_agent.api.app import app

        self.configure(AI_SQL_MAX_ROWS=5)
        with TestClient(app) as client:
            erp = next(
                database for database in app.state.pipeline.databases if database.name == "erp_db"
            )
            with sqlite3.connect(erp.db_path) as conn:
                expected = [row[0] for row in conn.execute("SELECT po_no FROM purchase_order")]
            # 演示库里这张表有 12 行：比上限多，才翻得出第二页
            self.assertGreater(len(expected), 10)

            pages = []
            while not pages or pages[-1]["has_more"]:
                self.assertLess(len(pages), 10, "翻页没有收敛")
                response = client.post(
                    "/catalog/erp_db/purchase_order/rows",
                    json={"page": len(pages) + 1, "page_size": 25},
                )
                self.assertEqual(response.status_code, 200, response.text)
                pages.append(response.json())

        key = [column["name"] for column in pages[0]["columns"]].index("po_no")
        seen = [row[key] for page in pages for row in page["rows"]]
        # 每一行恰好出现一次：没有哪一段被跳过，也没有重复
        self.assertEqual(sorted(seen), sorted(expected))
        # 实际的页大小与偏移如实报出来，页面按它们显示「第几到第几条」
        self.assertEqual([page["page_size"] for page in pages], [5] * len(pages))
        self.assertEqual(
            [page["offset"] for page in pages], [5 * index for index in range(len(pages))]
        )


class PostgresBranchTest(unittest.TestCase):
    """假连接上的单元测试：验证的是「怎么调驱动」。没有在真实 PostgreSQL 上跑过。"""

    def _executor(self, **limits):
        # 只跑「建执行器」这一步：整条流水线要先连库读表结构，本机没有 PostgreSQL
        stand_in = SimpleNamespace(
            config=PipelineConfig(**limits),
            databases=[
                DatabaseConfig(name="pg_db", dialect="postgres", dsn="postgresql://unused")
            ],
        )
        return Text2SQLPipeline._build_mcp_router(stand_in).executors["pg_db"]

    def test_the_limits_are_handed_to_the_postgres_executor(self):
        executor = self._executor(max_rows=7, max_execution_seconds=2.5)
        self.assertIsInstance(executor, PostgresMCPExecutor)
        self.assertEqual(_limits(executor), (7, 2.5))

    def test_the_driver_is_called_with_those_limits(self):
        executor = self._executor(max_rows=7, max_execution_seconds=2.5)
        connection = _FakeConnection(rows=[(index, f"n{index}") for index in range(20)])
        executor._pool._factory = lambda: connection

        with authorization_scope(ALLOW_ALL):
            result = executor.execute(
                MCPExecutionRequest(database="pg_db", sql="SELECT id, name FROM item")
            )

        self.assertTrue(result.success, result.error)
        # 多取一行用来判断是否截断
        self.assertIn(("fetchmany", 8), connection.log)
        self.assertEqual((result.row_count, result.truncated, result.row_limit), (7, True, 7))
        timeout = next(
            args[0] for kind, args in connection.log
            if kind == "conn" and "statement_timeout" in args[0]
        )
        self.assertEqual(timeout, "SET LOCAL statement_timeout = 2500")


if __name__ == "__main__":
    unittest.main()
