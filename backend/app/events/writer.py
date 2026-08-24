"""
Postgres 那一种写入器：在回答路径之外攒批写。

```
请求路径（同步、不 await、不抛）     后台（每个进程一个）
TurnRecorder.emit ──put_nowait──→ 有界队列 ──→ EventWriter：攒到 200 条或 250 毫秒
                  满了就丢、计数    10000 条      INSERT … ON CONFLICT DO NOTHING
                                                 失败退避重试 3 次，仍失败就丢掉、计数
                                                 停机：限时写完
```

写入口径是"最多一次"：进程崩溃会丢最后 250 毫秒，库挂了会丢重试之后的批次。这份日志是
工程数据，合规留档是 conversation_log，不受影响。丢了看得出来：seq 有缺口，或者没有
终结事件。
"""
from __future__ import annotations

import asyncio
import logging
import time

from app.core.metrics import TURN_EVENTS_QUEUE_DEPTH, TURN_EVENTS_WRITE_SECONDS
from app.events.recorder import EventLogStats
from app.events.types import Event

logger = logging.getLogger(__name__)


def _bad_row(ex: BaseException) -> bool:
    """
    这是不是"某一行存不下"一类的错误：数据异常（22 类，比如编码不对）、超出限制（54 类，比如
    索引项太长）、违反约束（23 类）。按 SQLSTATE 判，不依赖驱动的异常类。连不上库、超时不算。
    """
    state = str(getattr(ex, "sqlstate", "") or "")
    return state[:2] in {"22", "23", "54"}


class EventWriter:
    """把事件攒批写进 store（PgEventStore）。`put` 给记录器用，同步、O(1)、不抛。"""

    def __init__(
        self,
        store,
        stats: EventLogStats,
        *,
        max_queue: int = 10000,
        batch_size: int = 200,
        flush_s: float = 0.25,
        retries: int = 3,
        backoff_s: float = 0.5,
    ):
        self._store = store
        self._stats = stats
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=max_queue)
        self._batch_size = batch_size
        self._flush_s = flush_s
        self._retries = retries
        self._backoff_s = backoff_s
        self._task: asyncio.Task | None = None
        self._closed = False
        # 已经从队列里取出、还没写完的那一批。停机超时时它也算丢掉的
        self._holding: list[Event] = []

    @property
    def depth(self) -> int:
        return self._queue.qsize() + len(self._holding)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="turn-event-writer")

    def put(self, event: Event) -> None:
        if self._closed:
            self._stats.drop("shutdown")
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._stats.drop("queue_full")
        TURN_EVENTS_QUEUE_DEPTH.set(self._queue.qsize())

    async def _next_batch(self) -> None:
        """等到第一条，再最多等 flush_s 把后面的攒进来，满 batch_size 就不等了。"""
        self._holding.append(await self._queue.get())
        loop = asyncio.get_running_loop()
        until = loop.time() + self._flush_s
        while len(self._holding) < self._batch_size:
            try:
                self._holding.append(self._queue.get_nowait())
                continue
            except asyncio.QueueEmpty:
                pass
            remaining = until - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining, 0.05))

    async def _run(self) -> None:
        while True:
            await self._next_batch()
            batch = self._holding
            try:
                await self._write(batch)
            finally:
                self._holding = []
                for _ in batch:
                    self._queue.task_done()
                TURN_EVENTS_QUEUE_DEPTH.set(self._queue.qsize())

    async def _write(self, batch: list[Event]) -> None:
        for attempt in range(self._retries + 1):
            started = time.monotonic()
            try:
                await self._store.insert(batch)
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                self._stats.error(ex)
                if _bad_row(ex):
                    # 数据本身的问题（某一行存不下），重试多少次都一样：逐行写，只丢坏的那几行，
                    # 不让一条坏事件连累同一批里别的轮次
                    await self._write_rows(batch)
                    return
                if attempt == self._retries:
                    logger.warning("事件日志写入失败，丢掉 %d 条（%s）", len(batch), ex)
                    self._stats.drop("db_error", len(batch))
                    return
                await asyncio.sleep(self._backoff_s * (2 ** attempt))
                continue
            TURN_EVENTS_WRITE_SECONDS.observe(time.monotonic() - started)
            self._stats.wrote(len(batch))
            return

    async def _write_rows(self, batch: list[Event]) -> None:
        for event in batch:
            try:
                await self._store.insert([event])
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                self._stats.error(ex)
                logger.warning("事件日志有一条存不下，丢掉（%s %s #%d）: %s", event.type, event.turn_id,
                               event.seq, type(ex).__name__)
                self._stats.drop("db_error")
                continue
            self._stats.wrote(1)

    async def close(self, timeout_s: float = 10.0) -> None:
        """停机：限时把队列里剩下的写完。写不完的计成 shutdown 丢弃。"""
        self._closed = True
        if self._task is None:
            left = self._queue.qsize()
            if left:
                self._stats.drop("shutdown", left)
            return
        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout_s)
        except TimeoutError:
            left = self.depth
            logger.warning("事件日志没在 %.0f 秒内写完，丢掉剩下的 %d 条", timeout_s, left)
            if left:
                self._stats.drop("shutdown", left)
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
