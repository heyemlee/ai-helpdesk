"""用户令牌与限流的行为测试。不发真实网络请求。"""
import time

import pytest

from core.auth import AuthError, RateLimiter, UserTokenAuth

# ── UserTokenAuth ─────────────────────────────────────────────────────────────

def test_demo_mode_trusts_claimed_user_id():
    """未配置 secret 时保持旧行为：信任请求体，空值落到 anonymous。"""
    auth = UserTokenAuth(secret="")
    assert not auth.enabled
    assert auth.verify(None, "alice") == "alice"
    assert auth.verify(None, None) == "anonymous"
    assert auth.verify("whatever.1.2", "") == "anonymous"


def test_issue_and_verify_roundtrip():
    auth = UserTokenAuth(secret="unit-test-secret")
    token = auth.issue("alice")
    assert auth.verify(token) == "alice"
    # 请求体不带 user_id 或带 anonymous 时都以令牌为准
    assert auth.verify(token, "anonymous") == "alice"
    assert auth.verify(token, "alice") == "alice"


def test_verify_rejects_mismatched_user_id():
    """请求体声称的 user_id 与令牌不符必须拒绝——这是冒用意图，不能静默覆盖。"""
    auth = UserTokenAuth(secret="unit-test-secret")
    token = auth.issue("alice")
    with pytest.raises(AuthError):
        auth.verify(token, "bob")


def test_verify_rejects_tampered_signature():
    auth = UserTokenAuth(secret="unit-test-secret")
    user_id, expires_at, sig = auth.issue("alice").split(".")
    forged = f"bob.{expires_at}.{sig}"  # 换 user_id 不换签名
    with pytest.raises(AuthError):
        auth.verify(forged)


def test_verify_rejects_expired_token():
    auth = UserTokenAuth(secret="unit-test-secret")
    token = auth.issue("alice", ttl_s=-1)
    with pytest.raises(AuthError):
        auth.verify(token)


def test_verify_rejects_missing_or_malformed_token():
    auth = UserTokenAuth(secret="unit-test-secret")
    for bad in (None, "", "no-dots", "a.b", "a.b.c.d", "alice.notanumber.sig"):
        with pytest.raises(AuthError):
            auth.verify(bad)


def test_secrets_are_isolated():
    """一个环境签的令牌在另一个环境不可用。"""
    token = UserTokenAuth(secret="secret-a").issue("alice")
    with pytest.raises(AuthError):
        UserTokenAuth(secret="secret-b").verify(token)


def test_issue_rejects_invalid_user_id():
    auth = UserTokenAuth(secret="s")
    with pytest.raises(AuthError):
        auth.issue("")
    with pytest.raises(AuthError):
        auth.issue("a.b")  # '.' 是令牌分隔符


def test_issue_requires_secret():
    with pytest.raises(AuthError):
        UserTokenAuth(secret="").issue("alice")


# ── RateLimiter（进程内退化路径）──────────────────────────────────────────────

async def test_rate_limiter_disabled_by_default():
    limiter = RateLimiter(limit_per_window=0)
    assert not limiter.enabled
    for _ in range(100):
        assert await limiter.check("alice")


async def test_rate_limiter_blocks_over_limit():
    limiter = RateLimiter(limit_per_window=3, window_s=60)
    assert await limiter.check("alice")
    assert await limiter.check("alice")
    assert await limiter.check("alice")
    assert not await limiter.check("alice")  # 第 4 次拒绝


async def test_rate_limiter_isolates_users():
    limiter = RateLimiter(limit_per_window=1, window_s=60)
    assert await limiter.check("alice")
    assert not await limiter.check("alice")
    assert await limiter.check("bob")  # Bob 不受 Alice 影响


async def test_rate_limiter_resets_after_window(monkeypatch):
    limiter = RateLimiter(limit_per_window=1, window_s=60)
    assert await limiter.check("alice")
    assert not await limiter.check("alice")
    # 时间推进到下一个窗口
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 61)
    assert await limiter.check("alice")


class _BrokenRedis:
    async def incr(self, key):
        raise ConnectionError("redis down")


async def test_rate_limiter_degrades_when_redis_fails():
    """Redis 挂了限流退化为进程内计数，语义降级但不失效。"""
    limiter = RateLimiter(redis_client=_BrokenRedis(), limit_per_window=2, window_s=60)
    assert await limiter.check("alice")
    assert await limiter.check("alice")
    assert not await limiter.check("alice")
