"""软异常校验：基数校验、语义自检、空结果。

这类校验针对的是「SQL 跑通了但答案是错的」——最危险的一类错误，
因为它不会自己暴露。
"""

import unittest

from ai_sql_agent.validation import (
    AnomalyType,
    ResultValidator,
    SemanticSelfChecker,
    check_cardinality,
    parse_expectation,
)


class CardinalityParseTest(unittest.TestCase):
    def test_parses_natural_language_scales(self):
        self.assertEqual((parse_expectation("单条").low, parse_expectation("单条").high), (1, 1))
        self.assertEqual(parse_expectation("数条").high, 20)
        self.assertGreater(parse_expectation("数百条").high, parse_expectation("数十条").high)

    def test_parses_explicit_numbers_with_tolerance(self):
        expectation = parse_expectation("约20条")
        self.assertTrue(expectation.contains(20))
        self.assertTrue(expectation.contains(60))   # 放宽一档，不当精确值
        self.assertFalse(expectation.contains(500))

    def test_parses_range(self):
        expectation = parse_expectation("10-50 条")
        self.assertTrue(expectation.contains(30))
        self.assertFalse(expectation.contains(80))

    def test_unparseable_returns_none(self):
        self.assertIsNone(parse_expectation(""))
        self.assertIsNone(parse_expectation("说不好"))


class CardinalityCheckTest(unittest.TestCase):
    def test_join_fanout_blocks(self):
        """行数远超预期且含 JOIN → 疑似扇出重复计数，阻断并回溯。"""
        finding = check_cardinality(
            "数条", 500, sql="SELECT a.x, SUM(b.y) FROM a JOIN b ON a.id=b.id GROUP BY 1"
        )
        self.assertIsNotNone(finding)
        self.assertEqual(finding.anomaly, AnomalyType.CARDINALITY_DEVIATION)
        self.assertEqual(finding.severity, "block")

    def test_overflow_without_join_only_warns(self):
        finding = check_cardinality("数条", 60, sql="SELECT x FROM a")
        self.assertIsNotNone(finding)
        self.assertEqual(finding.severity, "warn")

    def test_within_expectation_passes(self):
        self.assertIsNone(check_cardinality("数条", 5, sql="SELECT x FROM a"))

    def test_marginal_overflow_is_not_flagged(self):
        """
        量级标签是模型随手给的估计，21 行 vs 上界 20 行说明不了任何问题。
        报了只会让提示变噪音，而提示一旦被忽略，真正的静默错误也跟着漏过去。
        """
        self.assertIsNone(check_cardinality("数条", 25, sql="SELECT x FROM a"))

    def test_fewer_rows_than_expected_is_not_flagged(self):
        """行数偏少更可能是数据本身少，交给空结果检查，不在这里报。"""
        self.assertIsNone(check_cardinality("数百条", 3, sql="SELECT x FROM a"))


class _StubClient:
    """按调用顺序返回预设响应：先复述，再判一致性。"""

    def __init__(self, paraphrase: str, verdict: str):
        self.responses = [f"复述：{paraphrase}", verdict]
        self.prompts = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.responses.pop(0)


