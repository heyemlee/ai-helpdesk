"""
模型凭证不可用保护。

起因：中继把 Key 停用后，每一个模型调用点仍然各自出网一次——
意图识别 401，接着改写 401、重排 401、自纠错再改写再重排各 401、专家 401，
非流式还要用同一把 Key 让通用 Agent 兜底一次。一条提问 6～8 次请求全部打在
一把明知不能用的凭证上，而且每个调用点都在自己的 `except Exception` 里
"成功降级"，链路看起来还在跑。

这里做的事只有一件：**一把凭证被明确拒绝之后，同一份配置的后续调用不再出网**，
直接抛 `LLMUnavailableError`。所有客户端都从 `llm_utils.create_llm_client`
这一个入口造出来，所以保护也只套在这一处。

状态按 **供应商 + 服务地址 + 凭证** 隔离：换一个中继地址或换一把 Key 是另一份
状态，互不影响。凭证只取摘要当字典键，不进日志、不进任何对外输出。

按错误的**明确语义**分两类处理：

| 情况 | 处理 |
| --- | --- |
| 401；403 且错误体明说凭证/账号不被允许；402 或错误体明说额度用尽 | 一直挡到进程重启或 `reset_all()`——等再久它也不会自己好 |
| 普通 429；403 但错误体没说清原因 | 有限退避，到期自动放行下一次真实调用去试 |

**不做定时探测。** 退避到期后是下一个真实请求去试；"一直挡"的那一类只能由人
处理完之后重启，或走已有的受保护入口（`POST /skills/reload`）解除。
"""
from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from app.core import admission
from app.events.model_calls import ModelCall, RecordedStream

logger = logging.getLogger(__name__)

REASON_CREDENTIAL = "credential_rejected"   # 401：Key 无效、停用、被吊销
REASON_PERMISSION = "permission_denied"     # 403：错误体明说这把凭证/这个账号不被允许
REASON_QUOTA = "quota_exhausted"            # 402，或错误体明说额度/余额用尽
REASON_FORBIDDEN = "forbidden"              # 403，但错误体没说清是不是凭证的问题
REASON_RATE_LIMIT = "rate_limited"          # 普通 429
# 不是保护自己判出来的状态，是给调用方用的：模型这一步没答上来（超时、5xx 之类），
# 原因不在凭证。和上面几个放在一起，是因为对外它们都叫"模型服务不可用"。
REASON_MODEL_ERROR = "model_error"
# 也不是凭证的事：本地准入控制判的"过载"（core/admission）——等令牌等不起，或者负载降级
# 到最后一档、这一轮不许出网。一次都没出网，不动这份配置的状态
REASON_OVERLOADED = admission.REASON_OVERLOADED

# 等人处理的那一类：不设到期时间
_UNTIL_RESET = frozenset({REASON_CREDENTIAL, REASON_PERMISSION, REASON_QUOTA})

# 有限退避：第 n 次连续失败等 base × 2^(n-1)，封顶 cap。成功一次就清零。
RATE_LIMIT_BACKOFF_S = (5.0, 60.0)
FORBIDDEN_BACKOFF_S = (30.0, 600.0)
# 服务端明说了 retry-after 就听它的，但不超过这个数——"有限"得是真的有限
RETRY_AFTER_CAP_S = 900.0

# 错误体里的类型字段（Anthropic 的 error.type、OpenAI 的 error.code / error.type）
_QUOTA_TYPES = frozenset({"billing_error", "insufficient_quota"})
_PERMISSION_TYPES = frozenset({
    "permission_error", "authentication_error", "permission_denied", "access_denied",
    "forbidden", "account_deactivated", "access_terminated",
    "unsupported_country_region_territory",
})
# 中继的错误体常常不带标准类型字段（实测撞到的那条是
# `{"error": "Invalid API key", "message": "API key is disabled"}`），只能看措辞。
# 两张表都只收"说的就是这件事"的说法，宁可漏判成有限退避，不误判成永久。
_QUOTA_PHRASES = (
    "insufficient_quota", "insufficient quota", "exceeded your current quota",
    "quota exceeded", "quota exhausted", "out of credits", "insufficient balance",
    "insufficient credit", "credit balance is too low",
    "余额不足", "额度不足", "额度已用", "额度耗尽", "欠费",
)
_PERMISSION_PHRASES = (
    "permission", "not allowed", "not authorized", "unauthorized", "access denied",
    "disabled", "revoked", "suspended", "banned", "deactivated",
    "无权", "没有权限", "禁用", "停用", "封禁",
)


