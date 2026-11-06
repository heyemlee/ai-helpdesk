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


def _log(database, rows, *, success=True, sql="SELECT 1", findings=()):
    return StepExecutionLog(
        database=database, cot_step=None, local_schema="", sql=sql if success else "",
        execution_request={},
        execution_result={"success": success, "rows": rows, "row_count": len(rows)},
        validation={"findings": [{"anomaly": name} for name in findings]},
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
        logs = [_log("erp_db", [{"order_no": "J1", "order_status": "已签单"}],
                     sql="SELECT order_status FROM orders"),
                _log("svc_db", [{"order_no": "J1", "delivery_status": "待配送"}],
                     sql="SELECT delivery_status FROM delivery")]
        item = self._score(logs, all_steps=True)
        self.assertTrue(item.correct)
        self.assertEqual(item.matched_step, 1)
        self.assertEqual(item.mismatch, "")
        # 报告里给的是对上的那一步的 SQL，不是最后一步的
        self.assertEqual(item.predicted_sql, "SELECT order_status FROM orders")
        default = self._score(logs, all_steps=False)
        self.assertFalse(default.correct)
        self.assertIsNone(default.matched_step)

    def test_earlier_match_in_a_flagged_run_is_not_counted(self):
        """
        前面某一步对上了，但这次结果带风险提示（has_risk）：客服的可信校验整份不用，
        客户其实没拿到答案。不能算对，记 unusable_match；错因照常说最后一步。
        """
        for last, mismatch in (
            (_log("svc_db", [], success=False), ""),  # 最后一步没执行：没有可比的结果
            (_log("svc_db", [{"order_no": "J1", "delivery_status": "待配送"}]), "wrong_values"),
        ):
            logs = [_log("erp_db", [{"order_no": "J1", "order_status": "已签单"}]),
                    _log("prod_db", [], findings=["empty_result"]),
                    last]
            item = self._score(logs, all_steps=True)
            self.assertFalse(item.correct)
            self.assertTrue(item.unusable_match)
            self.assertEqual(item.mismatch, mismatch)
            self.assertEqual(item.matched_step, 1)

    def test_earlier_match_cut_off_from_the_answer_preview_is_not_counted(self):
        """
        客服看到的是 answer 里那段截到约 1500 字的结果 JSON，不是 steps[] 的完整行。
        第 1 步行很多，把对上的第 2 步挤出了截断点：客服没看到，不能算对。
        """
        big = [{"order_no": f"J{n:05d}", "note": "x" * 40} for n in range(60)]
        logs = [_log("erp_db", big),
                _log("prod_db", [{"order_no": "J1", "order_status": "已签单"}]),
                _log("svc_db", [{"order_no": "J1", "delivery_status": "待配送"}])]
        item = self._score(logs, all_steps=True)
        self.assertFalse(item.correct)
        self.assertTrue(item.unusable_match)
        self.assertEqual(item.matched_step, 2)
        # 客服读到的是最后一步的错值、没有风险提示：照常算「取值不对」的静默错误
        self.assertEqual(item.mismatch, "wrong_values")
        self.assertTrue(item.silent_error)
        # 第 1 步小的时候同一份结果照样算对
        logs[0] = _log("erp_db", big[:1])
        self.assertTrue(self._score(logs, all_steps=True).correct)

    def test_answer_preview_keeps_its_limit(self):
        from ai_sql_agent.pipeline.conversation_service import (
            ANSWER_PREVIEW_CHARS,
            answer_result_preview,
        )

        short = [_log("erp_db", [{"a": 1}])]
        self.assertEqual(answer_result_preview(short), json.dumps([short[0].execution_result]))
        long = [_log("erp_db", [{"a": "x" * 3000}])]
        preview = answer_result_preview(long)
        self.assertEqual(len(preview), ANSWER_PREVIEW_CHARS - 10 + len("……"))
        self.assertTrue(preview.endswith("……"))

    def test_conflicting_value_in_another_step_cancels_the_earlier_match(self):
        """
        第 1 步「已签单」对上了，最后一步对同一个字段又说「生产中」：客服可能照着后者说，
        这是静默答错，不能被前面的对上抹掉。
        """
        logs = [_log("erp_db", [{"order_no": "J1", "order_status": "已签单"}]),
                _log("prod_db", [{"order_no": "J1", "order_status": "生产中"}])]
        item = self._score(logs, all_steps=True)
        self.assertFalse(item.correct)
        self.assertTrue(item.unusable_match)
        self.assertEqual(item.mismatch, "wrong_values")
        self.assertTrue(item.silent_error)

    def test_contradiction_under_an_alias_is_caught_too(self):
        """判分不看列名：两步都把状态叫「状态」，矛盾照样要认出来。"""
        logs = [_log("erp_db", [{"order_no": "J1", "状态": "已签单"}]),
                _log("prod_db", [{"order_no": "J1", "状态": "生产中"}])]
        item = self._score(logs, all_steps=True)
        self.assertFalse(item.correct)
        self.assertTrue(item.unusable_match)
        # 同一个单号出现在别的步骤里不算矛盾（标识列不是答案字段）
        logs = [_log("erp_db", [{"order_no": "J1", "状态": "已签单"}]),
                _log("svc_db", [{"order_no": "J1", "配送": "待配送"}])]
        self.assertTrue(self._score(logs, all_steps=True).correct)

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
