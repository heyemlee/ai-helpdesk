"""
事件日志的两个 Kafka 消费者（events/consumers.py）：投影写 turn_events，采样挑评测候选。

离线的用例直接把消息喂给 handle，提交、死信、库都用假的；最后几条连真的 broker 和 Postgres
（TEST_KAFKA_BOOTSTRAP、TEST_POSTGRES_DSN），没设就跳过。
"""
import asyncio
import json
import os
import time
import uuid
from types import SimpleNamespace

import pytest
from aiokafka.structs import TopicPartition

from app.events import EventLog, EventLogConfig
from app.events.consumers import (
    ConsumerSettings,
    Projector,
    Sampler,
    SamplingRules,
    Stopping,
    _stable_fraction,
    group_id,
    reasons_for,
)
from app.events.kafka import KafkaSettings, encode, ensure_topics
from app.events.store import MemoryEventStore
from app.events.types import ABANDONED_SEQ, Event

TEST_KAFKA_BOOTSTRAP = os.getenv("TEST_KAFKA_BOOTSTRAP", "").strip()
TEST_PG_DSN = os.getenv("TEST_POSTGRES_DSN", "").strip()
needs_both = pytest.mark.skipif(not (TEST_KAFKA_BOOTSTRAP and TEST_PG_DSN),
                                reason="未设置 TEST_KAFKA_BOOTSTRAP / TEST_POSTGRES_DSN")

KAFKA = KafkaSettings(bootstrap="kafka:9092")
TP = TopicPartition("ai-helpdesk.turn-events.staff", 0)


def _event(turn_id, seq, kind, *, conv="c1", data=None, body=None, ts=None, instance="staff") -> Event:
    return Event(turn_id=turn_id, seq=seq, instance=instance, conv_id=conv, request_id="r1", type=kind,
                 agent="", step=None, call_id="", ts=time.time() if ts is None else ts, t_ms=float(seq), v=1,
                 data=json.dumps(data or {}), body=None if body is None else json.dumps(body, ensure_ascii=False))


def _messages(events, start=0, raw=None):
    """事件 → 假的 Kafka 消息（offset 从 start 起连续）。raw 里给的 offset 换成原样的字节。"""
    out = []
    for index, event in enumerate(events):
        offset = start + index
        if raw and offset in raw:
            out.append(SimpleNamespace(offset=offset, key=b"bad", value=raw[offset]))
            continue
        key, value = encode(event)
        out.append(SimpleNamespace(offset=offset, key=key, value=value))
    return out


class Commits:
    """假的 consumer：只记下提交了什么。"""

    def __init__(self):
        self.offsets: list[dict] = []

    async def commit(self, offsets):
        self.offsets.append(dict(offsets))


class DeadLetters:
    def __init__(self):
        self.sent: list[tuple[str, bytes, dict]] = []

    async def start(self):
        pass

    async def stop(self):
        pass

    async def send(self, topic, value=None, key=None, headers=None):
        self.sent.append((topic, value, {k: v.decode() for k, v in headers or []}))
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future


def _settings(**overrides) -> ConsumerSettings:
    return ConsumerSettings(instance="staff", kafka=KAFKA, **overrides)


def _projector(store, **overrides):
    dead = DeadLetters()
    projector = Projector(_settings(**overrides), store, producer_factory=lambda _s: dead)
    projector._stop = asyncio.Event()
    return projector, dead


def _answered(turn_id, conv="c1", start_seq=0, *, outcome="answered", data=None, **started):
    return [_event(turn_id, start_seq, "turn_started", conv=conv, data=started, body={"message": "补件到哪了"}),
            _event(turn_id, start_seq + 1, "turn_completed", conv=conv,
                   data={"outcome": outcome, "answer_confidence": 0.9, **(data or {})})]


# ── 投影 ──────────────────────────────────────────────────────────────────────

