"""
事件日志的 Kafka 那一侧（events/kafka.py）：编码、生产者、topic、和 EventLog 的装配。

离线的用例用假的生产者和管理客户端；最后几条连真的 broker（TEST_KAFKA_BOOTSTRAP，CI 里
是一个 Kafka 服务），没设就跳过。
"""
import asyncio
import json
import os
import time
import uuid

import pytest
from aiokafka.errors import KafkaTimeoutError, MessageSizeTooLargeError

from app.events import EventLog, EventLogConfig
from app.events.kafka import (
    MAX_RETENTION_DAYS,
    BadEvent,
    KafkaPublisher,
    KafkaSettings,
    decode,
    encode,
    ensure_topics,
)
from app.events.recorder import EventLogStats, TurnRecorder, new_turn_id
from app.events.types import Event

TEST_KAFKA_BOOTSTRAP = os.getenv("TEST_KAFKA_BOOTSTRAP", "").strip()
needs_kafka = pytest.mark.skipif(not TEST_KAFKA_BOOTSTRAP, reason="未设置 TEST_KAFKA_BOOTSTRAP")


def _event(turn_id="t1", seq=0, kind="turn_started", conv="c1", body: dict | None = None) -> Event:
    return Event(turn_id=turn_id, seq=seq, instance="staff", conv_id=conv, request_id="r1", type=kind,
                 agent="", step=None, call_id="", ts=time.time(), t_ms=1.5, v=1,
                 data=json.dumps({"transport": "stream"}),
                 body=None if body is None else json.dumps(body, ensure_ascii=False))


class FakeProducer:
    """记下发了什么；按事件类型决定这一条太大（发的时候就拒）、或者 broker 没确认。"""

    def __init__(self, *, fail_start=0, too_large=(), unacked=()):
        self.fail_start = fail_start
        self.too_large, self.unacked = set(too_large), set(unacked)
        self.started = self.stopped = 0
        self.sent: list[tuple[str, bytes, bytes]] = []

    async def start(self):
        self.started += 1
        if self.fail_start:
            self.fail_start -= 1
            raise ConnectionError("broker 不在")

    async def stop(self):
        self.stopped += 1

    async def send(self, topic, value=None, key=None):
        kind = json.loads(value)["type"]
        if kind in self.too_large:
            raise MessageSizeTooLargeError()
        future = asyncio.get_running_loop().create_future()
        if kind in self.unacked:
            future.set_exception(KafkaTimeoutError())
        else:
            self.sent.append((topic, key, value))
            future.set_result(None)
        return future


def _publisher(producer: FakeProducer, stats: EventLogStats | None = None, instance="staff"):
    return KafkaPublisher(KafkaSettings(bootstrap="kafka:9092"), instance, stats or EventLogStats(),
                          producer_factory=lambda _settings: producer)


# ── 编码 ──────────────────────────────────────────────────────────────────────

def test_an_event_goes_through_kafka_unchanged():
    """data 和 body 是记下那一刻的 JSON 文本：原样带过去，不解开再编码。"""
    event = _event(body={"message": "柜门关不严 ✓", "user_id": "u1"})
    key, value = encode(event)
    assert key == b"c1" and decode(value) == event
    assert decode(encode(_event(body=None))[1]).body is None
    # 没有 conv_id 的（理论上不该有）按 turn_id 分区，不落进同一个空 key
    assert encode(_event(conv=""))[0] == b"t1"


@pytest.mark.parametrize("value", [b"", b"not json", b"[]", b'{"turn_id": "t"}',
                                   json.dumps({**json.loads(encode(_event())[1]), "seq": "0"}).encode()])
def test_a_message_that_is_not_an_event_is_rejected(value):
    with pytest.raises(BadEvent):
        decode(value)


# ── 生产者 ────────────────────────────────────────────────────────────────────

async def test_events_go_to_the_instance_topic_keyed_by_conversation():
    producer = FakeProducer()
    publisher = _publisher(producer, instance="customer")
    assert await publisher.insert([_event(seq=0), _event(seq=1, kind="turn_completed", conv="c1")]) == 2
    assert [(topic, key) for topic, key, _ in producer.sent] == [("ai-helpdesk.turn-events.customer", b"c1")] * 2
    assert producer.started == 1 and publisher.connected
    await publisher.insert([_event(seq=2)])
    assert producer.started == 1                                   # 连一次，之后复用
    await publisher.close()
    assert producer.stopped == 1 and not publisher.connected


