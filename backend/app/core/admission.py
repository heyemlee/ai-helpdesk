"""
模型调用的准入控制：全局令牌桶，加上"这一轮不许出网"的开关。

**为什么要它。** 原来只有事后保护（core/llm_guard）：撞到 429 才退避。并发一上来，
所有副本、所有在途请求一起出网，一起吃 429，再一起退避——上游的配额是全局的，
撞墙之后的退避救不回已经浪费掉的那一批请求，还把 429 引到了每一个调用点的降级分支里。
准入控制把这件事挪到出网之前：按供应商给的 RPM / TPM 发令牌，拿不到令牌就在本地等，
等不起就当场判"过载"，不出网。

- **令牌桶在 Redis 里，所有副本共用一只。** 每个副本各自限 RPM 的话，加副本就是把
  全局配额乘上副本数——这正是 KEDA 扩容时会发生的事。脚本里用 Redis 的 `TIME`，
  不信各副本自己的时钟。Redis 不可达时退化成进程内的桶（只警告一次）：限得没那么准，
  但不会因为限流组件坏了就把所有调用挡掉。
- **TPM 先按估算扣，回来再按实际用量结算。** 出网之前不知道这次会花多少 token：
  输入按字符数粗估，输出按 max_tokens 算上限，调用结束后用 usage 把多扣的退回去。
- **等多久有上限。** 拿不到令牌时按脚本算出的等待时间睡，累计超过
  `AI_HELPDESK_LLM_ADMISSION_WAIT_S` 就抛 `LLMUnavailableError("overloaded")`。调用点
  对它的处理和对"模型服务不可用"一样（意图退回规则、改写退回原句、作答报"没答上来"），
  这条路已经被停机演练走过，不新开一条。

`shed()` 是另一件事：负载降级到最后一档时（app/runs/degrade.py），这一轮的所有模型调用
在本地直接判"过载"，一次都不出网。用 contextvar 而不是参数，是因为模型调用点散在
意图识别、检索改写、重排、专家、画像各处，它们都经过同一个 GuardedLLMClient。

| 变量 | 默认 | 说明 |
|---|---|---|
| AI_HELPDESK_LLM_RPM | 0 | 每分钟请求数上限，0 不限 |
| AI_HELPDESK_LLM_TPM | 0 | 每分钟 token 上限（输入 + 输出），0 不限 |
| AI_HELPDESK_LLM_ADMISSION_WAIT_S | 20 | 一次调用最多等令牌多久 |
| AI_HELPDESK_LLM_BURST_S | 10 | 桶的容量按几秒的配额算：最多一口气放出这么多 |

**桶不装满一分钟。** 容量要是一整分钟的配额，桶满的时候一口气能放出一分钟的量，接着
再按速率补，任意 60 秒里最多放出将近两倍的配额——而供应商的 RPM 往往按更短的窗口执行
（每分钟 6 万次可能按每秒 1000 次卡）。容量取 10 秒的量，任意 60 秒里最多是配额的 1.17 倍；
上限配成供应商配额的八五折，就落在配额以内。
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from app.core.metrics import LLM_ADMISSION_REJECTED, LLM_ADMISSION_WAIT

logger = logging.getLogger(__name__)

REASON_OVERLOADED = "overloaded"

# 一个字符大约多少 token：中文一字一到一个半 token，英文四个字符一个 token。取 0.5，
# 对中文偏少、对英文偏多；反正调用结束会按实际用量结算，这里只决定"先扣多少"
_TOKENS_PER_CHAR = 0.5
_DEFAULT_MAX_TOKENS = 1024

# 多只桶一起扣：每只都够才一起扣，有一只不够就一只都不扣，返回要等的毫秒数。
# 时间取 Redis 的 TIME：所有副本对"现在"有同一个答案，不受各自时钟漂移影响
_ACQUIRE_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local n = #KEYS
local levels = {}
local wait = 0
for i = 1, n do
  local rate = tonumber(ARGV[(i - 1) * 3 + 1])
  local cap = tonumber(ARGV[(i - 1) * 3 + 2])
  local cost = tonumber(ARGV[(i - 1) * 3 + 3])
  local h = redis.call('HMGET', KEYS[i], 'tokens', 'ts')
  local tokens = tonumber(h[1]) or cap
  local ts = tonumber(h[2]) or now
  tokens = math.min(cap, tokens + math.max(0, now - ts) * rate)
  levels[i] = tokens
  if tokens < cost then
    wait = math.max(wait, math.ceil((cost - tokens) / rate))
  end
end
for i = 1, n do
  local rate = tonumber(ARGV[(i - 1) * 3 + 1])
  local cap = tonumber(ARGV[(i - 1) * 3 + 2])
  local cost = tonumber(ARGV[(i - 1) * 3 + 3])
  local tokens = levels[i]
  if wait == 0 then tokens = tokens - cost end
  redis.call('HSET', KEYS[i], 'tokens', tokens, 'ts', now)
  redis.call('PEXPIRE', KEYS[i], math.ceil(cap / rate) * 2 + 1000)
end
return wait
"""