class LLMUnavailableError(RuntimeError):
    """
    模型服务此刻不可用：凭证被拒、额度用尽，或还在限流退避期内。

    `sent` 说明这一次有没有出网：True 是"发出去了，被对面拒绝，保护由此触发"，
    False 是"保护已经在挡，这次根本没发"。消息里只有原因和状态码，没有凭证。
    """

    def __init__(self, reason: str, *, status_code: int | None = None,
                 retry_after_s: float | None = None, sent: bool = False):
        self.reason = reason
        self.status_code = status_code
        self.retry_after_s = retry_after_s
        self.sent = sent
        detail = f"HTTP {status_code}" if status_code else "no response"
        wait = f"，约 {retry_after_s:.0f}s 后放行" if retry_after_s is not None else ""
        how = "被服务端拒绝" if sent else "未出网"
        super().__init__(f"模型服务不可用（{reason}，{detail}，{how}{wait}）")


def unavailable_reason(ex: BaseException | None) -> str:
    """这个异常是不是"模型服务不可用"。是的话返回原因，否则空串。"""
    return ex.reason if isinstance(ex, LLMUnavailableError) else ""


def log_model_failure(log: logging.Logger, what: str, ex: BaseException) -> None:
    """
    调用点记"这次模型调用失败了"用的。

    保护已经在挡、根本没出网的那些降到 debug：状态变化时保护自己记过一条 error 了，
    停机期间再让每条提问的每个环节各占一条 warning，只会把那一条真正的信号淹掉。
    """
    blocked_before_send = isinstance(ex, LLMUnavailableError) and not ex.sent
    (log.debug if blocked_before_send else log.warning)("%s: %s", what, ex)


@dataclass(frozen=True)
class Verdict:
    reason: str
    status_code: int
    retry_after_s: float | None = None


def _semantics(ex: BaseException) -> tuple[str, str, bool]:
    """
    从 SDK 异常里取出错误体说了什么：`(类型字段, 全部措辞的小写拼接, 是不是结构化错误体)`。

    按属性取而不按类判：anthropic 和 openai 两个 SDK 的状态错误都带 `status_code`
    和 `body`，这一层不想知道现在用的是哪一个。
    """
    body = getattr(ex, "body", None)
    kinds = [getattr(ex, "type", None), getattr(ex, "code", None)]
    words: list[Any] = []
    structured = False
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            kinds += [error.get("type"), error.get("code")]
            words.append(error.get("message"))
        else:
            words.append(error)
        kinds += [body.get("type"), body.get("code")]
        words.append(body.get("message"))
        structured = any(k for k in kinds) or any(w for w in words)
    elif isinstance(body, str):
        words.append(body)
    kind = next((str(k).lower() for k in kinds if isinstance(k, str) and k and k != "error"), "")
    text = " ".join(str(w) for w in [*kinds, *words] if w).lower()
    return kind, text, structured


def _retry_after(ex: BaseException) -> float | None:
    headers = getattr(getattr(ex, "response", None), "headers", None)
    if headers is None:
        return None
    try:
        millis = headers.get("retry-after-ms")
        if millis:
            return max(0.0, float(millis) / 1000.0)
        seconds = headers.get("retry-after")
        return max(0.0, float(seconds)) if seconds else None
    except (TypeError, ValueError):
        # HTTP 日期形式的 retry-after：不解析，按没给处理，走指数退避
        return None


