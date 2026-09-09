"""
A2A 客户端：契约里「调用方」那一半的硬规则，用 httpx.MockTransport 钉住，不发网络。
"""
import asyncio
import json

import httpx
import pytest

from app.tools import a2a_client as mod
from app.tools.a2a_client import A2AClient, A2AError, StreamUnsupported, progress_label


def _sse(*results):
    body = "".join("data: " + json.dumps({"jsonrpc": "2.0", "id": "r", "result": r}) + "\n\n" for r in results)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


TASK = {"kind": "task", "id": "t1", "contextId": "c1", "status": {"state": "submitted"}}
def PROG(stage, **m):
    return {"kind": "status-update", "taskId": "t1", "contextId": "c1", "final": False,
            "status": {"state": "working", "message": {"parts": [{"kind": "text", "text": "x"}]}},
            "metadata": {"stage": stage, "trace_id": "tr", **m}}
ART = {"kind": "artifact-update", "taskId": "t1", "contextId": "c1", "lastChunk": True,
       "artifact": {"artifactId": "a", "parts": [
           {"kind": "text", "text": "订单已排产。"},
           {"kind": "data", "data": {"route": "database_query", "queried_database": True,
                                     "selected_databases": ["erp_db"], "has_risk": False,
                                     "degraded": False, "degrade_reason": "", "trace_id": "tr"}}]}}
DONE = {"kind": "status-update", "taskId": "t1", "contextId": "c1", "final": True,
        "status": {"state": "completed"}, "metadata": {"trace_id": "tr"}}
FAILED = {**DONE, "status": {"state": "failed"}, "metadata": {"trace_id": "tr", "error": "炸了"}}


def _client(handler, **kw) -> A2AClient:
    c = A2AClient("http://x", **kw)
    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient

    class Patched(orig):
        def __init__(self, *a, **k):
            k["transport"] = transport
            super().__init__(*a, **k)
    mod.httpx.AsyncClient = Patched
    return c


@pytest.fixture(autouse=True)
def _restore():
    orig = httpx.AsyncClient
    yield
    mod.httpx.AsyncClient = orig


def _card(streaming):
    return httpx.Response(200, json={"capabilities": {"streaming": streaming}})


# ── §5.1 唯一的成功判据 ───────────────────────────────────────────────────────

async def test_success_requires_final_completed_and_artifact():
    def h(req):
        return _sse(TASK, PROG("retrieval"), ART, DONE)
    out = await _client(h).stream("q", "u", "s")
    assert out["answer"] == "订单已排产。"
    assert out["queried_database"] is True and out["trace_id"] == "tr"


async def test_artifact_without_terminal_is_a_failure():
    """只收到 artifact、没收到终态，也算失败——堵死「半个结果被当成成功」。"""
    with pytest.raises(A2AError, match="没有终态"):
        await _client(lambda r: _sse(TASK, ART)).stream("q", "u", "s")


async def test_failed_terminal_is_a_failure():
    with pytest.raises(A2AError, match="failed"):
        await _client(lambda r: _sse(TASK, FAILED)).stream("q", "u", "s")


async def test_terminal_without_artifact_is_a_failure():
    with pytest.raises(A2AError, match="没有收到 artifact"):
        await _client(lambda r: _sse(TASK, DONE)).stream("q", "u", "s")


# ── §3.2 / §6.2：流打开之前的 JSON 错误，-32004 才可降级 ──────────────────────

async def test_32004_before_stream_is_stream_unsupported():
    def h(req):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "r",
                                         "error": {"code": -32004, "message": "off"}})
    with pytest.raises(StreamUnsupported):
        await _client(h).stream("q", "u", "s")


async def test_other_json_errors_are_not_downgradeable():
    def h(req):
        return httpx.Response(401, json={"jsonrpc": "2.0", "id": "r",
                                         "error": {"code": -32600, "message": "bad key"}})
    with pytest.raises(A2AError) as ei:
        await _client(h).stream("q", "u", "s")
    assert not isinstance(ei.value, StreamUnsupported)


# ── §1 A：静默 20s 判死；总长硬上限 ─────────────────────────────────────────

async def test_silence_timeout_kills_the_stream():
    async def slow_body():
        yield ("data: " + json.dumps({"jsonrpc": "2.0", "id": "r", "result": TASK}) + "\n\n").encode()
        await asyncio.sleep(0.5)
        yield b"data: {}\n\n"

    def h(req):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=httpx.AsyncByteStream.__new__(type("S", (httpx.AsyncByteStream,),
                                                                        {"__aiter__": lambda self: slow_body()})))
    with pytest.raises(A2AError, match="静默"):
        await _client(h, stream_silence_s=0.1, stream_hard_s=5).stream("q", "u", "s")


# ── §4.5：进度映射与 repair step 递增 ────────────────────────────────────────

async def test_progress_is_mapped_and_hidden_stages_are_skipped():
    seen = []
    def h(req):
        return _sse(TASK, PROG("normalization"), PROG("execution", step=1, database="erp_db"),
                    PROG("validation"), ART, DONE)
    await _client(h).stream("q", "u", "s", on_progress=seen.append)
    assert [e["stage"] for e in seen] == ["execution"], "normalization/validation 不展示"
    assert seen[0]["label"] == "正在查询（ERP 订单库）"


def test_progress_label_table():
    assert progress_label("retrieval") == "正在检索知识库"
    assert progress_label("generation", "mes_db") == "正在生成查询（MES 生产库）"
    assert progress_label("validation") is None


async def test_repair_step_must_increase(caplog):
    def h(req):
        return _sse(TASK, PROG("repair", step=1), PROG("repair", step=1), ART, DONE)
    await _client(h).stream("q", "u", "s")
    assert "未递增" in caplog.text


# ── message/send ─────────────────────────────────────────────────────────────

async def test_send_normalizes_like_query():
    def h(req):
        body = json.loads(req.content)
        assert body["method"] == "message/send"
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "r", "result": {
            "kind": "task", "id": "t", "contextId": "s", "status": {"state": "completed"},
            "artifacts": [ART["artifact"]], "metadata": {"trace_id": "tr"}}})
    out = await _client(h).send("q", "u", "s")
    assert out["answer"] == "订单已排产。" and out["selected_databases"] == ["erp_db"]


async def test_send_failed_task_raises():
    def h(req):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "r", "result": {
            "kind": "task", "id": "t", "contextId": "s", "status": {"state": "failed"},
            "artifacts": [], "metadata": {"error": "no"}}})
    with pytest.raises(A2AError):
        await _client(h).send("q", "u", "s")


# ── Card 缓存与降级 ───────────────────────────────────────────────────────────

async def test_card_failure_means_no_streaming():
    def h(req):
        return httpx.Response(500)
    assert await _client(h).streaming_supported() is False
