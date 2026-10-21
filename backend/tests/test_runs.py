"""
可恢复的运行（app/runs/）：存储、派发、worker、续流，全部用进程内的实现。

- 存储：同一个幂等键拿到同一个 run、换了内容报冲突；领取是条件 UPDATE，attempt 是栅栏；
  续租、写结局、交还都只有拿着租约的那一遍改得动；失联的次数不含主动交还的；
- worker：领走、跑完、确认；这一轮自己出错不重试；worker 失联之后别人接手（attempt + 1）；
  租约在跑的时候丢了就取消、结果作废；反复失联进死信、转人工；停机交还给别的 worker；
  派发丢了由清扫补；按积压降级；
- 续流：先发一帧 run，帧带 id，从 Last-Event-ID 接着读；被判失联的那一遍还在发的帧丢掉；
  帧流丢了从库里补终结帧；积压超限拒收（429）、同一个幂等键不算新负载。

Redis Streams 和 Postgres 那两份实现在 test_runs_backends.py 里跑同样的语义。
"""
import asyncio
import time

import pytest

from app.runs.broker import MemoryBroker
from app.runs.channel import MemoryChannel
from app.runs.config import RunConfig
from app.runs.degrade import DegradePolicy
from app.runs.memo import MemoryMemo
from app.runs.records import DEAD, FAILED, QUEUED, RUNNING, SUCCEEDED, RunRecord, new_run_id
from app.runs.service import Backpressure, RunService
from app.runs.store import IdempotencyConflict, MemoryRunStore, lost_attempts
from app.runs.worker import RunOutcome, RunWorker


def _config(**overrides) -> RunConfig:
    values = dict(mode="queued", role="all", concurrency=2, lease_s=0.3, max_attempts=3, queue_max=50,
                  redispatch_after_s=0.2, retention_days=0, wait_s=5.0)
    values.update(overrides)
    return RunConfig(**values)