def classify(ex: BaseException) -> Verdict | None:
    """
    这次失败是不是在说"这份配置此刻用不了"。不是就返回 None——超时、5xx、
    请求本身写错（400/404/413）都不是凭证的事，保护不管。
    """
    status = getattr(ex, "status_code", None)
    if not isinstance(status, int):
        return None
    kind, text, structured = _semantics(ex)

    if status == 401:
        return Verdict(REASON_CREDENTIAL, status)

    quota = kind in _QUOTA_TYPES or any(p in text for p in _QUOTA_PHRASES)
    if status == 402 or (quota and status in (403, 429)):
        return Verdict(REASON_QUOTA, status)
    # Anthropic 早先用 400 报余额不足。400 是"请求有问题"的大类，只认这一句原话
    if status == 400 and "credit balance is too low" in text:
        return Verdict(REASON_QUOTA, status)

    if status == 403:
        explicit = structured and (
            kind in _PERMISSION_TYPES or any(p in text for p in _PERMISSION_PHRASES))
        return Verdict(REASON_PERMISSION if explicit else REASON_FORBIDDEN, status)
    if status == 429:
        return Verdict(REASON_RATE_LIMIT, status, _retry_after(ex))
    return None


class LLMGuard:
    """一份配置（供应商 + 地址 + 凭证）的可用状态。进程内共享，不跨进程。"""

    def __init__(self, label: str, clock: Callable[[], float] = time.monotonic, provider: str = ""):
        self.label = label            # 供应商 @ 主机名，只给日志看
        # 事件日志的 model_call 记它（只有供应商名，不带地址）
        self.provider = provider or label.split(" @ ", 1)[0]
        self._clock = clock
        self._reason = ""
        self._status: int | None = None
        self._until: float | None = None    # None 且 reason 非空 = 一直挡到重启/解除
        self._strikes = 0
        self.blocked_calls = 0        # 保护期间挡下、没有出网的调用数

    def __repr__(self) -> str:
        return f"LLMGuard({self.label!r}, reason={self._reason or 'available'!r})"

    def _remaining(self) -> float | None:
        if not self._reason or self._until is None:
            return None
        return max(0.0, self._until - self._clock())

    def blocked_reason(self) -> str:
        """此刻不可用的原因，可用返回空串。有限退避到期后在这里自动放行。"""
        # 负载降级的最后一档（core/admission.shed）：这一轮自己判了"不出网"。放在最前面，
        # 编排器作答前的那道检查（_unavailable_before_answer）也就照"模型服务不可用"处理，
        # 不调 Agent、不建工单——和停机时同一条路。只影响绑定了它的那一轮，不改这份配置的状态
        shed = admission.shed_reason()
        if shed:
            return shed
        if not self._reason:
            return ""
        if self._until is not None and self._clock() >= self._until:
            # 到期只是放行下一次真实调用去试。失败次数留着——再被拒就退得更久
            logger.info("模型服务退避到期，放行下一次调用: %s（此前 %s）", self.label, self._reason)
            self._reason, self._status, self._until = "", None, None
            return ""
        return self._reason

    def check(self) -> None:
        """出网之前调。保护在挡就抛，不出网。"""
        reason = self.blocked_reason()
        if reason:
            self.blocked_calls += 1
            raise LLMUnavailableError(
                reason, status_code=self._status, retry_after_s=self._remaining(), sent=False)

    @property
    def bucket(self) -> str:
        """令牌桶按什么分（core/admission）：和这份状态同一个粒度，供应商 @ 主机名，不含凭证。"""
        return self.label.replace(" @ ", "@").replace(" ", "_")

    def record_success(self) -> None:
        if not self._reason:
            self._strikes = 0

    def record_failure(self, ex: BaseException) -> LLMUnavailableError | None:
        """
        一次调用失败之后调。判定为不可用就记下状态，并返回该抛给调用方的异常；
        和凭证无关的失败返回 None，调用方原样抛原异常。
        """
        verdict = classify(ex)
        if verdict is None:
            return None

        current = self.blocked_reason()
        delay: float | None = None
        if verdict.reason in _UNTIL_RESET:
            self._reason, self._status, self._until = verdict.reason, verdict.status_code, None
        elif current in _UNTIL_RESET:
            # 已经在等人处理了：同时在途的另一个请求吃到一次限流，不能把它降回有限退避
            pass
        else:
            base, cap = (RATE_LIMIT_BACKOFF_S if verdict.reason == REASON_RATE_LIMIT
                         else FORBIDDEN_BACKOFF_S)
            if verdict.retry_after_s is not None:
                delay = min(max(verdict.retry_after_s, 1.0), RETRY_AFTER_CAP_S)
            else:
                delay = min(base * (2 ** self._strikes), cap)
            if not current:
                # 同一轮里并发在途的几个请求一起被拒只算一次，不然一条提问就把退避顶到封顶
                self._strikes += 1
            until = self._clock() + delay
            self._until = until if self._until is None else max(self._until, until)
            self._reason, self._status = verdict.reason, verdict.status_code

        # 只在状态变化时记一条：从可用变成不可用，或从有限退避升级成等人处理。
        # 保护期间被挡下的每一次调用不再各记一条——那正是原来刷屏的样子。
        if not current or (current not in _UNTIL_RESET and self._reason in _UNTIL_RESET):
            remaining = self._remaining()
            logger.error(
                "模型服务不可用（%s，HTTP %s）: %s。%s，期间同一配置的模型调用不再出网。",
                self._reason, self._status, self.label,
                f"约 {remaining:.0f}s 后放行下一次调用" if remaining is not None
                else "需处理凭证/额度后重启，或调用 POST /skills/reload 解除",
            )
        return LLMUnavailableError(
            self._reason, status_code=self._status, retry_after_s=self._remaining(), sent=True)

    def status(self) -> dict[str, Any]:
        """给 /health 和工作台看的状态。只有原因和时间，没有地址和凭证。"""
        reason = self.blocked_reason()
        remaining = self._remaining()
        return {
            "available": not reason,
            "unavailable_reason": reason,
            "retry_after_s": round(remaining, 1) if remaining is not None else None,
            "blocked_calls": self.blocked_calls,
        }

    def reset(self) -> bool:
        """解除保护。返回解除前是不是在挡。"""
        was_blocked = bool(self._reason)
        self._reason, self._status, self._until, self._strikes = "", None, None, 0
        return was_blocked


