"""
模型凭证不可用保护（core/llm_guard）。

这一组不发任何真实请求：要么直接喂状态错误给保护本身，要么把 SDK 接到
conftest 里的 FakeRelay 上——SDK 走完自己的流程，但不开套接字。

要证明的几件事：
  - 一把凭证被明确拒绝之后，同一份配置不再出网；
  - 状态按"供应商 + 地址 + 凭证"隔离，别的配置不受牵连；
  - 普通 429 只是有限退避，到期自己放行，永远不会变成"一直挡"；
  - 403 和额度用尽看错误体说了什么，说不清的不当成永久；
  - 明文凭证不进日志、不进任何对外的输出。
"""
import json
import logging
from types import SimpleNamespace

import pytest

from app.core import llm_guard
from app.core.llm_guard import (
    FORBIDDEN_BACKOFF_S,
    RATE_LIMIT_BACKOFF_S,
    REASON_CREDENTIAL,
    REASON_FORBIDDEN,
    REASON_PERMISSION,
    REASON_QUOTA,
    REASON_RATE_LIMIT,
    RETRY_AFTER_CAP_S,
    GuardedLLMClient,
    LLMGuard,
    LLMUnavailableError,
    classify,
    guard_for,
)
from app.core.llm_utils import create_llm_client

URL = "https://relay-a.example"
DISABLED = "sk-disabled-0123456789abcdef"
GOOD = "sk-good-fedcba9876543210"


class _StatusError(Exception):
    """长得像 SDK 状态错误的东西：保护只认 status_code / body / response.headers。"""

    def __init__(self, status_code, body=None, headers=None):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.body = body
        self.response = SimpleNamespace(headers=headers or {})


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


async def _ask(client):
    return await client.messages.create(
        model="claude-test", max_tokens=16, messages=[{"role": "user", "content": "hi"}])


# ── 错误语义 ──────────────────────────────────────────────────────────────────

ANTHROPIC_PERMISSION = {"type": "error", "error": {
    "type": "permission_error", "message": "Your API key does not have permission to use this resource."}}
OPENAI_QUOTA = {"error": {"type": "insufficient_quota", "code": "insufficient_quota",
                          "message": "You exceeded your current quota."}}


@pytest.mark.parametrize("status, body, reason", [
    # 401 不看错误体：Key 无效、停用、吊销都是它
    (401, None, REASON_CREDENTIAL),
    (401, {"error": "Invalid API key", "message": "API key is disabled"}, REASON_CREDENTIAL),
    # 403：错误体明说是这把凭证/这个账号不被允许 → 等人处理
    (403, ANTHROPIC_PERMISSION, REASON_PERMISSION),
    (403, {"error": {"message": "该令牌已被禁用"}}, REASON_PERMISSION),
    # 403：说不清原因（网关/WAF 的 HTML 页、空体）→ 只是有限退避
    (403, "<html><body>403 Forbidden</body></html>", REASON_FORBIDDEN),
    (403, None, REASON_FORBIDDEN),
    (403, {"error": {"message": "request blocked"}}, REASON_FORBIDDEN),
    # 额度用尽：402，或错误体明说——哪怕状态码是 403/429
    (402, None, REASON_QUOTA),
    (429, OPENAI_QUOTA, REASON_QUOTA),
    (403, {"error": {"message": "账户余额不足，请充值"}}, REASON_QUOTA),
    (400, {"type": "error", "error": {"type": "invalid_request_error",
           "message": "Your credit balance is too low to access the Anthropic API."}}, REASON_QUOTA),
    # 普通 429
    (429, {"type": "error", "error": {"type": "rate_limit_error", "message": "Too many requests"}},
     REASON_RATE_LIMIT),
    (429, None, REASON_RATE_LIMIT),
])
def test_failures_are_classified_by_what_the_error_actually_says(status, body, reason):
    assert classify(_StatusError(status, body)).reason == reason


@pytest.mark.parametrize("ex", [
    _StatusError(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}),
    _StatusError(404), _StatusError(413), _StatusError(500), _StatusError(529),
    TimeoutError("timed out"), RuntimeError("boom"),
])
def test_failures_unrelated_to_the_credential_are_left_alone(ex):
    """超时、5xx、请求本身写错：不是凭证的事，保护不管，调用方拿到的还是原异常。"""
    guard = LLMGuard("test")
    assert classify(ex) is None
    assert guard.record_failure(ex) is None
    assert guard.blocked_reason() == ""


# ── 等人处理的那一类：一直挡到解除 ────────────────────────────────────────────

