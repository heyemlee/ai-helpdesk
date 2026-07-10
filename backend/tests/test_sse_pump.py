"""/chat/stream 准备阶段的进度泵：progress 在 meta 之前、静默满时发心跳。"""
import asyncio

from api.main import _pump_until_done


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