_GUARDS: dict[tuple[str, str, str], LLMGuard] = {}


def _address(base_url: str | None) -> str:
    return (base_url or "").strip().rstrip("/").lower()


def guard_for(provider: str, base_url: str | None, api_key: str) -> LLMGuard:
    """取这份配置的保护状态；同一份配置在进程里只有一份，所有客户端共用。"""
    # 凭证只以摘要的形式当键用，摘要本身也不出这个模块
    digest = hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()
    key = (provider, _address(base_url), digest)
    guard = _GUARDS.get(key)
    if guard is None:
        host = urlsplit(_address(base_url)).hostname or "官方地址"
        guard = _GUARDS[key] = LLMGuard(f"{provider} @ {host}", provider=provider)
    return guard


def reset_all() -> int:
    """解除本进程所有配置的保护，返回解除了几份。下一次真实调用会重新出网去试。"""
    cleared = sum(1 for guard in _GUARDS.values() if guard.reset())
    if cleared:
        logger.warning("已解除 %d 份模型配置的不可用保护，下一次调用会重新出网", cleared)
    return cleared


def forget_all() -> None:
    """清空登记表。只给测试用：用例之间不该共享上一个用例留下的状态。"""
    _GUARDS.clear()


# ── 套在客户端上 ──────────────────────────────────────────────────────────────
#
# 每次请求结束顺手发一条 model_call 事件（app/events/model_calls.py）：所有对话模型请求都
# 经过这一层，包括被保护挡下、根本没出网的。日志关着时那一步什么都不做。

