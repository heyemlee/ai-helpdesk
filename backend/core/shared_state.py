"""
跨实例共享状态。

核心问题：熔断器计数、路由惩罚这类**正确性相关**的状态原来放在进程内存里，
起第二个副本的瞬间语义就碎了——熔断器各断各的、Monitor 降权只作用于本进程。

SharedState 提供一个最小的异步 KV 抽象：
  - 配置了 Redis 且可达 → 所有实例共享同一份状态；
  - Redis 不可用 → 退化为进程内字典，单实例语义不变，并只警告一次。

刻意只搬正确性状态（熔断、惩罚、限流计数）。意图缓存和工具 TTL 缓存
是**性能**缓存，各实例各存一份只影响命中率不影响正确性，不值得为它们
付出每次读缓存一趟 Redis 往返的代价。
"""
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class SharedState:
    """Redis 优先、进程内兜底的异步 KV。所有方法不抛网络异常。"""

    def __init__(self, redis_client=None, prefix: str = "aihd"):
        self._redis = redis_client
        self._prefix = prefix
        self._local: dict[str, tuple[Any, float | None]] = {}  # key → (value, expire_at)
        self._warned = False

    def _key(self, key: str) -> str:
        return f"{self._prefix}:{key}"

    def _degrade(self, exc: Exception) -> None:
        if not self._warned:
            self._warned = True
            logger.warning(f"共享状态 Redis 不可用，退化为进程内存储: {exc}")

    # ── 基本 KV ───────────────────────────────────────────────────────────────

    async def get(self, key: str) -> str | None:
        if self._redis is not None:
            try:
                return await self._redis.get(self._key(key))
            except Exception as exc:
                self._degrade(exc)
        value, expire_at = self._local.get(key, (None, None))
        if expire_at is not None and expire_at < time.time():
            self._local.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: str, ttl_s: float | None = None) -> None:
        if self._redis is not None:
            try:
                if ttl_s:
                    await self._redis.set(self._key(key), value, ex=int(ttl_s))
                else:
                    await self._redis.set(self._key(key), value)
                return
            except Exception as exc:
                self._degrade(exc)
        self._local[key] = (value, time.time() + ttl_s if ttl_s else None)

    async def delete(self, key: str) -> None:
        if self._redis is not None:
            try:
                await self._redis.delete(self._key(key))
                return
            except Exception as exc:
                self._degrade(exc)
        self._local.pop(key, None)

    async def incr(self, key: str, ttl_s: float | None = None) -> int:
        """原子自增。首次自增时设置 TTL。"""
        if self._redis is not None:
            try:
                count = await self._redis.incr(self._key(key))
                if count == 1 and ttl_s:
                    await self._redis.expire(self._key(key), int(ttl_s))
                return count
            except Exception as exc:
                self._degrade(exc)
        value, expire_at = self._local.get(key, (0, time.time() + ttl_s if ttl_s else None))
        if expire_at is not None and expire_at < time.time():
            value, expire_at = 0, time.time() + ttl_s if ttl_s else None
        value = int(value) + 1
        self._local[key] = (value, expire_at)
        return value

    # ── Hash（路由惩罚共享用）─────────────────────────────────────────────────

    async def hset_map(self, key: str, mapping: dict[str, float], ttl_s: float | None = None) -> None:
        if not mapping:
            return
        if self._redis is not None:
            try:
                await self._redis.hset(self._key(key), mapping={k: str(v) for k, v in mapping.items()})
                if ttl_s:
                    await self._redis.expire(self._key(key), int(ttl_s))
                return
            except Exception as exc:
                self._degrade(exc)
        current, _ = self._local.get(key, ({}, None))
        current = dict(current or {})
        current.update(mapping)
        self._local[key] = (current, time.time() + ttl_s if ttl_s else None)

    async def hget_map(self, key: str) -> dict[str, float]:
        if self._redis is not None:
            try:
                raw = await self._redis.hgetall(self._key(key))
                return {k: float(v) for k, v in raw.items()}
            except Exception as exc:
                self._degrade(exc)
        value, expire_at = self._local.get(key, ({}, None))
        if expire_at is not None and expire_at < time.time():
            self._local.pop(key, None)
            return {}
        return dict(value or {})


class DistributedCircuitBreaker:
    """
    Redis 共享的三态熔断器：CLOSED → OPEN → HALF_OPEN → CLOSED。

    与进程内版本语义一致，但失败计数和打开时间放在 SharedState 里：
    实例 A 熔断之后，实例 B 对同一工具的调用同样被拒绝——这才是
    "保护下游"的本意，进程内熔断在多副本下只保护了 1/N 的流量。
    """

    def __init__(
        self,
        state: SharedState,
        name: str,
        failure_threshold: int = 5,
        recovery_s: float = 60.0,
    ):
        self._state = state
        self._name = name
        self.threshold = failure_threshold
        self.recovery_s = recovery_s

    @property
    def _fail_key(self) -> str:
        return f"cb:{self._name}:fails"

    @property
    def _opened_key(self) -> str:
        return f"cb:{self._name}:opened_at"

    async def allow(self) -> bool:
        opened_raw = await self._state.get(self._opened_key)
        if opened_raw is None:
            return True
        opened_at = float(opened_raw)
        if time.time() - opened_at >= self.recovery_s:
            return True  # HALF_OPEN：放一个探测请求
        return False

    async def record_success(self) -> None:
        await self._state.delete(self._fail_key)
        await self._state.delete(self._opened_key)

    async def record_failure(self) -> None:
        # 失败计数带 TTL：零星失败随时间衰减，不会永远累积到阈值
        fails = await self._state.incr(self._fail_key, ttl_s=self.recovery_s * 5)
        if fails >= self.threshold:
            await self._state.set(
                self._opened_key, str(time.time()), ttl_s=self.recovery_s * 5
            )
            logger.warning(f"熔断器打开: {self._name}（连续失败 {fails} 次）")

    async def state_name(self) -> str:
        opened_raw = await self._state.get(self._opened_key)
        if opened_raw is None:
            return "closed"
        if time.time() - float(opened_raw) >= self.recovery_s:
            return "half_open"
        return "open"