async def test_the_projector_writes_a_batch_then_commits_after_it():
    store = MemoryEventStore("staff")
    projector, dead = _projector(store)
    consumer = Commits()
    events = _answered("t1") + _answered("t2", conv="c2")
    await projector.handle(consumer, TP, _messages(events))
    assert [e.type for e in await store.events_of("t1")] == ["turn_started", "turn_completed"]
    assert consumer.offsets == [{TP: 4}] and dead.sent == []

    # 同一批再来一次（提交之前进程挂了）：主键去重，一条不多
    await projector.handle(consumer, TP, _messages(events))
    assert len(await store.events_of("t1")) == 2


async def test_bad_messages_go_to_the_dead_letter_topic_and_the_partition_moves_on():
    store = MemoryEventStore("staff")
    projector, dead = _projector(store)
    consumer = Commits()
    events = [*_answered("t1"), _event("t9", 0, "turn_started", instance="customer"), *_answered("t2")]
    await projector.handle(consumer, TP, _messages(events, start=10, raw={11: b"{not json"}))
    assert [(topic, headers["source"]) for topic, _, headers in dead.sent] == [
        ("ai-helpdesk.turn-events.staff.dlq", f"{TP.topic}:0:11"), ("ai-helpdesk.turn-events.staff.dlq", f"{TP.topic}:0:12")]
    assert "实例" in dead.sent[1][2]["error"]
    assert consumer.offsets == [{TP: 15}]
    assert [e.seq for e in await store.events_of("t1")] == [0]          # t1 的终结那条正是坏消息
    assert len(await store.events_of("t2")) == 2 and await store.events_of("t9") == []


async def test_a_replay_does_not_bring_back_a_forgotten_conversation():
    store = MemoryEventStore("staff")
    projector, _ = _projector(store)
    events = _answered("t1", conv="c-gone") + _answered("t2", conv="c-kept")
    await projector.handle(Commits(), TP, _messages(events))
    assert await store.delete_conversation("c-gone") == 2

    await projector.handle(Commits(), TP, _messages(events))           # 重置 offset，从头回放
    assert await store.events_of("t1") == [] and len(await store.events_of("t2")) == 2


async def test_text_past_its_retention_is_not_written_back():
    store = MemoryEventStore("staff")
    projector, _ = _projector(store, body_days=14, row_days=180)
    day = 86400
    old_body = _event("t1", 0, "turn_started", body={"message": "原话"}, ts=time.time() - 20 * day)
    too_old = _event("t2", 0, "turn_started", ts=time.time() - 200 * day)
    await projector.handle(Commits(), TP, _messages([old_body, too_old]))
    [written] = await store.events_of("t1")
    assert written.body is None and json.loads(written.data) == {}
    assert await store.events_of("t2") == []


class FlakyStore(MemoryEventStore):
    def __init__(self, failures=0, bad_turn=""):
        super().__init__("staff")
        self.failures, self.bad_turn, self.attempts = failures, bad_turn, 0

    async def insert_unless_forgotten(self, events):
        self.attempts += 1
        if self.failures:
            self.failures -= 1
            raise ConnectionError("库暂时连不上")
        if any(e.turn_id == self.bad_turn for e in events):
            raise type("DataError", (Exception,), {"sqlstate": "22021"})("invalid byte sequence")
        await super().insert_unless_forgotten(events)


async def test_an_unreachable_database_is_retried_and_nothing_is_skipped(monkeypatch):
    monkeypatch.setattr("app.events.consumers.asyncio.wait_for", _instant_wait)
    store = FlakyStore(failures=3)
    projector, _ = _projector(store)
    consumer = Commits()
    await projector.handle(consumer, TP, _messages(_answered("t1")))
    assert store.attempts == 4 and len(await store.events_of("t1")) == 2 and consumer.offsets == [{TP: 2}]


async def test_stopping_while_the_database_is_down_leaves_the_batch_uncommitted(monkeypatch):
    monkeypatch.setattr("app.events.consumers.asyncio.wait_for", _instant_wait)
    store = FlakyStore(failures=100)
    projector, _ = _projector(store)
    projector._stop.set()
    consumer = Commits()
    with pytest.raises(Stopping):
        await projector.handle(consumer, TP, _messages(_answered("t1")))
    assert consumer.offsets == []


