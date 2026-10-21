"""
排队模式的真后端：Redis Streams 的派发和帧流、Redis 里的令牌桶和工具记录、Postgres 的 run 存储，
以及按 run 去重的审计和工单。

设置了 TEST_REDIS_URL / TEST_POSTGRES_DSN 才跑（CI 里各起一个容器），本地没配就跳过——和
test_vector_storage.py 一样，进程内那一份（test_runs.py）跑过的语义，这里在真后端上再跑一遍：
"接口一致"不是一句注释。每个用例用自己的键前缀和实例名，互不干扰。
"""
import asyncio
import json
import os
import time
import uuid

import pytest

from app.runs.records import QUEUED, RUNNING, SUCCEEDED, RunRecord, new_run_id
from app.runs.store import IdempotencyConflict, MemoryRunStore, lost_attempts

TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", "").strip()
TEST_PG_DSN = os.getenv("TEST_POSTGRES_DSN", "").strip()

needs_redis = pytest.mark.skipif(not TEST_REDIS_URL, reason="未设置 TEST_REDIS_URL")
needs_pg = pytest.mark.skipif(not TEST_PG_DSN, reason="未设置 TEST_POSTGRES_DSN")


@pytest.fixture
async def redis():
    import redis.asyncio as aioredis

    client = aioredis.from_url(TEST_REDIS_URL, decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def prefix():
    return f"test-{uuid.uuid4().hex[:10]}"


# ── 派发队列 ──────────────────────────────────────────────────────────────────

@needs_redis
async def test_a_group_created_after_dispatch_still_sees_the_message(redis, prefix):
    """API 先起来先派发、worker 后建组：组从 0 建，之前进来的消息照样能读到。"""
    from app.runs.broker import RedisBroker

    broker = RedisBroker(redis, "staff", prefix=prefix)
    await redis.xadd(broker.stream, {"run": "early"})
    await broker.ensure_group()
    await broker.ensure_group()                                      # 重复建组不报错
    [delivery] = await broker.receive("w1", 10, 50)
    assert delivery.run_id == "early"
    assert await broker.receive("w1", 10, 20) == []                  # 一条消息只投给一个消费者


@needs_redis
async def test_pending_messages_are_reclaimed_after_they_sit_idle(redis, prefix):
    from app.runs.broker import RedisBroker

    broker = RedisBroker(redis, "staff", prefix=prefix)
    await broker.ensure_group()
    await broker.dispatch("r1")
    [delivery] = await broker.receive("dead-worker", 1, 50)
    assert await broker.backlog() == 1                               # 投递了没确认：还算积压

    assert await broker.reclaim("survivor", min_idle_ms=60_000, count=10) == []
    await asyncio.sleep(0.15)
    await broker.touch("dead-worker", delivery.message_id)           # 续租时摸一下：闲置清零
    assert await broker.reclaim("survivor", min_idle_ms=100, count=10) == []
    await asyncio.sleep(0.15)
    [taken] = await broker.reclaim("survivor", min_idle_ms=100, count=10)
    assert taken == delivery

    await broker.ack(taken.message_id)
    assert await broker.backlog() == 0


@needs_redis
async def test_backlog_counts_undelivered_and_unacknowledged(redis, prefix):
    from app.runs.broker import RedisBroker

    broker = RedisBroker(redis, "staff", prefix=prefix)
    assert await broker.backlog() == 0                               # 流还不存在
    await broker.ensure_group()
    for i in range(3):
        await broker.dispatch(f"r{i}")
    assert await broker.backlog() == 3
    await broker.receive("w1", 1, 50)
    assert await broker.backlog() == 3                               # 2 条没投递 + 1 条没确认


@needs_redis
async def test_the_group_is_rebuilt_when_redis_lost_the_stream(redis, prefix):
    from app.runs.broker import RedisBroker

    broker = RedisBroker(redis, "staff", prefix=prefix)
    await broker.ensure_group()
    await redis.delete(broker.stream)                                # Redis 重启没持久化
    assert await broker.receive("w1", 1, 20) == []                   # NOGROUP：重建，不抛
    await broker.dispatch("after-restart")
    [delivery] = await broker.receive("w1", 1, 50)
    assert delivery.run_id == "after-restart"


@needs_redis
async def test_the_sweep_lock_and_the_dead_letter_stream(redis, prefix):
    from app.runs.broker import RedisBroker

    broker = RedisBroker(redis, "staff", prefix=prefix)
    assert await broker.try_lock("sweep", 0.2) and not await broker.try_lock("sweep", 0.2)
    await asyncio.sleep(0.25)
    assert await broker.try_lock("sweep", 0.2)
    await broker.dead_letter("r9", "失联 3 次")
    [(_, fields)] = await redis.xrange(broker.dead_stream)
    assert fields["run"] == "r9" and fields["reason"] == "失联 3 次"


# ── 帧流和工具记录 ────────────────────────────────────────────────────────────

@needs_redis
async def test_frames_are_read_after_a_cursor_and_a_read_blocks_until_one_arrives(redis, prefix):
    from app.runs.channel import RedisChannel

    channel = RedisChannel(redis, prefix=prefix)
    first = await channel.publish("r1", "meta", {"intent": "补件"}, 1)
    await channel.publish("r1", "delta", {"text": "你好。"}, 1)
    frames = await channel.read("r1", "0-0", block_ms=10)
    assert [(f.event, f.data, f.attempt) for f in frames] == [
        ("meta", {"intent": "补件"}, 1), ("delta", {"text": "你好。"}, 1)]
    assert [f.event for f in await channel.read("r1", first, block_ms=10)] == ["delta"]

    async def later():
        await asyncio.sleep(0.05)
        await channel.publish("r1", "result", {"response": "你好。"}, 1)

    task = asyncio.create_task(later())
    started = time.monotonic()
    [result] = await channel.read("r1", frames[-1].id, block_ms=2000)
    assert result.event == "result" and time.monotonic() - started < 1.5
    await task
    assert 0 < await redis.ttl(f"{prefix}:run:r1:frames") <= 24 * 3600


@needs_redis
async def test_tool_results_are_remembered_per_run(redis, prefix):
    from app.runs.memo import RedisMemo, RunScope, memo_key

    memo = RedisMemo(redis, prefix=prefix)
    key = memo_key("call", "order_data_query", {"query": "SO-1"})
    first = RunScope("r1", 1, memo=memo)
    retry = RunScope("r1", 2, memo=memo)
    await first.remember(key, {"data": {"answer": "已出库"}})
    assert await first.recall(key) is None                           # 第一次执行不读
    assert await retry.recall(key) == {"data": {"answer": "已出库"}}
    assert await RunScope("r2", 2, memo=memo).recall(key) is None    # 别的 run 读不到


# ── 令牌桶 ────────────────────────────────────────────────────────────────────

@needs_redis
async def test_the_shared_bucket_admits_up_to_capacity_then_makes_callers_wait(redis, prefix):
    from app.core.admission import Admission, Limits
    from app.core.llm_guard import LLMUnavailableError

    a = Admission(Limits(rpm=6, max_wait_s=0.0, burst_s=60), redis, prefix=prefix)
    b = Admission(Limits(rpm=6, max_wait_s=0.0, burst_s=60), redis, prefix=prefix)   # 另一个副本，同一只桶
    for admission in (a, b, a, b, a, b):
        assert await admission.acquire("openai@relay", 10) == 0.0
    with pytest.raises(LLMUnavailableError) as rejected:
        await a.acquire("openai@relay", 10)
    assert rejected.value.reason == "overloaded" and rejected.value.sent is False

    # 等得起的那一种：桶空了就睡到补上为止（每 100ms 补一个），不判过载
    patient = Admission(Limits(rpm=600, max_wait_s=2.0), redis, prefix=f"{prefix}-fast")   # 容量 100
    waits = [await patient.acquire("openai@relay", 1) for _ in range(130)]
    assert waits[0] == 0.0 and any(w > 0 for w in waits) and max(waits) < 1.0


@needs_redis
async def test_two_buckets_are_charged_together_or_not_at_all(redis, prefix):
    from app.core.admission import Admission, Limits
    from app.core.llm_guard import LLMUnavailableError

    admission = Admission(Limits(rpm=100, tpm=1000, max_wait_s=0.0, burst_s=60), redis, prefix=prefix)
    await admission.acquire("p", 900)
    with pytest.raises(LLMUnavailableError):
        await admission.acquire("p", 500)                            # TPM 不够：RPM 也不扣
    rpm = float(await redis.hget(f"{prefix}:p:rpm", "tokens"))
    assert 98.5 < rpm < 99.5
    await admission.settle("p", estimated=900, actual=300)           # 估多了，退回 600
    await admission.acquire("p", 500)


# ── run 存储（进程内和 Postgres 跑同一组断言）─────────────────────────────────

@pytest.fixture(params=["memory", pytest.param("postgres", marks=needs_pg)])
async def run_store(request):
    instance = f"staff-{uuid.uuid4().hex[:6]}"
    if request.param == "memory":
        yield MemoryRunStore(instance)
        return
    from app.core.embedder import Embedder
    from app.runs.store import PgRunStore
    from app.storage.pg import Database

    db = Database(TEST_PG_DSN)
    await db.connect(embedding_dim=Embedder().dim)
    yield PgRunStore(db, instance)
    await db.close()


async def test_store_contract(run_store):
    run, created = await run_store.create(RunRecord(new_run_id(), "x", "c1", "u1", "补件到哪了", idempotency_key="k1"))
    assert created and run.status == QUEUED and run.instance == run_store.instance
    again, created = await run_store.create(RunRecord(new_run_id(), "x", "c1", "u1", "补件到哪了", idempotency_key="k1"))
    assert not created and again.run_id == run.run_id
    assert (await run_store.get_by_key("k1")).run_id == run.run_id
    with pytest.raises(IdempotencyConflict):
        await run_store.create(RunRecord(new_run_id(), "x", "c1", "u1", "别的话", idempotency_key="k1"))

    claimed = await run_store.claim(run.run_id, "w1", 0.2)
    assert (claimed.status, claimed.attempt, claimed.lease_owner) == (RUNNING, 1, "w1")
    assert await run_store.claim(run.run_id, "w2", 0.2) is None
    assert await run_store.set_turn(run.run_id, "w1", 1, "turn-1", 2)
    assert await run_store.checkpoint(run.run_id, "w1", 1, {"turn_id": "turn-1", "shown": "第一遍"})
    assert await run_store.heartbeat(run.run_id, "w1", 1, 0.2)

    await asyncio.sleep(0.3)                                         # w1 失联
    taken = await run_store.claim(run.run_id, "w2", 30)
    assert taken.attempt == 2 and taken.checkpoint == {"turn_id": "turn-1", "shown": "第一遍"}
    assert taken.history[0]["turn_id"] == "turn-1" and taken.history[0].get("committed") is True
    assert taken.history[1]["reclaimed"] is True and lost_attempts(taken) == 1
    assert not await run_store.heartbeat(run.run_id, "w1", 1, 30)
    assert await run_store.finish(run.run_id, "w1", 1, SUCCEEDED, result={"response": "旧"}) is None

    done = await run_store.finish(run.run_id, "w2", 2, SUCCEEDED, result={"response": "第一遍", "n": 1})
    assert done.status == SUCCEEDED and done.result == {"response": "第一遍", "n": 1}
    assert done.finished_at >= done.created_at and done.degrade_level == 2 and done.turn_id == "turn-1"
    assert (await run_store.counts()).get(SUCCEEDED) == 1
    assert await run_store.purge(older_than_s=3600) == 0
    assert await run_store.purge(older_than_s=0) == 1
    assert await run_store.get(run.run_id) is None


async def test_store_release_and_sweep(run_store):
    run, _ = await run_store.create(RunRecord(new_run_id(), "x", "c1", "u1", "订单"))
    claimed = await run_store.claim(run.run_id, "w1", 30)
    assert await run_store.release(run.run_id, "w1", claimed.attempt)
    released = await run_store.get(run.run_id)
    assert released.status == QUEUED and released.history[-1]["outcome"] == "released"
    assert await run_store.due_for_dispatch(idle_s=3600, lease_grace_s=0) == []
    await asyncio.sleep(0.05)
    assert await run_store.due_for_dispatch(idle_s=0.01, lease_grace_s=0) == [run.run_id]
    await run_store.mark_dispatched([run.run_id])
    assert await run_store.due_for_dispatch(idle_s=1, lease_grace_s=0) == []


@needs_pg
async def test_another_instance_cannot_read_or_claim_a_run():
    from app.core.embedder import Embedder
    from app.runs.store import PgRunStore
    from app.storage.pg import Database

    db = Database(TEST_PG_DSN)
    await db.connect(embedding_dim=Embedder().dim)
    try:
        suffix = uuid.uuid4().hex[:6]
        customer, staff = PgRunStore(db, f"customer-{suffix}"), PgRunStore(db, f"staff-{suffix}")
        run, _ = await customer.create(RunRecord(new_run_id(), "", "c1", "x", "客户的问题"))
        assert await staff.get(run.run_id) is None
        assert await staff.claim(run.run_id, "staff-worker", 30) is None
        assert (await customer.get(run.run_id)).status == QUEUED
    finally:
        await db.close()


# ── 按 run 去重的副作用 ───────────────────────────────────────────────────────

@needs_pg
async def test_postgres_audit_and_tickets_are_written_once_per_run():
    from app.core.embedder import Embedder
    from app.storage.conversation_log import PgConversationLog
    from app.storage.pg import Database
    from app.tickets.ticket_store import PgTicketStore

    db = Database(TEST_PG_DSN)
    await db.connect(embedding_dim=Embedder().dim)
    try:
        log = PgConversationLog(db)
        conv = f"conv-{uuid.uuid4().hex[:8]}"
        key = f"run:staff:{uuid.uuid4().hex}:audit"
        await asyncio.gather(*(log.append(conv, "u", "user", "原话", turn_id="t", instance="staff",
                                          dedup_key=f"{key}:user") for _ in range(3)))
        await log.append(conv, "u", "assistant", "回答", dedup_key=f"{key}:assistant")
        await log.append(conv, "u", "assistant", "第二遍的回答", dedup_key=f"{key}:assistant")
        assert [(e.role, e.content) for e in await log.get_conversation(conv)] == [
            ("user", "原话"), ("assistant", "回答")]

        tickets = PgTicketStore(db)
        ticket_id = f"tk_{uuid.uuid4().hex[:12]}"
        first = await tickets.create(conv_id=conv, user_id="u", reason="要人工", turn_id="t1", ticket_id=ticket_id)
        again = await tickets.create(conv_id=conv, user_id="u", reason="第二遍", turn_id="t2", ticket_id=ticket_id)
        assert again.id == first.id and again.reason == "要人工" and again.turn_id == "t1"
    finally:
        await db.close()


async def test_sqlite_audit_and_tickets_are_written_once_per_run(tmp_path):
    from app.storage.conversation_log import ConversationLog
    from app.tickets.ticket_store import TicketStore

    log = ConversationLog(db_path=str(tmp_path / "c.db"))
    for text in ("原话", "原话（重跑）"):
        await log.append("c1", "u", "user", text, dedup_key="run:staff:r1:audit:user")
    assert [e.content for e in await log.get_conversation("c1")] == ["原话"]
    await log.close()

    tickets = TicketStore(db_path=str(tmp_path / "t.db"))
    first = await tickets.create(conv_id="c1", user_id="u", reason="要人工", ticket_id="tk_fixed")
    again = await tickets.create(conv_id="c1", user_id="u", reason="重跑", ticket_id="tk_fixed")
    assert again.id == first.id == "tk_fixed" and again.reason == "要人工"
    assert (await tickets.stats())["total"] == 1
    await tickets.close()


@needs_redis
async def test_working_memory_gets_a_turn_once(redis, prefix):
    from app.memory.conversation_memory import MemoryManager

    memory = MemoryManager.__new__(MemoryManager)
    memory._redis = redis
    user, conv = f"{prefix}-u", f"{prefix}-c"
    assert await memory.add_turn(user, conv, "补件到哪了", "已登记。", dedup_key="run:staff:r1:memory")
    assert not await memory.add_turn(user, conv, "补件到哪了", "重跑的回答。", dedup_key="run:staff:r1:memory")
    stored = await redis.lrange(MemoryManager._wm_key(user, conv), 0, -1)
    assert [json.loads(item)["content"] for item in stored] == ["已登记。", "补件到哪了"]   # 最新的在前
    await redis.delete(MemoryManager._wm_key(user, conv), f"{MemoryManager._wm_key(user, conv)}:once:run:staff:r1:memory")


@needs_pg
async def test_several_processes_can_create_the_schema_on_a_fresh_database_at_once():
    """k8s 一次起五六个 api 和 worker：在一个新库上同时建表，谁都不能因为撞了系统表起不来。"""
    import asyncpg

    from app.core.embedder import Embedder
    from app.storage.pg import Database

    name = f"race_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(TEST_PG_DSN)
    await admin.execute(f'CREATE DATABASE "{name}"')
    try:
        dsn = TEST_PG_DSN.rsplit("/", 1)[0] + f"/{name}"
        dbs = [Database(dsn, max_size=2) for _ in range(6)]
        await asyncio.gather(*(db.connect(embedding_dim=Embedder().dim) for db in dbs))
        for db in dbs:
            await db.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
