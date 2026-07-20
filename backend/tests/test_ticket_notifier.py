"""
工单通知的可靠投递。

盯的是同一件事的三个侧面：**"客户被告知已转人工"和"值班的人真的收到了"
之间必须有东西连着**。原来的实现里没有——发一次、异常吞掉、连 HTTP 状态
都不看，失败之后这次通知连"曾经存在过"都没有。
"""
import json
import time

import pytest

from tickets.notifier import MAX_ATTEMPTS, TicketNotifier, backoff_delay, is_permanent
from tickets.ticket_store import TicketStore


@pytest.fixture
def store(tmp_path):
    return TicketStore(db_path=str(tmp_path / "tickets.db"))


async def _ticket(store, **kw):
    return await store.create(
        conv_id=kw.get("conv_id", "c1"), user_id="u1",
        reason=kw.get("reason", "客户要求人工"),
    )


class _FakeHttpx:
    """记录每次调用，按预置脚本返回状态码或抛异常。"""

    def __init__(self, script):
        self._script = list(script)
        self.calls: list[dict] = []

    def AsyncClient(self, **kwargs):  # noqa: N802 - 冒充 httpx 的名字
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, content=None, headers=None):
        self.calls.append({"url": url, "headers": headers or {},
                           "body": json.loads((content or b"{}").decode())})
        outcome = self._script.pop(0) if self._script else 200
        if isinstance(outcome, Exception):
            raise outcome
        return type("R", (), {"status_code": outcome, "text": f"body-{outcome}"})()


@pytest.fixture
def fake_http(monkeypatch):
    def _install(script):
        import httpx
        fake = _FakeHttpx(script)
        monkeypatch.setattr(httpx, "AsyncClient", fake.AsyncClient)
        return fake
    return _install


class TestDeliveryIsVerifiedNotAssumed:
    async def test_2xx_marks_delivered(self, store, fake_http):
        http = fake_http([200])
        n = TicketNotifier(store, "http://hooks.example/t")
        await n.enqueue(await _ticket(store))

        assert len(http.calls) == 1
        assert await store.notify_stats() == {"pending": 0, "delivered": 1, "failed": 0}

    async def test_5xx_is_not_treated_as_sent(self, store, fake_http):
        """
        **这一条是原实现最核心的缺陷。** 它 `await client.post(...)` 之后把返回值
        直接扔掉——500 也算发出去了。过期的 Slack / 企业微信 webhook 正是长期
        返回非 2xx，而外表毫无变化。
        """
        fake_http([500])
        n = TicketNotifier(store, "http://hooks.example/t")
        await n.enqueue(await _ticket(store))

        stats = await store.notify_stats()
        assert stats["delivered"] == 0
        assert stats["pending"] == 1, "非 2xx 必须留在待发送里，不能当成已送达"

    async def test_a_network_error_leaves_a_pending_record(self, store, fake_http):
        fake_http([ConnectionError("connection refused")])
        n = TicketNotifier(store, "http://hooks.example/t")
        await n.enqueue(await _ticket(store))

        due = await store.notify_due(time.time() + 3600)
        assert len(due) == 1 and due[0].attempts == 1
        assert "ConnectionError" in due[0].last_error


