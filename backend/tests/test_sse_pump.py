"""/chat/stream 准备阶段的进度泵：progress 在 meta 之前、静默满时发心跳。"""
import asyncio

import pytest

from app.api.main import _pump_answer, _pump_until_done


async def _collect(gen):
    return [f async for f in gen]


async def test_progress_frames_are_emitted_while_waiting():
    q: asyncio.Queue = asyncio.Queue()

    async def work():
        q.put_nowait({"stage": "retrieval", "label": "正在检索知识库"})
        await asyncio.sleep(0.02)
        q.put_nowait({"stage": "execution", "label": "正在查询（ERP 订单库）"})
        await asyncio.sleep(0.02)
        return "done"
    task = asyncio.create_task(work())
    frames = await _collect(_pump_until_done(task, q, heartbeat_s=5))
    assert [f for f in frames if f.startswith("event: progress")] and len(frames) == 2
    assert "retrieval" in frames[0] and "execution" in frames[1]
    assert task.result() == "done"


async def test_heartbeat_when_silent():
    q: asyncio.Queue = asyncio.Queue()

    async def work():
        await asyncio.sleep(0.25)
    task = asyncio.create_task(work())
    frames = await _collect(_pump_until_done(task, q, heartbeat_s=0.1))
    assert frames and all(f == ": ping\n\n" for f in frames), "静默期只有心跳注释"
    assert len(frames) >= 2


async def test_leftover_progress_is_flushed_before_returning():
    """任务结束瞬间队列里还有进度，必须吐完再返回——保证 progress 在 meta 之前。"""
    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait({"stage": "answering", "label": "正在组织回答"})
    task = asyncio.create_task(asyncio.sleep(0))
    await asyncio.sleep(0.01)
    frames = await _collect(_pump_until_done(task, q, heartbeat_s=5))
    assert len(frames) == 1 and "answering" in frames[0]


# ── 作答阶段：编排器事件流 + 进度队列 + 心跳 ──────────────────────────────────

async def _answer(events, q, heartbeat_s=5.0):
    return [item async for item in _pump_answer(events, q, heartbeat_s=heartbeat_s)]


async def test_answer_pump_passes_progress_between_events():
    """工具循环里专家作答到一半去查库：那段时间的进度要在事件之间发出去，不攒到最后。"""
    q: asyncio.Queue = asyncio.Queue()

    async def events():
        yield {"type": "routing"}
        await asyncio.sleep(0.02)
        q.put_nowait({"stage": "helpdesk.tool_call", "label": "正在查业务系统"})
        await asyncio.sleep(0.02)
        yield {"type": "delta", "text": "已查到。"}

    out = await _answer(events(), q)
    assert [source for source, _ in out] == ["event", "frame", "event"]
    assert "helpdesk.tool_call" in out[1][1] and out[2][1]["text"] == "已查到。"


async def test_answer_pump_sends_heartbeats_while_the_stream_is_silent():
    q: asyncio.Queue = asyncio.Queue()

    async def events():
        await asyncio.sleep(0.25)
        yield {"type": "delta", "text": "好的。"}

    out = await _answer(events(), q, heartbeat_s=0.1)
    pings = [item for source, item in out if source == "frame"]
    assert len(pings) >= 2 and all(p == ": ping\n\n" for p in pings)
    assert out[-1] == ("event", {"type": "delta", "text": "好的。"})


async def test_answer_pump_flushes_leftover_progress_and_reraises_failures():
    q: asyncio.Queue = asyncio.Queue()

    async def events():
        yield {"type": "routing"}
        q.put_nowait({"stage": "helpdesk.tool_result", "label": "查询完成"})
        raise RuntimeError("编排器炸了")

    seen = []
    with pytest.raises(RuntimeError, match="编排器炸了"):
        async for item in _pump_answer(events(), q, heartbeat_s=5):
            seen.append(item)
    assert seen[0] == ("event", {"type": "routing"})
    assert any(source == "frame" and "helpdesk.tool_result" in item for source, item in seen)