class SemanticSelfCheckTest(unittest.TestCase):
    def test_detects_mismatch(self):
        client = _StubClient(
            "统计了订单的数量",
            "结论：不一致\n理由：问的是金额，SQL 统计的是数量",
        )
        finding, paraphrase = SemanticSelfChecker(client).check(
            "订单总金额是多少", "SELECT COUNT(*) FROM sales_order"
        )
        self.assertIsNotNone(finding)
        self.assertEqual(finding.anomaly, AnomalyType.SEMANTIC_MISMATCH)
        self.assertEqual(finding.severity, "block")
        self.assertIn("数量", paraphrase)

    def test_consistent_passes(self):
        client = _StubClient("汇总了订单的金额", "结论：一致\n理由：口径相符")
        finding, paraphrase = SemanticSelfChecker(client).check(
            "订单总金额是多少", "SELECT SUM(order_amt) FROM sales_order"
        )
        self.assertIsNone(finding)
        self.assertTrue(paraphrase)

    def test_paraphrase_prompt_does_not_leak_the_question(self):
        """第一步只让模型复述 SQL，不能把问题给它，否则它会顺着问题脑补。"""
        client = _StubClient("汇总了金额", "结论：一致\n理由：ok")
        SemanticSelfChecker(client).check("订单总金额是多少", "SELECT SUM(order_amt) FROM t")
        self.assertNotIn("订单总金额是多少", client.prompts[0])

    def test_model_failure_does_not_break_pipeline(self):
        class Broken:
            def generate(self, prompt): raise RuntimeError("模型挂了")

        finding, paraphrase = SemanticSelfChecker(Broken()).check("问题", "SELECT 1")
        self.assertIsNone(finding)
        self.assertIsNone(paraphrase)

    def test_unavailable_client_is_noop(self):
        self.assertFalse(SemanticSelfChecker(None).available)

    def test_declared_units_are_given_to_the_reviewer_as_facts(self):
        """
        审阅者只看得到 SQL 的复述，看不到数据字典。问「PO-24-0103 以美元计的采购额」时，
        SQL 里没有任何币种字样（金额列存的本来就是美元），它就判成「漏了币种条件」——
        实测同一个问题三次里有一次因此被标成需要核实，而答案是对的。
        """
        client = _StubClient("输出了采购金额与已付金额之和", "结论：一致\n理由：ok")
        SemanticSelfChecker(client).check(
            "What are the purchase total and payments in USD for PO-24-0103?",
            "SELECT po_amt, SUM(paid_amt) AS total_paid_amt FROM purchase_order",
            known_units={"po_amt": "USD", "total_paid_amt": "USD"},
        )
        compare_prompt = client.prompts[1]
        self.assertIn("po_amt=USD、total_paid_amt=USD", compare_prompt)
        self.assertIn("不需要再按单位或币种筛选、换算", compare_prompt)
        # 要的是别的单位而 SQL 没换算，仍然要报：这条事实不是「币种问题一律放过」
        self.assertIn("别的**单位而 SQL 没有换算才算", compare_prompt)
        # 复述那一步仍然只看 SQL
        self.assertNotIn("USD", client.prompts[0])

    def test_prompts_are_unchanged_when_no_unit_is_declared(self):
        """没有单位可说时提示词逐字不变：这条改动不该碰到别的问题的判定。"""
        from ai_sql_agent.validation.self_check import _COMPARE_PROMPT, _MULTI_STEP_COMPARE_PROMPT

        plain = _StubClient("汇总了金额", "结论：一致\n理由：ok")
        SemanticSelfChecker(plain).check("订单总金额是多少", "SELECT SUM(order_amt) FROM t")
        self.assertNotIn("已知事实", plain.prompts[1])
        self.assertIn("SQL 的实际行为：汇总了金额\n\n输出格式", plain.prompts[1])
        self.assertEqual(
            plain.prompts[1],
            _COMPARE_PROMPT.format(query="订单总金额是多少", paraphrase="汇总了金额", facts=""),
        )

        multi = _StubClient("按工程汇总了采购额", "结论：一致\n理由：ok")
        SemanticSelfChecker(multi).check(
            "超预算项目的采购额", "SELECT job_no, SUM(po_amt) FROM purchase_order GROUP BY job_no",
            preceding_summary="第 1 步筛出了超预算项目", known_units={},
        )
        self.assertNotIn("已知事实", multi.prompts[1])
        self.assertIn("最后一步 SQL 的实际行为：按工程汇总了采购额\n\n输出格式", multi.prompts[1])
        self.assertEqual(
            multi.prompts[1],
            _MULTI_STEP_COMPARE_PROMPT.format(
                query="超预算项目的采购额", preceding="第 1 步筛出了超预算项目",
                paraphrase="按工程汇总了采购额", facts="",
            ),
        )

    def test_validator_passes_units_through_to_the_self_check(self):
        client = _StubClient("输出了金额", "结论：一致\n理由：ok")
        validator = ResultValidator(self_checker=SemanticSelfChecker(client))
        validator.validate(
            query="amount in USD", sql="SELECT po_amt FROM purchase_order", row_count=1,
            column_units={"po_amt": "USD"},
        )
        self.assertIn("po_amt=USD", client.prompts[1])


