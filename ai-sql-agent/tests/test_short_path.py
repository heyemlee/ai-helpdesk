"""短路径：简单查询跳过独立规划调用。"""

import unittest

from ai_sql_agent.pipeline.short_path import decide_short_path


class ShortPathTest(unittest.TestCase):
    def test_simple_single_table_query_uses_short_path(self):
        decision = decide_short_path(
            "查询准时率低于0.8的供应商", selected_tables={"erp_db": ["supplier"]}
        )
        self.assertTrue(decision.use_short_path)

    def test_multi_database_falls_back_to_planning(self):
        decision = decide_short_path(
            "查询超预算项目的采购金额",
            selected_tables={"pm_db": ["project"], "erp_db": ["purchase_order"]},
        )
        self.assertFalse(decision.use_short_path)
        self.assertIn("库", decision.reason)

    def test_aggregate_comparison_falls_back(self):
        """聚合后过滤是典型多步问题，必须走完整规划。"""
        decision = decide_short_path(
            "查询采购金额超过平均值的供应商", selected_tables={"erp_db": ["purchase_order"]}
        )
        self.assertFalse(decision.use_short_path)

    def test_many_candidate_tables_falls_back(self):
        decision = decide_short_path(
            "查询订单",
            selected_tables={"erp_db": ["a", "b", "c", "d", "e"]},
        )
        self.assertFalse(decision.use_short_path)

    def test_disabled_flag_forces_full_path(self):
        decision = decide_short_path(
            "查询准时率低于0.8的供应商",
            selected_tables={"erp_db": ["supplier"]},
            enabled=False,
        )
        self.assertFalse(decision.use_short_path)

    def test_unrecognized_query_is_conservative(self):
        """无法判定为简单查询时保守走完整链路，宁可多花一次调用。"""
        decision = decide_short_path("嗯", selected_tables={"erp_db": ["supplier"]})
        self.assertFalse(decision.use_short_path)


if __name__ == "__main__":
    unittest.main()