# 结算：按实际用量补扣或退回（delta 为正是退回）。退回不超过容量
_SETTLE_LUA = """
local cap = tonumber(ARGV[1])
local delta = tonumber(ARGV[2])
local tokens = tonumber(redis.call('HGET', KEYS[1], 'tokens'))
if tokens == nil then return 0 end
redis.call('HSET', KEYS[1], 'tokens', math.min(cap, tokens + delta))
return 1
"""


@dataclass(frozen=True)
class Limits:
    rpm: int = 0
    tpm: int = 0
    max_wait_s: float = 20.0
    burst_s: float = 10.0

    @property
    def active(self) -> bool:
        return self.rpm > 0 or self.tpm > 0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Limits:
        env = os.environ if env is None else env

        def _number(name: str, default: float) -> float:
            raw = env.get(name, "").strip()
            if not raw:
                return default
            try:
                value = float(raw)
            except ValueError:
                logger.warning("%s=%r 不是数字，按默认 %s", name, raw, default)
                return default
            return max(value, 0.0)

        return cls(
            rpm=int(_number("AI_HELPDESK_LLM_RPM", 0)),
            tpm=int(_number("AI_HELPDESK_LLM_TPM", 0)),
            max_wait_s=_number("AI_HELPDESK_LLM_ADMISSION_WAIT_S", 20.0),
            burst_s=max(_number("AI_HELPDESK_LLM_BURST_S", 10.0), 1.0),
        )


def refill(tokens: float, last_ms: float, now_ms: float, rate_per_ms: float, capacity: float) -> float:
    """令牌桶的补充：过了多久补多少，不超过容量。进程内的桶用它，Lua 脚本是同一个算式。"""
    return min(capacity, tokens + max(0.0, now_ms - last_ms) * rate_per_ms)


class _LocalBuckets:
    """进程内的桶。Redis 不可达时用；多副本下各限各的，所以只是兜底。"""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._state: dict[str, tuple[float, float]] = {}

    def acquire(self, specs: list[tuple[str, float, float, float]]) -> int:
        now = self._clock() * 1000
        levels = []
        wait = 0.0
        for key, rate, cap, cost in specs:
            tokens, last = self._state.get(key, (cap, now))
            tokens = refill(tokens, last, now, rate, cap)
            levels.append(tokens)
            if tokens < cost:
                wait = max(wait, (cost - tokens) / rate)
        for (key, _rate, _cap, cost), tokens in zip(specs, levels, strict=True):
            self._state[key] = (tokens - cost if wait == 0 else tokens, now)
        return int(wait + 0.999) if wait else 0

    def settle(self, key: str, capacity: float, delta: float) -> None:
        if key in self._state:
            tokens, last = self._state[key]
            self._state[key] = (min(capacity, tokens + delta), last)


def estimate_tokens(kwargs: Mapping[str, Any]) -> int:
    """出网之前估这次调用要花多少 token：输入按字符数粗估，输出按 max_tokens 算上限。"""
    chars = len(str(kwargs.get("system") or ""))
    for message in kwargs.get("messages") or []:
        content = message.get("content") if isinstance(message, Mapping) else None
        chars += len(content) if isinstance(content, str) else len(str(content or ""))
    tools = kwargs.get("tools")
    if tools:
        chars += len(str(tools))
    max_tokens = kwargs.get("max_tokens")
    output = int(max_tokens) if isinstance(max_tokens, int) and max_tokens > 0 else _DEFAULT_MAX_TOKENS
    return int(chars * _TOKENS_PER_CHAR) + output


def usage_tokens(message: Any) -> int | None:
    """调用结束后实际花了多少 token；拿不到用量返回 None（不结算，按估算算数）。"""
    usage = getattr(message, "usage", None)
    if usage is None:
        return None
    total = 0
    found = False
    for name in ("input_tokens", "output_tokens"):
        value = getattr(usage, name, None)
        if isinstance(value, int):
            total += value
            found = True
    return total if found else None


