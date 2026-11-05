"""
流式的资源调度。

复现条件与 `evidence/sql_probes.py` 的 `a2a_shared_executor_starvation` 一致：
把**默认**线程池限成 2 线程、同时开 2 条流。当时的结果是
`pipeline_started: 0`——两个消费者用 `run_in_executor(None, events.get)` 阻塞式
等事件，把默认池占满，生产者的 `asyncio.to_thread` 一个线程都拿不到。

「2 个线程」是复现条件，不是在说生产部署只有 2 个线程。这组用例钉的是那个
**结构**：消费者不该占线程，生产者不该和别人抢池子。
"""
from __future__ import annotations

import asyncio
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from ai_sql_agent.a2a import server as a2a
from ai_sql_agent.core import get_settings, reload_settings


class _Principal:
    caller = "test"
    user_id = "u_stream"


class StarvationTest(unittest.IsolatedAsyncioTestCase):
    async def test_pipeline_starts_even_when_the_default_pool_is_exhausted(self):
        started = []

        def fake_run(request, principal, parsed, began):
            started.append(True)
            time.sleep(0.01)
            return None

        # 默认池限成 2 线程：这正是曾让 pipeline 启动数归零的那个条件
        pool = ThreadPoolExecutor(max_workers=2)
        asyncio.get_running_loop().set_default_executor(pool)

        async def consume(index: int):
            permit = a2a._admission()
            permit.try_acquire()
            generator = a2a._stream_events(
                None, index, _Principal(),
                a2a.ParsedMessage("q", f"c{index}", "u", "m"),
                str(index), permit,
            )
            async for _event in generator:
                pass

        try:
            with patch.object(a2a, "_run_and_audit", fake_run):
                tasks = [asyncio.create_task(consume(i)) for i in range(2)]
                await asyncio.sleep(0.3)
                pipeline_started = len(started)
                completed = sum(task.done() for task in tasks)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        self.assertEqual(
            pipeline_started, 2,
            "两条流都该把 pipeline 跑起来；为 0 就是那个饥饿又回来了",
        )
        self.assertEqual(completed, 2, "两条流都该正常收尾")


class AdmissionTest(unittest.IsolatedAsyncioTestCase):
    async def test_admission_rejects_instead_of_queueing(self):
        """
        满了要**拒绝**，不是排队。排队只是把「拒绝」换成「全都超时」，
        而调用方 90s 硬上限一到照样全丢（契约 §5.5）。
        """
        admission = a2a._Admission(limit=1)
        self.assertTrue(admission.try_acquire())
        self.assertFalse(admission.try_acquire())
        admission.release()
        self.assertTrue(admission.try_acquire())

    async def test_release_never_goes_negative(self):
        admission = a2a._Admission(limit=1)
        admission.release()
        admission.release()
        self.assertTrue(admission.try_acquire())


class BackpressureTest(unittest.IsolatedAsyncioTestCase):
    """
    有界队列：进度事件在压力下可以丢，**终态和载荷不能丢**。

    以前是无界 `queue.Queue`，客户端读得慢时事件只在内存里堆着，堆多少没人知道。
    """

    async def test_progress_is_droppable_but_the_terminal_frame_is_not(self):
        saved = get_settings().a2a_stream_queue_size
        get_settings().a2a_stream_queue_size = 8
        emitted = []

        def slow_run(request, principal, parsed, began):
            # 塞满队列：远超 maxsize 的进度事件
            from ai_sql_agent.core.trace import stage

            for index in range(200):
                with stage("execution", step=index + 1, database="erp_db"):
                    pass
            return _Summary()

        try:
            with patch.object(a2a, "_run_and_audit", slow_run), \
                 patch.object(a2a, "_artifact", lambda *a, **k: {"parts": []}):
                permit = a2a._admission()
                permit.try_acquire()
                generator = a2a._stream_events(
                    None, "id", _Principal(),
                    a2a.ParsedMessage("q", "ctx", "u", "m"), "task", permit,
                )
                async for chunk in generator:
                    emitted.append(chunk)
        finally:
            get_settings().a2a_stream_queue_size = saved

        self.assertTrue(emitted)
        self.assertIn('"kind": "task"', emitted[0])
        # 不管中间丢了多少条进度，终态必须发出来且是最后一条
        self.assertIn('"final": true', emitted[-1])
        self.assertIn('"completed"', emitted[-1])


class EarlyDisconnectTest(unittest.IsolatedAsyncioTestCase):
    """
    客户端在首帧之后立刻断开：并发名额必须还回去。

    首帧的 yield 以前写在 try/finally 外面，生成器在那个 yield 上被关掉时 finally 不会跑——
    名额永远不还，满 `a2a_stream_max_concurrent` 次之后新流全被 429。
    """

    async def test_disconnect_right_after_the_first_frame_releases_the_permit(self):
        def slow_run(request, principal, parsed, began):
            time.sleep(0.05)
            return _Summary()

        permit = a2a._admission()
        self.assertTrue(permit.try_acquire())
        generator = a2a._stream_events(
            None, "id", _Principal(), a2a.ParsedMessage("q", "ctx", "u", "m"), "task", permit,
        )
        with patch.object(a2a, "_run_and_audit", slow_run):
            first = await generator.__anext__()
            await generator.aclose()  # 客户端此刻断开

        self.assertIn('"kind": "task"', first)
        self.assertEqual(permit.active, 0)


class _Summary:
    answer = "ok"

    def data_part(self):
        return {"steps": []}


class ExecutorLifecycleTest(unittest.TestCase):
    def test_pool_is_rebuilt_when_the_size_changes(self):
        saved = get_settings().a2a_stream_worker_threads
        try:
            get_settings().a2a_stream_worker_threads = 3
            first = a2a._stream_executor()
            self.assertEqual(first._max_workers, 3)
            get_settings().a2a_stream_worker_threads = 5
            second = a2a._stream_executor()
            self.assertEqual(second._max_workers, 5)
            self.assertIsNot(first, second)
        finally:
            get_settings().a2a_stream_worker_threads = saved
            a2a.shutdown_stream_executor()
            reload_settings()


if __name__ == "__main__":
    unittest.main()
