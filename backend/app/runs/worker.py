"""
worker：领 run、续租、执行、写结局；接手失联的 run；清扫。

一个 worker 进程里有三个循环：

1. **消费**：有空位就从自己实例的派发流里读一条（XREADGROUP），交给一个任务去跑。
   同时在跑的不超过 `AI_HELPDESK_WORKER_CONCURRENCY` 个——一轮对话大部分时间在等
   模型和下游，一个进程跑几个是划算的，但不能无限：每一个都占着模型的配额。
2. **接手**：每隔半个租约，用 XAUTOCLAIM 捡闲置超过 1.5 个租约的消息。那些消息的
   主人多半已经不在了（进程被杀、机器没了）；还活着的 worker 会在续租时把自己的消息
   摸一下，不会闲置到这个程度。
3. **清扫**：库里 `queued` 太久没人领的（派发那一步在 API 挂掉了、Redis 丢了消息）、
   租约过期太久的，重新派发。几个 worker 同时清扫只会多派发几条，领取时会被去重；
   有一把短锁让通常只有一个在扫。顺便删掉过了保留期的 run。

执行一个 run：

    领取（条件 UPDATE，attempt + 1） → 发一帧 attempt → 跑（main.py 给的 execute）
       ├─ 每 1/3 租约续租一次，同时把派发消息的闲置时间清零
       │    续租改不到行 = 已经被别人领走了：取消这一遍，结果作废，消息不确认
       ├─ 回答定下来 → 写提交点（带栅栏）→ 落副作用（工单、记忆、审计，都按 run 去重）
       └─ 跑完 → 写结局（带栅栏）→ 发终结帧 → XACK

**提交点。** 回答定下来、副作用还没落的那一刻，把回答写进库。之后失联的话，接手的那一遍
看到提交点就不再重新作答，照着它把副作用补完、发出同一个回答。没有它，"落完副作用、
还没写结局就被杀"的那一遍会让重跑重新作答：审计和记忆里是第一遍的回答（去重挡住了
第二遍），客户看到的却是第二遍的。

**先写库、再发终结帧、最后确认。** 写库失败（租约丢了）就什么都不发、不确认，让真正
拿着租约的那一遍收尾；发帧失败不要紧，续流的一方会从库里补出终结帧；确认失败
不要紧，消息被别人接手时领取会发现 run 已经结束，直接确认掉。

**停机（drain）。** 先停止读新消息，等手上的跑完；到时限还没跑完的，主动交还：
库里改回 queued、重新派发、确认原来那条消息——别的 worker 马上就能接手，不用等租约
过期。交还的那一次不算失联，不计入死信的次数。
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.metrics import (
    RUN_ATTEMPTS,
    RUN_BACKLOG,
    RUN_DEGRADED,
    RUN_DURATION,
    RUN_LEASE_LOST,
    RUN_QUEUE_WAIT,
    RUNS_FINISHED,
    RUNS_IN_FLIGHT,
)
from app.runs.config import RunConfig
from app.runs.degrade import NONE, Degrade, DegradePolicy
from app.runs.memo import RunScope
from app.runs.records import DEAD, FAILED, RunRecord
from app.runs.store import lost_attempts

logger = logging.getLogger(__name__)


@dataclass
class RunOutcome:
    """execute 的产出：这一轮的结局，和要发给客户端的终结帧。"""

    status: str                         # SUCCEEDED 或 FAILED
    terminal: tuple[str, dict[str, Any]]  # ("result", 载荷) 或 ("error", {"detail": ...})
    result: dict[str, Any] | None = None  # 存进库的响应体（/chat 的形状）
    error: str = ""


@dataclass
class RunContext:
    """
    execute 拿到的东西：哪个 run、按哪一档降级、怎么发帧、怎么记下 turn_id、怎么写提交点。

    `commit` 返回假说明租约已经不在手上：这一遍不许再落任何副作用，直接放弃。
    """

    run: RunRecord
    degrade: Degrade
    scope: RunScope
    publish: Callable[[str, dict[str, Any]], Awaitable[None]]
    set_turn: Callable[[str], Awaitable[None]]
    commit: Callable[[dict[str, Any]], Awaitable[bool]]


class RetryRun(Exception):
    """
    execute 抛它：这一遍撞上的是基础设施故障（Redis、Postgres 连不上），或者在提交点之后
    出了错——都不是这一轮的结局，换个时候再跑一遍是安全的（提交点之后按提交点收尾，
    不会重新作答）。worker 把 run 交还（outcome=retry），由清扫任务过一阵重新派发；
    计入死信的次数，一直不好就进死信。
    """


Execute = Callable[[RunContext], Awaitable[RunOutcome]]
# 进死信时调：建转人工的工单之类，返回给客户端看的结局（None 就只报错误）
OnDead = Callable[[RunRecord, str], Awaitable[dict[str, Any] | None]]


@dataclass
class _Job:
    run: RunRecord
    message_id: str
    task: asyncio.Task | None = None
    lost: bool = False
    released: bool = False
    started: float = field(default_factory=time.monotonic)


def default_owner() -> str:
    """worker 的名字：主机名（k8s 里是 pod 名）+ 进程号 + 一点随机数（同一个 pod 重启后不重名）。"""
    return f"{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(2)}"


class RunWorker:
    def __init__(
        self,
        *,
        store,
        broker,
        channel,
        memo,
        execute: Execute,
        config: RunConfig,
        policy: DegradePolicy | None = None,
        on_dead: OnDead | None = None,
        owner: str | None = None,
        block_ms: int = 2000,
        backlog_ttl_s: float = 1.0,
    ):
        self.store = store
        self.broker = broker
        self.channel = channel
        self.memo = memo
        self._execute = execute
        self.config = config
        self.policy = policy or DegradePolicy(thresholds=())
        self._on_dead = on_dead
        self.owner = owner or default_owner()
        self._block_ms = block_ms
        self._slots = asyncio.Semaphore(config.concurrency)
        self._inflight: dict[str, _Job] = {}
        self._loops: list[asyncio.Task] = []
        self._spawned: set[asyncio.Task] = set()
        self._draining = False
        # 降级看的积压读一次用一秒：每领一个 run 都去 XINFO 一次没必要，积压也不会在一秒里翻倍
        self._backlog_ttl_s = backlog_ttl_s
        self._backlog_cache: tuple[float, int] = (float("-inf"), 0)
        self._last_purge = 0.0

    # ── 生命周期 ──────────────────────────────────────────────────────────────

    @property
    def draining(self) -> bool:
        return self._draining

    @property
    def in_flight(self) -> int:
        return len(self._inflight)

    async def start(self) -> None:
        await self.broker.ensure_group()
        self._loops = [
            asyncio.create_task(self._consume_loop(), name="runs-consume"),
            asyncio.create_task(self._reclaim_loop(), name="runs-reclaim"),
            asyncio.create_task(self._sweep_loop(), name="runs-sweep"),
        ]
        logger.info("run worker %s 已启动：实例 %s，并发 %d，租约 %gs",
                    self.owner, self.store.instance, self.config.concurrency, self.config.lease_s)

    async def drain(self, timeout_s: float) -> dict[str, int]:
        """停止领新的，等手上的跑完；到时限没跑完的交还。返回跑完和交还的个数。"""
        self._draining = True
        for loop in self._loops:
            loop.cancel()
        await asyncio.gather(*self._loops, return_exceptions=True)
        jobs = [job for job in self._inflight.values() if job.task is not None]
        logger.info("run worker %s 停机：等 %d 个在跑的 run（最多 %gs）", self.owner, len(jobs), timeout_s)
        if jobs:
            await asyncio.wait([job.task for job in jobs], timeout=timeout_s)
        released = 0
        for job in list(self._inflight.values()):
            if job.task is not None and not job.task.done():
                job.released = True
                # cancel() 返回假：就在这一瞬间跑完了，让它照常写结局，不交还
                if job.task.cancel():
                    released += await self._release(job)
                else:
                    job.released = False
        if self._spawned:
            await asyncio.gather(*list(self._spawned), return_exceptions=True)
        return {"finished": len(jobs) - released, "released": released}

    # ── 三个循环 ──────────────────────────────────────────────────────────────

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._spawned.add(task)
        task.add_done_callback(self._spawned.discard)

    async def _consume_loop(self) -> None:
        failures = 0
        while not self._draining:
            await self._slots.acquire()
            handed_off = False
            try:
                deliveries = await self.broker.receive(self.owner, 1, self._block_ms)
                failures = 0
                for delivery in deliveries:
                    handed_off = True
                    self._spawn(self._run_delivery(delivery))
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                logger.warning("读派发队列失败（第 %d 次），稍后再试", failures, exc_info=failures == 1)
                await asyncio.sleep(min(0.5 * 2 ** min(failures, 5), 10.0))
            finally:
                if not handed_off:
                    self._slots.release()

    async def _reclaim_loop(self) -> None:
        interval = max(self.config.lease_s / 2, 0.05)
        min_idle_ms = int(self.config.lease_s * 1500)
        while not self._draining:
            await asyncio.sleep(interval)
            free = getattr(self._slots, "_value", 0)
            if free <= 0:
                continue
            try:
                deliveries = await self.broker.reclaim(self.owner, min_idle_ms, free)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("接手闲置消息失败", exc_info=True)
                continue
            for delivery in deliveries:
                if delivery.run_id in self._inflight:
                    continue        # 就在自己手上（消息是重复派发的那一条）
                await self._slots.acquire()
                self._spawn(self._run_delivery(delivery, reclaimed=True))

    async def _sweep_loop(self) -> None:
        interval = max(min(self.config.redispatch_after_s / 2, 15.0), 0.05)
        while not self._draining:
            await asyncio.sleep(interval)
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("清扫失败", exc_info=True)

    async def sweep_once(self) -> int:
        """重新派发没人领的、租约过期太久的 run；更新积压；到点删过期的 run。返回重新派发了几个。"""
        backlog = await self.broker.backlog()
        RUN_BACKLOG.labels(instance=self.store.instance).set(backlog)
        lock = getattr(self.broker, "try_lock", None)
        if lock is not None and not await lock("sweep", self.config.redispatch_after_s / 2):
            return 0
        due = await self.store.due_for_dispatch(
            idle_s=self.config.redispatch_after_s, lease_grace_s=self.config.lease_s * 2)
        for run_id in due:
            await self.broker.dispatch(run_id)
        await self.store.mark_dispatched(due)
        if due:
            logger.warning("清扫：%d 个 run 没人领或租约早已过期，重新派发", len(due))
        now = time.monotonic()
        if self.config.retention_days and now - self._last_purge > 3600:
            self._last_purge = now
            purged = await self.store.purge(self.config.retention_days * 86400)
            if purged:
                logger.info("删掉 %d 个过了保留期的 run", purged)
        return len(due)

    # ── 一个 run ──────────────────────────────────────────────────────────────

    async def _backlog(self) -> int:
        at, value = self._backlog_cache
        if time.monotonic() - at < self._backlog_ttl_s:
            return value
        try:
            value = await self.broker.backlog()
        except Exception:
            logger.debug("读积压失败，按上一次的算", exc_info=True)
        self._backlog_cache = (time.monotonic(), value)
        RUN_BACKLOG.labels(instance=self.store.instance).set(value)
        return value

    async def _run_delivery(self, delivery, reclaimed: bool = False) -> None:
        try:
            await self._handle(delivery, reclaimed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("处理 run %s 时出错，消息留着等别人接手", delivery.run_id)
        finally:
            self._slots.release()

    async def _handle(self, delivery, reclaimed: bool) -> None:
        run = await self.store.claim(delivery.run_id, self.owner, self.config.lease_s)
        if run is None:
            existing = await self.store.get(delivery.run_id)
            if existing is None or existing.terminal:
                # 跑完了（重复派发、或者原来那个 worker 写完结局没来得及确认），或者不是这个实例的
                await self.broker.ack(delivery.message_id)
            # 否则还在别人手上、租约有效：消息留着，它自己会确认
            return
        lost = lost_attempts(run)
        if lost >= self.config.max_attempts:
            await self._bury(run, delivery, lost)
            return

        RUN_ATTEMPTS.labels(kind="reclaimed" if run.attempt > 1 else "first").inc()
        if run.attempt == 1 and run.started_at is not None:
            RUN_QUEUE_WAIT.observe(max(run.started_at - run.created_at, 0.0))
        degrade = self.policy.level_for(await self._backlog()) if self.policy.enabled else NONE
        if degrade.level:
            RUN_DEGRADED.labels(level=str(degrade.level)).inc()
        if run.attempt > 1:
            logger.warning("接手 run %s：第 %d 次执行（之前失联 %d 次）", run.run_id, run.attempt, lost)

        job = _Job(run, delivery.message_id)
        self._inflight[run.run_id] = job
        RUNS_IN_FLIGHT.inc()
        scope = RunScope(run.run_id, run.attempt, instance=self.store.instance, memo=self.memo)

        async def publish(event: str, data: dict[str, Any]) -> None:
            await self._publish(run, event, data)

        async def set_turn(turn_id: str) -> None:
            await self.store.set_turn(run.run_id, self.owner, run.attempt, turn_id, degrade.level)

        async def commit(data: dict[str, Any]) -> bool:
            # 写不进去 = 租约已经不在手上：和续租失败同样处理，这一遍作废
            if not job.lost and await self.store.checkpoint(run.run_id, self.owner, run.attempt, data):
                return True
            if not job.lost:
                job.lost = True
                RUN_LEASE_LOST.inc()
                logger.warning("run %s 第 %d 次执行写提交点时发现租约已经不在手上，放弃", run.run_id, run.attempt)
            return False

        ctx = RunContext(run=run, degrade=degrade, scope=scope, publish=publish, set_turn=set_turn,
                         commit=commit)
        # 告诉续流的一方：从这一帧起是第几次执行。重跑时前端据此把上一遍发出去的半截清掉
        await publish("attempt", {"run_id": run.run_id, "attempt": run.attempt,
                                  "reclaimed": run.attempt > 1, "degrade": degrade.to_dict()})
        job.task = asyncio.create_task(self._execute(ctx), name=f"run-{run.run_id}")
        beat = asyncio.create_task(self._heartbeat(job), name=f"run-heartbeat-{run.run_id}")
        try:
            outcome = await job.task
        except asyncio.CancelledError:
            if job.lost or job.released:
                return              # 租约丢了（结果作废），或者停机交还（_release 收尾）
            raise
        except RetryRun as ex:
            logger.warning("run %s 第 %d 次执行撞上故障，交还，稍后重试: %s", run.run_id, run.attempt, ex)
            await self._release(job, outcome="retry", redispatch=False)
            return
        except Exception as ex:
            logger.exception("run %s 执行时抛了异常", run.run_id)
            outcome = RunOutcome(status=FAILED, terminal=("error", {"detail": str(ex)}),
                                 error=f"{type(ex).__name__}: {ex}")
        finally:
            beat.cancel()
            self._inflight.pop(run.run_id, None)
            RUNS_IN_FLIGHT.dec()

        if job.lost:
            return
        finished = await self.store.finish(run.run_id, self.owner, run.attempt, outcome.status,
                                           result=outcome.result, error=outcome.error)
        if finished is None:
            # 跑完了才发现租约不是自己的了：这一遍作废，消息不确认，交给拿着租约的那一遍
            RUN_LEASE_LOST.inc()
            logger.warning("run %s 第 %d 次执行跑完了，但租约已经不在手上，结果作废", run.run_id, run.attempt)
            return
        await self._publish(run, *outcome.terminal)
        await self.broker.ack(delivery.message_id)
        RUNS_FINISHED.labels(status=outcome.status).inc()
        if finished.finished_at is not None:
            RUN_DURATION.labels(status=outcome.status).observe(max(finished.finished_at - finished.created_at, 0.0))

    async def _heartbeat(self, job: _Job) -> None:
        """
        续租，同时把派发消息的闲置时间清零。

        续租改不到行：run 已经被别人领走了（这个 worker 被判失联，比如卡住了一个租约那么久）。
        取消这一遍。续租本身连不上库：一直连不上超过一个租约，别人眼里租约已经过期，
        也取消——证明不了自己还拿着租约，就当没拿着。
        """
        run = job.run
        last_ok = time.monotonic()
        while True:
            await asyncio.sleep(self.config.heartbeat_s)
            try:
                ok: bool | None = await self.store.heartbeat(
                    run.run_id, self.owner, run.attempt, self.config.lease_s)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("run %s 续租失败（连不上库）", run.run_id, exc_info=True)
                ok = None
            if ok:
                last_ok = time.monotonic()
            elif ok is False or time.monotonic() - last_ok > self.config.lease_s:
                job.lost = True
                RUN_LEASE_LOST.inc()
                logger.warning("run %s 第 %d 次执行的租约丢了，取消这一遍", run.run_id, run.attempt)
                if job.task is not None:
                    job.task.cancel()
                return
            try:
                await self.broker.touch(self.owner, job.message_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("摸派发消息失败（不影响租约）", exc_info=True)

    async def _release(self, job: _Job, outcome: str = "released", redispatch: bool = True) -> int:
        """
        交还：改回 queued、确认原来的消息。停机交还时马上重新派发，别的 worker 立刻接手；
        撞上故障交还（retry）时不马上派发，等清扫任务过 AI_HELPDESK_RUN_REDISPATCH_S 再派——
        故障多半还没好，立刻重跑只会把重试次数白白用掉。
        """
        run = job.run
        try:
            if not await self.store.release(run.run_id, self.owner, run.attempt, outcome=outcome):
                return 0
            if redispatch:
                await self.broker.dispatch(run.run_id)
            await self.broker.ack(job.message_id)
            if outcome == "released":
                logger.info("停机：run %s 交还，别的 worker 会接着跑", run.run_id)
            return 1
        except Exception:
            # 交还没成：租约过期之后照样会被接手，只是慢一点
            logger.warning("交还 run %s 失败，等租约过期后被接手", run.run_id, exc_info=True)
            return 0

    async def _bury(self, run: RunRecord, delivery, lost: int) -> None:
        """失联次数到了上限：进死信，转人工。"""
        reason = f"处理这一轮的 worker 失联或重试了 {lost} 次（上限 {self.config.max_attempts}）"
        payload = None
        if self._on_dead is not None:
            try:
                payload = await self._on_dead(run, reason)
            except Exception:
                logger.exception("run %s 进死信时转人工失败", run.run_id)
        finished = await self.store.finish(run.run_id, self.owner, run.attempt, DEAD,
                                           result=payload, error=reason)
        if finished is None:
            return
        try:
            await self.broker.dead_letter(run.run_id, reason)
        except Exception:
            logger.warning("写死信流失败（库里已经是 dead）", exc_info=True)
        await self._publish(run, *(("result", payload) if payload else ("error", {"detail": reason})))
        await self.broker.ack(delivery.message_id)
        RUNS_FINISHED.labels(status=DEAD).inc()
        logger.error("run %s 进死信：%s", run.run_id, reason)

    async def _publish(self, run: RunRecord, event: str, data: dict[str, Any]) -> None:
        """发一帧。发不出去只记日志：帧流是尽力而为的，结局在库里，续流时能补出来。"""
        try:
            await self.channel.publish(run.run_id, event, data, run.attempt)
        except Exception:
            logger.warning("run %s 的 %s 帧没发出去", run.run_id, event, exc_info=True)