async def test_a_message_too_large_for_the_broker_is_dropped_alone():
    stats = EventLogStats()
    producer = FakeProducer(too_large={"model_call"})
    publisher = _publisher(producer, stats)
    batch = [_event(seq=0), _event(seq=1, kind="model_call"), _event(seq=2, kind="turn_completed")]
    assert await publisher.insert(batch) == 2
    assert [json.loads(v)["seq"] for _, _, v in producer.sent] == [0, 2]
    assert stats.dropped == {"too_large": 1}


async def test_an_unacknowledged_batch_raises_so_the_writer_retries_it():
    producer = FakeProducer(unacked={"turn_completed"})
    publisher = _publisher(producer)
    with pytest.raises(KafkaTimeoutError):
        await publisher.insert([_event(seq=0), _event(seq=1, kind="turn_completed")])
    assert len(producer.sent) == 1                       # 确认过的那条，重试时会再发一次（消费者去重）


async def test_a_broker_that_is_down_at_startup_is_tried_again_on_the_next_batch():
    producer = FakeProducer(fail_start=1)
    publisher = _publisher(producer)
    with pytest.raises(ConnectionError):
        await publisher.insert([_event()])
    assert not publisher.connected
    assert await publisher.insert([_event()]) == 1
    assert producer.started == 2


# ── 装配 ──────────────────────────────────────────────────────────────────────

def test_the_kafka_sink_needs_a_bootstrap_address():
    config = EventLogConfig.from_env({"AI_HELPDESK_EVENT_LOG": "full", "AI_HELPDESK_INSTANCE": "staff",
                                      "AI_HELPDESK_EVENT_SINK": "kafka"})
    assert config.sink == "postgres" and any("AI_HELPDESK_KAFKA_BOOTSTRAP" in w for w in config.warnings)

    config = EventLogConfig.from_env({"AI_HELPDESK_EVENT_LOG": "full", "AI_HELPDESK_INSTANCE": "staff",
                                      "AI_HELPDESK_EVENT_SINK": "Kafka", "AI_HELPDESK_KAFKA_BOOTSTRAP": "kafka:9092",
                                      "AI_HELPDESK_KAFKA_TOPIC_PREFIX": "acme.events"})
    assert (config.sink, config.kafka.bootstrap, config.kafka.topic("staff")) == ("kafka", "kafka:9092",
                                                                                  "acme.events.staff")
    config = EventLogConfig.from_env({"AI_HELPDESK_EVENT_SINK": "s3"})
    assert config.sink == "postgres" and any("s3" in w for w in config.warnings)
    assert EventLogConfig.from_env({}).sink == "postgres"


class _NoDb:
    """sink=kafka 时写不经过库；读接口和保留期才用它，这几条用例都不读。"""


async def test_a_kafka_event_log_writes_through_the_publisher_and_flushes_on_close(monkeypatch):
    producer = FakeProducer()
    monkeypatch.setattr("app.events.kafka._producer", lambda _settings: producer)
    config = EventLogConfig(mode="full", instance="staff", body_days=0, row_days=0, sink="kafka",
                            kafka=KafkaSettings(bootstrap="kafka:9092"))
    log = await EventLog.open(config, db=_NoDb())
    turn = log.open_turn(conv_id="c-k", request_id="r1")
    turn.emit("turn_started", {"transport": "stream"}, {"user_id": "u1", "message": "补件到哪了"})
    turn.emit("turn_completed", {"outcome": "answered"})
    turn.close()
    health = log.health()
    assert (health["store"], health["sink"]) == ("postgres", "kafka")
    assert health["kafka"]["topic"] == "ai-helpdesk.turn-events.staff"

    await log.close()
    assert [decode(v) for _, _, v in producer.sent] == turn.events
    assert log.stats.written == 2 and producer.stopped == 1


# ── topic ─────────────────────────────────────────────────────────────────────

class FakeAdmin:
    existing: set[str] = set()

    def __init__(self, **_):
        self.created, self.altered = [], []

    async def start(self):
        pass

    async def close(self):
        pass

    async def list_topics(self):
        return list(self.existing) + ["__consumer_offsets"]

    async def create_topics(self, topics):
        self.created.extend(topics)

    async def alter_configs(self, resources):
        self.altered.extend(resources)


async def test_topics_are_created_once_and_their_retention_kept_in_line():
    settings = KafkaSettings(bootstrap="kafka:9092")
    admins: list[FakeAdmin] = []

    def factory(**kwargs):
        admins.append(FakeAdmin(**kwargs))
        return admins[-1]

    FakeAdmin.existing = {"ai-helpdesk.turn-events.staff"}
    out = await ensure_topics(settings, ["staff", "customer"], partitions=6, retention_days=7, admin_factory=factory)
    assert out == {"ai-helpdesk.turn-events.staff.dlq": "created", "ai-helpdesk.turn-events.customer": "created",
                   "ai-helpdesk.turn-events.customer.dlq": "created", "ai-helpdesk.turn-events.staff": "updated"}
    [admin] = admins
    assert {t.name: (t.num_partitions, t.topic_configs["retention.ms"]) for t in admin.created} == {
        name: (6, str(7 * 86_400_000)) for name, state in out.items() if state == "created"}
    assert [r.name for r in admin.altered] == ["ai-helpdesk.turn-events.staff"]


