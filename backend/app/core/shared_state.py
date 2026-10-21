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


# Redis 连接的超时（基础设施这一路和工作记忆那一路都用它）。不设的话，Redis 的 pod 被删、
# 节点没了，已经建好的连接上读不到回应也收不到 RST，一个请求能一直挂到 TCP 自己放弃
# ——k8s 演练里 POST /runs 就这么挂满了客户端的 30 秒。读超时要比阻塞读的最长时间长：
# worker 读派发队列一次最多阻塞 2 秒，续流读帧流一次最多 5 秒（app/runs/）
REDIS_TIMEOUTS = {
    "socket_connect_timeout": 3.0,
    "socket_timeout": 10.0,
    "socket_keepalive": True,
    "health_check_interval": 15,
}


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

    async def set_if_absent(self, key: str, value: str, ttl_s: float | None = None) -> bool:
        """
        原子占位：key 不存在时写入并返回 True，已存在则返回 False。

        Redis 侧是 `SET NX EX`，一次往返、跨实例互斥。**必须原子**——
        "先 get 再 set" 在并发下每个实例都会看到空值，于是每个实例都以为自己
        抢到了，那正是这个方法要防的事。

        TTL 不是可选的装饰：持有者崩了之后没人来释放，没有 TTL 这把锁就是永久的。
        """
        if self._redis is not None:
            try:
                acquired = await self._redis.set(
                    self._key(key), value, nx=True, ex=int(ttl_s) if ttl_s else None
                )
                return bool(acquired)
            except Exception as exc:
                self._degrade(exc)
        existing, expire_at = self._local.get(key, (None, None))
        if existing is not None and (expire_at is None or expire_at >= time.time()):
            return False
        self._local[key] = (value, time.time() + ttl_s if ttl_s else None)
        return True

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

    @property
    def _probe_key(self) -> str:
        return f"cb:{self._name}:probe"

    async def allow(self) -> bool:
        opened_raw = await self._state.get(self._opened_key)
        if opened_raw is None:
            return True
        opened_at = float(opened_raw)
        if time.time() - opened_at < self.recovery_s:
            return False

        # HALF_OPEN：**只放一个**探测请求。
        #
        # 原先这里直接 `return True`，意思是"恢复窗口到了就放行"——但窗口到了
        # 之后**每一个**到达的请求都会读到同一个 opened_at，于是全部放行。
        # 熔断器保护下游的作用在恢复的那一刻整个消失：一个刚缓过来的下游会被
        # 积压的流量一次打回去，然后再断一轮。副本越多、流量越大，这个尖峰越高。
        #
        # 许可用 SET NX 争，抢到的那个进去探，其余继续被拒。TTL 取一个恢复窗口：
        # 探测请求要是崩了没来得及汇报，许可会自己过期，下一轮还能再探。
        return await self._state.set_if_absent(self._probe_key, "1", ttl_s=self.recovery_s)

    async def record_success(self) -> None:
        await self._state.delete(self._fail_key)
        await self._state.delete(self._opened_key)
        await self._state.delete(self._probe_key)

    async def record_failure(self) -> None:
        # 失败计数带 TTL：零星失败随时间衰减，不会永远累积到阈值
        fails = await self._state.incr(self._fail_key, ttl_s=self.recovery_s * 5)
        if fails >= self.threshold:
            await self._state.set(
                self._opened_key, str(time.time()), ttl_s=self.recovery_s * 5
            )
            # 探测失败了：清掉许可，让下一个恢复窗口能重新发一个。
            # 留着的话下一轮探测会被一张已经用过的许可挡住，熔断器就再也合不上了。
            await self._state.delete(self._probe_key)
            logger.warning(f"熔断器打开: {self._name}（连续失败 {fails} 次）")

    async def state_name(self) -> str:
        opened_raw = await self._state.get(self._opened_key)
        if opened_raw is None:
            return "closed"
        if time.time() - float(opened_raw) >= self.recovery_s:
            return "half_open"
        return "open"
