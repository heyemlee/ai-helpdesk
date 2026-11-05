from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from fastapi import HTTPException

from ai_sql_agent.audit import AuditStore
from ai_sql_agent.auth import Principal
from ai_sql_agent.core import get_logger, get_settings, log_event
from ai_sql_agent.core.metrics import LIMIT_REJECTIONS

_logger = get_logger("api.limits")


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class TokenBucketLimiter:
    """
    进程内令牌桶限流。

    内部单实例部署不引 Redis：一个 dict + 一把锁足够。桶容量等于每分钟
    配额，允许短时突发，长期速率收敛到配额。
    """

    def __init__(self, clock=time.monotonic):
        self._buckets: Dict[str, _Bucket] = {}
        self._lock = threading.Lock()
        self._clock = clock

    def acquire(self, key: str, limit_per_minute: int) -> Optional[float]:
        """
        取一个令牌。返回 None 表示放行，返回秒数表示还要等多久。
        """
        if limit_per_minute <= 0:
            return None

        rate = limit_per_minute / 60.0
        now = self._clock()

        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(limit_per_minute), updated_at=now)
                self._buckets[key] = bucket
            else:
                elapsed = max(0.0, now - bucket.updated_at)
                bucket.tokens = min(
                    float(limit_per_minute), bucket.tokens + elapsed * rate
                )
                bucket.updated_at = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return None

            return max(1.0, (1.0 - bucket.tokens) / rate)


class QuotaGuard:
    """
    每日配额。

    统计口径直接来自审计表，重启不丢；为了不让每个请求都打一次库，
    结果缓存 `cache_seconds` 秒——配额是成本控制不是安全边界，
    短暂的少量超发可以接受。
    """

    def __init__(self, audit_store: AuditStore, cache_seconds: float = 15.0):
        self.audit_store = audit_store
        self.cache_seconds = cache_seconds
        self._cache: Dict[str, Tuple[float, int, int]] = {}
        self._lock = threading.Lock()

    def _usage(self, user_id: str) -> Tuple[int, int]:
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(user_id)
            if cached and now - cached[0] < self.cache_seconds:
                return cached[1], cached[2]

        usage = self.audit_store.daily_usage(user_id)

        with self._lock:
            self._cache[user_id] = (now, usage.queries, usage.tokens)
        return usage.queries, usage.tokens

    def invalidate(self, user_id: str) -> None:
        with self._lock:
            self._cache.pop(user_id, None)

    def check(self, principal: Principal) -> None:
        settings = get_settings()

        query_quota = principal.daily_query_quota or settings.default_daily_query_quota
        token_quota = principal.daily_token_quota or settings.default_daily_token_quota

        if query_quota <= 0 and token_quota <= 0:
            return

        queries, tokens = self._usage(principal.user_id)

        if query_quota > 0 and queries >= query_quota:
            _raise_quota(principal, "查询次数", queries, query_quota)

        if token_quota > 0 and tokens >= token_quota:
            _raise_quota(principal, "Token 用量", tokens, token_quota)


class RequestLimiter:
    """把限流与配额组合成一次请求前的准入检查。"""

    def __init__(self, audit_store: AuditStore):
        self.rate_limiter = TokenBucketLimiter()
        self.quota_guard = QuotaGuard(audit_store)

    def check(self, principal: Principal) -> None:
        settings = get_settings()
        limit = (
            principal.rate_limit_per_minute
            or settings.default_rate_limit_per_minute
        )

        retry_after = self.rate_limiter.acquire(principal.caller, limit)
        if retry_after is not None:
            LIMIT_REJECTIONS.inc(reason="rate_limit")
            log_event(
                _logger,
                logging.WARNING,
                "limit.rate_limited",
                caller=principal.caller,
                user_id=principal.user_id,
                limit_per_minute=limit,
                retry_after=round(retry_after, 2),
            )
            raise HTTPException(
                status_code=429,
                detail=f"请求过于频繁（限制 {limit} 次/分钟），请稍后重试。",
                headers={"Retry-After": str(int(retry_after) + 1)},
            )

        self.quota_guard.check(principal)


def _raise_quota(principal: Principal, name: str, used: int, quota: int) -> None:
    LIMIT_REJECTIONS.inc(reason="daily_quota")
    log_event(
        _logger,
        logging.WARNING,
        "limit.quota_exceeded",
        caller=principal.caller,
        user_id=principal.user_id,
        kind=name,
        used=used,
        quota=quota,
    )
    raise HTTPException(
        status_code=429,
        detail=f"今日{name}已达上限（{used}/{quota}），请明天再试或联系管理员调整配额。",
    )