@pytest.mark.parametrize("ex, reason", [
    (_StatusError(401), REASON_CREDENTIAL),
    (_StatusError(403, ANTHROPIC_PERMISSION), REASON_PERMISSION),
    (_StatusError(402), REASON_QUOTA),
])
def test_explicit_rejections_block_until_reset_however_long_we_wait(ex, reason):
    clock = _Clock()
    guard = LLMGuard("test", clock=clock)

    raised = guard.record_failure(ex)
    assert (raised.reason, raised.sent, raised.retry_after_s) == (reason, True, None)

    clock.advance(30 * 24 * 3600)                     # 等一个月也不会自己好
    with pytest.raises(LLMUnavailableError) as blocked:
        guard.check()
    assert (blocked.value.reason, blocked.value.sent) == (reason, False)
    assert guard.status() == {"available": False, "unavailable_reason": reason,
                              "retry_after_s": None, "blocked_calls": 1}

    assert guard.reset() is True
    guard.check()                                      # 解除之后放行
    assert guard.status()["available"] is True


def test_a_late_rate_limit_does_not_downgrade_a_rejected_credential():
    """凭证已经判了无效，同时在途的另一个请求吃到一次 429：不能因此变回"过几秒再试"。"""
    clock = _Clock()
    guard = LLMGuard("test", clock=clock)
    guard.record_failure(_StatusError(401))
    raised = guard.record_failure(_StatusError(429))
    assert raised.reason == REASON_CREDENTIAL
    clock.advance(3600)
    assert guard.blocked_reason() == REASON_CREDENTIAL


def test_a_rate_limit_is_upgraded_when_the_credential_is_then_rejected():
    guard = LLMGuard("test", clock=_Clock())
    guard.record_failure(_StatusError(429))
    guard.record_failure(_StatusError(401))
    assert guard.status()["unavailable_reason"] == REASON_CREDENTIAL
    assert guard.status()["retry_after_s"] is None


# ── 普通 429：有限退避，不永久封锁 ────────────────────────────────────────────

def test_rate_limit_backs_off_then_lets_the_next_real_call_through():
    clock = _Clock()
    guard = LLMGuard("test", clock=clock)
    base, cap = RATE_LIMIT_BACKOFF_S

    raised = guard.record_failure(_StatusError(429))
    assert (raised.reason, raised.retry_after_s) == (REASON_RATE_LIMIT, base)

    clock.advance(base - 0.1)
    with pytest.raises(LLMUnavailableError) as blocked:
        guard.check()
    assert blocked.value.sent is False and blocked.value.retry_after_s == pytest.approx(0.1)

    clock.advance(0.1)                                 # 到期：不用任何人来解除
    guard.check()
    assert guard.status()["available"] is True


def test_rate_limit_backoff_doubles_is_capped_and_is_never_permanent():
    clock = _Clock()
    guard = LLMGuard("test", clock=clock)
    base, cap = RATE_LIMIT_BACKOFF_S

    waits = []
    for _ in range(12):                                # 连着十二次：放行一次、又被限一次
        raised = guard.record_failure(_StatusError(429))
        waits.append(raised.retry_after_s)
        assert raised.retry_after_s is not None        # 始终有到期时间
        clock.advance(raised.retry_after_s)
        assert guard.blocked_reason() == ""            # 每一次都会自己到期

    assert waits[:4] == [base, base * 2, base * 4, base * 8]
    assert max(waits) == cap and waits[-1] == cap

    guard.record_success()                             # 成功一次，从头算
    assert guard.record_failure(_StatusError(429)).retry_after_s == base


def test_requests_rejected_together_count_as_one_strike():
    """同一轮里并发在途的几个请求一起吃到 429，不能一条提问就把退避顶到封顶。"""
    clock = _Clock()
    guard = LLMGuard("test", clock=clock)
    base, _ = RATE_LIMIT_BACKOFF_S
    for _ in range(5):
        guard.record_failure(_StatusError(429))
    assert guard.status()["retry_after_s"] <= base * 2
    clock.advance(base * 2)
    assert guard.record_failure(_StatusError(429)).retry_after_s == base * 2