async def test_a_row_the_database_rejects_goes_alone_to_the_dead_letter_topic():
    store = FlakyStore(bad_turn="t-bad")
    projector, dead = _projector(store)
    events = [*_answered("t1"), _event("t-bad", 0, "turn_started"), *_answered("t2")]
    await projector.handle(Commits(), TP, _messages(events))
    assert len(dead.sent) == 1 and "22021" in dead.sent[0][2]["error"]
    assert len(await store.events_of("t1")) == 2 and len(await store.events_of("t2")) == 2


async def _instant_wait(awaitable, timeout=None):
    if asyncio.iscoroutine(awaitable):
        awaitable.close()
    raise TimeoutError


# ── 采样的规则 ────────────────────────────────────────────────────────────────

NO_RANDOM = SamplingRules(random_rate=0.0)


def test_the_interesting_turns_are_picked_and_an_ordinary_one_is_not():
    assert reasons_for(_answered("t1"), NO_RANDOM) == []
    assert reasons_for(_answered("t1", outcome="handoff"), NO_RANDOM) == ["handoff"]
    assert reasons_for(_answered("t1", data={"answer_confidence": 0.3}), NO_RANDOM) == ["low_confidence"]
    assert reasons_for(_answered("t1", data={"violations": 2}), NO_RANDOM) == ["policy_violation"]
    taken_over = _answered("t1", run={"run_id": "r", "attempt": 2}, degrade={"level": 2})
    assert reasons_for(taken_over, NO_RANDOM) == ["taken_over", "degraded"]
    abandoned = [_event("t0", 0, "turn_started", data={"run": {"run_id": "r", "attempt": 1}}),
                 _event("t0", ABANDONED_SEQ, "turn_abandoned", data={"reason": "superseded", "by_attempt": 2})]
    assert reasons_for(abandoned, NO_RANDOM) == ["abandoned"]
    failed = [_event("t3", 0, "turn_started"), _event("t3", 1, "guard_replaced"), _event("t3", 2, "turn_failed")]
    assert reasons_for(failed, NO_RANDOM) == ["failed", "guard_replaced"]


def test_random_sampling_is_the_same_on_every_replay():
    turns = [f"turn-{i}" for i in range(4000)]
    picked = [t for t in turns if "random" in reasons_for(_answered(t), SamplingRules(random_rate=0.05))]
    assert 140 < len(picked) < 260                                    # 约 5%
    assert picked == [t for t in turns if _stable_fraction(t) < 0.05]


def test_rules_carry_their_parameters_in_the_label_and_the_consumer_group():
    assert SamplingRules().label == "v1"
    rules = SamplingRules.from_env({"AI_HELPDESK_SAMPLER_RANDOM_RATE": "0.2", "AI_HELPDESK_SAMPLER_MIN_CONFIDENCE": "x"})
    assert rules.label == "v1;random=0.2"
    sampler = Sampler(_settings(), db=None, rules=rules)
    assert sampler.group == "ai-helpdesk.turn-events.sampler.staff.v1_random_0_2"
    assert group_id(KAFKA, "projector", "staff") == "ai-helpdesk.turn-events.projector.staff"


# ── 采样的状态和提交点 ────────────────────────────────────────────────────────

class Candidates:
    """假的库：按主键 (instance, rules, turn_id) 只留第一份，和 ON CONFLICT DO NOTHING 一样。"""

    def __init__(self):
        self.rows: dict[tuple, tuple] = {}

    async def execute(self, sql, *args):
        self.rows.setdefault((args[0], args[1], args[2]), args)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _sampler(db, clock=None, **kwargs):
    sampler = Sampler(_settings(), db, NO_RANDOM, commit_every_s=0.0, monotonic=clock or Clock(), **kwargs)
    sampler._stop = asyncio.Event()
    return sampler


