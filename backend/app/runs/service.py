"""
API 这一侧：提交 run、查 run、续流、等结果。

**背压在门口。** 积压超过 `AI_HELPDESK_RUN_QUEUE_MAX` 就不收了，回 429 和 Retry-After。
收下一个注定要排很久的 run，客户等到的是超时；当场说"现在忙，几秒后再试"，客户端
（和上游网关）知道该怎么做。同一个 Idempotency-Key 的重复提交不算新负载，照样返回
原来那个 run。

**续流。** 一条连接对应一次读：先发一帧 `run`（run_id、状态、第几次执行），之后把帧流
原样转出去，每帧带 SSE 的 `id:`。断线重连时带上最后收到的 id（`Last-Event-ID`），从它
后面接着读。读的时候按 attempt 过滤：某一帧之后出现了第 n 次执行的 `attempt` 帧，比 n
小的帧都是被判失联的那一遍还在往外发的，丢掉。静默满一个心跳周期发一条注释，顺便
看一眼库：run 已经结束、帧流里却一直没有终结帧（帧流丢了），从库里的结局补出终结帧。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import AsyncIterator
from typing import Any

from app.core.metrics import RUN_BACKLOG, RUNS_SUBMITTED
from app.runs.config import RunConfig
from app.runs.records import DEAD, SUCCEEDED, TERMINAL_FRAMES, RunRecord, new_run_id
from app.runs.store import IdempotencyConflict, same_request

logger = logging.getLogger(__name__)


class Backpressure(Exception):
    """积压超过上限：这次不收。retry_after_s 是建议客户端等多久再来。"""

    def __init__(self, backlog: int, retry_after_s: int):
        super().__init__(f"积压 {backlog}，稍后再试")
        self.backlog = backlog
        self.retry_after_s = retry_after_s


def sse(event: str, data: dict[str, Any], event_id: str | None = None) -> str:
    """SSE 的一帧。和 api/main.py 的 `_sse` 一个格式，多一行可选的 id。"""
    head = f"id: {event_id}\n" if event_id else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def terminal_from_record(run: RunRecord) -> tuple[str, dict[str, Any]]:
    """帧流里没有终结帧时，从库里的结局补出来。"""
    if run.status in (SUCCEEDED, DEAD) and run.result is not None:
        return "result", run.result
    return "error", {"detail": run.error or f"run {run.status}"}


class RunService:
    def __init__(self, *, store, broker, channel, config: RunConfig, heartbeat_s: float = 15.0):
        self.store = store
        self.broker = broker
        self.channel = channel
        self.config = config
        self.heartbeat_s = heartbeat_s
        self._backlog_cache: tuple[float, int] = (0.0, 0)

    @property
    def instance(self) -> str:
        return self.store.instance

    async def backlog(self) -> int | None:
        """派发队列的积压。读不出来（Redis 连不上）返回 None。"""
        at, value = self._backlog_cache
        if time.monotonic() - at < 0.5:
            return value
        try:
            value = await asyncio.wait_for(self.broker.backlog(), timeout=2.0)
        except Exception:
            logger.warning("读不到派发队列的积压，这一次不做背压检查", exc_info=True)
            return None
        self._backlog_cache = (time.monotonic(), value)
        RUN_BACKLOG.labels(instance=self.instance).set(value)
        return value

    def retry_after(self, backlog: int) -> int:
        """建议多久后再来：超出上限越多等越久，2 到 60 秒。粗估，不是承诺。"""
        over = max(backlog - self.config.queue_max, 0)
        return int(min(60, max(2, 5 + math.ceil(over / 5))))

    async def submit(
        self,
        *,
        conv_id: str,
        user_id: str,
        message: str,
        request_id: str = "",
        idempotency_key: str = "",
        transport: str = "stream",
    ) -> tuple[RunRecord, bool]:
        """
        提交一轮。返回 (run, 是不是新建的)。

        同一个 Idempotency-Key 再来一次返回原来的 run（内容不一样抛 IdempotencyConflict）；
        积压超过上限抛 Backpressure。派发那一步失败不报错：run 已经落库了，清扫任务会在
        AI_HELPDESK_RUN_REDISPATCH_S 之后重新派发。
        """
        if idempotency_key:
            existing = await self.store.get_by_key(idempotency_key)
            if existing is not None:
                probe = RunRecord(run_id=existing.run_id, instance=self.instance, conv_id=conv_id,
                                  user_id=user_id, message=message)
                if not same_request(existing, probe):
                    raise IdempotencyConflict(existing)
                RUNS_SUBMITTED.labels(outcome="deduplicated").inc()
                return existing, False
        # 积压读不出来（Redis 连不上）时照收不误：run 落在 Postgres 里，派发这一步失败了也有
        # 清扫任务补——为一个读不到的数把客户挡在门外，不划算
        backlog = await self.backlog()
        if backlog is not None and backlog >= self.config.queue_max:
            RUNS_SUBMITTED.labels(outcome="rejected").inc()
            raise Backpressure(backlog, self.retry_after(backlog))
        run = RunRecord(
            run_id=new_run_id(), instance=self.instance, conv_id=conv_id, user_id=user_id,
            message=message, request_id=request_id, idempotency_key=idempotency_key,
            transport=transport,
        )
        stored, created = await self.store.create(run)
        if not created:
            RUNS_SUBMITTED.labels(outcome="deduplicated").inc()
            return stored, False
        try:
            await asyncio.wait_for(self.broker.dispatch(stored.run_id), timeout=2.0)
        except Exception:
            logger.warning("run %s 派发失败，等清扫任务重新派发", stored.run_id, exc_info=True)
        RUNS_SUBMITTED.labels(outcome="accepted").inc()
        if backlog is not None:
            self._backlog_cache = (self._backlog_cache[0], backlog + 1)
        return stored, True

    async def get(self, run_id: str) -> RunRecord | None:
        return await self.store.get(run_id)

    async def events(self, run: RunRecord, *, after: str | None = None) -> AsyncIterator[str]:
        """一个 run 的 SSE 帧：after 为空时从头读（先发一帧 run），否则从 after 后面接着读。"""
        if not after:
            yield sse("run", {"run_id": run.run_id, "conv_id": run.conv_id, "status": run.status,
                              "attempt": run.attempt})
        cursor = after or "0-0"
        attempt = 0
        # 一次最多阻塞 5 秒（Redis 连接的读超时是 10 秒，见 core/shared_state.REDIS_TIMEOUTS），
        # 静默满一个心跳周期才发心跳、看一眼库
        block_ms = max(int(min(self.heartbeat_s, 5.0) * 1000), 1)
        quiet_since = time.monotonic()
        while True:
            try:
                frames = await self.channel.read(run.run_id, cursor, block_ms=block_ms)
            except Exception:
                logger.warning("读 run %s 的帧流失败", run.run_id, exc_info=True)
                frames = []
                await asyncio.sleep(min(self.heartbeat_s, 1.0))
            for frame in frames:
                cursor = frame.id
                if frame.event == "attempt":
                    attempt = max(attempt, frame.attempt)
                elif frame.attempt < attempt:
                    continue                # 被判失联的那一遍还在发：丢掉
                yield sse(frame.event, frame.data, frame.id)
                if frame.event in TERMINAL_FRAMES:
                    return
            if frames:
                quiet_since = time.monotonic()
                continue
            if time.monotonic() - quiet_since < self.heartbeat_s:
                continue
            quiet_since = time.monotonic()
            current = await self.store.get(run.run_id)
            if current is None:
                yield sse("error", {"detail": "run 不存在或已超出保留期"})
                return
            if current.terminal:
                event, data = terminal_from_record(current)
                yield sse(event, data)
                return
            yield ": ping\n\n"

    async def wait(self, run_id: str, timeout_s: float) -> RunRecord | None:
        """等 run 结束（/chat 的非流式用）。超时返回当时的状态。"""
        deadline = time.monotonic() + timeout_s
        cursor = "0-0"
        while True:
            current = await self.store.get(run_id)
            if current is None or current.terminal:
                return current
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return current
            try:
                frames = await self.channel.read(run_id, cursor, block_ms=max(int(min(remaining, 1.0) * 1000), 1))
            except Exception:
                frames = []
                await asyncio.sleep(min(remaining, 0.5))
            if frames:
                cursor = frames[-1].id
