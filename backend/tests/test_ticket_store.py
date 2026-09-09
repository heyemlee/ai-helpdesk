"""转人工工单存储的行为测试。用 tmp_path 里的 SQLite，无外部依赖。"""
import pytest

from app.tickets.ticket_store import TicketStore


@pytest.fixture
def store(tmp_path):
    return TicketStore(db_path=str(tmp_path / "tickets.db"))


async def test_create_and_get_roundtrip(store):
    ticket = await store.create(
        conv_id="c1",
        user_id="alice",
        reason="意图置信度 0.30 低于 0.45",
        request_id="req-1",
        intent="other",
        answer_confidence=0.3,
        last_message="帮我看看那个东西",
    )
    assert ticket.id.startswith("tk_")
    assert ticket.status == "open"

    loaded = await store.get(ticket.id)
    assert loaded is not None
    assert loaded.conv_id == "c1"
    assert loaded.user_id == "alice"
    assert loaded.request_id == "req-1"
    assert loaded.reason.startswith("意图置信度")


async def test_get_missing_returns_none(store):
    assert await store.get("tk_missing") is None


async def test_status_machine_allows_valid_transitions(store):
    t = await store.create(conv_id="c1", user_id="u", reason="r")
    t = await store.update_status(t.id, "assigned")
    assert t.status == "assigned"
    t = await store.update_status(t.id, "resolved")
    assert t.status == "resolved"


async def test_status_machine_rejects_reopening_resolved(store):
    """resolved 是终态：不允许流转回 open/assigned。"""
    t = await store.create(conv_id="c1", user_id="u", reason="r")
    await store.update_status(t.id, "resolved")
    with pytest.raises(ValueError):
        await store.update_status(t.id, "open")


async def test_status_machine_rejects_unknown_status(store):
    t = await store.create(conv_id="c1", user_id="u", reason="r")
    with pytest.raises(ValueError):
        await store.update_status(t.id, "in_review")


async def test_update_missing_ticket_raises_keyerror(store):
    with pytest.raises(KeyError):
        await store.update_status("tk_missing", "assigned")


async def test_list_filters_by_status_and_orders_desc(store):
    a = await store.create(conv_id="c1", user_id="u", reason="first")
    b = await store.create(conv_id="c2", user_id="u", reason="second")
    await store.update_status(a.id, "resolved")

    open_tickets = await store.list(status="open")
    assert [t.id for t in open_tickets] == [b.id]

    all_tickets = await store.list()
    assert len(all_tickets) == 2


async def test_stats_counts_by_status(store):
    a = await store.create(conv_id="c1", user_id="u", reason="r")
    await store.create(conv_id="c2", user_id="u", reason="r")
    await store.update_status(a.id, "resolved")

    stats = await store.stats()
    assert stats["open"] == 1
    assert stats["resolved"] == 1
    assert stats["total"] == 2


async def test_persistence_across_instances(tmp_path):
    """同一个 db 文件换一个 Store 实例仍能读到——handoff 不再随进程消失。"""
    path = str(tmp_path / "tickets.db")
    t = await TicketStore(db_path=path).create(conv_id="c1", user_id="u", reason="r")
    reloaded = await TicketStore(db_path=path).get(t.id)
    assert reloaded is not None
    assert reloaded.id == t.id


async def test_long_last_message_is_truncated(store):
    t = await store.create(conv_id="c1", user_id="u", reason="r", last_message="x" * 2000)
    assert len(t.last_message) == 500
