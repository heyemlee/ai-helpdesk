"""对话审计日志的行为测试。tmp_path SQLite，无外部依赖。"""
import time

import pytest

from storage.conversation_log import ConversationLog


@pytest.fixture
def log(tmp_path):
    return ConversationLog(db_path=str(tmp_path / "conversations.db"))


async def test_append_and_read_back_in_order(log):
    await log.append("c1", "alice", "user", "我要补件", request_id="req-1")
    await log.append("c1", "alice", "assistant", "好的，正在处理", request_id="req-1", agent_type="billing")
    await log.append("c1", "alice", "user", "多久到账？", request_id="req-2")

    entries = await log.get_conversation("c1")
    assert [e.role for e in entries] == ["user", "assistant", "user"]
    assert entries[0].content == "我要补件"
    assert entries[1].agent_type == "billing"
    assert entries[2].request_id == "req-2"


async def test_conversations_are_isolated(log):
    await log.append("c1", "alice", "user", "hello")
    await log.append("c2", "bob", "user", "world")
    assert len(await log.get_conversation("c1")) == 1
    assert len(await log.get_conversation("c2")) == 1
    assert await log.get_conversation("missing") == []


async def test_stats_counts(log):
    await log.append("c1", "alice", "user", "a")
    await log.append("c1", "alice", "assistant", "b")
    await log.append("c2", "bob", "user", "c")
    stats = await log.stats()
    assert stats == {"messages": 3, "conversations": 2}


async def test_purge_before_removes_only_old_records(log, monkeypatch):
    await log.append("c1", "alice", "user", "old")
    cutoff = time.time() + 1
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 10)
    await log.append("c1", "alice", "user", "new")

    removed = await log.purge_before(cutoff)
    assert removed == 1
    remaining = await log.get_conversation("c1")
    assert [e.content for e in remaining] == ["new"]


async def test_persistence_across_instances(tmp_path):
    """审计日志换实例仍可读——这是"留档"与 Redis TTL 记忆的本质区别。"""
    path = str(tmp_path / "conversations.db")
    await ConversationLog(db_path=path).append("c1", "u", "user", "hello")
    entries = await ConversationLog(db_path=path).get_conversation("c1")
    assert len(entries) == 1
