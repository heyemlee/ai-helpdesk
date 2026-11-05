"""
模型凭证不可用之后，别再往外发请求。

2026-10-04 的事故：模型 Key 在一次批量评测中被服务端停用（401）。单次调用本来就不重试
鉴权失败，但没有任何地方记住这把 Key 已经被拒——之后每个问题照样让路由、抽词、规划各调
一次，评测脚本逐题照跑，两分半里又发出了约 650 次注定被拒的请求。

这组用例锁的是修完之后的行为。**全部走 httpx.MockTransport 和临时目录，不发任何真实请求**：

1. 第一次被明确拒绝之后，同一组「服务地址 + 凭证」不再出网——不管是哪个组件来调；
2. 不同的地址、不同的凭证互不影响；状态里、日志里、异常里都没有明文 Key；
3. 永久与临时分得开：普通 429 有限退避并遵循 Retry-After，到点自己恢复；
   写明额度耗尽才停；403 看它说的是什么，不因为一个模型被拒就停掉别的模型；
4. 鉴权失败不记成熔断器的「成功」；
5. 并发时，拒绝记下之后不再有新的请求发出（已经发出去的收不回来，用例里把这一点量出来）；
6. 整条链路：一个问题只发出一次被拒的调用，后面的阶段全部在本地短路；
   修复阶段遇到模型不可用是降级，不是 500；自检没做成会留下标记；
7. 评测入口遇到凭证不可用立即停，留下已完成的部分并标 incomplete，退出码非零，不出门禁结论；
   某一题拿到的是降级答复（服务端报错，状态表里没有记录）也一样停，不把断供记成答错；
8. 状态接口只展示已知故障，不探测；复位只走已有的管理员入口。
"""
import contextlib
import io
import json
import logging
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import httpx

from ai_sql_agent.core import (
    CIRCUIT_REGISTRY,
    MODEL_ACCESS,
    ModelAccessRegistry,
    ModelAuthError,
    ModelCallError,
    ModelCredentialUnavailableError,
    ModelQuotaExhaustedError,
    ModelRateLimitError,
    ModelTransport,
    RetryPolicy,
    reload_settings,
)
from ai_sql_agent.core import transport as transport_module
from ai_sql_agent.core.circuit import CircuitBreaker, CircuitConfig, CircuitState
from ai_sql_agent.core.model_access import (
    AMBIGUOUS_FORBIDDEN_HOLD_SECONDS,
    MAX_HOLD_SECONDS,
    AccessKey,
    credential_fingerprint,
)

URL = "https://models.example.test/v1/messages"
SECRET = "sk-unit-test-SECRET-0123456789abcdef"
DISABLED = {"error": "Invalid API key", "message": "API key is disabled"}


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Service:
    """一个假的模型服务：记下每一次真的「出网」的请求，按脚本应答。"""

    def __init__(self, respond):
        self.respond = respond
        self.requests = []
        self._lock = threading.Lock()

    def __call__(self, request):
        with self._lock:
            self.requests.append(request)
            index = len(self.requests)
        return self.respond(request, index)

    @property
    def calls(self):
        return len(self.requests)

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self))


def ok(_request=None, _index=0):
    return httpx.Response(200, json={"choices": [{"message": {"content": "SELECT 1;"}}]})


def transport(service, *, registry, component="coder", api_key=SECRET, provider="openai",
              max_retries=2, sleeps=None, backoff_max=8.0):
    return ModelTransport(
        component,
        api_key=api_key,
        provider=provider,
        client=service.client(),
        retry_policy=RetryPolicy(
            max_retries=max_retries, backoff_base_seconds=0, backoff_max_seconds=backoff_max
        ),
        sleeper=(sleeps.append if sleeps is not None else (lambda seconds: None)),
        access_registry=registry,
    )


class FirstRejectionStopsEverythingTest(unittest.TestCase):
    def setUp(self):
        self.registry = ModelAccessRegistry()
        self.service = Service(lambda request, index: httpx.Response(401, json=DISABLED))

    def test_after_one_rejection_no_component_reaches_the_network_again(self):
        components = ["thinking", "keyword_extractor", "thinking", "coder", "thinking", "coder"]
        transports = {name: transport(self.service, registry=self.registry, component=name)
                      for name in set(components)}

        with self.assertRaises(ModelAuthError) as first:
            transports["thinking"].post_json(URL, {}, model="m-1")
        self.assertNotIsInstance(first.exception, ModelCredentialUnavailableError)
        self.assertTrue(first.exception.access_unavailable)

        for name in components:
            with self.assertRaises(ModelCredentialUnavailableError) as later:
                transports[name].post_json(URL, {}, model="m-1")
            # 上层按 ModelCallError / ModelAuthError 走同一条降级链路
            self.assertIsInstance(later.exception, ModelCallError)
            self.assertTrue(later.exception.access_unavailable)
            self.assertFalse(later.exception.retryable)

        self.assertEqual(self.service.calls, 1)
        (block,) = self.registry.permanent_blocks()
        self.assertEqual((block["scope"], block["reason"]), ("credential", "credential_rejected"))
        self.assertEqual(block["short_circuited_calls"], len(components))

    def test_streaming_calls_are_covered_too(self):
        client = transport(self.service, registry=self.registry, component="thinking")
        with self.assertRaises(ModelAuthError):
            list(client.stream_sse(URL, {}, model="m-1"))
        with self.assertRaises(ModelCredentialUnavailableError):
            client.post_json(URL, {}, model="m-1")
        with self.assertRaises(ModelCredentialUnavailableError):
            list(client.stream_sse(URL, {}, model="m-1"))
        self.assertEqual(self.service.calls, 1)

    def test_a_retry_that_is_already_waiting_does_not_resend_once_the_key_is_known_bad(self):
        """一个调用正在退避等重试，另一个调用这时确认了凭证被拒：等的那个醒来不该再发。"""
        registry = self.registry
        service = Service(lambda request, index: httpx.Response(500, text="boom"))
        key = AccessKey.build(URL, "openai", SECRET)

        def while_waiting(_seconds):
            registry.block(key, scope="credential", reason="credential_rejected")

        client = ModelTransport(
            "coder", api_key=SECRET, client=service.client(),
            retry_policy=RetryPolicy(max_retries=3, backoff_base_seconds=0),
            sleeper=while_waiting, access_registry=registry,
        )
        with self.assertRaises(ModelCredentialUnavailableError):
            client.post_json(URL, {}, model="m-1")
        self.assertEqual(service.calls, 1)

    def test_reset_is_the_only_way_back_and_it_does_not_probe(self):
        client = transport(self.service, registry=self.registry)
        with self.assertRaises(ModelAuthError):
            client.post_json(URL, {}, model="m-1")
        for _ in range(3):
            with self.assertRaises(ModelCredentialUnavailableError):
                client.post_json(URL, {}, model="m-1")
        self.assertEqual(self.service.calls, 1)

        self.assertEqual(self.registry.reset_all(), 1)
        self.assertEqual(self.service.calls, 1, "复位本身不发请求")

        # 复位之后是下一个真实请求自己去试；还是被拒就重新记下
        with self.assertRaises(ModelAuthError) as again:
            client.post_json(URL, {}, model="m-1")
        self.assertNotIsInstance(again.exception, ModelCredentialUnavailableError)
        self.assertEqual(self.service.calls, 2)
        with self.assertRaises(ModelCredentialUnavailableError):
            client.post_json(URL, {}, model="m-1")
        self.assertEqual(self.service.calls, 2)


