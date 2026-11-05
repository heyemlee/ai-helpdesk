"""
中英文业务用例（`benchmark/bilingual_cases.json`）自身的质量守卫。

这份用例集是给模型回归用的：同一个业务问题用中文问一遍、英文问一遍，答案应当相同。
这里**不替代**模型回归——它只保证用例本身站得住：

- 四个业务场景各有一中一英，两条共用同一份 gold SQL；
- gold SQL 在内置演示数据上跑得通、有结果、没有重复行；
- 问句和 gold 的形状对得上（规则路由都判成查库；没有歧义句式）。

模型答得对不对，要用 `python -m benchmark.run_benchmark --dataset benchmark/bilingual_cases.json`
配真实模型去跑。
"""
import json
import sqlite3
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path

from ai_sql_agent.memory import ConversationMemoryContext, ShortTermContext
from ai_sql_agent.pipeline import RouteType, RuleBasedIntentClassifier
from ai_sql_agent.pipeline.demo_data import (
    create_erp_demo_database,
    create_mes_demo_database,
    create_pm_demo_database,
    create_wms_demo_database,
)

CASES = Path(__file__).resolve().parents[1] / "benchmark" / "bilingual_cases.json"
SCENARIOS = {
    "supplier_on_time_rate", "purchase_unpaid_balance",
    "cross_db_order_progress", "latest_stock_and_unit",
}


class BilingualCasesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = json.loads(CASES.read_text(encoding="utf-8"))
        cls.tmpdir = tempfile.TemporaryDirectory()
        root = Path(cls.tmpdir.name)
        cls.conn = sqlite3.connect(":memory:")
        builders = {
            "erp": create_erp_demo_database, "pm": create_pm_demo_database,
            "mes": create_mes_demo_database, "wms": create_wms_demo_database,
        }
        for alias, build in builders.items():
            path = build(root / f"{alias}_demo.db")
            cls.conn.execute(f"ATTACH DATABASE '{path}' AS {alias}")

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmpdir.cleanup()

    def test_each_scenario_is_asked_once_in_each_language(self):
        by_scenario = defaultdict(dict)
        for item in self.items:
            by_scenario[item["scenario"]][item["language"]] = item
        self.assertEqual(set(by_scenario), SCENARIOS)
        for scenario, pair in by_scenario.items():
            self.assertEqual(set(pair), {"zh", "en"}, scenario)
            # 同一个问题的两种问法，答案必须是同一个
            self.assertEqual(pair["zh"]["gold_sql"], pair["en"]["gold_sql"], scenario)
            self.assertEqual(pair["zh"]["gold_columns"], pair["en"]["gold_columns"], scenario)
            self.assertTrue(any("一" <= char <= "鿿" for char in pair["zh"]["query"]), scenario)
            self.assertTrue(pair["en"]["query"].isascii(), scenario)
        self.assertEqual(len({item["id"] for item in self.items}), len(self.items))

    def test_gold_sql_returns_rows_without_duplicates(self):
        for item in self.items:
            rows = [tuple(row) for row in self.conn.execute(item["gold_sql"])]
            self.assertGreater(len(rows), 0, item["id"])
            self.assertEqual(len(rows), len(set(rows)), item["id"])
            self.assertTrue(item["gold_columns"], item["id"])

    def test_cross_database_gold_ends_in_one_database(self):
        # 跨库查询的结果只来自最后一步所在的库：gold 的输出列不能横跨两个库，
        # 否则任何执行方式都对不上（评测比的是最后一步的结果集）
        item = next(i for i in self.items if i["scenario"] == "cross_db_order_progress" and i["language"] == "zh")
        select_list = item["gold_sql"].split(" FROM ")[0]
        self.assertIn("w.", select_list)
        self.assertNotIn("s.", select_list)

    def test_every_question_is_a_data_request_for_the_rule_router(self):
        context = ConversationMemoryContext(
            short_term=ShortTermContext(session_id="s", user_id="u", messages=[])
        )
        classifier = RuleBasedIntentClassifier()
        for item in self.items:
            decision = classifier.classify(item["query"], context)
            self.assertEqual(decision.route, RouteType.DATABASE_QUERY, item["id"])


if __name__ == "__main__":
    unittest.main()
