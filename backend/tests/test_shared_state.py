"""SharedState 与分布式熔断器的行为测试。

不连真实 Redis：进程内退化路径直接测；"共享"语义用两个引用同一
SharedState 的熔断器验证——这正是多实例共享的最小模型。
"""
import time

from core.shared_state import DistributedCircuitBreaker, SharedState

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


async def test_breaker_success_resets():
    state = SharedState(redis_client=None)
    breaker = DistributedCircuitBreaker(state, "search", failure_threshold=2, recovery_s=60)
    await breaker.record_failure()
    await breaker.record_success()  # 成功清零失败计数
    await breaker.record_failure()
    assert await breaker.allow()  # 只有 1 次连续失败，未达阈值