class Admission:
    """
    一组令牌桶：按 Limits 发令牌。`limits.active` 为假时 acquire 直接返回，零开销。

    桶按"供应商 @ 地址"分（和 llm_guard 的状态同一个粒度）：换一个中继就是另一份配额。
    """

    def __init__(self, limits: Limits, redis_client=None, prefix: str = "ai-helpdesk:llm-bucket"):
        self.limits = limits
        self._redis = redis_client
        self._prefix = prefix
        self._local = _LocalBuckets()
        self._acquire_script = None
        self._settle_script = None
        self._warned = False
        if redis_client is not None:
            try:
                self._acquire_script = redis_client.register_script(_ACQUIRE_LUA)
                self._settle_script = redis_client.register_script(_SETTLE_LUA)
            except Exception:
                logger.warning("令牌桶脚本注册失败，准入控制只在进程内生效", exc_info=True)

    @property
    def active(self) -> bool:
        return self.limits.active

    def _capacity(self, per_minute: int) -> float:
        """桶的容量：burst_s 秒的配额，至少 1。"""
        return max(per_minute * self.limits.burst_s / 60.0, 1.0)

    def _specs(self, label: str, cost_tokens: int) -> list[tuple[str, float, float, float]]:
        specs = []
        if self.limits.rpm > 0:
            specs.append((f"{self._prefix}:{label}:rpm", self.limits.rpm / 60000.0,
                          self._capacity(self.limits.rpm), 1.0))
        if self.limits.tpm > 0:
            capacity = self._capacity(self.limits.tpm)
            cost = float(min(cost_tokens, capacity))   # 单次比整桶还大时按整桶算，不然永远等不到
            specs.append((f"{self._prefix}:{label}:tpm", self.limits.tpm / 60000.0, capacity, cost))
        return specs

    async def _try(self, specs: list[tuple[str, float, float, float]]) -> int:
        if self._acquire_script is not None:
            try:
                args: list[Any] = []
                for _key, rate, cap, cost in specs:
                    args.extend([rate, cap, cost])
                return int(await self._acquire_script(keys=[s[0] for s in specs], args=args))
            except Exception as exc:
                if not self._warned:
                    self._warned = True
                    logger.warning(f"令牌桶 Redis 不可用，退化为进程内限流（多副本下限得不准）: {exc}")
        return self._local.acquire(specs)

    async def acquire(self, label: str, cost_tokens: int) -> float:
        """
        拿令牌，返回为它睡了多少秒（拿到就是 0）。等不起（累计超过 max_wait_s）抛
        LLMUnavailableError(overloaded)。

        睡的时长按脚本给的等待时间加一点抖动：一群调用同时等同一只桶时，不在同一毫秒一起醒。
        """
        if not self.active:
            return 0.0
        from app.core.llm_guard import LLMUnavailableError

        specs = self._specs(label, cost_tokens)
        waited = 0.0
        while True:
            wait_ms = await self._try(specs)
            if wait_ms <= 0:
                LLM_ADMISSION_WAIT.observe(waited)
                return waited
            if waited + wait_ms / 1000 > self.limits.max_wait_s:
                LLM_ADMISSION_WAIT.observe(waited)
                LLM_ADMISSION_REJECTED.inc()
                raise LLMUnavailableError(REASON_OVERLOADED, sent=False)
            pause = wait_ms / 1000 * (1 + random.uniform(0, 0.1))
            await asyncio.sleep(pause)
            waited += pause

    async def settle(self, label: str, estimated: int, actual: int | None) -> None:
        """按实际用量结算 TPM 桶：估多了退回，估少了补扣。拿不到用量不结算。"""
        if self.limits.tpm <= 0 or actual is None:
            return
        capacity = self._capacity(self.limits.tpm)
        delta = float(min(estimated, capacity) - actual)
        if not delta:
            return
        key = f"{self._prefix}:{label}:tpm"
        if self._settle_script is not None:
            try:
                await self._settle_script(keys=[key], args=[capacity, delta])
                return
            except Exception:
                logger.debug("令牌桶结算失败，按估算算数", exc_info=True)
        self._local.settle(key, capacity, delta)


# 进程里只有一份准入：lifespan 按配置装上（install），没装时什么都不限
_admission: Admission = Admission(Limits())


def install(admission: Admission) -> None:
    global _admission
    _admission = admission


def current() -> Admission:
    return _admission


# ── 这一轮不出网（负载降级的最后一档）────────────────────────────────────────

_shed: ContextVar[str] = ContextVar("llm_shed", default="")


def shed_reason() -> str:
    """当前这一轮是不是被判了"不许出网"。是就返回原因（overloaded），否则空串。"""
    return _shed.get()


@contextmanager
def shed(reason: str = REASON_OVERLOADED) -> Iterator[None]:
    """这个 with 里（以及从这里建出去的任务里）的模型调用都在本地判不可用，一次都不出网。"""
    token = _shed.set(reason)
    try:
        yield
    finally:
        try:
            _shed.reset(token)
        except ValueError:
            pass
