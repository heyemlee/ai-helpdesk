import tempfile
import unittest

from fastapi import HTTPException

from ai_sql_agent.api.limits import QuotaGuard, TokenBucketLimiter
from ai_sql_agent.audit import AuditRecord, SQLiteAuditStore
from ai_sql_agent.auth import Principal


class TokenBucketTest(unittest.TestCase):
    def setUp(self):
        self.now = {"t": 0.0}
        self.limiter = TokenBucketLimiter(clock=lambda: self.now["t"])

    def test_burst_up_to_capacity_then_rejects(self):
        passed = sum(
            1 for _ in range(5) if self.limiter.acquire("caller", 3) is None
        )
        self.assertEqual(passed, 3)

        retry_after = self.limiter.acquire("caller", 3)
        self.assertIsNotNone(retry_after)
        self.assertGreater(retry_after, 0)

    def test_refills_over_time(self):
        for _ in range(3):
            self.limiter.acquire("caller", 3)
        self.assertIsNotNone(self.limiter.acquire("caller", 3))

        self.now["t"] = 60.0
        self.assertIsNone(self.limiter.acquire("caller", 3))

    def test_zero_limit_means_unlimited(self):
        for _ in range(100):
            self.assertIsNone(self.limiter.acquire("caller", 0))

    def test_callers_have_independent_buckets(self):
        for _ in range(3):
            self.limiter.acquire("a", 3)
        self.assertIsNotNone(self.limiter.acquire("a", 3))
        self.assertIsNone(self.limiter.acquire("b", 3))


class AuditStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = SQLiteAuditStore(tempfile.mktemp(suffix=".db"))

    def _record(self, **overrides):
        payload = dict(
            trace_id="t1",
            caller="li",
            user_id="li",
            session_id="s1",
            query="供应商名称是多少",
        )
        payload.update(overrides)
        record = AuditRecord(**payload)
        self.store.record(record)
        return record

    def test_record_and_read_back(self):
        self._record(
            route="database_query",
            databases=["erp_db"],
            tables=["supplier"],
            sqls=[
                {
                    "database": "erp_db",
                    "sql": "SELECT supplier_name FROM supplier",
                }
            ],
            prompt_tokens=120,
            completion_tokens=30,
        )
        rows = self.store.recent(1)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["databases"], ["erp_db"])
        self.assertEqual(
            rows[0]["sqls"],
            [
                {
                    "database": "erp_db",
                    "sql": "SELECT supplier_name FROM supplier",
                }
            ],
        )
        self.assertTrue(rows[0]["success"])

    def test_daily_usage_aggregates_queries_and_tokens(self):
        self._record(prompt_tokens=100, completion_tokens=20)
        self._record(prompt_tokens=50, completion_tokens=10)
        self._record(user_id="other", prompt_tokens=999)

        usage = self.store.daily_usage("li")
        self.assertEqual(usage.queries, 2)
        self.assertEqual(usage.tokens, 180)

    def test_denied_tables_are_recorded(self):
        self._record(success=False, status_code=200, denied_tables=["salary"])
        row = self.store.recent(1)[0]
        self.assertEqual(row["denied_tables"], ["salary"])
        self.assertFalse(row["success"])

    def test_filter_by_user(self):
        self._record(user_id="a")
        self._record(user_id="b")
        self.assertEqual(len(self.store.recent(10, user_id="a")), 1)


class QuotaGuardTest(unittest.TestCase):
    def setUp(self):
        self.store = SQLiteAuditStore(tempfile.mktemp(suffix=".db"))
        self.guard = QuotaGuard(self.store, cache_seconds=0)

    def _principal(self, **overrides):
        payload = dict(caller="li", user_id="li")
        payload.update(overrides)
        return Principal(**payload)

    def _consume(self, count=1, tokens=0):
        for _ in range(count):
            self.store.record(
                AuditRecord(
                    trace_id="t",
                    caller="li",
                    user_id="li",
                    session_id="s",
                    query="q",
                    prompt_tokens=tokens,
                )
            )

    def test_no_quota_configured_passes(self):
        self._consume(100)
        self.guard.check(self._principal())

    def test_query_quota_blocks_when_reached(self):
        self._consume(2)
        with self.assertRaises(HTTPException) as ctx:
            self.guard.check(self._principal(daily_query_quota=2))
        self.assertEqual(ctx.exception.status_code, 429)

    def test_token_quota_blocks_when_reached(self):
        self._consume(1, tokens=5000)
        with self.assertRaises(HTTPException) as ctx:
            self.guard.check(self._principal(daily_token_quota=4000))
        self.assertEqual(ctx.exception.status_code, 429)

    def test_under_quota_passes(self):
        self._consume(1)
        self.guard.check(self._principal(daily_query_quota=5))


class MetricsRenderTest(unittest.TestCase):
    def test_histogram_buckets_are_cumulative(self):
        from ai_sql_agent.core.metrics import Histogram

        histogram = Histogram("test_seconds", "帮助文本", buckets=(0.1, 1.0))
        histogram.observe(0.05)
        histogram.observe(0.5)
        histogram.observe(5.0)

        text = "\n".join(histogram.collect())
        self.assertIn('test_seconds_bucket{le="0.1"} 1', text)
        self.assertIn('test_seconds_bucket{le="1"} 2', text)
        self.assertIn('test_seconds_bucket{le="+Inf"} 3', text)
        self.assertIn("test_seconds_count 3", text)
        self.assertIn("test_seconds_sum 5.55", text)

    def test_label_values_are_escaped(self):
        from ai_sql_agent.core.metrics import Counter

        counter = Counter("test_total", "帮助文本", ("path",))
        counter.inc(path='a"b')
        self.assertIn('path="a\\"b"', "\n".join(counter.collect()))

    def test_registry_renders_type_and_help(self):
        from ai_sql_agent.core import render_metrics

        text = render_metrics()
        self.assertIn("# TYPE ai_sql_requests_total counter", text)
        self.assertIn("# TYPE ai_sql_request_duration_seconds histogram", text)
        self.assertTrue(text.endswith("\n"))


class ProbeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        from ai_sql_agent.api.app import app

        cls.client_ctx = TestClient(app)
        cls.client = cls.client_ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_ctx.__exit__(None, None, None)

    def test_health_does_not_touch_dependencies(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})

    def test_readyz_reports_checks(self):
        body = self.client.get("/readyz").json()
        self.assertTrue(body["ready"])
        self.assertIn("memory_store", body["checks"])
        self.assertIn("audit_store", body["checks"])
        self.assertIn("pgvector", body["checks"])

    def test_metrics_endpoint_is_prometheus_text(self):
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertIn("ai_sql_up 1", response.text)
        self.assertIn("# TYPE", response.text)


if __name__ == "__main__":
    unittest.main()
