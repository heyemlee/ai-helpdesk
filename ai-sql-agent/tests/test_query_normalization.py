"""问题归一化：代词消解 + 相对时间表达式解析。"""

import unittest
from datetime import date

from ai_sql_agent.normalization import (
    CoreferenceResolver,
    FiscalConfig,
    QueryNormalizer,
    RelativeTimeParser,
)

TODAY = date(2024, 5, 15)


class RelativeTimeParserTest(unittest.TestCase):
    def setUp(self):
        self.parser = RelativeTimeParser()

    def _one(self, text):
        ranges = self.parser.parse(text, TODAY)
        self.assertTrue(ranges, f"未解析出区间：{text}")
        return ranges[0]

    def test_last_quarter(self):
        r = self._one("上季度哪些项目超预算了")
        self.assertEqual((r.start, r.end), (date(2024, 1, 1), date(2024, 3, 31)))

    def test_current_quarter(self):
        r = self._one("本季度的采购金额")
        self.assertEqual((r.start, r.end), (date(2024, 4, 1), date(2024, 6, 30)))

    def test_recent_n_months(self):
        r = self._one("近三个月的采购金额")
        self.assertEqual((r.start, r.end), (date(2024, 3, 1), TODAY))

    def test_recent_n_days_with_digits(self):
        r = self._one("最近30天的工单")
        self.assertEqual((r.start, r.end), (date(2024, 4, 16), TODAY))

    def test_last_month(self):
        r = self._one("上月完工的工单")
        self.assertEqual((r.start, r.end), (date(2024, 4, 1), date(2024, 4, 30)))

    def test_last_year(self):
        r = self._one("去年的合同额")
        self.assertEqual((r.start, r.end), (date(2023, 1, 1), date(2023, 12, 31)))

    def test_fiscal_year_shifts_quarter_boundaries(self):
        """财年 4 月起时，5 月属于第一财季，上季度应是 1-3 月。"""
        parser = RelativeTimeParser(FiscalConfig(fiscal_year_start_month=4))
        r = parser.parse("上季度", TODAY)[0]
        self.assertEqual((r.start, r.end), (date(2024, 1, 1), date(2024, 3, 31)))

    def test_fiscal_year_shifts_year_boundaries(self):
        parser = RelativeTimeParser(FiscalConfig(fiscal_year_start_month=4))
        r = parser.parse("今年", TODAY)[0]
        self.assertEqual((r.start, r.end), (date(2024, 4, 1), date(2025, 3, 31)))

    def test_no_time_expression_returns_empty(self):
        self.assertEqual(self.parser.parse("查询超预算的项目", TODAY), [])

    def test_invalid_fiscal_month_rejected(self):
        with self.assertRaises(ValueError):
            FiscalConfig(fiscal_year_start_month=13)


class CoreferenceResolverTest(unittest.TestCase):
    def setUp(self):
        self.resolver = CoreferenceResolver()

    def test_resolves_pronoun_from_previous_query(self):
        resolved, notes = self.resolver.resolve(
            "它们的采购金额是多少", ["查询实际成本超过预算的项目名称"]
        )
        self.assertIn("实际成本超过预算的项目", resolved)
        self.assertNotIn("它们", resolved)
        self.assertEqual(len(notes), 1)

    def test_no_history_keeps_query_unchanged(self):
        resolved, notes = self.resolver.resolve("它们的采购金额", [])
        self.assertEqual(resolved, "它们的采购金额")
        self.assertEqual(notes, [])

    def test_no_pronoun_keeps_query_unchanged(self):
        resolved, notes = self.resolver.resolve("查询超预算的项目", ["上一个问题"])
        self.assertEqual(resolved, "查询超预算的项目")
        self.assertEqual(notes, [])

    def test_unextractable_history_leaves_query_alone(self):
        """抽不出主体时保持原样——改错比不改危害大。"""
        resolved, _ = self.resolver.resolve("它们的金额", ["嗯", "好的"])
        self.assertEqual(resolved, "它们的金额")


class QueryNormalizerTest(unittest.TestCase):
    def test_combines_coreference_and_time(self):
        normalizer = QueryNormalizer()
        result = normalizer.normalize(
            "它们上季度的采购金额",
            history_queries=["查询实际成本超过预算的项目名称"],
            today=TODAY,
        )
        self.assertIn("实际成本超过预算的项目", result.normalized_query)
        self.assertIn("2024-01-01", result.normalized_query)
        self.assertTrue(result.changed)
        self.assertEqual(len(result.resolved_references), 1)
        self.assertEqual(len(result.date_ranges), 1)

    def test_unchanged_query_is_marked(self):
        result = QueryNormalizer().normalize("查询超预算的项目", today=TODAY)
        self.assertFalse(result.changed)

    def test_disabled_normalization_is_passthrough(self):
        normalizer = QueryNormalizer(
            enable_coreference=False, enable_time_parsing=False
        )
        result = normalizer.normalize(
            "它们上季度的金额", history_queries=["查询超预算的项目名称"], today=TODAY
        )
        self.assertEqual(result.normalized_query, "它们上季度的金额")

    def test_to_dict_is_serializable(self):
        result = QueryNormalizer().normalize("上季度的采购金额", today=TODAY)
        payload = result.to_dict()
        self.assertIn("date_ranges", payload)
        self.assertEqual(payload["date_ranges"][0]["start"], "2024-01-01")


if __name__ == "__main__":
    unittest.main()
