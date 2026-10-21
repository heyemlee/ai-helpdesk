"""
事件日志的另一种去处：Kafka（AI_HELPDESK_EVENT_SINK=kafka）。

```
请求路径（不变）                        后台（每个进程一个）
TurnRecorder.emit ──put──→ EventWriter ──→ KafkaPublisher：一批事件各发一条，等 broker 确认
                                           topic = ai-helpdesk.turn-events.<实例>
                                           key = conv_id：同一个会话、同一个 run 的几次执行进同一个分区
                                                      │
                      ┌───────────────────────────────┴───────────────────────────────┐
         投影消费者（写 turn_events，读接口照旧读库）               采样消费者（挑出值得进评测集的轮次）
```

只有事件日志走 Kafka：它只追加、有几个各读各的消费者、要能回放。派发和帧流仍在 Redis——
一条消息是一次几十秒的任务、要逐条确认和接手，每个 run 一条能续读的帧流，这两样都不是 Kafka
的形状（docs/design/durable-agent-runs.md）。

口径：

- 生产者是幂等的（acks=all），broker 那一侧不会因为生产者自己的重试多出重复。EventWriter 整批
  重试时，已经确认过的那几条会再发一次，所以 topic 里可能有重复：消费者按 (turn_id, seq) 去重，
  投影靠的是主键。
- 和写 Postgres 一样是"最多一次"：进程被杀，队列和生产者缓冲里的一起丢。
- 一条超过 broker 上限的消息只丢它自己（too_large），不连累同一批。
- 连不上 Kafka 不挡启动：第一次发的时候才连；连不上就像库挂了一样退避重试，最后丢掉、计数。
- topic 只留 7 天，短于最短的原文保留期（客户实例 14 天）：回放永远不会把库里已经清空的原文
  写回去。长期存储仍是 Postgres，Kafka 是传输加上 7 天的回放窗口。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from app.events.recorder import EventLogStats
from app.events.types import COLUMNS, Event

logger = logging.getLogger(__name__)

TOPIC_PREFIX = "ai-helpdesk.turn-events"
INSTANCES = ("staff", "customer")

# topic 留几天。必须短于最短的原文保留期（客户实例 AI_HELPDESK_EVENT_LOG_BODY_DAYS=14）：
# 否则重置 offset 回放时，库里已经清空的原文会被投影写回去
DEFAULT_RETENTION_DAYS = 7
MAX_RETENTION_DAYS = 13
DEFAULT_PARTITIONS = 6
DAY_MS = 86_400_000


@dataclass(frozen=True)
class KafkaSettings:
    bootstrap: str = ""
    topic_prefix: str = TOPIC_PREFIX
    client_id: str = "ai-helpdesk"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> KafkaSettings:
        env = os.environ if env is None else env
        return cls(
            bootstrap=env.get("AI_HELPDESK_KAFKA_BOOTSTRAP", "").strip(),
            topic_prefix=env.get("AI_HELPDESK_KAFKA_TOPIC_PREFIX", "").strip() or TOPIC_PREFIX,
            client_id=env.get("AI_HELPDESK_KAFKA_CLIENT_ID", "").strip() or "ai-helpdesk",
        )

    def topic(self, instance: str) -> str:
        """一个实例一个 topic：客户原话和员工的订单数据不在一处，保留期和权限能分开设。"""
        return f"{self.topic_prefix}.{instance}"

    def dead_letter_topic(self, instance: str) -> str:
        return f"{self.topic_prefix}.{instance}.dlq"


# ── 编码 ──────────────────────────────────────────────────────────────────────

class BadEvent(ValueError):
    """topic 里的一条消息解不成事件：消费者把它送进死信 topic，不卡住分区。"""


def encode(event: Event) -> tuple[bytes, bytes]:
    """
    一条事件 → (key, value)。

    value 是信封的 JSON，字段就是 `turn_events` 的列；data 和 body 仍是记下那一刻的 JSON 文本，
    原样带着，不解开再编码。key 是 conv_id（同一个会话的轮次进同一个分区、按发出的顺序读到）。
    """
    key = (event.conv_id or event.turn_id).encode()
    value = json.dumps({name: getattr(event, name) for name in COLUMNS},
                       ensure_ascii=False, separators=(",", ":")).encode()
    return key, value


def decode(value: bytes | None) -> Event:
    try:
        raw = json.loads(value or b"")
        event = Event(**{name: raw[name] for name in COLUMNS})
    except (ValueError, KeyError, TypeError) as ex:
        raise BadEvent(f"不是一条事件: {type(ex).__name__}") from ex
    if not isinstance(event.turn_id, str) or not event.turn_id or not isinstance(event.seq, int) \
            or not isinstance(event.data, str) or not (event.body is None or isinstance(event.body, str)):
        raise BadEvent("事件的字段类型不对")
    return event


# ── 生产者 ────────────────────────────────────────────────────────────────────

def _producer(settings: KafkaSettings):
    from aiokafka import AIOKafkaProducer

    return AIOKafkaProducer(
        bootstrap_servers=settings.bootstrap, client_id=settings.client_id,
        acks="all", enable_idempotence=True,
        # 攒 20 毫秒再发：EventWriter 已经按批交过来，这里只是让同一批落进少几个请求
        linger_ms=20, compression_type="gzip",
        # 一个请求最多等 15 秒；broker 重启时由 EventWriter 退避重试，不在这里无限等
        request_timeout_ms=15_000, retry_backoff_ms=500,
    )


def _too_large(ex: BaseException) -> bool:
    """这一条本身发不出去（比 broker 的上限大）：重试多少次都一样，只丢它自己。"""
    from aiokafka.errors import MessageSizeTooLargeError, RecordListTooLargeError, RecordTooLargeError

    return isinstance(ex, (MessageSizeTooLargeError, RecordTooLargeError, RecordListTooLargeError))


async def _quietly(awaitable) -> None:
    try:
        await awaitable
    except Exception:
        logger.debug("关闭 Kafka 客户端时出错", exc_info=True)


class KafkaPublisher:
    """
    EventWriter 的另一种 store：把一批事件发进本实例的 topic，全部确认了才算写完。

    `insert` 抛出时 EventWriter 退避重试整批（见本模块开头：可能多出重复，消费者去重）。
    """

    backend = "kafka"

    def __init__(self, settings: KafkaSettings, instance: str, stats: EventLogStats,
                 producer_factory: Callable[[KafkaSettings], Any] | None = None):
        self.settings = settings
        self.topic = settings.topic(instance)
        self._stats = stats
        self._factory = producer_factory if producer_factory is not None else _producer
        self._producer = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._producer is not None

    async def _ready(self):
        """第一次发的时候才连。连不上就抛，下一批再试——不挡启动，也不一直重连。"""
        if self._producer is None:
            async with self._lock:
                if self._producer is None:
                    producer = self._factory(self.settings)
                    try:
                        await producer.start()
                    except BaseException:
                        await _quietly(producer.stop())
                        raise
                    self._producer = producer
                    logger.info("事件日志接上 Kafka：%s → %s", self.settings.bootstrap, self.topic)
        return self._producer

    async def insert(self, events: list[Event]) -> int:
        """发出这一批，等 broker 确认。返回真正写进去的条数（太大的那几条不算）。"""
        producer = await self._ready()
        sent: list[tuple[Event, asyncio.Future]] = []
        written = 0
        for event in events:
            key, value = encode(event)
            try:
                sent.append((event, await producer.send(self.topic, value=value, key=key)))
            except Exception as ex:
                if not _too_large(ex):
                    raise
                self._drop_too_large(event, len(value))
        failure: BaseException | None = None
        for event, future in sent:
            try:
                await future
            except Exception as ex:
                if _too_large(ex):
                    self._drop_too_large(event, 0)
                    continue
                failure = failure or ex
                continue
            written += 1
        if failure is not None:
            raise failure
        return written

    def _drop_too_large(self, event: Event, size: int) -> None:
        logger.warning("事件 %s（%s #%d）超过 Kafka 的消息上限%s，丢掉这一条", event.type, event.turn_id,
                       event.seq, f"（{size} 字节）" if size else "")
        self._stats.drop("too_large")

    async def close(self) -> None:
        """停机：把生产者缓冲里的发完再断开（EventWriter 已经先把队列写完）。"""
        producer, self._producer = self._producer, None
        if producer is not None:
            await _quietly(producer.stop())

    def health(self) -> dict[str, Any]:
        return {"bootstrap": self.settings.bootstrap, "topic": self.topic, "connected": self.connected}


# ── topic ─────────────────────────────────────────────────────────────────────

async def ensure_topics(settings: KafkaSettings, instances: list[str], *,
                        partitions: int = DEFAULT_PARTITIONS, retention_days: int = DEFAULT_RETENTION_DAYS,
                        replication: int = 1, admin_factory: Callable[..., Any] | None = None) -> dict[str, str]:
    """
    建各实例的事件 topic 和死信 topic；已经有的把保留期改成这里的值。返回 {topic: created / updated}。

    分区数只在新建时定：给已有的 topic 加分区会改变 key 落在哪个分区，同一个会话前后的事件会
    分到两处、顺序就不保证了，要加得停写、手工做。
    """
    if not 0 < retention_days <= MAX_RETENTION_DAYS:
        raise ValueError(f"topic 保留期 {retention_days} 天：要在 1 到 {MAX_RETENTION_DAYS} 天之间，"
                         "短于最短的原文保留期（客户实例 14 天）")
    unknown = [i for i in instances if i not in INSTANCES]
    if unknown or not instances:
        raise ValueError(f"实例只能是 {' / '.join(INSTANCES)}：{unknown or instances}")
    from aiokafka.admin import AIOKafkaAdminClient, NewTopic
    from aiokafka.admin.config_resource import ConfigResource, ConfigResourceType

    admin = (admin_factory or AIOKafkaAdminClient)(bootstrap_servers=settings.bootstrap,
                                                    client_id=f"{settings.client_id}-admin")
    configs = {"retention.ms": str(retention_days * DAY_MS), "cleanup.policy": "delete"}
    wanted = [name for instance in instances
              for name in (settings.topic(instance), settings.dead_letter_topic(instance))]
    out: dict[str, str] = {}
    await admin.start()
    try:
        existing = set(await admin.list_topics())
        missing = [name for name in wanted if name not in existing]
        if missing:
            await admin.create_topics([
                NewTopic(name, num_partitions=partitions, replication_factor=replication, topic_configs=configs)
                for name in missing
            ])
            out.update({name: "created" for name in missing})
        present = [name for name in wanted if name in existing]
        if present:
            await admin.alter_configs([ConfigResource(ConfigResourceType.TOPIC, name, configs=configs)
                                       for name in present])
            out.update({name: "updated" for name in present})
    finally:
        await _quietly(admin.close())
    return out
