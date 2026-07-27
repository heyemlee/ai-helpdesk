"""SharedState 与分布式熔断器的行为测试。

不连真实 Redis：进程内退化路径直接测；"共享"语义用两个引用同一
SharedState 的熔断器验证——这正是多实例共享的最小模型。
"""
import time

from app.core.shared_state import DistributedCircuitBreaker, SharedState

# ── SharedState 进程内退化 ────────────────────────────────────────────────────

async def test_kv_roundtrip_without_redis():
    state = SharedState(redis_client=None)
    assert await state.get("k") is None
    await state.set("k", "v")
    assert await state.get("k") == "v"
    await state.delete("k")
    assert await state.get("k") is None


async def test_ttl_expiry_without_redis(monkeypatch):
    state = SharedState(redis_client=None)
    await state.set("k", "v", ttl_s=10)
    assert await state.get("k") == "v"
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 11)
    assert await state.get("k") is None


async def test_incr_without_redis():
    state = SharedState(redis_client=None)
    assert await state.incr("n") == 1
    assert await state.incr("n") == 2
    assert await state.incr("n") == 3


async def test_hash_merge_without_redis():
    state = SharedState(redis_client=None)
    await state.hset_map("penalties", {"technical_0": 0.5})
    await state.hset_map("penalties", {"billing_0": 0.2})
    merged = await state.hget_map("penalties")
    assert merged == {"technical_0": 0.5, "billing_0": 0.2}


class _BrokenRedis:
    def __getattr__(self, name):
        async def _fail(*args, **kwargs):
            raise ConnectionError("redis down")
        return _fail


async def test_degrades_to_local_when_redis_fails():
    """Redis 抛异常时退化为进程内存储，调用方无感知。"""
    state = SharedState(redis_client=_BrokenRedis())
    await state.set("k", "v")
    assert await state.get("k") == "v"
    assert await state.incr("n") == 1


# ── DistributedCircuitBreaker ─────────────────────────────────────────────────

async def test_breaker_opens_after_threshold():
    state = SharedState(redis_client=None)
    breaker = DistributedCircuitBreaker(state, "search", failure_threshold=3, recovery_s=60)
    assert await breaker.allow()
    for _ in range(3):
        await breaker.record_failure()
    assert not await breaker.allow()
    assert await breaker.state_name() == "open"


async def test_breaker_state_is_shared_across_instances():
    """两个熔断器共享同一 SharedState = 两个服务实例：A 熔断后 B 也拒绝。"""
    state = SharedState(redis_client=None)
    instance_a = DistributedCircuitBreaker(state, "search", failure_threshold=2, recovery_s=60)
    instance_b = DistributedCircuitBreaker(state, "search", failure_threshold=2, recovery_s=60)

    await instance_a.record_failure()
    await instance_a.record_failure()

    assert not await instance_a.allow()
    assert not await instance_b.allow()  # B 没见过任何失败，但同样被熔断保护


async def test_breaker_isolated_by_tool_name():
    state = SharedState(redis_client=None)
    search = DistributedCircuitBreaker(state, "search", failure_threshold=1, recovery_s=60)
    other = DistributedCircuitBreaker(state, "order_lookup", failure_threshold=1, recovery_s=60)
    await search.record_failure()
    assert not await search.allow()
    assert await other.allow()  # 不同工具互不影响


async def test_breaker_half_open_after_recovery(monkeypatch):
    state = SharedState(redis_client=None)
    breaker = DistributedCircuitBreaker(state, "search", failure_threshold=1, recovery_s=60)
    await breaker.record_failure()
    assert not await breaker.allow()

    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 61)
    assert await breaker.allow()  # HALF_OPEN 放行探测
    assert await breaker.state_name() == "half_open"


async def test_half_open_admits_exactly_one_probe(monkeypatch):
    """
    恢复窗口到了之后**只放一个**探测请求。

    原先窗口一到就无条件放行，于是积压的流量会在同一瞬间全部涌向一个刚缓过来
    的下游，把它再打断一轮。副本越多这个尖峰越高——而熔断器存在的理由正是
    不要让这件事发生。
    """
    state = SharedState(redis_client=None)
    breaker = DistributedCircuitBreaker(state, "search", failure_threshold=1, recovery_s=60)
    await breaker.record_failure()

    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 61)

    admitted = [await breaker.allow() for _ in range(5)]
    assert admitted == [True, False, False, False, False]


async def test_half_open_permit_is_shared_across_instances(monkeypatch):
    """
    许可要跨实例互斥，否则每个副本各放一个，尖峰变成 N 个而不是 1 个。

    这里两个 breaker 共用一个 SharedState，模拟同一个 Redis 后面的两个副本。
    """
    state = SharedState(redis_client=None)
    a = DistributedCircuitBreaker(state, "search", failure_threshold=1, recovery_s=60)
    b = DistributedCircuitBreaker(state, "search", failure_threshold=1, recovery_s=60)
    await a.record_failure()

    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 61)

    assert await a.allow() is True
    assert await b.allow() is False, "第二个副本不该再放一个探测请求"


async def test_failed_probe_lets_the_next_window_try_again(monkeypatch):
    """
    探测失败之后许可必须清掉，否则熔断器再也合不上。

    留着一张用过的许可，下一个恢复窗口的探测会被它挡住 —— 表现是下游早就好了、
    熔断器却永远停在 open，而且没有任何报错。
    """
    state = SharedState(redis_client=None)
    breaker = DistributedCircuitBreaker(state, "search", failure_threshold=1, recovery_s=60)
    await breaker.record_failure()

    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 61)
    assert await breaker.allow() is True
    await breaker.record_failure()          # 探测也失败，重新打开

    monkeypatch.setattr(time, "time", lambda: real_time() + 122)
    assert await breaker.allow() is True, "下一个恢复窗口应当能再探一次"


async def test_successful_probe_closes_the_breaker(monkeypatch):
    state = SharedState(redis_client=None)
    breaker = DistributedCircuitBreaker(state, "search", failure_threshold=1, recovery_s=60)
    await breaker.record_failure()

    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 61)
    assert await breaker.allow() is True
    await breaker.record_success()

    # 合上之后是正常放行，不再受许可限制
    assert [await breaker.allow() for _ in range(3)] == [True, True, True]
    assert await breaker.state_name() == "closed"


async def test_set_if_absent_is_first_writer_wins():
    state = SharedState(redis_client=None)
    assert await state.set_if_absent("lock", "1", ttl_s=60) is True
    assert await state.set_if_absent("lock", "2", ttl_s=60) is False
    assert await state.get("lock") == "1", "后来者不得覆盖已有的值"


async def test_set_if_absent_releases_after_ttl(monkeypatch):
    """没有 TTL 的话，持有者崩掉就等于把这把锁永久锁死。"""
    state = SharedState(redis_client=None)
    assert await state.set_if_absent("lock", "1", ttl_s=30) is True

    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 31)
    assert await state.set_if_absent("lock", "2", ttl_s=30) is True


async def test_breaker_success_resets():
    state = SharedState(redis_client=None)
    breaker = DistributedCircuitBreaker(state, "search", failure_threshold=2, recovery_s=60)
    await breaker.record_failure()
    await breaker.record_success()  # 成功清零失败计数
    await breaker.record_failure()
    assert await breaker.allow()  # 只有 1 次连续失败，未达阈值
