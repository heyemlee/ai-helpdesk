"""
模型访问状态：哪一组「服务地址 + 凭证」已经被模型服务明确拒绝，后面的调用不必再发。

为什么需要这一层。鉴权失败本来就不重试，但也没有任何地方**记住**它：
一把 Key 被停用之后，每个请求照样先让路由调一次、抽词调一次、规划再调一次，
后台摘要每一轮再试一次，基准脚本逐题照跑——2026-10-04 实际发生过，Key 停用后的
两分半里又发出了约 650 次注定被拒的请求。熔断器管不了这件事：它数的是基础设施类失败
（超时、5xx），而且以前把鉴权失败记成了「成功」。

这里记三种状态，都只存在于本进程的内存里：

| 状态 | 触发 | 范围 | 何时解除 |
| --- | --- | --- | --- |
| 凭证被拒 | 401；403 且响应明确说凭证无效 / 停用 | 服务地址 + 凭证 | 重启，或管理员复位 |
| 额度耗尽 | 402；响应明确说额度 / 余额用尽 | 服务地址 + 凭证 | 重启，或管理员复位 |
| 模型无权限 | 403 且响应明确说无权使用该模型 | 服务地址 + 凭证 + 模型 | 重启，或管理员复位 |
| 暂缓 | 429 带了等不起的 Retry-After；语义不明的 403 | 服务地址 + 凭证 + 模型 | 到时间自动解除 |

**隔离。** 键是（服务地址，协议，凭证指纹）。换一个地址、换一把 Key、换一种协议，
互不影响；「模型无权限」和「暂缓」再细到模型，不会因为一个模型被拒就停掉别的模型。

**不存、不输出明文 Key。** 凭证只以指纹参与比较：进程启动时取一段随机盐，
对 Key 做带盐的 BLAKE2。指纹不可逆，换个进程就不同，拿它反推或者跨进程比对都做不到。
对外展示时服务地址也只给不透明的标识，不给主机名。

**不主动探测。** 永久性的拒绝只由「重启」或管理员入口（`POST /admin/circuit/reset`）解除，
不会自己发请求去试凭证好了没有；暂缓到点之后，是下一个真实请求自己去试。

**拦不住已经发出去的请求。** 判定发生在请求发出之前。并发时，第一个拒绝回来之前已经
发出去的那几个请求收不回来——能保证的是：拒绝一旦记下，之后不会再有新的请求发出。
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

SCOPE_CREDENTIAL = "credential"
SCOPE_MODEL = "model"

REASON_CREDENTIAL_REJECTED = "credential_rejected"
REASON_QUOTA_EXHAUSTED = "quota_exhausted"
REASON_PERMISSION_DENIED = "permission_denied"
REASON_FORBIDDEN = "forbidden"
REASON_RATE_LIMITED = "rate_limited"

MAX_HOLD_SECONDS = 300.0
"""
暂缓的上限。服务端说「一小时后再来」也只按这么久算：到点放一个真实请求过去，
还在限流就再暂缓一轮。暂缓永远不会变成永久封锁。
"""

AMBIGUOUS_FORBIDDEN_HOLD_SECONDS = 60.0
"""
语义不明的 403（网关拦截页、地区限制、没说原因的拒绝）暂缓多久。
它可能是确定性的，也可能过一会儿就好；不敢当成永久，也不该让同一个请求的
后续阶段立刻再撞一次。
"""

_SALT = os.urandom(16)


def _digest(text: str, size: int) -> str:
    return hashlib.blake2b(text.encode("utf-8"), key=_SALT, digest_size=size).hexdigest()


def credential_fingerprint(api_key: str) -> str:
    """凭证指纹。只用于进程内比较与展示，不可逆。空凭证也有一个确定的指纹。"""
    return "k-" + _digest(api_key or "", 6)


def endpoint_of(url: str) -> str:
    """请求地址 → 服务地址（协议 + 主机 + 端口）。路径不算：同一个服务的不同接口共用一把凭证。"""
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    if not host:
        return (url or "").strip().lower()
    port = f":{parts.port}" if parts.port else ""
    return f"{(parts.scheme or 'https').lower()}://{host}{port}"


@dataclass(frozen=True)
class AccessKey:
    """一组「服务地址 + 协议 + 凭证」。凭证只以指纹出现。"""

    endpoint: str
    provider: str
    credential: str

    @classmethod
    def build(cls, url: str, provider: str, api_key: str) -> "AccessKey":
        return cls(
            endpoint=endpoint_of(url),
            provider=(provider or "openai").lower(),
            credential=credential_fingerprint(api_key),
        )

    @property
    def endpoint_id(self) -> str:
        return "e-" + _digest(self.endpoint, 4)


@dataclass
class AccessBlock:
    """一条已知的不可用状态。"""

    scope: str
    reason: str
    permanent: bool
    model: str = ""
    """范围是模型时，是哪个模型；范围是凭证时为空。"""

    since: float = 0.0
    """记下这条状态的墙上时间（time.time()），只用于展示。"""

    until: Optional[float] = None
    """暂缓到什么时候（单调时钟）；永久性的为 None。"""

    status_code: Optional[int] = None
    component: str = ""
    """第一次撞上它的组件。"""

    short_circuited: int = 0
    """之后有多少次调用因此没有发出去。"""

    def remaining(self, now: float) -> float:
        if self.until is None:
            return 0.0
        return max(0.0, self.until - now)


class ModelAccessRegistry:
    """进程内的模型访问状态表。线程安全。"""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ):
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._blocks: Dict[Tuple[AccessKey, str], AccessBlock] = {}

    def now(self) -> float:
        """状态表用的单调时钟的当前读数（算「暂缓还剩多久」用）。"""
        return self._clock()

    # ------------------------------------------------------------------
    # 发请求之前问
    # ------------------------------------------------------------------

    def check(self, key: AccessKey, model: str = "") -> Optional[AccessBlock]:
        """
        这次调用该不该发。返回挡住它的那条状态；可以发返回 None。

        凭证级的状态挡住这组凭证的所有模型；模型级的只挡同一个模型。
        到期的暂缓在这里顺手清掉。
        """
        now = self._clock()
        slots = ["", model] if model else [""]
        with self._lock:
            for slot in slots:
                entry = self._blocks.get((key, slot))
                if entry is None:
                    continue
                if not entry.permanent and entry.remaining(now) <= 0:
                    del self._blocks[(key, slot)]
                    continue
                entry.short_circuited += 1
                return entry
        return None

    # ------------------------------------------------------------------
    # 收到响应之后记
    # ------------------------------------------------------------------

    def block(
        self,
        key: AccessKey,
        *,
        scope: str,
        reason: str,
        model: str = "",
        status_code: Optional[int] = None,
        component: str = "",
    ) -> AccessBlock:
        """记一条永久性的不可用状态（直到重启或管理员复位）。已经有了就保留最早的那条。"""
        slot = model if scope == SCOPE_MODEL else ""
        with self._lock:
            existing = self._blocks.get((key, slot))
            if existing is not None and existing.permanent:
                return existing
            entry = AccessBlock(
                scope=scope,
                reason=reason,
                permanent=True,
                model=slot,
                since=self._wall_clock(),
                status_code=status_code,
                component=component,
            )
            self._blocks[(key, slot)] = entry
            return entry

    def hold(
        self,
        key: AccessKey,
        *,
        reason: str,
        seconds: float,
        model: str = "",
        status_code: Optional[int] = None,
        component: str = "",
    ) -> Optional[AccessBlock]:
        """
        暂缓：这组凭证的这个模型在一段时间内不再发请求，到点自动解除。

        时长封顶 `MAX_HOLD_SECONDS`。已经有永久状态时不动它。
        """
        duration = max(0.0, min(float(seconds), MAX_HOLD_SECONDS))
        if duration <= 0:
            return None
        slot = model or ""
        with self._lock:
            existing = self._blocks.get((key, slot))
            if existing is not None and existing.permanent:
                return existing
            until = self._clock() + duration
            if existing is not None and existing.until is not None and existing.until >= until:
                return existing
            entry = AccessBlock(
                scope=SCOPE_MODEL,
                reason=reason,
                permanent=False,
                model=slot,
                since=self._wall_clock(),
                until=until,
                status_code=status_code,
                component=component,
            )
            self._blocks[(key, slot)] = entry
            return entry

    def record_success(self, key: AccessKey, model: str = "") -> None:
        """一次调用成功了：这个模型上的暂缓可以提前解除。永久状态不因此解除。"""
        slot = model or ""
        with self._lock:
            entry = self._blocks.get((key, slot))
            if entry is not None and not entry.permanent:
                del self._blocks[(key, slot)]

    # ------------------------------------------------------------------
    # 展示与复位
    # ------------------------------------------------------------------

    def snapshot(self) -> List[Dict[str, Any]]:
        """
        当前已知的状态，给状态接口和日志用。

        不含 Key，不含主机名：服务地址与凭证都只给不透明的标识。到期的暂缓不列出。
        """
        now = self._clock()
        with self._lock:
            items = [
                (key, entry)
                for (key, _slot), entry in self._blocks.items()
                if entry.permanent or entry.remaining(now) > 0
            ]
        return [
            {
                "endpoint": key.endpoint_id,
                "provider": key.provider,
                "credential": key.credential,
                "scope": entry.scope,
                "model": entry.model,
                "reason": entry.reason,
                "permanent": entry.permanent,
                "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(entry.since)),
                "retry_in_seconds": None if entry.permanent else round(entry.remaining(now), 1),
                "status_code": entry.status_code,
                "first_seen_by": entry.component,
                "short_circuited_calls": entry.short_circuited,
            }
            for key, entry in sorted(
                items, key=lambda item: (item[0].endpoint, item[0].credential, item[1].model)
            )
        ]

    def permanent_blocks(self) -> List[Dict[str, Any]]:
        """只列永久性的（凭证被拒、额度耗尽、模型无权限）。基准入口据此中止。"""
        return [item for item in self.snapshot() if item["permanent"]]

    def reset_all(self) -> int:
        """清掉所有状态，返回清掉了几条。只给测试、重启之外的管理员复位用。"""
        with self._lock:
            count = len(self._blocks)
            self._blocks.clear()
        return count


MODEL_ACCESS = ModelAccessRegistry()
"""进程内唯一的状态表。统一调用入口（`ModelTransport`）默认用它。"""