class IsolationTest(unittest.TestCase):
    def setUp(self):
        self.registry = ModelAccessRegistry()

        def respond(request, index):
            token = request.headers.get("authorization", "") + request.headers.get("x-api-key", "")
            # 只有「默认端口上的这个服务 + 这把 Key」会被拒；换端口就是另一个服务
            if SECRET in token and request.url.host == "models.example.test" and request.url.port is None:
                return httpx.Response(401, json=DISABLED)
            return ok()

        self.service = Service(respond)

    def reject_the_first_configuration(self):
        with self.assertRaises(ModelAuthError):
            transport(self.service, registry=self.registry).post_json(URL, {}, model="m-1")

    def test_another_credential_on_the_same_service_is_untouched(self):
        self.reject_the_first_configuration()
        other = transport(self.service, registry=self.registry, api_key="sk-another-key-123456")
        self.assertIn("choices", other.post_json(URL, {}, model="m-1"))

    def test_the_same_credential_on_another_service_is_untouched(self):
        self.reject_the_first_configuration()
        same_key = transport(self.service, registry=self.registry)
        self.assertIn("choices", same_key.post_json("https://other.example.test/v1/messages", {}, model="m-1"))
        self.assertIn("choices", same_key.post_json("https://models.example.test:8443/v1/messages", {}, model="m-1"))

    def test_another_path_on_the_same_service_shares_the_state(self):
        self.reject_the_first_configuration()
        before = self.service.calls
        with self.assertRaises(ModelCredentialUnavailableError):
            transport(self.service, registry=self.registry).post_json(
                "https://MODELS.example.test/v1/chat/completions", {}, model="another-model"
            )
        self.assertEqual(self.service.calls, before)

    def test_another_protocol_is_a_different_configuration(self):
        self.reject_the_first_configuration()
        anthropic = transport(self.service, registry=self.registry, provider="anthropic")
        # 这个假服务照样会拒它（Key 相同），但那是它自己发出去问到的，不是被别人的状态挡住的
        with self.assertRaises(ModelAuthError) as raised:
            anthropic.post_json(URL, {}, model="m-1")
        self.assertNotIsInstance(raised.exception, ModelCredentialUnavailableError)

    def test_separate_registries_do_not_see_each_other(self):
        self.reject_the_first_configuration()
        fresh = transport(self.service, registry=ModelAccessRegistry())
        with self.assertRaises(ModelAuthError) as raised:
            fresh.post_json(URL, {}, model="m-1")
        self.assertNotIsInstance(raised.exception, ModelCredentialUnavailableError)


class NoPlaintextKeyTest(unittest.TestCase):
    def test_the_key_never_appears_in_state_errors_or_logs(self):
        registry = ModelAccessRegistry()
        # 有的服务会把 Key 原样写回报错里
        service = Service(lambda request, index: httpx.Response(
            401, json={"error": {"message": f"Incorrect API key provided: {SECRET}. Bearer {SECRET}"}}
        ))
        client = transport(service, registry=registry)

        with self.assertLogs("ai_sql.model", level=logging.DEBUG) as captured:
            with self.assertRaises(ModelAuthError) as first:
                client.post_json(URL, {}, model="m-1")
            with self.assertRaises(ModelCredentialUnavailableError) as later:
                client.post_json(URL, {}, model="m-1")

        everything = "\n".join(
            [str(first.exception), repr(first.exception), str(later.exception)]
            + [record.getMessage() for record in captured.records]
            + [json.dumps(registry.snapshot(), ensure_ascii=False)]
        )
        self.assertNotIn(SECRET, everything)
        self.assertNotIn(SECRET[:16], everything)
        self.assertNotIn("models.example.test", json.dumps(registry.snapshot()))
        self.assertIn("<redacted>", str(first.exception))

    def test_fingerprints_are_opaque_and_stable_within_the_process(self):
        one, same, other = (credential_fingerprint(value) for value in (SECRET, SECRET, SECRET + "x"))
        self.assertEqual(one, same)
        self.assertNotEqual(one, other)
        self.assertNotIn(SECRET[3:12], one)
        self.assertLessEqual(len(one), 16)


class PermanentVersusTemporaryTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.registry = ModelAccessRegistry(clock=self.clock)

    # ---- 429 ----

    def test_plain_rate_limit_backs_off_as_told_and_then_succeeds(self):
        service = Service(lambda request, index: (
            httpx.Response(429, headers={"Retry-After": "2"}, text="slow down") if index < 3 else ok()
        ))
        sleeps = []
        client = transport(service, registry=self.registry, sleeps=sleeps)

        self.assertIn("choices", client.post_json(URL, {}, model="m-1"))
        self.assertEqual(service.calls, 3)
        self.assertEqual(sleeps, [2.0, 2.0], "等多久照 Retry-After 来")
        self.assertEqual(self.registry.snapshot(), [])

    def test_rate_limit_retries_are_bounded_and_leave_nothing_behind(self):
        service = Service(lambda request, index: httpx.Response(429, headers={"Retry-After": "1"}))
        client = transport(service, registry=self.registry, max_retries=2)

        with self.assertRaises(ModelRateLimitError):
            client.post_json(URL, {}, model="m-1")
        self.assertEqual(service.calls, 3)
        self.assertEqual(self.registry.snapshot(), [])

        # 没有被封：下一次调用照样发得出去
        with self.assertRaises(ModelRateLimitError):
            client.post_json(URL, {}, model="m-1")
        self.assertEqual(service.calls, 6)

    def test_a_long_retry_after_is_honoured_by_waiting_not_by_retrying_early(self):
        state = {"limited": True}
        service = Service(lambda request, index: (
            httpx.Response(429, headers={"Retry-After": "120"}) if state["limited"] else ok()
        ))
        sleeps = []
        client = transport(service, registry=self.registry, sleeps=sleeps, backoff_max=8.0)

        with self.assertRaises(ModelRateLimitError):
            client.post_json(URL, {}, model="m-1")
        self.assertEqual((service.calls, sleeps), (1, []), "等不起就不在这次调用里重试")

        # 暂缓期内：别的阶段、别的组件再来调，都不出网
        planner = transport(service, registry=self.registry, component="thinking")
        for caller in (client, planner, client):
            with self.assertRaises(ModelRateLimitError) as held:
                caller.post_json(URL, {}, model="m-1")
            self.assertFalse(held.exception.access_unavailable, "这是暂缓，不是凭证不可用")
        self.assertEqual(service.calls, 1)
        self.assertEqual(self.registry.permanent_blocks(), [])
        (hold,) = self.registry.snapshot()
        self.assertEqual((hold["reason"], hold["permanent"], hold["model"]), ("rate_limited", False, "m-1"))

        # 别的模型不受这次限流牵连
        self.assertIn("choices", transport(
            Service(ok), registry=self.registry).post_json(URL, {}, model="m-2"))

        # 到点自动恢复：不需要重启，也不需要谁来复位
        self.clock.advance(121)
        state["limited"] = False
        self.assertIn("choices", client.post_json(URL, {}, model="m-1"))
        self.assertEqual(service.calls, 2)
        self.assertEqual(self.registry.snapshot(), [])

    def test_an_absurd_retry_after_cannot_become_a_permanent_block(self):
        service = Service(lambda request, index: httpx.Response(429, headers={"Retry-After": "999999"}))
        client = transport(service, registry=self.registry)
        with self.assertRaises(ModelRateLimitError):
            client.post_json(URL, {}, model="m-1")
        (hold,) = self.registry.snapshot()
        self.assertLessEqual(hold["retry_in_seconds"], MAX_HOLD_SECONDS)

        self.clock.advance(MAX_HOLD_SECONDS + 1)
        with self.assertRaises(ModelRateLimitError):
            client.post_json(URL, {}, model="m-1")
        self.assertEqual(service.calls, 2, "封顶之后会放一个真实请求过去再看")

    def test_retry_after_given_as_a_date_is_understood(self):
        from email.utils import formatdate
        import time as real_time

        response = httpx.Response(429, headers={"Retry-After": formatdate(real_time.time() + 90, usegmt=True)})
        seconds = transport_module._parse_retry_after(response)
        self.assertTrue(80 <= seconds <= 91, seconds)
        self.assertIsNone(transport_module._parse_retry_after(httpx.Response(429, headers={"Retry-After": "soon"})))

    # ---- 额度 ----

    def test_explicit_quota_exhaustion_stops_at_once(self):
        bodies = [
            (429, {"error": {"type": "insufficient_quota", "message": "You exceeded your current quota"}}),
            (402, {"error": "payment required"}),
            (400, {"type": "error", "error": {"type": "invalid_request_error",
                                              "message": "Your credit balance is too low to access the API."}}),
            (403, {"error": {"message": "该令牌额度已用尽"}}),
        ]
        for status, body in bodies:
            with self.subTest(status=status):
                registry = ModelAccessRegistry()
                service = Service(lambda request, index, s=status, b=body: httpx.Response(s, json=b))
                client = transport(service, registry=registry, max_retries=3)

                with self.assertRaises(ModelQuotaExhaustedError) as raised:
                    client.post_json(URL, {}, model="m-1")
                self.assertFalse(raised.exception.retryable)
                self.assertTrue(raised.exception.access_unavailable)
                with self.assertRaises(ModelCredentialUnavailableError):
                    client.post_json(URL, {}, model="another-model")
                self.assertEqual(service.calls, 1, "没有重试，也没有第二次出网")
                self.assertEqual(registry.permanent_blocks()[0]["reason"], "quota_exhausted")

    def test_a_rate_limit_that_merely_mentions_quota_is_still_a_rate_limit(self):
        service = Service(lambda request, index: httpx.Response(
            429, json={"error": {"message": "Quota exceeded for requests per minute, please slow down"}}
        ))
        client = transport(service, registry=self.registry, max_retries=1)
        with self.assertRaises(ModelRateLimitError):
            client.post_json(URL, {}, model="m-1")
        self.assertEqual(service.calls, 2)
        self.assertEqual(self.registry.permanent_blocks(), [])

    # ---- 403 ----

    def test_403_that_names_the_credential_stops_the_credential(self):
        service = Service(lambda request, index: httpx.Response(403, json=DISABLED))
        client = transport(service, registry=self.registry)
        with self.assertRaises(ModelAuthError):
            client.post_json(URL, {}, model="m-1")
        with self.assertRaises(ModelCredentialUnavailableError):
            client.post_json(URL, {}, model="m-2")
        self.assertEqual(service.calls, 1)

    def test_403_about_one_model_stops_that_model_only(self):
        def respond(request, index):
            if json.loads(request.content)["model"] == "m-premium":
                return httpx.Response(403, json={"type": "error", "error": {
                    "type": "permission_error",
                    "message": "Your API key does not have permission to use the specified resource."}})
            return ok()

        service = Service(respond)
        client = transport(service, registry=self.registry)

        with self.assertRaises(ModelAuthError):
            client.post_json(URL, {"model": "m-premium"}, model="m-premium")
        with self.assertRaises(ModelCredentialUnavailableError) as blocked:
            client.post_json(URL, {"model": "m-premium"}, model="m-premium")
        self.assertEqual(blocked.exception.block_scope, "model")
        self.assertEqual(service.calls, 1)

        # 同一把凭证的别的模型、别的凭证，都照常
        self.assertIn("choices", client.post_json(URL, {"model": "m-basic"}, model="m-basic"))
        self.assertIn("choices", transport(service, registry=self.registry, api_key="sk-other-000000").post_json(
            URL, {"model": "m-basic"}, model="m-basic"))
        (block,) = self.registry.permanent_blocks()
        self.assertEqual((block["scope"], block["model"], block["reason"]), ("model", "m-premium", "permission_denied"))

    def test_403_that_explains_nothing_is_not_treated_as_permanent(self):
        state = {"blocked": True}
        service = Service(lambda request, index: (
            httpx.Response(403, text="<html><title>Access denied</title></html>") if state["blocked"] else ok()
        ))
        client = transport(service, registry=self.registry)

        with self.assertRaises(ModelAuthError) as raised:
            client.post_json(URL, {}, model="m-1")
        self.assertFalse(raised.exception.access_unavailable)
        self.assertEqual(self.registry.permanent_blocks(), [])

        # 同一个请求的后续阶段不会立刻再撞一次……
        with self.assertRaises(ModelCredentialUnavailableError) as held:
            client.post_json(URL, {}, model="m-1")
        self.assertFalse(held.exception.access_unavailable)
        self.assertEqual(service.calls, 1)
        # ……别的模型不受牵连……
        state["blocked"] = False
        self.assertIn("choices", client.post_json(URL, {}, model="m-2"))
        # ……过一会儿它自己会再试
        self.clock.advance(AMBIGUOUS_FORBIDDEN_HOLD_SECONDS + 1)
        self.assertIn("choices", client.post_json(URL, {}, model="m-1"))

    def test_model_scoped_states_need_a_model_name(self):
        """不知道是哪个模型，就没法只拦这一个模型——那就不拦，而不是去拦整把凭证。"""
        service = Service(lambda request, index: httpx.Response(403, text="forbidden"))
        client = transport(service, registry=self.registry)
        for _ in range(2):
            with self.assertRaises(ModelAuthError):
                client.post_json(URL, {})
        self.assertEqual(service.calls, 2)
        self.assertEqual(self.registry.snapshot(), [])


class CircuitBreakerIsNotToldItSucceededTest(unittest.TestCase):
    def breaker_transport(self, service, breaker):
        client = transport(service, registry=ModelAccessRegistry(), max_retries=0)
        client.circuit = breaker
        return client

    def test_a_rejection_does_not_clear_the_failure_count(self):
        clock = FakeClock()
        breaker = CircuitBreaker("unit", CircuitConfig(failure_threshold=3, reset_seconds=30), clock=clock)
        breaker.record_failure()
        breaker.record_failure()

        rejected = Service(lambda request, index: httpx.Response(401, json=DISABLED))
        with self.assertRaises(ModelAuthError):
            self.breaker_transport(rejected, breaker).post_json(URL, {}, model="m-1")

        self.assertEqual(breaker.snapshot()["consecutive_failures"], 2, "以前这里会被清成 0")
        breaker.record_failure()
        self.assertIs(breaker.state, CircuitState.OPEN)

    def test_a_rejected_probe_does_not_close_an_open_breaker(self):
        clock = FakeClock()
        breaker = CircuitBreaker("unit", CircuitConfig(failure_threshold=1, reset_seconds=30), clock=clock)
        breaker.record_failure()
        self.assertIs(breaker.state, CircuitState.OPEN)
        clock.advance(31)

        rejected = Service(lambda request, index: httpx.Response(401, json=DISABLED))
        with self.assertRaises(ModelAuthError):
            self.breaker_transport(rejected, breaker).post_json(URL, {}, model="m-1")

        self.assertIsNot(breaker.state, CircuitState.CLOSED, "被拒的试探不能把熔断器合上")
        # 试探位让出来了：下一个调用（比如另一把凭证）可以接着去试，不会卡死在半开
        self.assertTrue(breaker.allow())
        breaker.record_success()
        self.assertIs(breaker.state, CircuitState.CLOSED)

    def test_ordinary_request_errors_still_prove_the_link_is_up(self):
        breaker = CircuitBreaker("unit", CircuitConfig(failure_threshold=3, reset_seconds=30))
        breaker.record_failure()
        bad_request = Service(lambda request, index: httpx.Response(400, text="unknown parameter"))
        with self.assertRaises(ModelCallError):
            self.breaker_transport(bad_request, breaker).post_json(URL, {}, model="m-1")
        self.assertEqual(breaker.snapshot()["consecutive_failures"], 0)


