import unittest

import httpx

from ai_sql_agent.core import ModelAccessRegistry, ModelTransport, RetryPolicy
from ai_sql_agent.core.circuit import CircuitBreaker, CircuitConfig, CircuitState
from ai_sql_agent.core.errors import (
    ModelAuthError,
    ModelCircuitOpenError,
    ModelConnectionError,
)


class CircuitBreakerTest(unittest.TestCase):
    def setUp(self):
        self.now = {"t": 0.0}
        self.breaker = CircuitBreaker(
            "test",
            CircuitConfig(failure_threshold=3, reset_seconds=30),
            clock=lambda: self.now["t"],
        )

    def test_stays_closed_below_threshold(self):
        for _ in range(2):
            self.breaker.record_failure()
        self.assertIs(self.breaker.state, CircuitState.CLOSED)
        self.assertTrue(self.breaker.allow())

    def test_opens_at_threshold(self):
        for _ in range(3):
            self.breaker.record_failure()
        self.assertIs(self.breaker.state, CircuitState.OPEN)
        self.assertFalse(self.breaker.allow())

    def test_success_resets_the_counter(self):
        self.breaker.record_failure()
        self.breaker.record_failure()
        self.breaker.record_success()
        self.breaker.record_failure()
        self.assertIs(self.breaker.state, CircuitState.CLOSED)

    def test_half_open_after_cooldown_then_closes_on_success(self):
        for _ in range(3):
            self.breaker.record_failure()

        self.now["t"] = 29
        self.assertFalse(self.breaker.allow())

        self.now["t"] = 31
        self.assertTrue(self.breaker.allow())
        self.assertIs(self.breaker.state, CircuitState.HALF_OPEN)
        # 半开时只放一个试探请求，其余继续短路
        self.assertFalse(self.breaker.allow())

        self.breaker.record_success()
        self.assertIs(self.breaker.state, CircuitState.CLOSED)

    def test_half_open_failure_reopens_immediately(self):
        for _ in range(3):
            self.breaker.record_failure()
        self.now["t"] = 31
        self.breaker.allow()
        self.breaker.record_failure()
        self.assertIs(self.breaker.state, CircuitState.OPEN)

    def test_disabled_breaker_always_allows(self):
        breaker = CircuitBreaker("test", CircuitConfig(enabled=False))
        for _ in range(50):
            breaker.record_failure()
        self.assertTrue(breaker.allow())


class TransportCircuitTest(unittest.TestCase):
    def _transport(self, handler, component="unit-test", threshold=3):
        transport = ModelTransport(
            component,
            api_key="k",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            retry_policy=RetryPolicy(max_retries=0, backoff_base_seconds=0),
            sleeper=lambda seconds: None,
            # 各用各的状态表：一个用例里被拒的假 Key 不该挡住别的用例
            access_registry=ModelAccessRegistry(),
        )
        transport.circuit = CircuitBreaker(
            component, CircuitConfig(failure_threshold=threshold, reset_seconds=30)
        )
        return transport

    def test_short_circuits_after_repeated_infra_failures(self):
        calls = {"n": 0}

        def down(request):
            calls["n"] += 1
            raise httpx.ConnectError("refused", request=request)

        transport = self._transport(down)

        for _ in range(3):
            with self.assertRaises(ModelConnectionError):
                transport.post_json("https://model/v1", {})

        # 第 4 次开始不再发起请求，立即失败
        with self.assertRaises(ModelCircuitOpenError):
            transport.post_json("https://model/v1", {})
        self.assertEqual(calls["n"], 3)

    def test_auth_errors_do_not_trip_the_breaker(self):
        """
        确定性错误本来就秒回，熔断只会让改好配置后还要等冷却。

        「被拒之后别再发」不归熔断器管，归模型访问状态表：5 次调用里只有第一次真的发了出去，
        其余在本地短路（短路的错误也是一种 ModelAuthError，上层走同一条降级链路）。
        """
        calls = {"n": 0}

        def unauthorized(request):
            calls["n"] += 1
            return httpx.Response(401, text="bad key")

        transport = self._transport(unauthorized)

        for _ in range(5):
            with self.assertRaises(ModelAuthError):
                transport.post_json("https://model/v1", {})

        self.assertIs(transport.circuit.state, CircuitState.CLOSED)
        self.assertEqual(calls["n"], 1)

    def test_circuit_open_error_is_a_model_call_error(self):
        # 上层的降级链路捕获的是 ModelCallError，熔断必须落进同一个分支
        from ai_sql_agent.core.errors import ModelCallError

        self.assertTrue(issubclass(ModelCircuitOpenError, ModelCallError))
        self.assertFalse(ModelCircuitOpenError("x").retryable)


class CircuitDegradationTest(unittest.TestCase):
    def test_pipeline_degrades_when_circuit_is_open(self):
        from ai_sql_agent.pipeline import Text2SQLPipeline

        pipeline = Text2SQLPipeline()

        def short_circuited(**kwargs):
            raise ModelCircuitOpenError("已熔断", component="thinking")

        pipeline.cot_planner.plan = short_circuited

        result = pipeline.run("查询准时率低于0.9的供应商名称", user_id="li")
        self.assertTrue(result.degraded)


if __name__ == "__main__":
    unittest.main()
