"""
按所有步骤判分（`BenchmarkRunner(score_all_steps=True)`，客服转来的问法用）。

客服拿到的是每一步的结果。客户问「J26-00010 做到哪一步了」常被规划成 订单 → 生产 → 配送，
工序在第 2 步，最后一步查的是配送；只比最后一步就把答对的题记成错（2026-11-06 那一轮的
c02、c03）。默认仍只比最后一步：150 条评测集和门禁不变。
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ai_sql_agent.pipeline.objects import DatabaseConfig, StepExecutionLog
from benchmark.runner import BenchmarkRunner


def _log(database, rows, *, success=True, sql="SELECT 1"):
    return StepExecutionLog(
        database=database, cot_step=None, local_schema="", sql=sql if success else "",
        execution_request={},
        execution_result={"success": success, "rows": rows, "row_count": len(rows)},
    )


class FakePipeline:
    def __init__(self, db_path, logs):
        self.databases = [DatabaseConfig(name="erp_db", db_path=db_path)]
        self.business_metas = {}
        self._logs = logs

    def run(self, query):
        return SimpleNamespace(step_logs=self._logs, degraded=False,
                               retrieved_columns=[], prompt_columns=[])


class ScoreAllStepsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.db = root / "erp.db"
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE orders (order_no TEXT, order_status TEXT)")
            conn.execute("INSERT INTO orders VALUES ('J1', '已签单')")
        self.dataset = root / "cases.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _score(self, logs, *, all_steps, gold="SELECT order_status FROM erp.orders WHERE order_no = 'J1'"):
        self.dataset.write_text(json.dumps([
            {"id": "x1", "category": "customer", "query": "J1 现在什么进度了？", "gold_sql": gold}
        ]), encoding="utf-8")
        runner = BenchmarkRunner(
            pipeline=FakePipeline(self.db, logs), dataset_path=self.dataset,
            access_check=lambda: [], score_all_steps=all_steps,
        )
        try:
            return runner.run().items[0]
        finally:
            runner.gold_executor.close()

    def test_answer_in_an_earlier_step_counts_when_the_last_step_was_held(self):
        # c18 的形状：第 1 步答对，第 2 步 0 行，第 3 步被拦下没执行
        logs = [_log("erp_db", [{"order_no": "J1", "order_status": "已签单"}]),
                _log("prod_db", []),
                _log("svc_db", [], success=False)]
        item = self._score(logs, all_steps=True)
        self.assertTrue(item.correct)
        self.assertEqual(item.matched_step, 1)
        self.assertFalse(self._score(logs, all_steps=False).correct)

    def test_answer_in_an_earlier_step_counts_when_the_last_step_queried_elsewhere(self):
        # c02 的形状：工序在前一步，最后一步查了配送状态
        logs = [_log("erp_db", [{"order_no": "J1", "order_status": "已签单"}]),
                _log("svc_db", [{"order_no": "J1", "delivery_status": "待配送"}])]
        item = self._score(logs, all_steps=True)
        self.assertTrue(item.correct)
        self.assertEqual(item.matched_step, 1)
        self.assertEqual(item.mismatch, "")
        default = self._score(logs, all_steps=False)
        self.assertFalse(default.correct)
        self.assertIsNone(default.matched_step)

    def test_no_step_matching_is_still_wrong(self):
        logs = [_log("erp_db", [{"order_no": "J1", "order_status": "生产中"}]),
                _log("svc_db", [{"order_no": "J1", "delivery_status": "待配送"}])]
        item = self._score(logs, all_steps=True)
        self.assertFalse(item.correct)
        self.assertIsNone(item.matched_step)
        self.assertEqual(item.mismatch, "wrong_values")

    def test_empty_gold_only_looks_at_the_last_step(self):
        # 标准答案是空的：前面某一步碰巧 0 行不能算「对上」
        logs = [_log("prod_db", []),
                _log("svc_db", [{"order_no": "J1", "delivery_status": "待配送"}])]
        item = self._score(logs, all_steps=True,
                           gold="SELECT order_status FROM erp.orders WHERE order_no = 'NOPE'")
        self.assertFalse(item.correct)


if __name__ == "__main__":
    unittest.main()