class BoundedConcurrencyTest(unittest.TestCase):
    def test_nothing_new_is_dispatched_after_the_first_rejection_lands(self):
        """
        4 个工作线程、12 个请求、每个请求 4 个阶段。头 4 个调用被卡在一起同时在路上，
        然后一起收到 401。

        量出来的两件事：
        - 拒绝记下之后，剩下的 44 次阶段调用一次都没有出网；
        - 已经在路上的那 4 个收不回来——出网次数等于并发数，不是 1。
        """
        workers, requests, stages = 4, 12, ["thinking", "keyword_extractor", "thinking", "coder"]
        registry = ModelAccessRegistry()
        in_flight = threading.Barrier(workers, timeout=10)

        def respond(request, index):
            if index <= workers:
                in_flight.wait()
            return httpx.Response(401, json=DISABLED)

        service = Service(respond)
        outcomes = []

        def one_request(_number):
            results = []
            for component in stages:
                try:
                    transport(service, registry=registry, component=component).post_json(URL, {}, model="m-1")
                    results.append("ok")
                except ModelCredentialUnavailableError:
                    results.append("short-circuited")
                except ModelAuthError:
                    results.append("rejected")
            outcomes.append(results)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(one_request, range(requests)))

        flat = [result for results in outcomes for result in results]
        self.assertEqual(service.calls, workers)
        self.assertEqual(flat.count("rejected"), workers)
        self.assertEqual(flat.count("short-circuited"), requests * stages.__len__() - workers)
        self.assertNotIn("ok", flat)
        # 每个请求最多只有它的第一个阶段出过网；后面的阶段没有被继续派发
        for results in outcomes:
            self.assertTrue(all(result == "short-circuited" for result in results[1:]), results)


class _ModelServiceEnvironment(unittest.TestCase):
    """
    整条链路的用例：配一把假的凭证，把进程共用的 HTTP Client 换成 MockTransport。

    这样路由、抽词、规划、生成、自检、摘要用的都是它们真实的 Client 类，
    但一个字节都出不了本机——地址用的是 `.invalid`，本来也解析不了。
    """

    ENV = {
        "AI_SQL_MODEL_PROVIDER": "anthropic",
        "AI_SQL_ANTHROPIC_API_KEY": SECRET,
        "AI_SQL_ANTHROPIC_BASE_URL": "https://model.invalid/v1",
        "AI_SQL_SEMANTIC_DIR": "",
        "AI_SQL_AUTH_MODE": "disabled",
        # These tests use private temporary stores even when CI provides pgvector.
        "AI_SQL_PGVECTOR_DSN": "",
        "AI_SQL_HTTP_MAX_RETRIES": "0",
        "AI_SQL_CERTIFIED_SQL_ENABLED": "false",
    }
    EXTRA_ENV = {}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmp.name).resolve()
        self._cwd = os.getcwd()
        env = {**self.ENV, **self.EXTRA_ENV,
               "AI_SQL_MEMORY_DB_PATH": str(self.directory / "memory.db"),
               "AI_SQL_AUDIT_DB_PATH": str(self.directory / "audit.db")}
        self._saved = {key: os.environ.get(key) for key in env}
        os.chdir(self.directory)
        os.environ.update(env)
        reload_settings()

        self.service = Service(self.respond)
        self._saved_client = transport_module._shared_client
        transport_module._shared_client = self.service.client()
        MODEL_ACCESS.reset_all()
        CIRCUIT_REGISTRY.reset_all()
        self.addCleanup(self._restore)

    def respond(self, request, index):
        return httpx.Response(401, json=DISABLED)

    def _restore(self):
        transport_module._shared_client = self._saved_client
        MODEL_ACCESS.reset_all()
        CIRCUIT_REGISTRY.reset_all()
        os.chdir(self._cwd)
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reload_settings()
        self._tmp.cleanup()


class WholeRequestTest(_ModelServiceEnvironment):
    def build_service(self):
        from ai_sql_agent.memory import create_memory_service
        from ai_sql_agent.pipeline import DynamicSqlAgentService, Text2SQLPipeline

        memory = create_memory_service()
        self.addCleanup(memory.close)
        return DynamicSqlAgentService(Text2SQLPipeline(), memory)

    def test_one_question_sends_one_rejected_call_and_the_next_question_sends_none(self):
        service = self.build_service()

        first = service.run(user_id="u", session_id="s-1",
                            query="Which suppliers have an on-time delivery rate below 0.9?")
        # 路由先调模型、被拒；之后抽词、规划都在本地短路。以前这里是 3 次。
        self.assertEqual(self.service.calls, 1)
        self.assertTrue(first.pipeline_result.degraded)
        self.assertEqual(first.pipeline_result.degrade_reason, "model_unavailable")
        # 规则兜底仍然把它判成查数，而不是闲聊
        self.assertEqual(first.decision.route.value, "database_query")

        second = service.run(user_id="u", session_id="s-2", query="查询准时率低于0.9的供应商名称")
        third = service.run(user_id="u", session_id="s-3", query="准时率是什么意思？")
        self.assertEqual(self.service.calls, 1, "凭证已知不可用之后，新的问题一次都不该出网")
        self.assertTrue(second.pipeline_result.degraded)
        self.assertTrue(third.degraded)
        self.assertEqual(third.degrade_reason, "model_unavailable")

        (block,) = MODEL_ACCESS.permanent_blocks()
        self.assertEqual(block["reason"], "credential_rejected")
        self.assertNotIn(SECRET, json.dumps(MODEL_ACCESS.snapshot()))

    def test_summary_self_check_and_keyword_extraction_stay_local_after_the_rejection(self):
        from ai_sql_agent.memory.objects import MemoryMessage, MessageRole
        from ai_sql_agent.memory.summarizer import ModelMemorySummarizer
        from ai_sql_agent.planning import ThinkingModelClient, ThinkingModelConfig
        from ai_sql_agent.retrieval.keyword_extractor_client import AliyunKeywordExtractor
        from ai_sql_agent.validation import ResultValidator, SemanticSelfChecker
        from ai_sql_agent.validation.objects import SELF_CHECK_NOT_RUN

        def thinking():
            return ThinkingModelClient(ThinkingModelConfig(use_mock_when_no_api_key=False))

        with self.assertRaises(ModelAuthError):
            thinking().generate("ping")
        self.assertEqual(self.service.calls, 1)

        message = MemoryMessage(id=1, session_id="s", user_id="u", role=MessageRole.USER, content="问题")
        for _turn in range(3):  # 后台摘要每一轮都会再试一次
            with self.assertRaises(ModelCredentialUnavailableError):
                ModelMemorySummarizer(thinking()).summarize_conversation("", [message])
        with self.assertRaises(ModelCredentialUnavailableError):
            AliyunKeywordExtractor().extract("准时率低于0.9的供应商")

        outcome = ResultValidator(self_checker=SemanticSelfChecker(thinking())).validate(
            query="准时率低于0.9的供应商", sql="SELECT supplier_name FROM supplier", row_count=3,
        )
        self.assertEqual(outcome.self_check, SELF_CHECK_NOT_RUN)
        self.assertEqual(outcome.self_check_error, "ModelCredentialUnavailableError")

        self.assertEqual(self.service.calls, 1)