async def test_an_open_turn_holds_the_commit_point_until_it_ends():
    db, consumer = Candidates(), Commits()
    sampler = _sampler(db)
    handoff = _answered("t-h", conv="c1", outcome="handoff")
    ordinary = _answered("t-o", conv="c2")
    # offset 0..3：两轮都结束了；offset 4：第三轮刚开始
    await sampler.handle(consumer, TP, _messages([*handoff, *ordinary, _event("t-open", 0, "turn_started", conv="c3")]))
    await sampler.tick(consumer)
    assert consumer.offsets[-1] == {TP: 4}                            # 停在没结束那一轮的第一条
    assert [key[2] for key in db.rows] == ["t-h"]
    row = db.rows[("staff", NO_RANDOM.label, "t-h")]
    assert json.loads(row[6]) == ["handoff"] and json.loads(row[7])["outcome"] == "handoff"

    await sampler.handle(consumer, TP, _messages([_event("t-open", 1, "turn_completed", conv="c3",
                                                         data={"outcome": "handoff"})], start=5))
    await sampler.tick(consumer)
    assert consumer.offsets[-1] == {TP: 6} and ("staff", NO_RANDOM.label, "t-open") in db.rows


async def test_a_restart_reads_the_open_turn_again_and_samples_nothing_twice():
    db = Candidates()
    events = [*_answered("t-h", outcome="handoff"), _event("t-open", 0, "turn_started", conv="c3")]
    first, consumer = _sampler(db), Commits()
    await first.handle(consumer, TP, _messages(events))
    await first.on_stop(consumer)
    resume_at = consumer.offsets[-1][TP]
    assert resume_at == 2

    # 新进程从提交点读起：t-open 的头一条又来一次，加上之后的终结事件
    second, consumer = _sampler(db), Commits()
    tail = [_event("t-open", 0, "turn_started", conv="c3"),
            _event("t-open", 1, "turn_completed", conv="c3", data={"outcome": "unavailable"})]
    await second.handle(consumer, TP, _messages(tail, start=resume_at))
    assert sorted(key[2] for key in db.rows) == ["t-h", "t-open"]
    assert json.loads(db.rows[("staff", NO_RANDOM.label, "t-open")][6]) == ["unavailable"]


async def test_events_after_a_turn_ended_are_ignored_and_a_turn_that_never_ends_is_let_go():
    db, consumer, clock = Candidates(), Commits(), Clock()
    sampler = _sampler(db, clock, max_open_s=60)
    events = [*_answered("t1", outcome="handoff"), _event("t1", 2, "model_call"),      # 轮次之后的后台任务
              _event("t-stuck", 0, "turn_started", conv="c9")]
    await sampler.handle(consumer, TP, _messages(events))
    assert len(db.rows) == 1 and sampler.open_turns == 1
    await sampler.tick(consumer)
    assert consumer.offsets[-1] == {TP: 3}

    clock.now += 61
    await sampler.tick(consumer)
    assert sampler.open_turns == 0 and consumer.offsets[-1] == {TP: 4}


async def test_partitions_handed_to_another_consumer_are_committed_and_forgotten():
    db, consumer = Candidates(), Commits()
    sampler = _sampler(db)
    sampler._consumer = consumer
    other = TopicPartition(TP.topic, 1)
    await sampler.handle(consumer, TP, _messages([_event("t-open", 0, "turn_started")], start=7))
    await sampler.handle(consumer, other, _messages(_answered("t2"), start=3))
    await sampler.on_partitions_revoked([TP])
    assert consumer.offsets[-1] == {TP: 7} and set(sampler._parts) == {other}


# ── 真的 broker 和 Postgres ───────────────────────────────────────────────────