@pytest.mark.parametrize("days", [0, MAX_RETENTION_DAYS + 1, 30])
async def test_a_topic_may_not_keep_text_longer_than_the_database_does(days):
    with pytest.raises(ValueError, match="原文保留期"):
        await ensure_topics(KafkaSettings(bootstrap="kafka:9092"), ["staff"], retention_days=days,
                            admin_factory=FakeAdmin)


async def test_only_known_instances_get_topics():
    with pytest.raises(ValueError, match="实例"):
        await ensure_topics(KafkaSettings(bootstrap="kafka:9092"), ["staff", "Staff"], admin_factory=FakeAdmin)


# ── 真的 broker ───────────────────────────────────────────────────────────────

async def _drop_topics(settings: KafkaSettings, names: list[str]) -> None:
    from aiokafka.admin import AIOKafkaAdminClient

    admin = AIOKafkaAdminClient(bootstrap_servers=settings.bootstrap)
    await admin.start()
    try:
        await admin.delete_topics(names)
    finally:
        await admin.close()


async def _read_all(settings: KafkaSettings, topic: str, count: int, timeout_s: float = 20.0):
    from aiokafka import AIOKafkaConsumer

    consumer = AIOKafkaConsumer(topic, bootstrap_servers=settings.bootstrap, group_id=None,
                                auto_offset_reset="earliest", enable_auto_commit=False)
    await consumer.start()
    got = []
    try:
        deadline = time.monotonic() + timeout_s
        while len(got) < count and time.monotonic() < deadline:
            for messages in (await consumer.getmany(timeout_ms=500)).values():
                got.extend(messages)
    finally:
        await consumer.stop()
    return got


@needs_kafka
async def test_a_turn_reaches_the_broker_in_order_and_decodes_to_the_same_events():
    settings = KafkaSettings(bootstrap=TEST_KAFKA_BOOTSTRAP, topic_prefix=f"test-{uuid.uuid4().hex[:8]}.turn-events")
    topics = [settings.topic("staff"), settings.dead_letter_topic("staff")]
    assert await ensure_topics(settings, ["staff"], partitions=3, retention_days=1) == dict.fromkeys(topics, "created")
    try:
        assert await ensure_topics(settings, ["staff"], partitions=3, retention_days=1) == dict.fromkeys(topics,
                                                                                                        "updated")
        log = await EventLog.open(EventLogConfig(mode="full", instance="staff", body_days=0, row_days=0,
                                                 sink="kafka", kafka=settings), db=_NoDb())
        turns = []
        for conv in ("conv-a", "conv-b"):
            turn = log.open_turn(conv_id=conv, request_id="r1")
            turn.emit("turn_started", {"transport": "stream"}, {"user_id": "u1", "message": f"{conv} 补件到哪了"})
            turn.emit("routed", {"primary": "order"})
            turn.emit("turn_completed", {"outcome": "answered"})
            turn.close()
            turns.append(turn)
        log.close_abandoned(new_turn_id(), conv_id="conv-a", data={"reason": "superseded"})
        await log.close()
        assert log.stats.written == 7 and log.stats.dropped == {}

        messages = await _read_all(settings, settings.topic("staff"), 7)
        by_key: dict[bytes, list[Event]] = {}
        for message in messages:
            by_key.setdefault(message.key, []).append(decode(message.value))
        assert set(by_key) == {b"conv-a", b"conv-b"}
        # 同一个会话在一个分区里，按发出的顺序
        assert by_key[b"conv-b"] == turns[1].events
        assert by_key[b"conv-a"][:3] == turns[0].events and by_key[b"conv-a"][3].type == "turn_abandoned"
    finally:
        await _drop_topics(settings, topics)


def test_a_recorder_can_start_numbering_from_a_reserved_seq():
    stats = EventLogStats()
    sink = []

    class Sink:
        def put(self, event):
            sink.append(event)

    turn = TurnRecorder("t9", instance="staff", conv_id="c", mode="meta", sink=Sink(), stats=stats, first_seq=1_000_000)
    turn.emit("turn_abandoned", {"reason": "dead_letter"})
    assert [e.seq for e in sink] == [1_000_000]