class RepairDegradesTest(unittest.TestCase):
    """修复要靠模型重写 SQL。模型不可用时是降级，不是一个冒到 HTTP 层的异常。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cwd = os.getcwd()
        self._backend_env = mock.patch.dict(os.environ, {"AI_SQL_PGVECTOR_DSN": ""})
        self._backend_env.start()
        os.chdir(self._tmp.name)
        self.addCleanup(self._restore_environment)
        reload_settings()

    def _restore_environment(self):
        os.chdir(self._cwd)
        self._backend_env.stop()
        reload_settings()
        self._tmp.cleanup()

    def pipeline_whose_first_sql_fails(self, repair_error, **kwargs):
        from ai_sql_agent.execution import MCPExecutionResult
        from ai_sql_agent.pipeline import Text2SQLPipeline

        pipeline = Text2SQLPipeline(**kwargs)
        real_execute = pipeline.mcp_router.execute
        state = {"n": 0}

        def execute(request):
            state["n"] += 1
            if state["n"] == 1:
                database = request.database if hasattr(request, "database") else request["database"]
                sql = request.sql if hasattr(request, "sql") else request["sql"]
                return MCPExecutionResult(database=database, sql=sql, success=False,
                                          error="no such column: supplier_nam")
            return real_execute(request)

        def repair(**_kwargs):
            raise repair_error

        pipeline.mcp_router.execute = execute
        pipeline.sql_repair.repair = repair
        return pipeline

    def test_credential_unavailable_during_repair_is_a_degraded_result(self):
        error = ModelCredentialUnavailableError(
            "模型凭证已被服务端拒绝，本次调用未发出。", component="coder",
            block_scope="credential", block_reason="credential_rejected", block_permanent=True,
        )
        result = self.pipeline_whose_first_sql_fails(error).run("查询准时率低于0.9的供应商名称", user_id="li")

        self.assertTrue(result.degraded)
        self.assertEqual(result.degrade_reason, "model_unavailable")
        self.assertEqual(result.step_logs, [])

    def test_a_timeout_during_repair_degrades_the_same_way_and_can_replay_history(self):
        from ai_sql_agent.core.errors import ModelTimeoutError

        pipeline = self.pipeline_whose_first_sql_fails(
            ModelTimeoutError("超时", component="coder"),
            sql_history_lookup=lambda user_id, query: [
                ("erp_db", "SELECT supplier_name FROM supplier WHERE on_time_rate < 0.9")
            ],
        )
        result = pipeline.run("查询准时率低于0.9的供应商名称", user_id="li")

        self.assertTrue(result.degraded)
        self.assertEqual(result.degrade_reason, "model_unavailable_replayed_sql")
        self.assertTrue(result.step_logs[0].execution_result["success"])

    def test_the_http_layer_answers_200_degraded_not_500(self):
        from fastapi.testclient import TestClient

        from ai_sql_agent.api.app import app

        saved = {key: os.environ.get(key) for key in (
            "AI_SQL_AUTH_MODE", "AI_SQL_SEMANTIC_DIR", "AI_SQL_MEMORY_DB_PATH", "AI_SQL_AUDIT_DB_PATH")}
        os.environ.update({
            "AI_SQL_AUTH_MODE": "disabled", "AI_SQL_SEMANTIC_DIR": "",
            "AI_SQL_MEMORY_DB_PATH": str(Path(self._tmp.name) / "memory.db"),
            "AI_SQL_AUDIT_DB_PATH": str(Path(self._tmp.name) / "audit.db"),
        })
        reload_settings()

        def restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            reload_settings()

        self.addCleanup(restore)
        error = ModelCredentialUnavailableError(
            "模型凭证已被服务端拒绝，本次调用未发出。", component="coder",
            block_scope="credential", block_reason="credential_rejected", block_permanent=True,
        )
        with TestClient(app) as client:
            from ai_sql_agent.audit import SQLiteAuditStore
            from ai_sql_agent.execution import MCPExecutionResult

            # A temporary file alone does not override the CI PostgreSQL backend.
            self.assertIsInstance(app.state.audit, SQLiteAuditStore)
            self.assertEqual(app.state.audit.last_successful_sqls(
                "li", "查询准时率低于0.9的供应商名称"), [])
            pipeline = app.state.pipeline
            real_execute, state = pipeline.mcp_router.execute, {"n": 0}

            def execute(request):
                state["n"] += 1
                if state["n"] == 1:
                    database = request.database if hasattr(request, "database") else request["database"]
                    sql = request.sql if hasattr(request, "sql") else request["sql"]
                    return MCPExecutionResult(database=database, sql=sql, success=False, error="no such column: x")
                return real_execute(request)

            def repair(**_kwargs):
                raise error

            pipeline.mcp_router.execute = execute
            pipeline.sql_repair.repair = repair
            response = client.post("/query", json={
                "user_id": "li", "session_id": "repair-degrade", "query": "查询准时率低于0.9的供应商名称"})

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["degraded"])
        self.assertEqual(body["degrade_reason"], "model_unavailable")
        self.assertEqual(body["execution_status"], "failed")


class StageLoggingDoesNotMaskTheErrorTest(unittest.TestCase):
    def test_a_stage_that_carries_an_error_field_still_raises_the_original_exception(self):
        """
        修复阶段进来时带着 `error=<要修的执行错误>`。阶段里再抛异常，日志要记两个 error——
        以前这里自己抛 TypeError 把原异常盖掉，上层的 `except ModelCallError` 接不到，变成 500。
        """
        from ai_sql_agent.core import stage

        original = ModelCredentialUnavailableError("未发出", component="coder")
        with self.assertLogs("ai_sql.pipeline", level=logging.ERROR) as captured:
            with self.assertRaises(ModelCredentialUnavailableError) as raised:
                with stage("repair", logging.getLogger("ai_sql.pipeline"),
                           step=1, error="no such column: x", status="odd", duration_ms=-1):
                    raise original
        self.assertIs(raised.exception, original)
        self.assertEqual(captured.records[0].getMessage(), "stage.repair")

        # 正常结束的阶段带着同名字段也不该出事
        with stage("repair", logging.getLogger("ai_sql.pipeline"), error=None, status="odd"):
            pass


class SelfCheckIsMarkedHonestlyTest(unittest.TestCase):
    def validate(self, client, **overrides):
        from ai_sql_agent.validation import ResultValidator, SemanticSelfChecker

        arguments = {"query": "订单总金额是多少", "sql": "SELECT SUM(order_amt) FROM sales_order", "row_count": 1}
        arguments.update(overrides)
        return ResultValidator(self_checker=SemanticSelfChecker(client)).validate(**arguments)

    def test_a_self_check_that_could_not_run_is_not_reported_as_passed(self):
        from ai_sql_agent.validation import AnomalyType
        from ai_sql_agent.validation.objects import SELF_CHECK_NOT_RUN

        class Unavailable:
            def generate(self, prompt):
                raise ModelCredentialUnavailableError("未发出", component="thinking")

        outcome = self.validate(Unavailable())

        self.assertFalse(outcome.passed, "没核对过不能算通过")
        self.assertFalse(outcome.should_retry, "但也没有理由为此回溯重跑")
        self.assertEqual(outcome.self_check, SELF_CHECK_NOT_RUN)
        (finding,) = outcome.findings
        self.assertEqual((finding.anomaly, finding.severity), (AnomalyType.SELF_CHECK_NOT_RUN, "warn"))
        payload = outcome.to_dict()
        self.assertEqual(payload["self_check"], "not_run")
        self.assertEqual(payload["self_check_error"], "ModelCredentialUnavailableError")
        self.assertEqual(payload["findings"][0]["anomaly"], "self_check_not_run")

    def test_the_three_other_states_are_told_apart(self):
        class Says:
            def __init__(self, verdict):
                self.replies = ["复述：汇总了订单金额", verdict]

            def generate(self, prompt):
                return self.replies.pop(0)

        agreed = self.validate(Says("结论：一致\n理由：口径相符"))
        self.assertEqual((agreed.self_check, agreed.passed, agreed.findings), ("passed", True, []))

        disagreed = self.validate(Says("结论：不一致\n理由：问的是数量"))
        self.assertEqual((disagreed.self_check, disagreed.passed), ("mismatch", False))

        # 本来就不做自检的情形（没配模型、空结果）：如实写 not_applicable，不算没通过
        nothing_to_check = self.validate(None)
        self.assertEqual((nothing_to_check.self_check, nothing_to_check.passed), ("not_applicable", True))
        empty = self.validate(Says("不该被调用"), row_count=0)
        self.assertEqual(empty.self_check, "not_applicable")

    def test_the_mark_reaches_the_caller_as_a_risk(self):
        """响应里靠 risk_findings / has_risk 传达；契约里 has_risk 为真 = 这个结果不能直接信。"""
        from ai_sql_agent.api.app import STATIC_DIR
        from ai_sql_agent.api.result_summary import StepLogResponse

        class Unavailable:
            def generate(self, prompt):
                raise ModelCredentialUnavailableError("未发出", component="thinking")

        findings = self.validate(Unavailable()).to_dict()["findings"]
        step = StepLogResponse(database="erp_db", sql="SELECT 1", final_sql="SELECT 1", success=True,
                               row_count=1, risk_findings=findings)
        self.assertEqual(step.risk_findings[0]["anomaly"], "self_check_not_run")

        for language in ("en", "zh"):
            text = (STATIC_DIR / "js" / "i18n" / f"{language}.js").read_text(encoding="utf-8")
            self.assertIn("'anomaly.self_check_not_run'", text)
            self.assertIn("'anomaly.self_check_not_run.help'", text)


BLOCK = [{"scope": "credential", "reason": "credential_rejected", "permanent": True, "model": "",
          "since": "2026-10-04T12:00:00Z", "endpoint": "e-00000000", "credential": "k-000000000000"}]


class BenchmarkStopsTest(unittest.TestCase):
    """评测入口。用的是内置演示库和 Mock 模型，「凭证不可用」由注入的检查函数来宣布。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmp.name).resolve()
        self._cwd = os.getcwd()
        os.chdir(self.directory)
        MODEL_ACCESS.reset_all()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(os.chdir, self._cwd)
        self.addCleanup(MODEL_ACCESS.reset_all)
        repo = str(Path(__file__).resolve().parent.parent)
        if repo not in sys.path:
            sys.path.insert(0, repo)

    def runner_that_loses_access_during_item(self, number):
        from benchmark.runner import BenchmarkRunner

        state = {"runs": 0, "blocked": False}
        runner = BenchmarkRunner(access_check=lambda: BLOCK if state["blocked"] else [])
        real_run = runner.pipeline.run

        def run(query, *args, **kwargs):
            state["runs"] += 1
            if state["runs"] == number:
                state["blocked"] = True  # 这一题跑到一半，凭证被拒了
            return real_run(query, *args, **kwargs)

        runner.pipeline.run = run
        return runner, state

    def test_the_run_stops_at_once_and_keeps_what_was_finished(self):
        from benchmark.run_benchmark import SMOKE_IDS

        runner, state = self.runner_that_loses_access_during_item(3)
        report = runner.run(ids=SMOKE_IDS)

        self.assertTrue(report.incomplete)
        self.assertEqual(report.abort_reason, "model_access_unavailable")
        self.assertEqual(state["runs"], 3, "第 4 题没有再去跑")
        self.assertEqual([item.item_id for item in report.items], SMOKE_IDS[:2],
                         "出事那一题是在故障里跑出来的，不计入已完成的结果")
        self.assertEqual((report.total, report.planned_total, report.correct), (2, 4, 2))
        payload = report.to_dict()
        self.assertTrue(payload["incomplete"])
        self.assertEqual(payload["planned_total"], 4)
        self.assertEqual(len(payload["items"]), 2)
        self.assertEqual(payload["abort_detail"], BLOCK)

    def test_a_finished_run_is_not_marked(self):
        from benchmark.run_benchmark import SMOKE_IDS
        from benchmark.runner import BenchmarkRunner

        report = BenchmarkRunner().run(ids=SMOKE_IDS)
        self.assertFalse(report.incomplete)
        self.assertEqual((report.total, report.planned_total), (4, 4))
        self.assertFalse(report.to_dict()["incomplete"])

    def test_a_run_that_starts_with_a_known_bad_credential_runs_nothing(self):
        from benchmark.run_benchmark import SMOKE_IDS
        from benchmark.runner import BenchmarkRunner

        runner = BenchmarkRunner(access_check=lambda: BLOCK)
        runner.pipeline.run = mock.Mock(side_effect=AssertionError("不该再调 pipeline"))
        report = runner.run(ids=SMOKE_IDS)
        self.assertTrue(report.incomplete)
        self.assertEqual((report.total, report.items), (0, []))

    def test_the_real_state_table_is_what_stops_it_by_default(self):
        """不注入检查函数：一次真的（mock 出来的）401 记进进程的状态表，评测随即停下。"""
        from benchmark.run_benchmark import SMOKE_IDS
        from benchmark.runner import BenchmarkRunner

        runner = BenchmarkRunner()
        rejected = Service(lambda request, index: httpx.Response(401, json=DISABLED))
        real_run, state = runner.pipeline.run, {"runs": 0}

        def run(query, *args, **kwargs):
            state["runs"] += 1
            if state["runs"] == 2:
                with contextlib.suppress(ModelAuthError):
                    ModelTransport("thinking", api_key=SECRET, client=rejected.client()).post_json(
                        URL, {}, model="m-1")
            return real_run(query, *args, **kwargs)

        runner.pipeline.run = run
        report = runner.run(ids=SMOKE_IDS)

        self.assertTrue(report.incomplete)
        self.assertEqual((state["runs"], len(report.items), rejected.calls), (2, 1, 1))
        self.assertEqual(report.abort_detail[0]["reason"], "credential_rejected")
        self.assertNotIn(SECRET, json.dumps(report.to_dict(), ensure_ascii=False))

    def test_holdout_raises_with_the_finished_rows_instead_of_returning_a_number(self):
        from benchmark.runner import BenchmarkIncomplete, run_holdout_recall

        holdout = self.directory / "holdout.json"
        holdout.write_text(json.dumps([
            {"id": f"h{n}", "query": f"问题 {n}", "gold_columns": ["erp_db.supplier.supplier_name"]}
            for n in range(1, 6)
        ], ensure_ascii=False), encoding="utf-8")
        state = {"runs": 0}

        class Pipeline:
            def run(self, query):
                state["runs"] += 1
                return mock.Mock(prompt_columns=["erp_db.supplier.supplier_name"], retrieved_columns=[])

        with self.assertRaises(BenchmarkIncomplete) as raised:
            run_holdout_recall(Pipeline(), holdout, access_check=lambda: BLOCK if state["runs"] >= 3 else [])

        error = raised.exception
        self.assertEqual((error.stage, error.completed, error.planned, error.hits), ("holdout", 2, 5, 2))
        self.assertEqual([row["id"] for row in error.rows], ["h1", "h2"])
        self.assertEqual(state["runs"], 3)
        self.assertTrue(error.to_dict()["incomplete"])

    # ---- 状态表里没有记录，这一题却拿到了降级答复 ----

    def runner_that_degrades_at_item(self, number, *, replayed=False):
        """第 number 题的模型调用失败：流水线照常返回，走的是降级链路，凭证状态表里什么都没记。"""
        from ai_sql_agent.pipeline.objects import PipelineResult
        from benchmark.runner import BenchmarkRunner

        state = {"runs": 0}
        runner = BenchmarkRunner(access_check=lambda: [])
        real_run = runner.pipeline.run

        def run(query, *args, **kwargs):
            state["runs"] += 1
            if state["runs"] != number:
                return real_run(query, *args, **kwargs)
            if replayed:  # 重放了历史 SQL：结果可能是对的，但不是模型答的
                result = real_run(query, *args, **kwargs)
                result.degraded, result.degrade_reason = True, "model_unavailable_replayed_sql"
                return result
            return PipelineResult(query=query, keywords=[], schema_context="", cot_output="",
                                  degraded=True, degrade_reason="model_unavailable")

        runner.pipeline.run = run
        return runner, state

    def test_a_degraded_answer_stops_the_run_like_a_rejected_key(self):
        from benchmark.run_benchmark import SMOKE_IDS

        runner, state = self.runner_that_degrades_at_item(3)
        report = runner.run(ids=SMOKE_IDS)

        self.assertTrue(report.incomplete)
        self.assertEqual(report.abort_reason, "model_unavailable")
        self.assertEqual(state["runs"], 3, "第 4 题没有再去跑")
        self.assertEqual([item.item_id for item in report.items], SMOKE_IDS[:2],
                         "降级的那一题不是模型答的：不计入，也不记成答错")
        self.assertEqual((report.total, report.planned_total, report.correct), (2, 4, 2))
        (detail,) = report.abort_detail
        self.assertEqual((detail["scope"], detail["item"], detail["reason"]),
                         ("item", SMOKE_IDS[2], "model_unavailable"))

    def test_a_replayed_answer_is_not_a_measurement_either(self):
        from benchmark.run_benchmark import SMOKE_IDS

        runner, _state = self.runner_that_degrades_at_item(2, replayed=True)
        report = runner.run(ids=SMOKE_IDS)

        self.assertTrue(report.incomplete)
        self.assertEqual([item.item_id for item in report.items], SMOKE_IDS[:1])
        self.assertEqual(report.abort_detail[0]["reason"], "model_unavailable_replayed_sql")

    def test_holdout_stops_on_a_degraded_answer_instead_of_counting_a_miss(self):
        from benchmark.runner import BenchmarkIncomplete, run_holdout_recall

        holdout = self.directory / "holdout.json"
        holdout.write_text(json.dumps([
            {"id": f"h{n}", "query": f"问题 {n}", "gold_columns": ["erp_db.supplier.supplier_name"]}
            for n in range(1, 6)
        ], ensure_ascii=False), encoding="utf-8")
        state = {"runs": 0}

        class Pipeline:
            def run(self, query):
                state["runs"] += 1
                if state["runs"] == 3:  # 降级的结果里没有进 Prompt 的字段，照常算就是一次「没召回」
                    return mock.Mock(degraded=True, degrade_reason="model_unavailable",
                                     prompt_columns=[], retrieved_columns=[])
                return mock.Mock(prompt_columns=["erp_db.supplier.supplier_name"], retrieved_columns=[])

        with self.assertRaises(BenchmarkIncomplete) as raised:
            run_holdout_recall(Pipeline(), holdout, access_check=lambda: [])

        error = raised.exception
        self.assertEqual((error.completed, error.planned, error.hits), (2, 5, 2))
        self.assertEqual((error.reason, error.blocks[0]["item"]), ("model_unavailable", "h3"))
        self.assertEqual(error.to_dict()["abort_reason"], "model_unavailable")
        self.assertEqual(state["runs"], 3)

    # ---- 入口：退出码、落盘、不出结论 ----

    def run_cli(self, main, argv, runner):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_run_benchmark_saves_the_partial_report_and_exits_non_zero(self):
        import benchmark.run_benchmark as cli

        runner, _state = self.runner_that_loses_access_during_item(3)
        output = self.directory / "report.json"
        with mock.patch.object(cli, "BenchmarkRunner", lambda **kwargs: runner):
            code, stdout, stderr = self.run_cli(
                cli.main, ["run_benchmark", "--smoke", "--output", str(output)], runner)

        self.assertEqual(code, 3)
        saved = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(saved["incomplete"])
        self.assertEqual(saved["abort_reason"], "model_access_unavailable")
        self.assertEqual(len(saved["partial_round"]["items"]), 2)
        self.assertTrue(saved["partial_round"]["incomplete"])
        self.assertIn("未完成", stderr)
        self.assertNotIn("执行准确率", stdout, "没跑完的数字不打印成结果")

    def test_run_benchmark_without_an_output_path_still_saves(self):
        import benchmark.run_benchmark as cli

        runner, _state = self.runner_that_loses_access_during_item(2)
        with mock.patch.object(cli, "BenchmarkRunner", lambda **kwargs: runner):
            code, _stdout, stderr = self.run_cli(cli.main, ["run_benchmark", "--smoke"], runner)

        self.assertEqual(code, 3)
        (saved,) = list((self.directory / "runtime_data").glob("benchmark_incomplete_*.json"))
        self.assertTrue(json.loads(saved.read_text(encoding="utf-8"))["incomplete"])
        self.assertIn(saved.name, stderr)

    def test_repeat_mode_keeps_the_finished_rounds_and_stops(self):
        import benchmark.run_benchmark as cli

        # 每轮 4 题：第 1 轮跑完，第 2 轮的第 2 题出事
        runner, state = self.runner_that_loses_access_during_item(6)
        output = self.directory / "repeat.json"
        with mock.patch.object(cli, "BenchmarkRunner", lambda **kwargs: runner):
            code, stdout, _stderr = self.run_cli(
                cli.main, ["run_benchmark", "--smoke", "--repeat", "3", "--output", str(output)], runner)

        self.assertEqual(code, 3)
        saved = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual((saved["rounds_planned"], saved["rounds_completed"]), (3, 1))
        self.assertEqual(len(saved["partial_round"]["items"]), 1)
        self.assertEqual(state["runs"], 6, "没有第 3 轮，也没有补跑")
        self.assertNotIn("轮汇总", stdout)

    def test_gate_gives_no_verdict(self):
        import benchmark.gate as gate

        runner, _state = self.runner_that_loses_access_during_item(3)
        partial = self.directory / "gate-partial.json"
        with mock.patch.object(gate, "BenchmarkRunner", lambda **kwargs: runner):
            code, stdout, stderr = self.run_cli(
                gate.main, ["gate", "--smoke", "--partial-output", str(partial)], runner)

        self.assertEqual(code, 3)
        self.assertNotIn("通过发布门禁", stdout + stderr)
        self.assertNotIn("PASS", stdout)
        self.assertNotIn("阻断发布", stderr, "也不是「未达标」——是没有结论")
        self.assertIn("不判定", stderr)
        saved = json.loads(partial.read_text(encoding="utf-8"))
        self.assertTrue(saved["incomplete"])
        self.assertEqual(len(saved["partial_round"]["items"]), 2)

    def test_gate_stops_when_the_holdout_part_is_cut_short(self):
        import benchmark.gate as gate
        from benchmark.runner import BenchmarkIncomplete, BenchmarkRunner

        runner = BenchmarkRunner()
        partial = self.directory / "gate-holdout.json"

        def holdout(pipeline, access_check=None):
            raise BenchmarkIncomplete("留出集没跑完", stage="holdout", completed=7, planned=40, hits=7,
                                      blocks=BLOCK, rows=[{"id": "h01", "hit": True}])

        with mock.patch.object(gate, "BenchmarkRunner", lambda **kwargs: runner), \
                mock.patch.object(gate, "run_holdout_recall", holdout):
            code, stdout, stderr = self.run_cli(
                gate.main, ["gate", "--smoke", "--repeat", "2", "--partial-output", str(partial)], runner)

        self.assertEqual(code, 3)
        self.assertNotIn("通过发布门禁", stdout + stderr)
        saved = json.loads(partial.read_text(encoding="utf-8"))
        self.assertEqual((saved["rounds_planned"], saved["rounds_completed"]), (2, 0))
        self.assertEqual(saved["partial_holdout"]["completed"], 7)
        self.assertFalse(saved["partial_round"]["incomplete"], "评测集那一半是跑完了的，如实保留")

    def test_gate_names_the_model_service_not_the_key(self):
        import benchmark.gate as gate

        runner, _state = self.runner_that_degrades_at_item(3)
        partial = self.directory / "gate-partial.json"
        with mock.patch.object(gate, "BenchmarkRunner", lambda **kwargs: runner):
            code, stdout, stderr = self.run_cli(
                gate.main, ["gate", "--smoke", "--partial-output", str(partial)], runner)

        self.assertEqual(code, 3)
        self.assertNotIn("阻断发布", stderr, "断供不是「未达标」")
        self.assertNotIn("通过发布门禁", stdout + stderr)
        self.assertIn("模型服务不可用", stderr)
        self.assertNotIn("模型凭证不可用", stderr)
        self.assertIn("pipeline.model_unavailable", stderr, "告诉人去日志里找哪一行")
        saved = json.loads(partial.read_text(encoding="utf-8"))
        self.assertEqual(saved["abort_reason"], "model_unavailable")
        self.assertEqual(saved["abort_detail"][0]["item"], "q046")
        self.assertEqual(len(saved["partial_round"]["items"]), 2)

    def test_run_benchmark_says_to_wait_for_the_service_not_to_fix_the_key(self):
        import benchmark.run_benchmark as cli

        runner, _state = self.runner_that_degrades_at_item(2)
        output = self.directory / "report.json"
        with mock.patch.object(cli, "BenchmarkRunner", lambda **kwargs: runner):
            code, stdout, stderr = self.run_cli(
                cli.main, ["run_benchmark", "--smoke", "--output", str(output)], runner)

        self.assertEqual(code, 3)
        saved = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(saved["abort_reason"], "model_unavailable")
        self.assertEqual(len(saved["partial_round"]["items"]), 1)
        self.assertIn("模型服务不可用", stderr)
        self.assertIn("确认模型服务恢复后", stderr)
        self.assertNotIn("执行准确率", stdout)