class TestRetryAndEscalation:
    async def test_retry_succeeds_on_the_second_attempt(self, store, fake_http):
        http = fake_http([503, 200])
        n = TicketNotifier(store, "http://hooks.example/t")
        await n.enqueue(await _ticket(store))
        assert (await store.notify_stats())["pending"] == 1

        # 退避到期后由补投循环再试一次
        due = await store.notify_due(time.time() + 3600)
        await n._attempt(due[0])

        assert len(http.calls) == 2
        assert (await store.notify_stats())["delivered"] == 1

    async def test_4xx_is_permanent_and_escalates_without_burning_retries(
        self, store, fake_http, caplog
    ):
        """
        4xx 是"这个请求本身有问题"，重试一百次也一样。直接判失败并升级，
        不要拿五次重试去掩盖一个需要人处理的问题。
        """
        import logging
        http = fake_http([404])
        n = TicketNotifier(store, "http://hooks.example/t")
        with caplog.at_level(logging.ERROR, logger="tickets.notifier"):
            await n.enqueue(await _ticket(store))

        assert len(http.calls) == 1, "永久失败不该继续重试"
        assert (await store.notify_stats())["failed"] == 1
        assert any("需要人工介入" in r.message for r in caplog.records)

    @pytest.mark.parametrize("code,permanent", [
        (400, True), (401, True), (403, True), (404, True),
        (408, False), (429, False), (500, False), (502, False), (503, False),
    ])
    def test_which_status_codes_are_worth_retrying(self, code, permanent):
        """408 和 429 是 4xx 里的例外：它们明确是"待会再来"。"""
        assert is_permanent(code) is permanent

    async def test_exhausting_the_attempts_escalates(self, store, fake_http, caplog):
        import logging
        fake_http([503] * (MAX_ATTEMPTS + 2))
        n = TicketNotifier(store, "http://hooks.example/t")
        await n.enqueue(await _ticket(store))

        with caplog.at_level(logging.ERROR, logger="tickets.notifier"):
            for _ in range(MAX_ATTEMPTS):
                due = await store.notify_due(time.time() + 86400)
                if not due:
                    break
                await n._attempt(due[0])

        assert (await store.notify_stats())["failed"] == 1
        assert await store.notify_due(time.time() + 86400) == [], \
            "判失败之后不该继续排队重试——那只会把问题埋得更深"
        assert any("需要人工介入" in r.message for r in caplog.records)

    def test_backoff_grows_and_is_capped(self):
        delays = [backoff_delay(i) for i in range(1, 8)]
        assert delays[0] < delays[1] < delays[2], "要递增，不然重试就是打洪水"
        assert all(d <= 600 * 1.2 + 1 for d in delays), "要封顶"


class TestDeduplication:
    async def test_the_same_ticket_is_only_enqueued_once(self, store, fake_http):
        """
        去重靠唯一索引，**不靠调用方记得只调一次**。

        重复调用是正常的（重试、并发、补投循环和请求路径撞上），
        把去重交给调用点等于交给最容易出错的地方。
        """
        http = fake_http([500, 500])
        n = TicketNotifier(store, "http://hooks.example/t")
        ticket = await _ticket(store)
        await n.enqueue(ticket)
        await n.enqueue(ticket)

        assert len(http.calls) == 1, "第二次入队应当被去重挡掉"
        assert sum((await store.notify_stats()).values()) == 1

    async def test_idempotency_key_is_stable_across_retries(self, store, fake_http):
        """
        重试发的是**同一个事件**，不是新事件——所以幂等键必须稳定，
        接收方才能自己去重。不稳定的话重试会在对面变成两张工单通知。
        """
        http = fake_http([503, 200])
        n = TicketNotifier(store, "http://hooks.example/t")
        await n.enqueue(await _ticket(store))
        due = await store.notify_due(time.time() + 3600)
        await n._attempt(due[0])

        keys = {c["headers"]["Idempotency-Key"] for c in http.calls}
        assert len(keys) == 1, f"两次尝试用了不同的幂等键: {keys}"


class TestOutboxSurvivesRestart:
    async def test_pending_records_are_picked_up_by_a_fresh_notifier(
        self, tmp_path, fake_http
    ):
        """
        进程重启之后积压要能被捡回来。

        原来的实现是一个 fire-and-forget task：进程一关，那次通知连"曾经存在过"
        都没有。先落记录的意义就在这里——记录在库里，换个进程照样能接着发。
        """
        db = str(tmp_path / "tickets.db")
        first = TicketStore(db_path=db)
        fake_http([503])
        n1 = TicketNotifier(first, "http://hooks.example/t")
        await n1.enqueue(await _ticket(first))
        await first.close()

        # 换一个进程（换一个 store 实例 + 一个新的 notifier）
        http = fake_http([200])
        second = TicketStore(db_path=db)
        n2 = TicketNotifier(second, "http://hooks.example/t")
        due = await second.notify_due(time.time() + 3600)
        assert len(due) == 1, "重启后应当还能查到这条待发送记录"
        await n2._attempt(due[0])

        assert len(http.calls) == 1
        assert (await second.notify_stats())["delivered"] == 1
        await second.close()


class TestDisabledIsANoop:
    async def test_no_url_means_no_record_and_no_call(self, store, fake_http):
        """未配置 webhook 时行为与从前一致：只落工单，不留投递记录。"""
        http = fake_http([200])
        n = TicketNotifier(store, "")
        assert n.enabled is False
        await n.enqueue(await _ticket(store))
        assert http.calls == []
        assert await store.notify_stats() == {"pending": 0, "delivered": 0, "failed": 0}
