"""
派发队列：一个实例一条 Redis Stream，worker 组成一个消费组。

**为什么是 Redis Streams 而不是 Kafka**（完整的取舍见 docs/design/durable-agent-runs.md）：
这里要的是工作队列，一条消息就是一次十几秒到一分钟的模型任务。Streams 的消费组
逐条确认（XACK），没确认的留在 pending 列表里，过了闲置时间可以被别的消费者用
XAUTOCLAIM 接手——相当于 SQS 的可见性超时。Kafka 按分区提交 offset：一条慢消息
堵住同分区后面所有消息，并发上限是分区数，处理时间超过 max.poll.interval.ms 还会
触发重平衡、反而制造重复处理。而 Redis 已经在这个部署里了。

几个要点：

- **消息里只有 run_id。** 要跑什么、跑到哪了，都去 Postgres 读。消息丢了（Redis 重启
  没持久化）由清扫任务按库重新派发；消息重复（重新派发撞上原来那条）由领取时的
  条件 UPDATE 去重。所以这里只要"至少送到一次"。
- **消费组从 0 开始建。** API 可能比任何 worker 先起来、先派发；从 `$` 建的话，
  组建立之前进来的消息永远没人读。
- **续租的同时摸一下消息。** 一次 run 跑得比闲置阈值久时，XCLAIM 给自己（JUSTID，
  不加投递计数）把闲置时间清零，别人的 XAUTOCLAIM 就不会把一个还活着的 worker
  手上的消息抢走。即使抢走了，领取那一步也领不到（租约还有效）。
- **积压 = 还没投递的（lag）+ 投递了没确认的（pending）。** 降级档位和 KEDA 的扩缩
  看的都是它。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

GROUP = "workers"
# 派发流只留最近这么多条（约数，XADD MAXLEN ~）。确认过的消息留着没用；没确认的被裁掉
# 也不要紧，清扫任务会按库重新派发
DISPATCH_MAXLEN = 10_000


@dataclass(frozen=True)
class Delivery:
    message_id: str
    run_id: str


def stream_key(prefix: str, instance: str) -> str:
    return f"{prefix}:runs:{instance}"


class RedisBroker:
    def __init__(self, redis, instance: str, *, prefix: str = "ai-helpdesk", group: str = GROUP):
        self._redis = redis
        self.instance = instance
        self.stream = stream_key(prefix, instance)
        self.dead_stream = f"{self.stream}:dead"
        self.group = group

    async def ensure_group(self) -> None:
        try:
            await self._redis.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def dispatch(self, run_id: str) -> str:
        return await self._redis.xadd(self.stream, {"run": run_id},
                                      maxlen=DISPATCH_MAXLEN, approximate=True)

    async def receive(self, consumer: str, count: int, block_ms: int) -> list[Delivery]:
        try:
            reply = await self._redis.xreadgroup(
                self.group, consumer, {self.stream: ">"}, count=count, block=block_ms)
        except Exception as exc:
            if "NOGROUP" in str(exc):
                # 流或组被删掉了（Redis 清过库、重启没持久化）：重建，下一圈再读
                await self.ensure_group()
                return []
            raise
        return _deliveries(reply)

    async def ack(self, message_id: str) -> None:
        await self._redis.xack(self.stream, self.group, message_id)

    async def touch(self, consumer: str, message_id: str) -> None:
        """把这条消息的闲置时间清零（续租时调）。JUSTID：不算一次新的投递。"""
        await self._redis.xclaim(self.stream, self.group, consumer, 0, [message_id], justid=True)

    async def reclaim(self, consumer: str, min_idle_ms: int, count: int) -> list[Delivery]:
        """接手闲置超过 min_idle_ms 的消息：原来的 worker 多半已经不在了。"""
        try:
            reply = await self._redis.xautoclaim(
                self.stream, self.group, consumer, min_idle_ms, start_id="0-0", count=count)
        except Exception as exc:
            if "NOGROUP" in str(exc):
                await self.ensure_group()
                return []
            raise
        # Redis 7：[下一个游标, [(id, fields), ...], [已删掉的 id, ...]]
        claimed = reply[1] if isinstance(reply, list | tuple) and len(reply) > 1 else []
        out = []
        for message_id, fields in claimed:
            if not fields:
                continue
            run_id = fields.get("run") or fields.get(b"run")
            if run_id:
                out.append(Delivery(_text(message_id), _text(run_id)))
        return out

    async def dead_letter(self, run_id: str, reason: str) -> None:
        await self._redis.xadd(self.dead_stream, {"run": run_id, "reason": reason[:500],
                                                  "at": f"{time.time():.3f}"},
                               maxlen=DISPATCH_MAXLEN, approximate=True)

    async def try_lock(self, name: str, ttl_s: float) -> bool:
        """一把短锁（SET NX PX）：几个 worker 同时清扫时，通常只让一个扫。拿不到不等。"""
        return bool(await self._redis.set(f"{self.stream}:lock:{name}", "1", nx=True,
                                          px=max(int(ttl_s * 1000), 1)))

    async def backlog(self) -> int:
        """还没跑完的派发：没投递的加上投递了没确认的。"""
        try:
            groups = await self._redis.xinfo_groups(self.stream)
        except Exception as exc:
            if "no such key" in str(exc).lower():
                return 0
            raise
        for info in groups:
            if _text(info.get("name", "")) != self.group:
                continue
            pending = int(info.get("pending") or 0)
            lag = info.get("lag")
            if lag is None:
                # 流被裁剪过或删过条目时 Redis 算不出 lag：用"加进来的减去读过的"
                stream = await self._redis.xinfo_stream(self.stream)
                added = stream.get("entries-added")
                read = info.get("entries-read")
                lag = (int(added) - int(read)) if added is not None and read is not None else 0
            return pending + max(int(lag), 0)
        return 0

    async def ping(self) -> bool:
        return bool(await self._redis.ping())


def _text(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _deliveries(reply) -> list[Delivery]:
    out = []
    for _stream, entries in reply or []:
        for message_id, fields in entries:
            run_id = fields.get("run") or fields.get(b"run")
            if run_id:
                out.append(Delivery(_text(message_id), _text(run_id)))
    return out


class MemoryBroker:
    """进程内的派发队列：单测用。语义和 RedisBroker 一致（消费组、pending、接手）。"""

    def __init__(self, instance: str = "staff", clock=time.monotonic):
        self.instance = instance
        self._clock = clock
        self._entries: list[tuple[str, str]] = []
        self._next = 0                      # 下一条没投递过的
        self._pending: dict[str, tuple[str, float]] = {}   # message_id → (consumer, 最近一次被碰的时间)
        self._seq = 0
        self._changed = asyncio.Condition()
        self.dead: list[tuple[str, str]] = []
        self.acked: list[str] = []

    async def ensure_group(self) -> None:
        return None

    async def dispatch(self, run_id: str) -> str:
        self._seq += 1
        message_id = f"{self._seq}-0"
        self._entries.append((message_id, run_id))
        async with self._changed:
            self._changed.notify_all()
        return message_id

    async def receive(self, consumer: str, count: int, block_ms: int) -> list[Delivery]:
        deadline = self._clock() + block_ms / 1000
        async with self._changed:
            while self._next >= len(self._entries):
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return []
                try:
                    await asyncio.wait_for(self._changed.wait(), remaining)
                except TimeoutError:
                    return []
            out = []
            while self._next < len(self._entries) and len(out) < count:
                message_id, run_id = self._entries[self._next]
                self._next += 1
                self._pending[message_id] = (consumer, self._clock())
                out.append(Delivery(message_id, run_id))
            return out

    async def ack(self, message_id: str) -> None:
        if self._pending.pop(message_id, None) is not None:
            self.acked.append(message_id)

    async def touch(self, consumer: str, message_id: str) -> None:
        if message_id in self._pending:
            self._pending[message_id] = (consumer, self._clock())

    async def reclaim(self, consumer: str, min_idle_ms: int, count: int) -> list[Delivery]:
        now = self._clock()
        runs = dict(self._entries)
        out = []
        for message_id, (_owner, touched) in list(self._pending.items()):
            if len(out) >= count:
                break
            if (now - touched) * 1000 >= min_idle_ms:
                self._pending[message_id] = (consumer, now)
                out.append(Delivery(message_id, runs[message_id]))
        return out

    async def dead_letter(self, run_id: str, reason: str) -> None:
        self.dead.append((run_id, reason))

    async def backlog(self) -> int:
        return (len(self._entries) - self._next) + len(self._pending)

    def pending_of(self, consumer: str) -> list[str]:
        return [m for m, (owner, _t) in self._pending.items() if owner == consumer]

    async def ping(self) -> bool:
        return True
