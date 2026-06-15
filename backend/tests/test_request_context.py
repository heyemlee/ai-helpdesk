"""请求追踪上下文与结构化日志的行为测试。"""
import asyncio
import json
import logging

from core.request_context import (
    JsonFormatter,
    RequestIdFilter,
    current_request_id,
    new_request_id,
    request_id_var,
)


def _make_record(msg: str = "hello") -> logging.LogRecord:
    return logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=None,
    )


def test_default_request_id_is_dash():
    assert current_request_id() == "-"


async def test_request_id_propagates_across_async_calls():
    """contextvar 在 async 调用链里自动传播——这是编排器日志能对上的前提。"""
    async def deep_call() -> str:
        await asyncio.sleep(0)
        return current_request_id()

    token = request_id_var.set("req-abc")
    try:
        assert await deep_call() == "req-abc"
    finally:
        request_id_var.reset(token)


async def test_concurrent_requests_do_not_leak_ids():
    """两个并发请求各自持有自己的 request_id，互不污染。"""
    async def handler(rid: str) -> str:
        token = request_id_var.set(rid)
        try:
            await asyncio.sleep(0.01)
            return current_request_id()
        finally:
            request_id_var.reset(token)

    results = await asyncio.gather(handler("req-1"), handler("req-2"))
    assert results == ["req-1", "req-2"]


def test_filter_injects_request_id():
    record = _make_record()
    token = request_id_var.set("req-xyz")
    try:
        assert RequestIdFilter().filter(record)
    finally:
        request_id_var.reset(token)
    assert record.request_id == "req-xyz"


def test_json_formatter_produces_parseable_line():
    record = _make_record("用户请求失败")
    record.request_id = "req-1"
    line = JsonFormatter().format(record)
    payload = json.loads(line)
    assert payload["message"] == "用户请求失败"
    assert payload["request_id"] == "req-1"
    assert payload["level"] == "INFO"
    assert "\n" not in line  # 单行，日志采集器按行切分


def test_json_formatter_includes_exception():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="failed", args=(), exc_info=sys.exc_info(),
        )
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exc_info"]


def test_new_request_id_is_unique():
    ids = {new_request_id() for _ in range(100)}
    assert len(ids) == 100
