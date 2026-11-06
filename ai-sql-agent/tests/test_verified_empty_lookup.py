"""A zero-row execution is healthy only after its semantic check succeeds."""
import unittest

from ai_sql_agent.validation import AnomalyType, ResultValidator, SemanticSelfChecker
from ai_sql_agent.validation.objects import SELF_CHECK_PASSED, SELF_CHECK_NOT_RUN
from test_result_validation import _StubClient


class VerifiedEmptyLookupTest(unittest.TestCase):
    def test_verified_empty_lookup_has_no_artificial_risk(self):
        client = _StubClient("按编号查询订单状态", "结论：一致\n理由：查询条件和字段一致")
        result = ResultValidator(SemanticSelfChecker(client)).validate(
            query="查询 CONTROL-71 的订单状态", sql="SELECT order_no,order_status FROM orders WHERE order_no='CONTROL-71'",
            row_count=0, expected_cardinality="单条")
        self.assertEqual(result.self_check, SELF_CHECK_PASSED)
        self.assertTrue(result.passed)
        self.assertFalse(result.findings)
        self.assertEqual(len(client.prompts), 2)
        self.assertIn("服务的执行元数据", client.prompts[1])

    def test_wrong_scope_zero_rows_remain_blocked(self):
        client = _StubClient("按另一个订单查询", "结论：不一致\n理由：订单编号错误")
        result = ResultValidator(SemanticSelfChecker(client)).validate(
            query="查询 CONTROL-71", sql="SELECT order_status FROM orders WHERE order_no='CONTROL-72'", row_count=0)
        self.assertFalse(result.passed)
        self.assertTrue(result.should_retry)
        self.assertIn(AnomalyType.SEMANTIC_MISMATCH, [f.anomaly for f in result.findings])
        self.assertIn(AnomalyType.EMPTY_RESULT, [f.anomaly for f in result.findings])

    def test_failed_semantic_check_never_proves_empty(self):
        class Broken:
            def generate(self, _):
                raise TimeoutError("stub")
        result = ResultValidator(SemanticSelfChecker(Broken())).validate(query="查询状态", sql="SELECT status FROM orders", row_count=0)
        self.assertEqual(result.self_check, SELF_CHECK_NOT_RUN)
        self.assertFalse(result.passed)
        self.assertIn(AnomalyType.EMPTY_RESULT, [f.anomaly for f in result.findings])

    def test_unavailable_or_disabled_review_retains_empty_warning(self):
        for validator in (ResultValidator(), ResultValidator(enable_self_check=False)):
            result = validator.validate(query="查询状态", sql="SELECT status FROM orders", row_count=0)
            self.assertIn(AnomalyType.EMPTY_RESULT, [f.anomaly for f in result.findings])