class _GuardedStream:
    """`async with client.messages.stream(...)` 的那一层。请求是在进入时才发的。"""

    def __init__(self, factory: Callable[[], Any], guard: LLMGuard, kwargs: dict[str, Any]):
        self._factory = factory
        self._guard = guard
        self._kwargs = kwargs
        self._estimate = 0
        self._manager: Any = None
        self._call = ModelCall(guard.provider, kwargs, stream=True)
        self._stream: RecordedStream | None = None

    async def __aenter__(self) -> Any:
        try:
            self._guard.check()
            # 准入控制（core/admission）：拿到令牌才出网，等不起就在本地判过载
            self._estimate = admission.estimate_tokens(self._kwargs)
            await admission.current().acquire(self._guard.bucket, self._estimate)
        except LLMUnavailableError as ex:
            self._call.finish(status="blocked", sent=False, error=ex, reason=ex.reason)
            raise
        self._manager = self._factory()
        try:
            inner = await self._manager.__aenter__()
        except Exception as ex:
            blocked = self._guard.record_failure(ex)
            self._call.finish(status="error", sent=True, error=ex, reason=blocked.reason if blocked else "")
            if blocked is not None:
                raise blocked from ex
            raise
        self._stream = RecordedStream(inner, self._call)
        return self._stream

    async def __aexit__(self, exc_type, exc, tb) -> Any:
        blocked = None
        if exc is None:
            self._guard.record_success()
        elif isinstance(exc, Exception):
            blocked = self._guard.record_failure(exc)
        try:
            suppressed = await self._manager.__aexit__(exc_type, exc, tb)
        finally:
            # 调用方没取最终消息（中途出错）时没有用量，照实记 null
            final = self._stream.final if self._stream is not None else None
            await admission.current().settle(self._guard.bucket, self._estimate,
                                             admission.usage_tokens(final))
            if exc is None:
                self._call.finish(status="ok", sent=True, message=final)
            else:
                self._call.finish(status="error", sent=True, message=final, error=exc,
                                  reason=blocked.reason if blocked else "")
        if blocked is not None:
            raise blocked from exc
        return suppressed


class _GuardedMessages:
    def __init__(self, inner: Any, guard: LLMGuard):
        self._inner = inner
        self._guard = guard

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def _call(self, method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        call = ModelCall(self._guard.provider, kwargs, stream=False)
        estimate = admission.estimate_tokens(kwargs)
        try:
            self._guard.check()
            # 准入控制（core/admission）：拿到令牌才出网，等不起就在本地判过载
            await admission.current().acquire(self._guard.bucket, estimate)
        except LLMUnavailableError as ex:
            call.finish(status="blocked", sent=False, error=ex, reason=ex.reason)
            raise
        try:
            result = await method(*args, **kwargs)
        except Exception as ex:
            blocked = self._guard.record_failure(ex)
            call.finish(status="error", sent=True, error=ex, reason=blocked.reason if blocked else "")
            if blocked is not None:
                raise blocked from ex
            raise
        except BaseException as ex:       # 被取消：照实记一条出错，不算凭证的事
            call.finish(status="error", sent=True, error=ex)
            raise
        self._guard.record_success()
        call.finish(status="ok", sent=True, message=result)
        await admission.current().settle(self._guard.bucket, estimate, admission.usage_tokens(result))
        return result

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call(self._inner.create, *args, **kwargs)

    async def parse(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call(self._inner.parse, *args, **kwargs)

    def stream(self, *args: Any, **kwargs: Any) -> _GuardedStream:
        return _GuardedStream(lambda: self._inner.stream(*args, **kwargs), self._guard, kwargs)


class GuardedLLMClient:
    """
    带保护的客户端。调用点照旧写 `client.messages.create/parse/stream`，
    其余属性原样透给里面那个 SDK 客户端。
    """

    def __init__(self, inner: Any, guard: LLMGuard):
        self.unguarded = inner
        self.guard = guard
        self.messages = _GuardedMessages(inner.messages, guard)

    def __getattr__(self, name: str) -> Any:
        if name == "unguarded":       # 还没初始化完就被探属性（copy 之类）：别递归
            raise AttributeError(name)
        return getattr(self.unguarded, name)
