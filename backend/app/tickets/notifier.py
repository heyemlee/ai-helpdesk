"""
工单通知的可靠投递。

**这条链路上"客户以为已经转人工了"和"值班的人真的收到了"是两件事，
而原来的实现没有任何东西连着它们**：

```python
async def _notify_ticket_webhook(url, ticket):
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json=...)      # ← 返回值直接扔掉
    except Exception as ex:
        logger.warning(...)                        # ← 失败只留一条 warning
```

三处都会让通知**静默丢掉**：

1. **不看 HTTP 状态。** 接收方返回 500、401、404 都算"发出去了"。
   Slack / 企业微信的 webhook 过期之后正是 4xx——外表毫无变化。
2. **不重试。** 一次网络抖动 = 这张工单永远没人知道。
3. **没有待发送记录。** 进程重启、或者 `_spawn_background` 那个 task 还没跑完
   就被关掉，这次通知连"曾经存在过"都没有。

本模块把它换成一个 outbox：**先落记录，再发**。落记录这一步和工单本身在同一个
存储里（`ticket_notifications` 表），所以"工单存在但通知没发出去"是一个**可查询
的状态**，而不是一段找不回来的日志。

设计取舍：

- **去重靠唯一索引，不靠调用方自觉。** `ticket_id` 唯一：同一张工单只入队一次，
  重试重发的是同一条记录。记录 id 随 `Idempotency-Key` 发出去，接收方也能去重。
- **按状态码分永久失败与可重试。** 4xx（除 408 / 429）是"这个请求本身有问题"，
  重试一百次也一样，直接判 `failed` 并升级；5xx / 超时 / 连接失败 / 429 才退避重试。
- **升级是 ERROR 加计数器，不是再发一次。** 到达上限还没送达，说明接收端出了
  需要人处理的问题；这时候还继续重试只会把问题埋得更深。
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time

logger = logging.getLogger(__name__)

# 退避：5s → 15s → 45s → …，封顶 10 分钟。加 ±20% 抖动，避免多副本同时重试
# 把一个刚恢复的接收端再打一遍（和熔断器半开态是同一类问题）。
BASE_BACKOFF_S = 5.0
BACKOFF_FACTOR = 3.0
MAX_BACKOFF_S = 600.0
JITTER = 0.2

# 试到第几次还不成就判 failed 并升级。5 次在上面的退避下大约覆盖 10 分钟——
# 短暂抖动够用，接收端真挂了也不至于一直重试到没人看得出问题。
MAX_ATTEMPTS = 5

# 不重试的状态码：这些是"请求本身有问题"，重试一百次也一样。
# 408（请求超时）和 429（限流）例外——它们明确是"待会再来"。
_RETRY_ANYWAY = frozenset({408, 429})


def backoff_delay(attempts: int) -> float:
    """第 `attempts` 次失败之后等多久再试。"""
    raw = min(BASE_BACKOFF_S * (BACKOFF_FACTOR ** max(0, attempts - 1)), MAX_BACKOFF_S)
    return raw * (1.0 + random.uniform(-JITTER, JITTER))


def is_permanent(status_code: int) -> bool:
    """这个状态码值不值得重试。"""
    return 400 <= status_code < 500 and status_code not in _RETRY_ANYWAY


class TicketNotifier:
    """
    工单通知的 outbox 投递器。

    `enqueue()` 在请求路径上调用（落记录 + 立刻试一次），`run_forever()` 是
    后台补投循环——进程重启后靠它把积压的 pending 捡回来。
    """

    def __init__(self, store, url: str, *, poll_interval_s: float = 10.0,
                 timeout_s: float = 5.0, failures_counter=None, pending_gauge=None):
        self._store = store
        self._url = (url or "").strip()
        self._poll_interval_s = poll_interval_s
        self._timeout_s = timeout_s
        # 指标可选：没有 Prometheus 也要能跑（单测里就没有）
        self._failures = failures_counter
        self._pending = pending_gauge

    @property
    def enabled(self) -> bool:
        return bool(self._url) and self._store is not None

    async def enqueue(self, ticket) -> None:
        """
        登记并立刻试一次。

        **先落记录再发**：反过来的话，发送和落记录之间进程挂掉，这次通知就
        没有任何痕迹——那正是原来的形态。
        """
        if not self.enabled:
            return
        payload = json.dumps(
            {"type": "ticket.created", "ticket": ticket.to_dict()}, ensure_ascii=False
        )
        delivery = await self._store.notify_enqueue(ticket.id, self._url, payload)
        if delivery is None:
            # 已经登记过。不是错误——重复调用是正常的（重试、并发），
            # 去重就该在这里发生，而不是指望每个调用点都记得只调一次。
            logger.debug("工单 %s 的通知已在队列里，跳过重复入队", ticket.id)
            return
        await self._attempt(delivery)

    async def run_forever(self) -> None:
        """后台补投循环。被取消时干净退出。"""
        if not self.enabled:
            return
        while True:
            try:
                await asyncio.sleep(self._poll_interval_s)
                await self.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 循环本身不能被一次异常带走，否则积压再也不会被捡起来
                logger.exception("工单通知补投循环出错，下一轮继续")

    async def drain_once(self) -> int:
        """把当前到期的都试一遍，返回处理条数。"""
        if not self.enabled:
            return 0
        due = await self._store.notify_due(time.time())
        for delivery in due:
            await self._attempt(delivery)
        if self._pending is not None:
            stats = await self._store.notify_stats()
            self._pending.set(stats.get("pending", 0))
        return len(due)

    async def _attempt(self, delivery) -> None:
        import httpx

        attempts = delivery.attempts + 1
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                resp = await client.post(
                    delivery.url,
                    content=delivery.payload.encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        # 接收方据此去重：重试发的是同一个事件，不是新事件
                        "Idempotency-Key": delivery.id,
                        "X-Ticket-Id": delivery.ticket_id,
                    },
                )
        except Exception as ex:
            await self._schedule_retry(delivery, attempts, f"{type(ex).__name__}: {ex}")
            return

        # **这里是原来完全没有的一步。** 少了它，一个过期的 webhook URL
        # 会一直返回 4xx，而这边一直以为通知发出去了。
        if 200 <= resp.status_code < 300:
            await self._store.notify_mark(delivery.id, "delivered", attempts=attempts)
            logger.info("工单通知已送达: ticket=%s delivery=%s 第 %d 次尝试",
                        delivery.ticket_id, delivery.id, attempts)
            return

        error = f"HTTP {resp.status_code}: {resp.text[:200]}"
        if is_permanent(resp.status_code):
            await self._fail(delivery, attempts, error, permanent=True)
        else:
            await self._schedule_retry(delivery, attempts, error)

    async def _schedule_retry(self, delivery, attempts: int, error: str) -> None:
        if attempts >= MAX_ATTEMPTS:
            await self._fail(delivery, attempts, error, permanent=False)
            return
        delay = backoff_delay(attempts)
        await self._store.notify_mark(
            delivery.id, "pending", attempts=attempts,
            next_attempt_at=time.time() + delay, last_error=error,
        )
        logger.warning("工单通知第 %d 次失败，%.0fs 后重试: ticket=%s %s",
                       attempts, delay, delivery.ticket_id, error)

    async def _fail(self, delivery, attempts: int, error: str, *, permanent: bool) -> None:
        """
        判定失败并**升级**。

        升级不是再发一次——到这一步说明接收端出了需要人处理的问题（URL 过期、
        鉴权掉了、服务下线），继续重试只会把问题埋得更深。所以：一条 ERROR
        （带工单号和会话号，人能顺着查下去）+ 一个计数器（能配告警）。
        记录留在 `failed` 状态，`/health` 的 `ticket_notifications` 里看得到。
        """
        await self._store.notify_mark(
            delivery.id, "failed", attempts=attempts, last_error=error,
        )
        if self._failures is not None:
            self._failures.inc()
        logger.error(
            "工单通知最终失败，需要人工介入: ticket=%s delivery=%s 尝试 %d 次 "
            "原因=%s（%s）。客户已被告知转人工，但接收端没有确认收到。",
            delivery.ticket_id, delivery.id, attempts, error,
            "状态码表明重试无意义" if permanent else f"已达上限 {MAX_ATTEMPTS} 次",
        )
