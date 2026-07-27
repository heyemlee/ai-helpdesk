"""
HTTP 指标的两个坑：标签基数，以及流式响应的计时点。

两个都不会让请求失败——它们只让**面板上的数字是错的**，而错的数字比没有数字
更糟：没有数字时人会去查日志，有数字时人会信它。
"""
import importlib
import sys

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY


@pytest.fixture
def app_module(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("AI_HELPDESK_FRONTEND_DIST", "/nonexistent")
    monkeypatch.setenv("AI_HELPDESK_API_KEY", "")
    monkeypatch.setenv("APP_ENV", "development")
    sys.modules.pop("app.api.main", None)
    return importlib.import_module("app.api.main")


def _paths_seen() -> set[str]:
    """当前 http_requests_total 上出现过的 path 标签值。"""
    seen = set()
    for metric in REGISTRY.collect():
        if metric.name != "http_requests":
            continue
        for sample in metric.samples:
            if sample.name.endswith("_total"):
                seen.add(sample.labels.get("path", ""))
    return seen


class TestLabelCardinalityIsBounded:
    def test_unmatched_paths_collapse_into_one_label(self, app_module):
        """
        404 的路径**不能**原样进标签。

        标签值不回收：一个扫描器打一串不存在的路径，就能让时序数量无限增长，
        直到 Prometheus 吃不消——而这类流量恰恰是最多的那种。
        """
        client = TestClient(app_module.app)
        for path in ("/nope-1", "/nope-2", "/admin.php", "/.env"):
            client.get(path)

        seen = _paths_seen()
        assert app_module.UNMATCHED_PATH in seen
        for path in ("/nope-1", "/nope-2", "/admin.php", "/.env"):
            assert path not in seen, f"原始路径 {path} 进了标签，基数是无界的"

    def test_matched_routes_still_use_the_template(self, app_module):
        """有界的那一半不能跟着丢：模板本身是有限集合，要留住。"""
        TestClient(app_module.app).get("/monitor")
        assert "/monitor" in _paths_seen()

    def test_rate_limit_counter_has_no_user_id_label(self):
        """
        限流计数器不带 user_id。

        被限流的往往正是刷接口的调用方，它们制造的 user_id 最多——
        把它当标签等于让攻击者决定时序数量。要知道是谁就去看日志。
        """
        from app.core.metrics import RATE_LIMIT_HITS
        assert RATE_LIMIT_HITS._labelnames == (), \
            f"限流计数器还带着标签 {RATE_LIMIT_HITS._labelnames}"
        RATE_LIMIT_HITS.inc()  # 不带标签也要能直接 inc


class TestStreamingDurationIsMeasuredAtTheEnd:
    def test_a_streaming_response_is_timed_over_the_whole_stream(self, app_module):
        """
        流式响应的耗时要量到**流真的结束**，不是量到"响应对象造出来了"。

        `call_next` 返回时 SSE 一个字节都还没发。在那一刻记时间，量到的是
        毫秒级的对象构造耗时，而真实的 /chat/stream 是十几秒——p95 面板上
        那条线看着漂亮，其实把这条路径的真实延迟整个藏了起来。
        """
        import asyncio

        from fastapi.responses import StreamingResponse

        @app_module.app.get("/__test_stream")
        async def _stream():                       # pragma: no cover - 测试用路由
            async def _gen():
                for _ in range(3):
                    await asyncio.sleep(0.05)
                    yield b"data: x\n\n"
            return StreamingResponse(_gen(), media_type="text/event-stream")

        client = TestClient(app_module.app)
        before = _duration_sum("/__test_stream")
        resp = client.get("/__test_stream")
        assert resp.status_code == 200
        elapsed = _duration_sum("/__test_stream") - before

        assert elapsed >= 0.15, (
            f"记到的耗时只有 {elapsed:.3f}s，说明还是在响应对象造出来的那一刻就记了，"
            "而不是等这条流跑完"
        )


def _duration_sum(path: str) -> float:
    for metric in REGISTRY.collect():
        if metric.name != "http_request_duration_seconds":
            continue
        for sample in metric.samples:
            if sample.name.endswith("_sum") and sample.labels.get("path") == path:
                return sample.value
    return 0.0