async def _until(check, timeout_s=20.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await check():
            return True
        await asyncio.sleep(0.2)
    return False


@pytest.fixture
async def stack():
    from app.core.embedder import Embedder
    from app.events.store import PgEventStore
    from app.storage.pg import Database

    kafka = KafkaSettings(bootstrap=TEST_KAFKA_BOOTSTRAP, topic_prefix=f"test-{uuid.uuid4().hex[:8]}.turn-events")
    await ensure_topics(kafka, ["staff"], partitions=3, retention_days=1)
    db = Database(TEST_PG_DSN)
    await db.connect(embedding_dim=Embedder().dim)
    log = await EventLog.open(EventLogConfig(mode="full", instance="staff", body_days=0, row_days=0,
                                             sink="kafka", kafka=kafka), db=db)
    yield SimpleNamespace(kafka=kafka, db=db, log=log, store=PgEventStore(db, "staff"))
    await log.close()
    from aiokafka.admin import AIOKafkaAdminClient

    admin = AIOKafkaAdminClient(bootstrap_servers=kafka.bootstrap)
    await admin.start()
    try:
        await admin.delete_topics([kafka.topic("staff"), kafka.dead_letter_topic("staff")])
    finally:
        await admin.close()
    await db.close()


def _turn(log, conv, outcome="answered"):
    turn = log.open_turn(conv_id=conv, request_id="r1")
    turn.emit("turn_started", {"transport": "stream"}, {"user_id": "u1", "message": f"{conv} 的问题"})
    turn.emit("turn_completed", {"outcome": outcome, "answer_confidence": 0.9})
    turn.close()
    return turn


async def _run(consumer_loop, until, timeout_s=30.0):
    stop = asyncio.Event()
    task = asyncio.create_task(consumer_loop.run(stop))
    try:
        assert await _until(until, timeout_s), "消费者没在时限内处理完"
    finally:
        stop.set()
        await asyncio.wait_for(task, 15)


@needs_both
async def test_the_projector_fills_turn_events_from_kafka_and_a_replay_rebuilds_it(stack):
    conv = f"conv-{uuid.uuid4().hex[:8]}"
    turns = [_turn(stack.log, conv), _turn(stack.log, f"{conv}-b", outcome="handoff")]
    await stack.log.close()                                            # 发完、确认完
    settings = ConsumerSettings(instance="staff", kafka=stack.kafka, body_days=0, row_days=0)

    async def projected():
        return all([len(await stack.store.events_of(t.turn_id)) == 2 for t in turns])

    await _run(Projector(settings, stack.store), projected)
    assert [e.type for e in await stack.store.events_of(turns[0].turn_id)] == ["turn_started", "turn_completed"]

    # 回放：库里删掉（第二个会话按删除请求删），换一个消费组从头读——只有没被删除请求删过的回来
    await stack.db.execute("DELETE FROM turn_events WHERE turn_id = $1", turns[0].turn_id)
    assert await stack.store.delete_conversation(f"{conv}-b") == 2
    replay = Projector(settings, stack.store)
    replay.group += ".replay"

    async def rebuilt():
        return len(await stack.store.events_of(turns[0].turn_id)) == 2

    await _run(replay, rebuilt)
    assert await stack.store.events_of(turns[1].turn_id) == []


@needs_both
async def test_the_sampler_resumes_an_open_turn_after_a_restart(stack):
    conv = f"conv-{uuid.uuid4().hex[:8]}"
    rules = SamplingRules(name=f"t{uuid.uuid4().hex[:6]}", random_rate=0.0)
    settings = ConsumerSettings(instance="staff", kafka=stack.kafka)
    _turn(stack.log, conv, outcome="handoff")
    _turn(stack.log, f"{conv}-ok")
    pending = stack.log.open_turn(conv_id=f"{conv}-late", request_id="r1")
    pending.emit("turn_started", {"transport": "stream"})
    await asyncio.sleep(0.5)

    async def sampled(n):
        rows = await stack.db.fetch("SELECT turn_id FROM eval_candidates WHERE rules = $1", rules.label)
        return len(rows) == n

    first = Sampler(settings, stack.db, rules)
    await _run(first, lambda: sampled(1))
    await asyncio.sleep(0.1)

    pending.emit("turn_completed", {"outcome": "unavailable"})          # 采样器停着的时候这一轮结束了
    pending.close()
    await stack.log.close()
    second = Sampler(settings, stack.db, rules)
    await _run(second, lambda: sampled(2))
    rows = await stack.db.fetch("SELECT turn_id, reasons FROM eval_candidates WHERE rules = $1 ORDER BY turn_ts",
                                rules.label)
    assert [json.loads(r["reasons"]) for r in rows] == [["handoff"], ["unavailable"]]
    assert rows[1]["turn_id"] == pending.turn_id