class Desk:
    """一个实例的存储、派发队列、帧流和记录，外加提交的一侧。"""

    def __init__(self, **config):
        self.config = _config(**config)
        self.store = MemoryRunStore("staff")
        self.broker = MemoryBroker("staff")
        self.channel = MemoryChannel()
        self.memo = MemoryMemo()
        self.service = RunService(store=self.store, broker=self.broker, channel=self.channel,
                                  config=self.config, heartbeat_s=0.05)
        self.workers: list[RunWorker] = []

    def worker(self, execute, *, owner=None, policy=None, on_dead=None) -> RunWorker:
        worker = RunWorker(store=self.store, broker=self.broker, channel=self.channel, memo=self.memo,
                           execute=execute, config=self.config, policy=policy, on_dead=on_dead,
                           owner=owner, block_ms=20, backlog_ttl_s=0.0)
        self.workers.append(worker)
        return worker

    async def submit(self, message="补件到哪了", key="", conv_id="c1"):
        run, _ = await self.service.submit(conv_id=conv_id, user_id="u1", message=message,
                                           idempotency_key=key)
        return run

    async def until(self, run_id, *statuses, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = await self.store.get(run_id)
            if run is not None and run.status in statuses:
                return run
            await asyncio.sleep(0.01)
        raise AssertionError(f"run {run_id} 没有在 {timeout}s 内到达 {statuses}：{await self.store.get(run_id)}")

    async def stop(self):
        for worker in self.workers:
            if not worker.draining:
                await worker.drain(1.0)


@pytest.fixture
async def desk():
    d = Desk()
    yield d
    await d.stop()


def _answer(text="好的，补件已经登记。"):
    async def execute(ctx):
        await ctx.publish("delta", {"text": text})
        return RunOutcome(status=SUCCEEDED, terminal=("result", {"response": text}),
                          result={"response": text, "conv_id": ctx.run.conv_id})
    return execute


def _blocking(started: asyncio.Event | None = None, release: asyncio.Event | None = None, text="慢慢答。"):
    """跑到一半停住，等 release 才答完（不给 release 就一直停着，直到被取消）。"""
    async def execute(ctx):
        await ctx.publish("delta", {"text": "第一句。"})
        if started is not None:
            started.set()
        await (release.wait() if release is not None else asyncio.Event().wait())
        return RunOutcome(status=SUCCEEDED, terminal=("result", {"response": text}), result={"response": text})
    return execute


# ── 存储 ──────────────────────────────────────────────────────────────────────

async def test_the_same_idempotency_key_returns_the_same_run_and_a_different_message_conflicts():
    store = MemoryRunStore("staff")
    first, created = await store.create(RunRecord(new_run_id(), "staff", "c1", "u1", "订单到哪了", idempotency_key="k1"))
    again, created_again = await store.create(RunRecord(new_run_id(), "staff", "c1", "u1", "订单到哪了", idempotency_key="k1"))
    assert created and not created_again and again.run_id == first.run_id
    with pytest.raises(IdempotencyConflict) as conflict:
        await store.create(RunRecord(new_run_id(), "staff", "c1", "u1", "换了一句话", idempotency_key="k1"))
    assert conflict.value.existing.run_id == first.run_id
    # 不给幂等键时 run_id 自己就是键：每次都是新的
    a, _ = await store.create(RunRecord(new_run_id(), "staff", "c1", "u1", "同一句"))
    b, _ = await store.create(RunRecord(new_run_id(), "staff", "c1", "u1", "同一句"))
    assert a.run_id != b.run_id


async def test_attempt_is_a_fence_only_the_lease_holder_can_heartbeat_or_finish():
    now = [1000.0]
    store = MemoryRunStore("staff", clock=lambda: now[0])
    run, _ = await store.create(RunRecord(new_run_id(), "staff", "c1", "u1", "订单到哪了"))
    first = await store.claim(run.run_id, "w1", lease_s=30)
    assert first.attempt == 1 and first.status == RUNNING
    assert await store.claim(run.run_id, "w2", lease_s=30) is None          # 租约有效：领不到

    now[0] += 31                                                              # w1 失联，租约过期
    second = await store.claim(run.run_id, "w2", lease_s=30)
    assert second.attempt == 2 and second.history[-1]["reclaimed"] is True
    # w1 回来了：续租、写提交点、写结局、交还，一样都改不动
    assert not await store.heartbeat(run.run_id, "w1", 1, 30)
    assert not await store.checkpoint(run.run_id, "w1", 1, {"turn_id": "t1"})
    assert await store.finish(run.run_id, "w1", 1, SUCCEEDED, result={"response": "旧的"}) is None
    assert not await store.release(run.run_id, "w1", 1)

    assert await store.heartbeat(run.run_id, "w2", 2, 30)
    done = await store.finish(run.run_id, "w2", 2, SUCCEEDED, result={"response": "新的"})
    assert done.status == SUCCEEDED and done.result == {"response": "新的"} and done.lease_owner == ""
    assert lost_attempts(done) == 1                       # 第一次没有收场
    assert [h.get("outcome") for h in done.history] == [None, SUCCEEDED]


async def test_a_released_attempt_does_not_count_as_lost():
    now = [0.0]
    store = MemoryRunStore("staff", clock=lambda: now[0])
    run, _ = await store.create(RunRecord(new_run_id(), "staff", "c1", "u1", "订单到哪了"))
    claimed = await store.claim(run.run_id, "w1", 30)
    assert await store.release(run.run_id, "w1", claimed.attempt)
    again = await store.claim(run.run_id, "w2", 30)
    assert again.attempt == 2 and lost_attempts(again) == 0
    assert again.history[0]["outcome"] == "released"


async def test_the_sweep_finds_runs_nobody_dispatched_and_leases_long_expired():
    now = [0.0]
    store = MemoryRunStore("staff", clock=lambda: now[0])
    queued, _ = await store.create(RunRecord(new_run_id(), "staff", "c1", "u1", "a"))
    running, _ = await store.create(RunRecord(new_run_id(), "staff", "c1", "u1", "b"))
    await store.claim(running.run_id, "w1", 10)
    now[0] = 20
    assert await store.due_for_dispatch(idle_s=5, lease_grace_s=20) == [queued.run_id]
    now[0] = 40                                                   # 租约过期超过宽限
    assert await store.due_for_dispatch(idle_s=5, lease_grace_s=20) == [queued.run_id, running.run_id]
    await store.mark_dispatched([queued.run_id, running.run_id])
    assert await store.due_for_dispatch(idle_s=5, lease_grace_s=20) == []


async def test_each_store_only_sees_its_own_instance():
    """两个实例共用一张表：员工那边的 worker 连客户的 run 都读不到。"""
    staff = MemoryRunStore("staff")
    run, _ = await staff.create(RunRecord(new_run_id(), "customer", "c1", "u1", "订单"))
    assert run.instance == "staff"                    # 写进哪个存储就是哪个实例的，不信记录上填的


# ── worker ────────────────────────────────────────────────────────────────────

async def test_a_run_is_claimed_executed_finished_and_acknowledged(desk):
    worker = desk.worker(_answer())
    await worker.start()
    run = await desk.submit()
    done = await desk.until(run.run_id, SUCCEEDED)

    assert done.attempt == 1 and done.result["response"] == "好的，补件已经登记。"
    events = [(f.event, f.attempt) for f in desk.channel.frames(run.run_id)]
    assert events == [("attempt", 1), ("delta", 1), ("result", 1)]
    await asyncio.sleep(0.05)
    assert desk.broker.acked and await desk.broker.backlog() == 0


async def test_an_error_in_the_turn_itself_is_reported_and_not_retried(desk):
    async def boom(ctx):
        raise RuntimeError("编排器炸了")

    worker = desk.worker(boom)
    await worker.start()
    run = await desk.submit()
    done = await desk.until(run.run_id, FAILED)
    assert done.attempt == 1 and "编排器炸了" in done.error
    assert desk.channel.frames(run.run_id)[-1].event == "error"


async def test_a_run_whose_worker_died_is_reclaimed_by_another_worker(desk):
    """一个 worker 读走了消息、领了租约，然后死掉（不续租、不确认）。别的 worker 接手，第 2 次执行。"""
    run = await desk.submit()
    [delivery] = await desk.broker.receive("dead-worker", 1, 10)
    assert (await desk.store.claim(run.run_id, "dead-worker", desk.config.lease_s)).attempt == 1

    worker = desk.worker(_answer("接手之后答完了。"), owner="survivor")
    await worker.start()
    done = await desk.until(run.run_id, SUCCEEDED, timeout=5)

    assert done.attempt == 2 and done.result["response"] == "接手之后答完了。"
    assert [h["owner"] for h in done.history] == ["dead-worker", "survivor"]
    assert lost_attempts(done) == 1
    attempts = [f for f in desk.channel.frames(run.run_id) if f.event == "attempt"]
    assert attempts[-1].attempt == 2 and attempts[-1].data["reclaimed"] is True
    await asyncio.sleep(0.05)
    assert delivery.message_id in desk.broker.acked


async def test_a_worker_that_loses_its_lease_mid_run_stops_and_its_result_is_discarded(desk):
    started = asyncio.Event()
    worker = desk.worker(_blocking(started), owner="slow")
    await worker.start()
    run = await desk.submit()
    await asyncio.wait_for(started.wait(), 5)

    # 另一个 worker 认定它失联了（比如它卡住了一个租约那么久），把 run 领走
    desk.store._runs[run.run_id].lease_until = 0
    thief = await desk.store.claim(run.run_id, "thief", 30)
    assert thief.attempt == 2

    await asyncio.sleep(desk.config.heartbeat_s * 3)
    assert worker.in_flight == 0                              # 续租失败，取消了那一遍
    current = await desk.store.get(run.run_id)
    assert (current.status, current.lease_owner, current.attempt) == (RUNNING, "thief", 2)
    assert not any(f.event == "result" for f in desk.channel.frames(run.run_id))


async def test_a_crash_looping_run_goes_to_the_dead_letter_and_is_handed_off():
    desk = Desk(max_attempts=2)
    run = await desk.submit()
    for owner in ("crash-1", "crash-2"):
        await desk.store.claim(run.run_id, owner, 0.01)
        await asyncio.sleep(0.02)                      # 每一次都在租约内死掉
    handed = []

    async def on_dead(dead, reason):
        handed.append((dead.run_id, reason))
        return {"response": "已转人工", "handoff": True, "ticket_id": "tk_x"}

    worker = desk.worker(_answer(), on_dead=on_dead)
    await worker.start()
    done = await desk.until(run.run_id, DEAD)
    await desk.stop()

    assert handed and handed[0][0] == run.run_id and "失联或重试了 2 次" in handed[0][1]
    assert done.result["handoff"] is True and "失联" in done.error
    assert desk.broker.dead == [(run.run_id, done.error)]
    assert desk.channel.frames(run.run_id)[-1].event == "result"


async def test_draining_hands_back_a_run_that_does_not_finish_in_time(desk):
    started = asyncio.Event()
    slow = desk.worker(_blocking(started), owner="leaving")
    await slow.start()
    run = await desk.submit()
    await asyncio.wait_for(started.wait(), 5)

    drained = await slow.drain(0.05)
    assert drained == {"finished": 0, "released": 1}
    released = await desk.store.get(run.run_id)
    assert released.status == QUEUED and released.history[-1]["outcome"] == "released"

    fresh = desk.worker(_answer("新来的 worker 答完了。"), owner="arriving")
    await fresh.start()
    done = await desk.until(run.run_id, SUCCEEDED)
    assert done.attempt == 2 and lost_attempts(done) == 0       # 主动交还不算失联


async def test_a_run_whose_dispatch_was_lost_is_found_by_the_sweep(desk):
    """API 落了库、XADD 之前挂了（或者 Redis 丢了消息）：清扫按库重新派发。"""
    run, _ = await desk.store.create(RunRecord(new_run_id(), "staff", "c1", "u1", "补件到哪了"))
    desk.store._runs[run.run_id].dispatched_at -= 10
    worker = desk.worker(_answer())
    await worker.start()
    done = await desk.until(run.run_id, SUCCEEDED)
    assert done.attempt == 1


async def test_the_backlog_picks_the_degrade_level():
    desk = Desk(concurrency=1)
    for i in range(5):
        await desk.submit(message=f"问题 {i}", conv_id=f"c{i}")
    seen = []

    async def execute(ctx):
        seen.append(ctx.degrade.level)
        return RunOutcome(status=SUCCEEDED, terminal=("result", {"response": "好"}), result={"response": "好"})

    worker = desk.worker(execute, policy=DegradePolicy(thresholds=(2, 3, 4, 5)))
    await worker.start()
    for run_id in list(desk.store._runs):
        await desk.until(run_id, SUCCEEDED)
    await desk.stop()
    # 一次跑一个：第一个被领走时积压是 5（领走了没确认的也算），之后每跑完一个少一个
    assert seen == [4, 3, 2, 1, 0]


# ── 降级档位 ──────────────────────────────────────────────────────────────────

def test_the_degrade_ladder_is_cumulative():
    policy = DegradePolicy(thresholds=(10, 20, 30, 40))
    assert policy.level_for(9).level == 0
    third = policy.level_for(35)
    assert (third.level, third.no_tool_loop, third.direct_search, third.no_delegation, third.shed) == (
        3, True, True, True, False)
    assert third.to_dict() == {"level": 3, "backlog": 35, "threshold": 30,
                               "off": ["tool_loop", "rewrite_rerank", "order_data"]}
    assert policy.level_for(1000).shed


@pytest.mark.parametrize("raw, expected", [
    ("", (20, 40, 80, 160)),
    ("off", ()),
    ("5,10,15,20", (5, 10, 15, 20)),
    ("20,10,30,40", (20, 40, 80, 160)),         # 不是递增：按默认
    ("1,2,3", (20, 40, 80, 160)),               # 不是四档：按默认
])
def test_degrade_thresholds_from_env(raw, expected):
    assert DegradePolicy.from_env({"AI_HELPDESK_DEGRADE_BACKLOG": raw}).thresholds == expected


# ── 提交和续流 ────────────────────────────────────────────────────────────────

async def test_backpressure_rejects_new_runs_but_not_a_replay_of_the_same_key():
    desk = Desk(queue_max=2)
    first = await desk.submit(message="一", key="k1")
    await desk.submit(message="二", key="k2")
    with pytest.raises(Backpressure) as rejected:
        await desk.submit(message="三", key="k3")
    assert rejected.value.backlog == 2 and 2 <= rejected.value.retry_after_s <= 60
    replay = await desk.submit(message="一", key="k1")        # 客户端重试：同一个 run，不算新负载
    assert replay.run_id == first.run_id
    with pytest.raises(IdempotencyConflict):
        await desk.submit(message="不是一", key="k1")


async def test_the_stream_starts_with_the_run_and_resumes_after_the_last_event_id(desk):
    worker = desk.worker(_answer())
    await worker.start()
    run = await desk.submit()
    frames = [f async for f in desk.service.events(run)]
    assert frames[0].startswith("event: run\n") and run.run_id in frames[0]
    ids = [f.split("\n", 1)[0][4:] for f in frames if f.startswith("id: ")]
    assert len(ids) == 3 and "event: result" in frames[-1]

    # 断线：收到第一帧（attempt）之后断了，带着它的 id 重连，只拿到之后的两帧
    resumed = [f async for f in desk.service.events(run, after=ids[0])]
    assert [f.split("\n")[1] for f in resumed] == ["event: delta", "event: result"]
    assert not any(f.startswith("event: run") for f in resumed)


async def test_frames_from_a_superseded_attempt_are_dropped(desk):
    run = await desk.submit()
    await desk.channel.publish(run.run_id, "attempt", {"attempt": 1}, 1)
    await desk.channel.publish(run.run_id, "delta", {"text": "第一遍。"}, 1)
    await desk.channel.publish(run.run_id, "attempt", {"attempt": 2}, 2)
    await desk.channel.publish(run.run_id, "delta", {"text": "被判失联的那一遍还在发。"}, 1)
    await desk.channel.publish(run.run_id, "delta", {"text": "第二遍。"}, 2)
    await desk.channel.publish(run.run_id, "result", {"response": "第二遍。"}, 2)
    frames = [f async for f in desk.service.events(run)]
    texts = [f for f in frames if "event: delta" in f]
    assert len(texts) == 2 and "被判失联" not in "".join(frames)


async def test_a_lost_frame_stream_is_completed_from_the_stored_result(desk):
    worker = desk.worker(_answer())
    await worker.start()
    run = await desk.submit()
    await desk.until(run.run_id, SUCCEEDED)
    desk.channel.drop(run.run_id)                 # Redis 重启、帧流没了
    frames = [f async for f in desk.service.events(run)]
    assert frames[-1].startswith("event: result") and "好的，补件已经登记。" in frames[-1]


async def test_wait_returns_the_finished_run(desk):
    worker = desk.worker(_answer())
    await worker.start()
    run = await desk.submit()
    done = await desk.service.wait(run.run_id, timeout_s=5)
    assert done.status == SUCCEEDED and done.result["response"]


async def test_wait_gives_up_after_the_timeout_and_reports_the_run_still_running(desk):
    worker = desk.worker(_blocking())
    await worker.start()
    run = await desk.submit()
    current = await desk.service.wait(run.run_id, timeout_s=0.2)
    assert current.status in (QUEUED, RUNNING)


# ── 配置 ──────────────────────────────────────────────────────────────────────

def test_queued_mode_refuses_to_start_without_its_dependencies():
    config = RunConfig.from_env({"AI_HELPDESK_RUN_MODE": "queued"})
    problems = config.problems(instance="", postgres=False, redis=True)
    assert len(problems) == 2 and any("AI_HELPDESK_INSTANCE" in p for p in problems)
    assert RunConfig.from_env({}).problems(instance="", postgres=False, redis=False) == []


def test_roles_decide_which_side_this_process_runs():
    api = RunConfig.from_env({"AI_HELPDESK_RUN_MODE": "queued", "AI_HELPDESK_ROLE": "api"})
    worker = RunConfig.from_env({"AI_HELPDESK_RUN_MODE": "queued", "AI_HELPDESK_ROLE": "worker"})
    inline = RunConfig.from_env({"AI_HELPDESK_ROLE": "worker"})
    assert (api.serves_api, api.runs_worker) == (True, False)
    assert (worker.serves_api, worker.runs_worker) == (False, True)
    assert (inline.serves_api, inline.runs_worker) == (False, False)
    bad = RunConfig.from_env({"AI_HELPDESK_RUN_MODE": "queue", "AI_HELPDESK_RUN_LEASE_S": "-1"})
    assert bad.mode == "inline" and bad.lease_s == 30.0 and len(bad.warnings) == 2


async def test_a_submission_is_accepted_even_when_redis_is_unreachable():
    """读不到积压、派发也失败：run 照样落库收下，由清扫任务稍后派发——不为一个读不到的数挡客户。"""
    desk = Desk()

    async def down(*args, **kwargs):
        raise ConnectionError("redis is restarting")

    desk.broker.backlog = down
    desk.broker.dispatch = down
    run = await desk.submit()
    assert (await desk.store.get(run.run_id)).status == QUEUED
