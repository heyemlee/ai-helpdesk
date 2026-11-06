"""
客服转来的问法（`benchmark/customer_cases.json`）自身的质量守卫，和离线跑通。

这份题集测的是客服问订单时原样转过来的客户原话（「我这单到哪了」「还要多久」）。题目对着
生产在用的精简业务库，由 `benchmark.customer` 按固定种子现建。这里只保证：

- 题集站得住：每题问的单号库里有，标准答案非空、不重复，只读一个库；
- 标准答案只输出问到的事实，单号留给模型自己带（判分允许多一列，正好容下它）；
- 入口离线跑得通，报告里单独一行，不碰门禁的分组。

**不替代**模型回归——答得对不对，要配真实模型跑 `python -m benchmark.run_benchmark --customer`。
"""
import json
import os
import re
import sqlite3
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

from benchmark.customer import CUSTOMER_CASES, build_simple_erp, customer_summary
from benchmark.runner import CUSTOMER_CATEGORY

ORDER_NO = re.compile(r"\b[Jj]\d{2}-\d{5}\b")
TABLE_REF = re.compile(r"\b(erp|prod|svc|wms)\.([a-z_]+)\b")
SIMPLE_ERP = {"erp": "erp_db", "prod": "prod_db", "svc": "svc_db", "wms": "wms_db"}


class CustomerCasesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from ai_sql_agent.semantic import load_semantic_dir

        cls.items = json.loads(CUSTOMER_CASES.read_text(encoding="utf-8"))
        cls.tmpdir = tempfile.TemporaryDirectory()
        root = Path(cls.tmpdir.name)
        cls.layer = load_semantic_dir(build_simple_erp(root))
        cls.conn = sqlite3.connect(":memory:")
        for alias in SIMPLE_ERP:
            cls.conn.execute(f"ATTACH DATABASE '{root / 'simple' / (alias + '.db')}' AS {alias}")

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmpdir.cleanup()

    def test_size_ids_and_languages(self):
        self.assertGreaterEqual(len(self.items), 20)
        self.assertLessEqual(len(self.items), 25)
        self.assertEqual(len({item["id"] for item in self.items}), len(self.items))
        for item in self.items:
            self.assertEqual(item["category"], CUSTOMER_CATEGORY, item["id"])
            self.assertIn(item["language"], ("zh", "en"), item["id"])
            if item["language"] == "en":
                self.assertTrue(item["query"].isascii(), item["id"])
            else:
                self.assertTrue(any("一" <= char <= "鿿" for char in item["query"]), item["id"])

    def test_every_question_names_orders_that_exist_and_gold_asks_about_them(self):
        known = {row[0] for row in self.conn.execute("SELECT order_no FROM erp.orders")}
        for item in self.items:
            asked = {number.upper() for number in ORDER_NO.findall(item["query"])}
            self.assertTrue(asked, f"{item['id']} 没带单号：题目一律按单号问")
            self.assertLessEqual(asked, known, item["id"])
            # 标准答案查的就是客户问的那几张单，不多不少
            self.assertEqual(set(ORDER_NO.findall(item["gold_sql"])), asked, item["id"])

    def test_gold_sql_returns_rows_without_duplicates(self):
        for item in self.items:
            rows = [tuple(row) for row in self.conn.execute(item["gold_sql"])]
            self.assertGreater(len(rows), 0, item["id"])
            self.assertEqual(len(rows), len(set(rows)), item["id"])

    def test_gold_reads_one_database(self):
        # 评测比的是最后一步的结果集，标准答案的输出不能横跨两个库
        for item in self.items:
            aliases = {alias for alias, _ in TABLE_REF.findall(item["gold_sql"])}
            self.assertEqual(len(aliases), 1, item["id"])

    def test_gold_outputs_only_the_asked_facts(self):
        """
        单号不放进标准答案的输出列，除非一题问了好几张单（那时要靠单号分清哪行是哪张）。

        模型会把单号当标识带上（规划提示词规则 11），判分允许多输出一列，正好容下它；
        标准答案再带一份单号，模型多带一个别的字段就要被判错了。
        """
        for item in self.items:
            select_list = item["gold_sql"].split(" FROM ")[0]
            several = len(set(ORDER_NO.findall(item["query"]))) > 1
            self.assertEqual("order_no" in select_list, several, item["id"])

    def test_gold_does_not_depend_on_names(self):
        # 生成器里的客户名、供应商名和生产那份写法不同，按单号查到的其它字段一致
        for item in self.items:
            for column in ("customer_name", "supplier_name"):
                self.assertNotIn(column, item["gold_sql"], item["id"])

    def test_gold_columns_exist_in_the_semantic_layer(self):
        for item in self.items:
            self.assertTrue(item["gold_columns"], item["id"])
            for qualified in item["gold_columns"]:
                database, table, column = qualified.split(".")
                tables = self.layer.databases[database].tables
                self.assertIn(column, tables[table]["columns"], f"{item['id']}: {qualified}")
            for alias, table in TABLE_REF.findall(item["gold_sql"]):
                self.assertIn(table, self.layer.databases[SIMPLE_ERP[alias]].tables, item["id"])


