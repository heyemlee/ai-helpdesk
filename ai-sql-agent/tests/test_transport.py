import unittest

import httpx

from ai_sql_agent.core import (
    ModelAccessRegistry,
    ModelAuthError,
    ModelRateLimitError,
    ModelResponseError,
    ModelServerError,
    ModelTimeoutError,
    ModelTransport,
    RetryPolicy,
    extract_chat_content,
    usage_scope,
    usage_summary,
    use_trace_id,
)


def make_transport(handler, *, max_retries=2):
    """
    构造一个走 MockTransport 的 ModelTransport，重试不真的 sleep。

    每个 transport 用自己的一张模型访问状态表：进程内共用的那张会记住「这把 Key 被拒过」，
    一个用例里的 401 不该让后面用同一个假地址、假 Key 的用例发不出请求。
    """
    return ModelTransport(
        "coder",
        api_key="test-key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        retry_policy=RetryPolicy(max_retries=max_retries, backoff_base_seconds=0),
        sleeper=lambda seconds: None,
        access_registry=ModelAccessRegistry(),
    )


class RetryPolicyTest(unittest.TestCase):
    def test_backoff_grows_and_is_capped(self):
        policy = RetryPolicy(
            max_retries=5, backoff_base_seconds=1.0, backoff_max_seconds=4.0
        )
        # 抖动区间 [0.5, 1.0]，只断言量级与上限
        self.assertLessEqual(policy.delay_for(0), 1.0)
        self.assertGreaterEqual(policy.delay_for(3), 2.0)
        self.assertLessEqual(policy.delay_for(10), 4.0)

    def test_retry_after_header_wins(self):
        policy = RetryPolicy(backoff_base_seconds=10.0, backoff_max_seconds=30.0)
        self.assertEqual(policy.delay_for(0, retry_after=2.0), 2.0)


class TransportRetryTest(unittest.TestCase):
    def test_retries_rate_limit_then_succeeds(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(429, headers={"Retry-After": "0"}, text="slow down")
            return httpx.Response(200, json={"ok": True})

        result = make_transport(handler).post_json("https://model/v1", {}, model="qwen-plus")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls["n"], 3)

    def test_gives_up_after_max_retries(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(500, text="boom")

        with self.assertRaises(ModelServerError):
            make_transport(handler, max_retries=2).post_json("https://model/v1", {})

        self.assertEqual(calls["n"], 3)

    def test_auth_error_is_not_retried(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(401, text="bad key")

        with self.assertRaises(ModelAuthError):
            make_transport(handler).post_json("https://model/v1", {})

        self.assertEqual(calls["n"], 1)

    def test_timeout_is_classified_and_retried(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            raise httpx.ReadTimeout("timed out", request=request)

        with self.assertRaises(ModelTimeoutError):
            make_transport(handler, max_retries=1).post_json("https://model/v1", {})

        self.assertEqual(calls["n"], 2)

    def test_non_json_body_raises_response_error(self):
        def handler(request):
            return httpx.Response(200, text="<html>gateway</html>")

        with self.assertRaises(ModelResponseError):
            make_transport(handler).post_json("https://model/v1", {})


class UsageAccountingTest(unittest.TestCase):
    def test_usage_is_accumulated_per_request(self):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "SELECT 1;"}}],
                    "usage": {"prompt_tokens": 120, "completion_tokens": 30},
                },
            )

        transport = make_transport(handler)

        with use_trace_id():
            transport.post_json("https://model/v1", {}, model="qwen-plus")
            transport.post_json("https://model/v1", {}, model="qwen-plus")
            summary = usage_summary()

        self.assertEqual(summary["calls"], 2)
        self.assertEqual(summary["total_tokens"], 300)
        self.assertEqual(summary["by_component"], {"coder": 300})

    def test_usage_outside_trace_context_is_ignored(self):
        self.assertEqual(usage_summary()["calls"], 0)

    def test_usage_scope_collects_without_an_http_entrypoint(self):
        # Pipeline 被当库直接调用（benchmark、脚本）时没有 use_trace_id()，
        # 用量不该整条丢掉
        transport = make_transport(self._usage_handler())

        with usage_scope():
            transport.post_json("https://model/v1", {}, model="qwen-plus")
            summary = usage_summary()

        self.assertEqual(summary["calls"], 1)
        self.assertEqual(summary["total_tokens"], 150)

    def test_nested_usage_scope_keeps_the_outer_accumulator(self):
        # 内层不能新建累加器，否则 HTTP 入口拿到的汇总会漏掉内层的调用
        transport = make_transport(self._usage_handler())

        with use_trace_id():
            transport.post_json("https://model/v1", {}, model="qwen-plus")
            with usage_scope():
                transport.post_json("https://model/v1", {}, model="qwen-plus")
            summary = usage_summary()

        self.assertEqual(summary["calls"], 2)
        self.assertEqual(summary["total_tokens"], 300)

    @staticmethod
    def _usage_handler():
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "SELECT 1;"}}],
                    "usage": {"prompt_tokens": 120, "completion_tokens": 30},
                },
            )

        return handler


class ExtractChatContentTest(unittest.TestCase):
    def test_extracts_and_strips(self):
        result = {"choices": [{"message": {"content": "  SELECT 1;  "}}]}
        self.assertEqual(
            extract_chat_content(result, component="coder", model="m"), "SELECT 1;"
        )

    def test_missing_field_raises_response_error(self):
        with self.assertRaises(ModelResponseError):
            extract_chat_content({"error": "nope"}, component="coder", model="m")


class StreamTest(unittest.TestCase):
    def test_sse_stream_yields_content_deltas(self):
        body = (
            'data: {"choices":[{"delta":{"content":"步骤"}}]}\n'
            'data: {"choices":[{"delta":{"content":"1"}}]}\n'
            "data: [DONE]\n"
        )

        def handler(request):
            return httpx.Response(200, text=body)

        chunks = list(
            make_transport(handler).stream_sse("https://model/v1", {}, model="qwen-plus")
        )
        self.assertEqual(chunks, ["步骤", "1"])


if __name__ == "__main__":
    unittest.main()