class ResultValidatorTest(unittest.TestCase):
    def test_empty_result_warns_but_does_not_block(self):
        outcome = ResultValidator().validate(
            query="x", sql="SELECT 1", row_count=0, expected_cardinality="数条"
        )
        self.assertEqual(outcome.findings[0].anomaly, AnomalyType.EMPTY_RESULT)
        self.assertTrue(outcome.passed)
        self.assertFalse(outcome.should_retry)

    def test_fanout_blocks_and_requests_retry(self):
        outcome = ResultValidator().validate(
            query="x",
            sql="SELECT a.x, SUM(b.y) FROM a JOIN b ON a.id=b.id GROUP BY 1",
            row_count=800,
            expected_cardinality="数条",
        )
        self.assertFalse(outcome.passed)
        self.assertTrue(outcome.should_retry)

    def test_self_check_only_runs_on_final_step(self):
        client = _StubClient("随便", "结论：不一致\n理由：不该被调用")
        validator = ResultValidator(self_checker=SemanticSelfChecker(client))
        outcome = validator.validate(
            query="x", sql="SELECT 1", row_count=3,
            expected_cardinality="数条", is_final_step=False,
        )
        self.assertTrue(outcome.passed)
        self.assertEqual(client.prompts, [])

    def test_disabled_validation_reports_nothing(self):
        validator = ResultValidator(enable_cardinality=False, enable_self_check=False)
        outcome = validator.validate(
            query="x",
            sql="SELECT a FROM a JOIN b ON 1=1",
            row_count=9999,
            expected_cardinality="单条",
        )
        self.assertTrue(outcome.passed)


if __name__ == "__main__":
    unittest.main()


class RiskFindingsReachTheUserTest(unittest.TestCase):
    """
    软异常必须回传给调用方，否则「有提示」只是服务端日志里的事。

    静默错误率的口径是「答错且用户没收到任何提示」。如果校验结论只进日志、
    不进响应，那它对用户来说依然是静默的，指标就变成自我安慰。
    """

    def test_response_model_carries_risk_findings(self):
        from ai_sql_agent.api.app import QueryResponse, StepLogResponse

        step = StepLogResponse(
            database="erp_db",
            sql="SELECT 1",
            final_sql="SELECT 1",
            success=True,
            row_count=0,
            risk_findings=[{"anomaly": "empty_result", "detail": "返回 0 行", "severity": "warn"}],
        )
        response = QueryResponse(
            route="database_query",
            confidence=1.0,
            reason="",
            answer="",
            queried_database=True,
            step_logs=[step],
            has_risk=True,
        )
        payload = response.model_dump()
        self.assertTrue(payload["has_risk"])
        self.assertEqual(payload["step_logs"][0]["risk_findings"][0]["anomaly"], "empty_result")

    def test_frontend_renders_the_findings(self):
        """界面不渲染等于没提示，这里直接检查页面读了这两个字段、并且有对应的提示文案。"""
        from pathlib import Path

        static = Path(__file__).resolve().parents[1] / "src" / "ai_sql_agent" / "api" / "static" / "js"
        model = (static / "model.js").read_text(encoding="utf-8")
        self.assertIn("risk_findings", model)
        self.assertIn("has_risk", model)

        # 提示默认可见：它在结果卡片里，不在折叠起来的「查询过程」里
        result = (static / "result.js").read_text(encoding="utf-8")
        self.assertIn("notice.review.title", result)
        for language in ("en", "zh"):
            dictionary = (static / "i18n" / f"{language}.js").read_text(encoding="utf-8")
            self.assertIn("'notice.review.title'", dictionary)
