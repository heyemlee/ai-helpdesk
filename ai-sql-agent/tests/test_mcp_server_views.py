"""
stdio MCP 服务器的视图目录接线。

管道和数据目录已经把 `ViewCatalog` 放进授权作用域；`ai_sql_agent.mcp_server.server` 自己开
作用域（stdio 进程没有调用方身份，也不跨进程继承），以前没带目录，所以同一条
`SELECT * FROM v_payroll` 在这里能读出被 `deny_tables` 禁掉的 `payroll`。

这里**从工具入口 `execute_sql` 进去**，不是直接调关卡：证明作用域里带了目录，
目录的刷新时机（启动 + 每次调用前重扫被查的库）和失败语义（扫描失败 → 受限策略被拒）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_sql_agent.auth import ViewCatalog
from ai_sql_agent.auth.views import LOADED
from ai_sql_agent.execution import MCPRouter, SQLiteMCPExecutor

SCHEMA = """
CREATE TABLE employees(id INTEGER PRIMARY KEY, name TEXT, salary INT);
INSERT INTO employees VALUES (101,'ann',9000),(102,'bob',4500);
CREATE TABLE payroll(emp INT, amount INT);
INSERT INTO payroll VALUES (101,1),(102,2);
CREATE VIEW v_payroll AS SELECT emp, amount FROM payroll;
CREATE VIEW v_chain AS SELECT emp FROM v_payroll;
"""

server = None
_import_dir = None


def setUpModule():
    """导入这个模块会在 cwd 里建 demo 库：放进一次性目录里导入，别写到别人的 cwd。"""
    global server, _import_dir
    _import_dir = tempfile.TemporaryDirectory()
    previous = os.getcwd()
    os.chdir(_import_dir.name)
    try:
        from ai_sql_agent.mcp_server import server as module

        server = module
    finally:
        os.chdir(previous)


def tearDownModule():
    _import_dir.cleanup()


class StartupTest(unittest.TestCase):
    def test_every_registered_database_has_a_loaded_catalog_at_startup(self):
        for name in server._DATABASES:
            with self.subTest(database=name):
                state = server._views.state(name)
                self.assertEqual(state.status, LOADED)
                self.assertTrue(state.objects.tables)


class ExecuteSqlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "app.db"
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(SCHEMA)

        router = MCPRouter()
        router.register_executor(
            database="app",
            executor=SQLiteMCPExecutor(database="app", db_path=self.db_path, readonly=True),
        )
        self.views = ViewCatalog()
        self._patches = [
            patch.object(server, "_router", router),
            patch.object(server, "_DATABASES", {"app": (str(self.db_path), None)}),
            patch.object(server, "_views", self.views),
        ]
        for item in self._patches:
            item.start()
            self.addCleanup(item.stop)
        server._refresh_views("app")  # 等同于启动时那一轮

    def _policy(self, **rules):
        path = Path(self.tmp.name) / "policy.json"
        path.write_text(json.dumps(rules), encoding="utf-8")
        patcher = patch.dict(os.environ, {"AI_SQL_MCP_POLICY_FILE": str(path)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _call(self, sql):
        return json.loads(server.execute_sql("app", sql))

    def test_a_view_over_a_denied_table_is_refused(self):
        self._policy(deny_tables=["app.payroll"])
        for sql in ("SELECT * FROM v_payroll", "SELECT emp FROM v_chain"):
            with self.subTest(sql=sql):
                result = self._call(sql)
                self.assertFalse(result["success"])
                self.assertEqual(result["rows"], [])
                self.assertIn("payroll", result["error"])

    def test_an_ordinary_table_still_works(self):
        self._policy(deny_tables=["app.payroll"])
        result = self._call("SELECT name FROM employees ORDER BY name")
        self.assertTrue(result["success"], result.get("error"))
        self.assertEqual([row["name"] for row in result["rows"]], ["ann", "bob"])

    def test_without_a_registered_catalog_the_hole_is_still_there(self):
        # 同一条 SQL，目录没登记过这个库：证明防线是目录提供的，也证明上面几条不是空转
        self._policy(deny_tables=["app.payroll"])
        with patch.object(server, "_views", ViewCatalog()), patch.object(
            server, "_refresh_views", lambda database: None
        ):
            result = self._call("SELECT * FROM v_payroll")
        self.assertTrue(result["success"], result.get("error"))
        self.assertEqual(len(result["rows"]), 2)

    def test_a_view_created_after_startup_is_seen_on_the_next_call(self):
        self._policy(deny_tables=["app.payroll"])
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE VIEW v_late AS SELECT * FROM payroll")
        result = self._call("SELECT * FROM v_late")
        self.assertFalse(result["success"])
        # 被识别成视图并按基表拒绝，而不是因为「目录里没登记」而拒绝
        self.assertIn("payroll", result["error"])
        self.assertNotIn("无法确认", result["error"])

    def test_a_failed_scan_refuses_a_restricted_policy(self):
        self._policy(deny_tables=["app.payroll"])
        self.db_path.unlink()  # 下一次扫描会失败
        result = self._call("SELECT name FROM employees")
        self.assertFalse(result["success"])
        self.assertIn("无法确认", result["error"])
        self.assertEqual(self.views.state("app").status, "failed")

    def test_a_failed_scan_does_not_block_an_unrestricted_server(self):
        # 没配策略 = 全放开，没有东西可遮：目录失败不该再多拒一层（库不在了，是执行器报错）
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_SQL_MCP_POLICY_FILE", None)
            self.db_path.unlink()
            result = self._call("SELECT name FROM employees")
        self.assertFalse(result["success"])
        self.assertNotIn("无法确认", result["error"])


if __name__ == "__main__":
    unittest.main()