@pytest.mark.parametrize("headers, expected", [
    ({"retry-after": "120"}, 120.0),                   # 服务端说了就听它的
    ({"retry-after-ms": "2500"}, 2.5),
    ({"retry-after": "99999"}, RETRY_AFTER_CAP_S),     # 但"有限"得是真的有限
    ({"retry-after": "0"}, 1.0),                       # 也不至于 0 秒就再打
    ({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, RATE_LIMIT_BACKOFF_S[0]),
])
def test_rate_limit_honours_retry_after_within_a_ceiling(headers, expected):
    guard = LLMGuard("test", clock=_Clock())
    raised = guard.record_failure(_StatusError(429, headers=headers))
    assert raised.retry_after_s == expected


def test_an_unexplained_403_is_only_a_bounded_backoff():
    clock = _Clock()
    guard = LLMGuard("test", clock=clock)
    base, cap = FORBIDDEN_BACKOFF_S
    raised = guard.record_failure(_StatusError(403, "<html>403 Forbidden</html>"))
    assert (raised.reason, raised.retry_after_s) == (REASON_FORBIDDEN, base)
    clock.advance(base)
    assert guard.blocked_reason() == ""
    for _ in range(10):
        wait = guard.record_failure(_StatusError(403)).retry_after_s
        clock.advance(wait)
    assert wait == cap


# ── 接在客户端上：被拒之后不再出网 ────────────────────────────────────────────

async def test_after_one_rejection_the_same_configuration_stops_sending(relay):
    relay.reject(DISABLED)
    client = create_llm_client(DISABLED, URL)
    assert isinstance(client, GuardedLLMClient)

    with pytest.raises(LLMUnavailableError) as first:
        await _ask(client)
    assert (first.value.reason, first.value.status_code, first.value.sent) == (
        REASON_CREDENTIAL, 401, True)
    assert relay.sent() == 1

    # create / parse / stream 三个入口，以及同一份配置的另一个客户端对象
    twin = create_llm_client(DISABLED, URL)
    for attempt in (
        lambda: _ask(client),
        lambda: _ask(twin),
        lambda: client.messages.parse(model="m", max_tokens=16, output_format=dict,
                                      messages=[{"role": "user", "content": "hi"}]),
    ):
        with pytest.raises(LLMUnavailableError) as later:
            await attempt()
        assert later.value.sent is False
    with pytest.raises(LLMUnavailableError):
        async with client.messages.stream(model="m", max_tokens=16,
                                          messages=[{"role": "user", "content": "hi"}]):
            pytest.fail("被挡的流不该进得来")

    assert relay.sent() == 1                           # 仍然只有最初那一次
    assert client.guard.status()["blocked_calls"] == 4


async def test_the_sdk_does_not_retry_an_auth_failure_on_its_own(relay):
    """默认 LLM_MAX_RETRIES=2 时，401/403 也只发一次——SDK 只重试 408/409/429/5xx。"""
    relay.max_retries = 2
    relay.reject(DISABLED, status=401)
    relay.reject("sk-forbidden", status=403, body=ANTHROPIC_PERMISSION)
    for key in (DISABLED, "sk-forbidden"):
        with pytest.raises(LLMUnavailableError):
            await _ask(create_llm_client(key, URL))
    assert (relay.sent(DISABLED), relay.sent("sk-forbidden")) == (1, 1)


async def test_a_rejected_stream_trips_the_guard_too(relay):
    relay.reject(DISABLED)
    client = create_llm_client(DISABLED, URL)
    with pytest.raises(LLMUnavailableError) as first:
        async with client.messages.stream(model="m", max_tokens=16,
                                          messages=[{"role": "user", "content": "hi"}]):
            pytest.fail("被拒的流不该进得来")
    assert first.value.sent is True
    with pytest.raises(LLMUnavailableError):
        await _ask(client)
    assert relay.sent() == 1


async def test_configurations_do_not_affect_each_other(relay):
    """同地址换一把 Key、同一把 Key 换一个地址，都是另一份状态。"""
    relay.reject(DISABLED, host="relay-a.example")

    with pytest.raises(LLMUnavailableError):
        await _ask(create_llm_client(DISABLED, URL))

    same_address_other_key = create_llm_client(GOOD, URL)
    same_key_other_address = create_llm_client(DISABLED, "https://relay-b.example")
    await _ask(same_address_other_key)
    await _ask(same_key_other_address)
    assert same_address_other_key.guard.status()["available"] is True
    assert same_key_other_address.guard.status()["available"] is True

    # 被拒的那份还挡着；地址的大小写和结尾斜杠不构成"另一份配置"
    with pytest.raises(LLMUnavailableError) as still:
        await _ask(create_llm_client(DISABLED, "https://RELAY-A.example/"))
    assert still.value.sent is False
    assert (relay.sent(DISABLED, "relay-a.example"), relay.sent(GOOD), relay.sent(host="relay-b.example")) \
        == (1, 1, 1)


def test_the_provider_is_part_of_the_configuration():
    assert guard_for("anthropic", URL, DISABLED) is guard_for("anthropic", URL + "/", DISABLED)
    assert guard_for("anthropic", URL, DISABLED) is not guard_for("openai", URL, DISABLED)
    assert guard_for("anthropic", URL, DISABLED) is not guard_for("anthropic", None, DISABLED)


async def test_failures_that_are_not_about_the_credential_pass_through_unchanged(relay):
    relay.reject(GOOD, status=500, body={"type": "error", "error": {
        "type": "api_error", "message": "upstream exploded"}})
    client = create_llm_client(GOOD, URL)
    for _ in range(3):
        with pytest.raises(Exception) as raised:
            await _ask(client)
        assert not isinstance(raised.value, LLMUnavailableError)
        assert raised.value.status_code == 500
    assert relay.sent() == 3                           # 每次都照常发出去
    assert client.guard.status()["available"] is True


async def test_a_rate_limited_configuration_recovers_without_anyone_stepping_in(relay):
    relay.reject(GOOD, status=429, body={"type": "error", "error": {
        "type": "rate_limit_error", "message": "slow down"}})
    client = create_llm_client(GOOD, URL)
    clock = client.guard._clock = _Clock()             # 不真的睡：把这份保护的钟换掉

    with pytest.raises(LLMUnavailableError) as limited:
        await _ask(client)
    assert (limited.value.reason, limited.value.sent) == (REASON_RATE_LIMIT, True)
    with pytest.raises(LLMUnavailableError):
        await _ask(client)
    assert relay.sent() == 1                           # 退避期内不发

    relay.allow(GOOD)                                  # 对面缓过来了
    clock.advance(RATE_LIMIT_BACKOFF_S[0])
    await _ask(client)                                 # 到期后的下一次真实调用自己去试
    assert relay.sent() == 2
    assert client.guard.status() == {"available": True, "unavailable_reason": "",
                                     "retry_after_s": None, "blocked_calls": 1}


async def test_reset_lets_the_next_real_call_try_again(relay):
    """恢复不靠定时探测：人处理完凭证之后解除保护，下一次真实调用自己去试。"""
    relay.reject(DISABLED)
    client = create_llm_client(DISABLED, URL)
    with pytest.raises(LLMUnavailableError):
        await _ask(client)
    with pytest.raises(LLMUnavailableError):
        await _ask(client)
    assert relay.sent() == 1

    assert llm_guard.reset_all() == 1
    assert llm_guard.reset_all() == 0                  # 没有在挡的就不算
    with pytest.raises(LLMUnavailableError) as again:  # 还没修好：这一次真的发了，又被拒
        await _ask(client)
    assert again.value.sent is True and relay.sent() == 2

    relay.allow(DISABLED)                              # 修好了
    llm_guard.reset_all()
    await _ask(client)
    assert relay.sent() == 3 and client.guard.status()["available"] is True


# ── 明文凭证不出现在任何地方 ──────────────────────────────────────────────────

async def test_the_credential_never_appears_in_logs_errors_or_status(relay, caplog):
    caplog.set_level(logging.DEBUG, logger="app")
    relay.reject(DISABLED)
    client = create_llm_client(DISABLED, URL)

    errors = []
    for _ in range(2):
        with pytest.raises(LLMUnavailableError) as raised:
            await _ask(client)
        errors.append(raised.value)
    llm_guard.reset_all()

    ours = [r for r in caplog.records if r.name.startswith("app.")]
    assert any("模型服务不可用" in r.getMessage() for r in ours)      # 确实记了日志
    exposed = "\n".join([
        *(r.getMessage() for r in ours),
        *(str(e) for e in errors), *(repr(e) for e in errors),
        repr(client.guard), json.dumps(client.guard.status()),
        repr(sorted(llm_guard._GUARDS)),                              # 登记表的键
    ])
    assert DISABLED not in exposed
    assert "relay-a.example" in client.guard.label                    # 日志里只认得出主机名
    assert "relay-a.example" not in json.dumps(client.guard.status())  # 对外状态连地址都不带


async def test_blocked_calls_are_logged_once_not_once_per_call(relay, caplog):
    """保护期间被挡下的每一次调用不再各占一条 error——那正是停机时刷屏的样子。"""
    caplog.set_level(logging.INFO, logger="app.core.llm_guard")
    relay.reject(DISABLED)
    client = create_llm_client(DISABLED, URL)
    for _ in range(20):
        with pytest.raises(LLMUnavailableError):
            await _ask(client)
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1