class RelayDropsTheModelTest(_ModelServiceEnvironment):
    """
    2026-10-14 那次门禁：跑到第 65 题，中转站开始对每个请求回 404「这个分组里没有账号支持该模型」。

    这不是凭证故障，状态表里不会记；评测以前照样逐题跑完，三轮里 386 题被记成答错，
    门禁打出「阻断发布」。现在第一道拿到降级答复的题就停。
    """

    def respond(self, request, index):
        return httpx.Response(404, json={"error": {
            "message": 'Model "m-1" is not supported by any configured account in this group',
            "type": "model_not_found",
        }})

    def test_the_benchmark_stops_at_the_first_degraded_answer(self):
        from benchmark.run_benchmark import SMOKE_IDS
        from benchmark.runner import BenchmarkRunner

        runner = BenchmarkRunner()
        queries = []
        real_run = runner.pipeline.run

        def run(query, *args, **kwargs):
            queries.append(query)
            return real_run(query, *args, **kwargs)

        runner.pipeline.run = run
        report = runner.run(ids=SMOKE_IDS)

        self.assertGreater(self.service.calls, 0, "请求真的发到了（假的）模型服务")
        self.assertEqual(MODEL_ACCESS.permanent_blocks(), [], "404 不会记进状态表——所以要看这一题的结果")
        self.assertEqual(len(queries), 1, "第一题就停，后面的题一道都没跑")
        self.assertTrue(report.incomplete)
        self.assertEqual(report.abort_reason, "model_unavailable")
        self.assertEqual((report.total, report.items), (0, []))
        self.assertEqual(report.abort_detail[0]["item"], SMOKE_IDS[0])


