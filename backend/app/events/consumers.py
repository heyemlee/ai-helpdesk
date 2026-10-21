"""
事件日志在 Kafka 那一侧的两个消费者（AI_HELPDESK_EVENT_SINK=kafka 时）：投影和采样。

各是一个消费组、各记各的 offset：投影停了不耽误采样；采样改了规则就换一个消费组从头读一遍
（topic 留 7 天），不碰投影。两个都在同一个镜像里跑，入口见 `python -m app.events`。

投影（Projector）：读本实例的 topic，批量写进 `turn_events`，读接口照旧读库。

- 写库成功之后才提交 offset。进程在两者之间挂掉，重启后这一批再来一次，主键 (turn_id, seq)
  去重：至少一次投递，加上幂等的写入。
- 解不开的消息、库拒收的那一行（数据本身的问题，SQLSTATE 22 / 23 / 54 类）送进死信 topic，
  带上原因和来处，然后照常提交——一条坏消息不卡住整个分区。连不上库这类问题不跳过：一直退避
  重试，offset 不动，积压涨上去由告警发现。
- 删除请求删过的会话（forgotten_conversations）不写：判断和写入是同一条语句。
- 原文过了保留期的去掉原文再写，整行过了保留期的不写。topic 本来就只留 7 天，这是第二道保险。

采样（Sampler）：等一轮结束（终结事件，或者别人替它补的 turn_abandoned），按规则判断它值不值得
进评测集，值得的记进 `eval_candidates`（只有结构，导出用例时再去事件日志取原文）。

- 有状态：一轮的事件要攒齐了才能判断。所以 offset 不能处理一条提交一条——没结束的轮次在内存
  里，进程一挂就没了。每个分区只提交到"手上最早一个还没结束的轮次的第一条消息"：重启后从那里
  重读，把没结束的轮次补齐；已经采过的按主键去重。
- 一轮迟迟不结束（终结事件丢了，又没人替它补）就会一直压着这个分区的提交点：开着超过
  max_open_s（默认 15 分钟）的丢掉不采，计数。
- 同一个会话的事件在一个分区里，按发出的顺序到达：接手的第二遍开始之前，第一遍那一轮的
  turn_abandoned 已经先到了。
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.core.metrics import (
    EVAL_CANDIDATES,
    SAMPLER_OPEN_TURNS,
    TURN_EVENTS_CONSUMED,
    TURN_EVENTS_CONSUMER_LAG,
    TURN_EVENTS_CONSUMER_SKIPPED,
)
from app.events.kafka import BadEvent, KafkaSettings, decode
from app.events.projection import summarize_turn
from app.events.types import CLOSING_TYPES, TERMINAL_TYPES, Event, own_seq
from app.events.writer import _bad_row

logger = logging.getLogger(__name__)

DAY_S = 86400.0


def group_id(settings: KafkaSettings, consumer: str, instance: str, suffix: str = "") -> str:
    """消费组的名字：<topic 前缀>.<消费者>.<实例>[.<规则>]。一个消费组一份 offset。"""
    return ".".join(part for part in (settings.topic_prefix, consumer, instance, suffix) if part)


def _days(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    try:
        return max(int(raw), 0) if raw else default
    except ValueError:
        logger.warning("%s=%r 不是整数，按默认 %d", name, raw, default)
        return default


@dataclass(frozen=True)
class ConsumerSettings:
    instance: str
    kafka: KafkaSettings
    # 和这个实例的服务配成一样（客户实例原文 14 天）：投影按它们去掉过期的原文、跳过过期的整行
    body_days: int = 30
    row_days: int = 180
    max_records: int = 500
    poll_ms: int = 1000
    lag_every_s: float = 15.0

    @classmethod
    def from_env(cls, instance: str, env: Mapping[str, str] | None = None) -> ConsumerSettings:
        env = os.environ if env is None else env
        return cls(
            instance=instance,
            kafka=KafkaSettings.from_env(env),
            body_days=_days(env, "AI_HELPDESK_EVENT_LOG_BODY_DAYS", 30),
            row_days=_days(env, "AI_HELPDESK_EVENT_LOG_DAYS", 180),
        )


def _consumer(settings: KafkaSettings, group: str):
    from aiokafka import AIOKafkaConsumer

    return AIOKafkaConsumer(
        bootstrap_servers=settings.bootstrap, client_id=f"{settings.client_id}-{group}", group_id=group,
        # 提交完全自己管（见本模块开头）；一个新消费组从 topic 留着的最早那条开始读
        enable_auto_commit=False, auto_offset_reset="earliest",
    )


def _producer(settings: KafkaSettings):
    from aiokafka import AIOKafkaProducer

    return AIOKafkaProducer(bootstrap_servers=settings.bootstrap, client_id=f"{settings.client_id}-dlq",
                            acks="all", enable_idempotence=True)


class Stopping(Exception):
    """要停了：手上这一批不提交，重启后再来一次。"""


class _ConsumerLoop:
    """两个消费者共用的那一圈：订阅、拉一批、交给 handle、定时报积压、停机收尾。"""

    consumer_name = ""

    def __init__(self, settings: ConsumerSettings, group: str, *,
                 consumer_factory: Callable[[KafkaSettings, str], Any] | None = None,
                 producer_factory: Callable[[KafkaSettings], Any] | None = None):
        self.settings = settings
        self.group = group
        self.topic = settings.kafka.topic(settings.instance)
        self._consumer_factory = consumer_factory or _consumer
        self._producer_factory = producer_factory or _producer
        self._consumer = None
        self._producer = None
        self._stop: asyncio.Event | None = None
        self._lag_at = 0.0

    @property
    def stopping(self) -> bool:
        return self._stop is not None and self._stop.is_set()

    # ── 订阅时机（aiokafka 的 ConsumerRebalanceListener）─────────────────────────

    async def on_partitions_revoked(self, revoked) -> None:
        """分区要交给别人了：采样那边先把能提交的提交掉、丢掉这些分区的状态。"""

    async def on_partitions_assigned(self, assigned) -> None:
        logger.info("%s %s 分到分区 %s", self.consumer_name, self.group, sorted(tp.partition for tp in assigned))

    # ── 主循环 ────────────────────────────────────────────────────────────────

    async def run(self, stop: asyncio.Event) -> None:
        """跑到 stop 被设上。连不上 broker、拉取出错都在里面退避重来，不往外抛。"""
        self._stop = stop
        delay = 1.0
        while not stop.is_set():
            try:
                await self._run_once(stop)
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Stopping:
                break
            except Exception:
                logger.warning("%s %s 出错，%.0f 秒后重连", self.consumer_name, self.group, delay, exc_info=True)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
                delay = min(delay * 2, 30.0)
        await self._close_producer()

    async def _run_once(self, stop: asyncio.Event) -> None:
        consumer = self._consumer_factory(self.settings.kafka, self.group)
        consumer.subscribe([self.topic], listener=_listener(self))
        await consumer.start()
        self._consumer = consumer
        logger.info("%s 开始消费 %s（消费组 %s）", self.consumer_name, self.topic, self.group)
        try:
            while not stop.is_set():
                batches = await consumer.getmany(timeout_ms=self.settings.poll_ms,
                                                 max_records=self.settings.max_records)
                for tp, messages in batches.items():
                    if messages:
                        await self.handle(consumer, tp, messages)
                await self.tick(consumer)
                if time.monotonic() - self._lag_at >= self.settings.lag_every_s:
                    await self._report_lag(consumer)
            await self.on_stop(consumer)
        finally:
            self._consumer = None
            await _quietly(consumer.stop())

    async def handle(self, consumer, tp, messages) -> None:
        raise NotImplementedError

    async def tick(self, consumer) -> None:
        """每拉一次之后调一次（有没有消息都调）。"""

    async def on_stop(self, consumer) -> None:
        """正常停机、还连着 broker 的时候调：把能提交的提交掉。"""

    # ── 公用 ──────────────────────────────────────────────────────────────────

    def _skip(self, reason: str, n: int = 1) -> None:
        TURN_EVENTS_CONSUMER_SKIPPED.labels(consumer=self.consumer_name, instance=self.settings.instance,
                                            reason=reason).inc(n)

    def _consumed(self, n: int) -> None:
        TURN_EVENTS_CONSUMED.labels(consumer=self.consumer_name, instance=self.settings.instance).inc(n)

    async def _retrying(self, attempt: Callable[[], Any]) -> Any:
        """
        库或 broker 暂时不可用：退避重试，直到成功或要停机。数据本身的问题（_bad_row）直接抛给
        调用方处理；要停机时抛 Stopping，这一批不提交。
        """
        delay = 0.5
        while True:
            try:
                return await attempt()
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                if _bad_row(ex):
                    raise
                if self.stopping:
                    raise Stopping() from ex
                logger.warning("%s 写不进去（%s），%.1f 秒后重试", self.consumer_name, type(ex).__name__, delay)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
                delay = min(delay * 2, 30.0)

    async def _commit(self, consumer, offsets: dict) -> bool:
        """提交 offset。正赶上重新分配分区时会失败：不要紧，接手的那个从上次提交的地方重读。"""
        if not offsets:
            return True
        try:
            await consumer.commit(offsets)
            return True
        except Exception as ex:
            logger.warning("%s 提交 offset 失败（%s）：这几个分区会重读一段", self.consumer_name, type(ex).__name__)
            return False

    async def _report_lag(self, consumer) -> None:
        self._lag_at = time.monotonic()
        assigned = list(consumer.assignment())
        if not assigned:
            return
        try:
            ends = await consumer.end_offsets(assigned)
            for tp in assigned:
                committed = await consumer.committed(tp)
                if committed is None:
                    committed = (await consumer.beginning_offsets([tp]))[tp]
                TURN_EVENTS_CONSUMER_LAG.labels(consumer=self.consumer_name, instance=self.settings.instance,
                                                partition=str(tp.partition)).set(max(ends[tp] - committed, 0))
        except Exception:
            logger.debug("%s 读不到积压", self.consumer_name, exc_info=True)

    async def _dead_letter(self, tp, rejected: list[tuple[Any, str]]) -> None:
        """坏消息原样送进死信 topic，头里带着原因和来处（topic:分区:offset）。"""
        topic = self.settings.kafka.dead_letter_topic(self.settings.instance)

        async def send() -> None:
            if self._producer is None:
                producer = self._producer_factory(self.settings.kafka)
                try:
                    await producer.start()
                except BaseException:
                    await _quietly(producer.stop())
                    raise
                self._producer = producer
            futures = [await self._producer.send(
                topic, value=message.value, key=message.key,
                headers=[("error", reason.encode()), ("consumer", self.consumer_name.encode()),
                         ("source", f"{tp.topic}:{tp.partition}:{message.offset}".encode())],
            ) for message, reason in rejected]
            await asyncio.gather(*futures)

        await self._retrying(send)
        for message, reason in rejected:
            logger.warning("%s 把 %s:%d:%d 送进了死信：%s", self.consumer_name, tp.topic, tp.partition,
                           message.offset, reason)

    async def _close_producer(self) -> None:
        producer, self._producer = self._producer, None
        if producer is not None:
            await _quietly(producer.stop())


async def _quietly(awaitable) -> None:
    try:
        await awaitable
    except Exception:
        logger.debug("关闭 Kafka 客户端时出错", exc_info=True)


def _listener(loop: _ConsumerLoop):
    """aiokafka 要的是 ConsumerRebalanceListener 的实例：转给消费者自己的两个方法。"""
    from aiokafka.abc import ConsumerRebalanceListener

    class Listener(ConsumerRebalanceListener):
        async def on_partitions_revoked(self, revoked):
            await loop.on_partitions_revoked(revoked)

        async def on_partitions_assigned(self, assigned):
            await loop.on_partitions_assigned(assigned)

    return Listener()


# ── 投影 ──────────────────────────────────────────────────────────────────────

class Projector(_ConsumerLoop):
    """读 topic，写 `turn_events`（PgEventStore.insert_unless_forgotten）。"""

    consumer_name = "projector"

    def __init__(self, settings: ConsumerSettings, store, *, clock: Callable[[], float] = time.time, **factories):
        super().__init__(settings, group_id(settings.kafka, "projector", settings.instance), **factories)
        self.store = store
        self._clock = clock

    async def handle(self, consumer, tp, messages) -> None:
        now = self._clock()
        good: list[tuple[Any, Event]] = []
        rejected: list[tuple[Any, str]] = []
        for message in messages:
            try:
                event = decode(message.value)
            except BadEvent as ex:
                rejected.append((message, str(ex)))
                self._skip("bad_message")
                continue
            if event.instance != self.settings.instance:
                rejected.append((message, f"实例是 {event.instance!r}，不是这个 topic 的 {self.settings.instance!r}"))
                self._skip("bad_message")
                continue
            if self.settings.row_days and event.ts < now - self.settings.row_days * DAY_S:
                self._skip("expired")
                continue
            if self.settings.body_days and event.body is not None and event.ts < now - self.settings.body_days * DAY_S:
                event = dataclasses.replace(event, body=None)
                self._skip("stripped")
            good.append((message, event))

        if good:
            forgotten = await self._retrying(lambda: self.store.forgotten_among([e.conv_id for _, e in good]))
            if forgotten:
                self._skip("forgotten", sum(1 for _, e in good if e.conv_id in forgotten))
            for event, reason in await self._write([e for _, e in good]):
                message = next(m for m, e in good if (e.turn_id, e.seq) == (event.turn_id, event.seq))
                rejected.append((message, reason))
                self._skip("bad_row")
        if rejected:
            await self._dead_letter(tp, rejected)
        await self._commit(consumer, {tp: messages[-1].offset + 1})
        self._consumed(len(messages))

    async def _write(self, events: list[Event]) -> list[tuple[Event, str]]:
        """整批写；库因为数据拒收时逐行写，返回写不进去的那几行和原因。"""
        try:
            await self._retrying(lambda: self.store.insert_unless_forgotten(events))
            return []
        except Stopping:
            raise
        except Exception as ex:
            if not _bad_row(ex):
                raise
        rejected = []
        for event in events:
            try:
                await self._retrying(lambda e=event: self.store.insert_unless_forgotten([e]))
            except Stopping:
                raise
            except Exception as ex:
                if not _bad_row(ex):
                    raise
                rejected.append((event, f"库拒收（SQLSTATE {getattr(ex, 'sqlstate', '')}，{type(ex).__name__}）"))
        return rejected


# ── 采样 ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SamplingRules:
    """
    哪些轮次值得进评测集。改了规则就改 label：换一个消费组（从 topic 留着的最早那条读起）、
    记成新的一份候选，旧的那份不动。
    """

    name: str = "v1"
    random_rate: float = 0.05
    min_confidence: float = 0.5

    @property
    def label(self) -> str:
        """存进库、拼进消费组名字的版本号：参数和默认不一样时带上参数。"""
        parts = [self.name]
        if self.random_rate != SamplingRules.random_rate:
            parts.append(f"random={self.random_rate:g}")
        if self.min_confidence != SamplingRules.min_confidence:
            parts.append(f"min_conf={self.min_confidence:g}")
        return ";".join(parts)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SamplingRules:
        env = os.environ if env is None else env

        def ratio(name: str, default: float) -> float:
            raw = env.get(name, "").strip()
            try:
                value = float(raw) if raw else default
            except ValueError:
                value = -1.0
            if not 0.0 <= value <= 1.0:
                logger.warning("%s=%r 要在 0 到 1 之间，按默认 %g", name, raw, default)
                return default
            return value

        return cls(name=env.get("AI_HELPDESK_SAMPLER_RULES", "").strip() or "v1",
                   random_rate=ratio("AI_HELPDESK_SAMPLER_RANDOM_RATE", cls.random_rate),
                   min_confidence=ratio("AI_HELPDESK_SAMPLER_MIN_CONFIDENCE", cls.min_confidence))


def _stable_fraction(turn_id: str) -> float:
    """turn_id → [0, 1) 里一个固定的数：同一轮无论回放几次，随机抽样的结果都一样。"""
    return int.from_bytes(hashlib.sha256(turn_id.encode()).digest()[:8], "big") / 2 ** 64


def reasons_for(events: list[Event], rules: SamplingRules) -> list[str]:
    """一轮（已经结束）为什么值得看。空表示不采。"""
    decoded = sorted((e.to_dict() for e in events), key=lambda e: e["seq"])
    kinds = {e["type"] for e in decoded}
    terminal = next((e for e in decoded if own_seq(e["seq"]) and e["type"] in TERMINAL_TYPES), None)
    completed = terminal["data"] if terminal is not None and terminal["type"] == "turn_completed" else {}
    started = next((e["data"] for e in decoded if e["type"] == "turn_started"), {})
    reasons = []
    if terminal is None and "turn_abandoned" in kinds:
        reasons.append("abandoned")                   # worker 被杀的那一遍
    elif terminal is not None and terminal["type"] in ("turn_failed", "turn_aborted"):
        reasons.append(terminal["type"].removeprefix("turn_"))
    outcome = completed.get("outcome")
    if outcome in ("handoff", "unavailable"):
        reasons.append(outcome)
    if ((started.get("run") or {}).get("attempt") or 1) > 1:
        reasons.append("taken_over")                  # 接手的那一遍：和上面的 abandoned 是一对
    if ((started.get("degrade") or {}).get("level") or 0) > 0:
        reasons.append("degraded")
    if "guard_replaced" in kinds:
        reasons.append("guard_replaced")
    if completed.get("violations"):
        reasons.append("policy_violation")
    confidence = completed.get("answer_confidence")
    if outcome == "answered" and isinstance(confidence, (int, float)) and confidence < rules.min_confidence:
        reasons.append("low_confidence")
    if kinds & {"agent_fallback", "loop_fallback"}:
        reasons.append("fallback")
    # 随机的那一份对所有轮次一视同仁：拿它做基线，看上面那些挑出来的偏了多少
    turn_id = decoded[0]["turn_id"] if decoded else ""
    if turn_id and _stable_fraction(turn_id) < rules.random_rate:
        reasons.append("random")
    return reasons


_INSERT_CANDIDATE = (
    "INSERT INTO eval_candidates (instance, rules, turn_id, conv_id, turn_ts, sampled_at, reasons, summary)"
    " VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb)"
    " ON CONFLICT (instance, rules, turn_id) DO NOTHING"
)

# 每个分区记住最近结束的多少轮：结束之后才到的事件（轮次之后的后台任务、重复投递）认得出来
CLOSED_MEMORY = 5000


@dataclass
class _OpenTurn:
    first_offset: int
    opened_at: float
    events: dict[int, Event] = field(default_factory=dict)


@dataclass
class _PartitionState:
    open: dict[str, _OpenTurn] = field(default_factory=dict)
    closed: OrderedDict = field(default_factory=OrderedDict)
    next_offset: int | None = None
    committed: int | None = None

    def watermark(self) -> int | None:
        """能提交到哪：手上最早一个没结束的轮次的第一条；都结束了就是处理到的下一条。"""
        if self.next_offset is None:
            return None
        return min((turn.first_offset for turn in self.open.values()), default=self.next_offset)


class Sampler(_ConsumerLoop):
    """读 topic，一轮结束时按规则判断，值得的写进 `eval_candidates`。"""

    consumer_name = "sampler"

    def __init__(self, settings: ConsumerSettings, db, rules: SamplingRules | None = None, *,
                 max_open_s: float = 900.0, commit_every_s: float = 5.0,
                 clock: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic,
                 **factories):
        self.rules = rules or SamplingRules()
        label = "".join(c if c.isalnum() or c in "-_" else "_" for c in self.rules.label)
        super().__init__(settings, group_id(settings.kafka, "sampler", settings.instance, label), **factories)
        self.db = db
        self.max_open_s = max_open_s
        self.commit_every_s = commit_every_s
        self._clock = clock
        self._monotonic = monotonic
        self._parts: dict[Any, _PartitionState] = {}
        self._committed_at = 0.0

    @property
    def open_turns(self) -> int:
        return sum(len(part.open) for part in self._parts.values())

    async def handle(self, consumer, tp, messages) -> None:
        part = self._parts.setdefault(tp, _PartitionState())
        for message in messages:
            part.next_offset = message.offset + 1
            try:
                event = decode(message.value)
            except BadEvent:
                self._skip("bad_message")             # 进死信是投影那边的事，这里只是不看
                continue
            if event.turn_id in part.closed:
                self._skip("late")
                continue
            turn = part.open.get(event.turn_id)
            if turn is None:
                turn = part.open[event.turn_id] = _OpenTurn(message.offset, self._monotonic())
            turn.events.setdefault(event.seq, event)
            if event.type in CLOSING_TYPES:
                await self._decide(turn)
                del part.open[event.turn_id]
                part.closed[event.turn_id] = None
                while len(part.closed) > CLOSED_MEMORY:
                    part.closed.popitem(last=False)
        self._consumed(len(messages))
        SAMPLER_OPEN_TURNS.labels(instance=self.settings.instance).set(self.open_turns)

    async def _decide(self, turn: _OpenTurn) -> None:
        events = [turn.events[seq] for seq in sorted(turn.events)]
        reasons = reasons_for(events, self.rules)
        if not reasons:
            return
        head = events[0]
        started = next((json.loads(e.data) for e in events if e.type == "turn_started"), {})
        summary = summarize_turn(events)
        summary.update({key: started[key] for key in ("run", "degrade") if key in started})
        row = (self.settings.instance, self.rules.label, head.turn_id, head.conv_id, head.ts, self._clock(),
               json.dumps(reasons), json.dumps(summary, ensure_ascii=False, default=str))
        await self._retrying(lambda: self.db.execute(_INSERT_CANDIDATE, *row))
        for reason in reasons:
            EVAL_CANDIDATES.labels(instance=self.settings.instance, reason=reason).inc()

    async def tick(self, consumer) -> None:
        now = self._monotonic()
        for part in self._parts.values():
            for turn_id, turn in list(part.open.items()):
                if now - turn.opened_at > self.max_open_s:
                    del part.open[turn_id]
                    part.closed[turn_id] = None
                    self._skip("expired_open", len(turn.events))
                    logger.warning("轮次 %s 开着超过 %.0f 秒没结束，丢掉不采", turn_id, self.max_open_s)
        if now - self._committed_at >= self.commit_every_s:
            await self._commit_watermarks(consumer)
        SAMPLER_OPEN_TURNS.labels(instance=self.settings.instance).set(self.open_turns)

    async def _commit_watermarks(self, consumer, partitions=None) -> None:
        self._committed_at = self._monotonic()
        offsets = {}
        for tp, part in self._parts.items():
            if partitions is not None and tp not in partitions:
                continue
            mark = part.watermark()
            if mark is not None and mark != part.committed:
                offsets[tp] = mark
        if offsets and await self._commit(consumer, offsets):
            for tp, mark in offsets.items():
                self._parts[tp].committed = mark

    async def on_partitions_revoked(self, revoked) -> None:
        if self._consumer is not None:
            await self._commit_watermarks(self._consumer, set(revoked))
        for tp in revoked:
            self._parts.pop(tp, None)

    async def on_stop(self, consumer) -> None:
        await self._commit_watermarks(consumer)