class CustomerOfflineRunTest(unittest.TestCase):
    """入口离线（Mock 模型、规则路由）跑得通，客服问法在报告里单独一行。"""

    def setUp(self):
        from ai_sql_agent.core import get_settings, reload_settings

        self._tmp = tempfile.TemporaryDirectory()
        self._cwd = os.getcwd()
        # 内存向量索引：pgvector 的表按库名分，精简库的 erp_db 会和演示库的 erp_db 互相覆盖
        self._env = mock.patch.dict(os.environ, {"AI_SQL_PGVECTOR_DSN": ""})
        self._env.start()
        os.chdir(self._tmp.name)
        self.addCleanup(self._restore)
        reload_settings()
        if get_settings().has_model_credentials:
            self.skipTest("配着模型凭证：这组只验离线入口，不该发真实请求")

    def _restore(self):
        from ai_sql_agent.core import reload_settings

        os.chdir(self._cwd)
        self._env.stop()
        reload_settings()
        self._tmp.cleanup()

    def test_runs_through_and_reports_its_own_row(self):
        from benchmark.customer import customer_runner

        items = json.loads(CUSTOMER_CASES.read_text(encoding="utf-8"))
        with customer_runner() as runner:
            # 客服拿到的是每一步的结果：答案在前面某一步也算对
            self.assertTrue(runner.score_all_steps)
            report = runner.run()

        self.assertFalse(report.incomplete)
        self.assertEqual(report.total, len(items))
        # 只有自己这一组：不冒出 single_db / cross_db，门禁的数字不受影响
        self.assertEqual(list(report.grouped_accuracy()), [CUSTOMER_CATEGORY])
        for item in report.items:
            self.assertIn(item.route, ("database_query", "data_qa"), item.item_id)
            self.assertNotIn("model_classifier", item.route_signals, item.item_id)

        lines = customer_summary(report)
        self.assertTrue(lines[0].startswith(f"客服转来的问法（{CUSTOMER_CATEGORY}，单独一行，不计入门禁，每一步都比）"))
        self.assertTrue(any("生产入口的路由（规则判）" in line for line in lines))

    def test_vector_retrieval_does_not_emit_runtime_warnings(self):
        # The pipeline catches retrieval exceptions, so turning warnings into
        # errors alone would otherwise leave this test green after a failure.
        from ai_sql_agent.retrieval.vector_index import VectorIndex
        from benchmark.customer import customer_runner

        original = VectorIndex.search
        failures = []
        calls = []

        def checked(index, *args, **kwargs):
            calls.append(1)
            with warnings.catch_warnings():
                warnings.simplefilter("error", RuntimeWarning)
                try:
                    return original(index, *args, **kwargs)
                except RuntimeWarning as exc:
                    failures.append(str(exc))
                    raise

        with mock.patch.object(VectorIndex, "search", checked), customer_runner() as runner:
            runner.run()
        self.assertTrue(calls)
        self.assertEqual(failures, [])

    def test_never_touches_a_configured_pgvector(self):
        """
        配了 pgvector 也不用：那里的索引表按命名空间全局共用，现建的库指纹不同，
        一建索引就会 TRUNCATE 同一个 DSN 上正在跑的 SQL Agent 的索引。
        """
        from ai_sql_agent.core import get_settings, reload_settings
        from ai_sql_agent.retrieval.vector_index import VectorIndex
        from benchmark.customer import customer_runner

        configured = "postgresql://nobody@127.0.0.1:9/shared"
        os.environ["AI_SQL_PGVECTOR_DSN"] = configured
        reload_settings()

        with customer_runner() as runner:
            self.assertEqual(get_settings().pgvector_dsn, "")
            service = runner.pipeline.schema_retrieval_service
            indexes = [service.db_vector_index, service.table_vector_index] + [
                column_service.vector_index for column_service in service.column_services.values()
            ]
            for index in indexes:
                self.assertIs(type(index), VectorIndex)

        # 退出后原样放回，不替调用方改配置
        self.assertEqual(os.environ["AI_SQL_PGVECTOR_DSN"], configured)
        self.assertEqual(get_settings().pgvector_dsn, configured)


if __name__ == "__main__":
    unittest.main()