class StatusEndpointTest(_ModelServiceEnvironment):
    """状态接口只读本进程记下的故障；复位只走已有的、要管理员身份的入口。"""

    def rejected_once(self):
        from ai_sql_agent.planning import ThinkingModelClient, ThinkingModelConfig

        with self.assertRaises(ModelAuthError):
            ThinkingModelClient(ThinkingModelConfig(use_mock_when_no_api_key=False)).generate("ping")
        self.assertEqual(self.service.calls, 1)

    def test_readyz_shows_the_known_fault_without_probing_or_leaking(self):
        from fastapi.testclient import TestClient

        from ai_sql_agent.api.app import app

        with TestClient(app) as client:
            healthy = client.get("/readyz").json()
            self.assertEqual(healthy["checks"]["model_credentials"], "ok")
            self.assertNotIn("model_access", healthy["checks"])
            self.assertEqual(self.service.calls, 0, "状态接口不探测模型")

            self.rejected_once()
            response = client.get("/readyz")
            body = response.json()

            self.assertEqual(response.status_code, 200, "只做展示，不因此摘流量")
            self.assertTrue(body["ready"])
            self.assertIn("rejected", body["checks"]["model_credentials"])
            (fault,) = body["checks"]["model_access"]
            self.assertEqual((fault["scope"], fault["reason"], fault["permanent"]),
                             ("credential", "credential_rejected", True))
            self.assertEqual(self.service.calls, 1, "再看多少次状态也不会多发一个请求")
            text = response.text
            self.assertNotIn(SECRET, text)
            self.assertNotIn("model.invalid", text)

            # 复位：走已有的管理员入口；它不验证凭证，下一个真实请求自己去试
            reset = client.post("/admin/circuit/reset").json()
            self.assertEqual(len(reset["model_access_before"]), 1)
            self.assertEqual(reset["model_access_after"], [])
            self.assertEqual(self.service.calls, 1)
            self.assertEqual(client.get("/readyz").json()["checks"]["model_credentials"], "ok")

    def test_admin_reset_also_lets_keyword_extraction_use_the_model_again(self):
        """
        抽词器在模型调用失败一次之后会一直用规则抽词，直到进程重启。
        管理员复位既然是「不重启的恢复方式」，就得把它也放回来。
        """
        from fastapi.testclient import TestClient

        from ai_sql_agent.api.app import app

        with TestClient(app) as client:
            extractor = app.state.pipeline.keyword_extractor
            question = "准时率低于0.9的供应商"
            # 模型抽词被拒，退回规则抽词
            self.assertEqual(extractor.extract(question), extractor.rule_extractor.extract(question))
            self.assertTrue(extractor._model_failed, "第一次被拒之后停用了模型抽词")
            self.assertEqual(self.service.calls, 1)

            client.post("/admin/circuit/reset")
            self.assertFalse(extractor._model_failed)
            self.assertEqual(MODEL_ACCESS.snapshot(), [])
            self.assertEqual(self.service.calls, 1, "复位不发请求")

    def test_there_is_no_reset_route_other_than_the_admin_one(self):
        from ai_sql_agent.api.app import app

        resets = sorted(
            route.path for route in app.routes
            if "reset" in getattr(route, "path", "") or "model_access" in getattr(route, "path", "")
        )
        self.assertEqual(resets, ["/admin/circuit/reset"])


class AdminResetIsProtectedTest(_ModelServiceEnvironment):
    EXTRA_ENV = {"AI_SQL_AUTH_MODE": "api_key"}

    def setUp(self):
        super().setUp()
        keys = self.directory / "keys.json"
        keys.write_text(json.dumps({
            "keys": [
                {"key": "k-admin", "caller": "ops", "user_id": "ops", "is_admin": True},
                {"key": "k-user", "caller": "buyer", "user_id": "buyer", "roles": ["everything"]},
            ],
            "policies": {"everything": {"allow_tables": ["*.*"]}},
        }), encoding="utf-8")
        os.environ["AI_SQL_API_KEYS_FILE"] = str(keys)
        self.addCleanup(os.environ.pop, "AI_SQL_API_KEYS_FILE", None)
        reload_settings()

    def test_only_an_admin_key_can_clear_the_state(self):
        from fastapi.testclient import TestClient

        from ai_sql_agent.api.app import app

        key = AccessKey.build("https://model.invalid/v1/messages", "anthropic", SECRET)
        with TestClient(app) as client:
            MODEL_ACCESS.block(key, scope="credential", reason="credential_rejected")

            self.assertEqual(client.post("/admin/circuit/reset").status_code, 401)
            self.assertEqual(
                client.post("/admin/circuit/reset", headers={"X-API-Key": "k-user"}).status_code, 403)
            self.assertEqual(len(MODEL_ACCESS.permanent_blocks()), 1, "非管理员清不掉")

            cleared = client.post("/admin/circuit/reset", headers={"X-API-Key": "k-admin"})
            self.assertEqual(cleared.status_code, 200)
            self.assertEqual(MODEL_ACCESS.permanent_blocks(), [])


if __name__ == "__main__":
    unittest.main()
