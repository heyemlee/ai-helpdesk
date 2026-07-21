"""
工作记忆压缩的读-改-写竞态。

压缩中间要等一次模型（几秒），期间同一个会话完全可能又来了新消息。
原来的写法是「读全部 → 等模型 → `DEL` 整个 key → 把读到的最后 5 条推回去」，
于是等待期间新到的消息**既没进摘要也没留在工作记忆里**——用户看到的是
"客服突然不记得我刚说的话"，而日志里什么都没有。

这一组用一个最小的假 Redis 复现那个时间窗：在"等模型"那一刻插进新消息，
然后断言它们还在。
"""
import asyncio

import pytest

from memory.conversation_memory import MemoryManager, MsgRole


class FakeRedis:
    """够用的假 Redis：list + string + SET NX EX。所有方法都是 async。"""

    def __init__(self):
        self.lists: dict[str, list[str]] = {}
        self.strings: dict[str, str] = {}

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    async def lrange(self, key, start, end):
        items = self.lists.get(key, [])
        return items[start:] if end == -1 else items[start:end + 1]

    async def llen(self, key):
        return len(self.lists.get(key, []))

    async def ltrim(self, key, start, end):
        items = self.lists.get(key, [])
        self.lists[key] = items[start:] if end == -1 else items[start:end + 1]
        return True

    async def delete(self, key):
        self.lists.pop(key, None)
        self.strings.pop(key, None)
        return 1

    async def expire(self, key, ttl):
        return True

    async def get(self, key):
        return self.strings.get(key)

    async def setex(self, key, ttl, value):
        self.strings[key] = value
        return True

    async def append(self, key, value):
        self.strings[key] = self.strings.get(key, "") + value
        return len(self.strings[key])

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.strings:
            return None
        self.strings[key] = value
        return True

    async def aclose(self):
        return None


class _Store:
    """情景记忆的假存储，压缩会往里写。"""

    def __init__(self):
        self.docs = []

    async def add(self, *args, **kwargs):
        self.docs.append((args, kwargs))

    async def search(self, *args, **kwargs):
        return []

    async def profile_get(self, *a, **kw):
        return {}

    async def profile_set(self, *a, **kw):
        return None


@pytest.fixture
def manager(monkeypatch):
    m = MemoryManager.__new__(MemoryManager)
    m._redis = FakeRedis()
    m._store = _Store()
    m._embedder = None
    m._client = None
    m._model = "test-model"
    return m


def _install_summariser(manager, on_call=None, text="摘要：客户在问交期"):
    """把压缩里那次 LLM 调用换掉，并在"模型正在跑"的那一刻插一个回调。"""
    async def _fake(*args, **kwargs):
        if on_call is not None:
            await on_call()
        return type("R", (), {"content": [type("B", (), {"type": "text", "text": text})()]})()

    import memory.conversation_memory as mod
    mod.structured_create = _fake
    manager._store_episodic = _episodic_recorder(manager)


def _episodic_recorder(manager):
    async def _store_episodic(user_id, conv_id, text, summary):
        manager._store.docs.append((user_id, conv_id, summary))
    return _store_episodic


async def _fill(manager, n: int, prefix="msg"):
    for i in range(n):
        await manager.add_message("u1", "c1", MsgRole.USER, f"{prefix}-{i}")


async def _seed(manager, n: int, prefix="msg"):
    """直接塞进列表，**不走 add_message**——那条路会自己触发压缩。

    并发用例要的是"列表已经过阈值、然后两个压缩同时进来"这个初始状态。
    """
    import json
    key = manager._wm_key("u1", "c1")
    for i in range(n):
        await manager._redis.lpush(key, json.dumps({
            "role": "user", "content": f"{prefix}-{i}",
            "ts": "2026-09-05T00:00:00", "metadata": {},
        }))


class TestMessagesArrivingDuringCompressionSurvive:
    async def test_a_message_sent_while_the_model_runs_is_not_dropped(self, manager):
        """
        **核心用例。** 压缩等模型的那几秒里客户又说了一句，那句话必须还在。

        原实现在这里会 `DEL` 掉整个 key 再推回旧快照——那句话既不在摘要里
        （摘要是按旧快照做的），也不在工作记忆里。没有报错，没有日志。
        """
        arrived = []

        async def _during_llm():
            await manager.add_message("u1", "c1", MsgRole.USER, "等待期间说的话")
            arrived.append(True)

        _install_summariser(manager, on_call=_during_llm)
        await _fill(manager, MemoryManager.COMPRESS_AT)

        assert arrived, "回调没被触发，这条用例没有真的复现那个时间窗"
        remaining = [m.content for m in await manager._get_working_memory("u1", "c1")]
        assert "等待期间说的话" in remaining

    async def test_only_the_summarised_messages_are_trimmed(self, manager):
        """裁掉的必须正好是进了摘要的那些，不能多裁。"""
        _install_summariser(manager)
        await _fill(manager, MemoryManager.COMPRESS_AT)

        remaining = [m.content for m in await manager._get_working_memory("u1", "c1")]
        assert len(remaining) == MemoryManager.KEEP_RECENT
        # 保留的应当是最新的那几条
        expected = [f"msg-{i}" for i in range(
            MemoryManager.COMPRESS_AT - MemoryManager.KEEP_RECENT, MemoryManager.COMPRESS_AT)]
        assert remaining == expected

    async def test_several_messages_during_compression_all_survive(self, manager):
        async def _during_llm():
            for i in range(3):
                await manager.add_message("u1", "c1", MsgRole.USER, f"并发-{i}")

        _install_summariser(manager, on_call=_during_llm)
        await _fill(manager, MemoryManager.COMPRESS_AT)

        remaining = [m.content for m in await manager._get_working_memory("u1", "c1")]
        for i in range(3):
            assert f"并发-{i}" in remaining


class TestConcurrentCompressionsDoNotClobberEachOther:
    async def test_only_one_compression_runs_at_a_time(self, manager):
        """
        同一会话两条请求几乎同时到时，两个压缩会各按自己的旧快照重建列表，
        后一个把前一个的结果盖掉。锁让第二个直接跳过——不压这一轮没有损失，
        还超阈值的话下一条消息会再触发。
        """
        running = 0
        overlaps = []

        async def _during_llm():
            nonlocal running
            running += 1
            # 真的让出控制权，否则两个协程根本不会交错，这条用例就成了摆设
            # （没有锁的实现也能"通过"）
            await asyncio.sleep(0.01)
            if running > 1:
                overlaps.append(True)
            running -= 1

        _install_summariser(manager, on_call=_during_llm)
        # 用 _seed 而不是 _fill：后者走 add_message，会自己先压一轮，
        # 于是下面两个 _compress 都在阈值以下直接返回，用例就成了摆设。
        await _seed(manager, MemoryManager.COMPRESS_AT)

        await asyncio.gather(
            manager._compress("u1", "c1"),
            manager._compress("u1", "c1"),
        )
        assert overlaps == [], "两个压缩重叠跑了"
        assert manager._store.docs, "至少要有一个压缩真的跑起来，否则这条没验到东西"

    async def test_the_lock_is_released_even_if_compression_blows_up(self, manager):
        """
        锁必须在异常路径上也释放，否则一次偶发失败会让这个会话**再也压不了**，
        工作记忆无限增长直到 24h TTL 过期。
        """
        async def _boom(*args, **kwargs):
            raise RuntimeError("模型挂了")

        import memory.conversation_memory as mod
        mod.structured_create = _boom
        manager._store_episodic = _episodic_recorder(manager)

        await _fill(manager, MemoryManager.COMPRESS_AT)
        lock_key = f"{manager._wm_key('u1', 'c1')}:compressing"
        assert lock_key not in manager._redis.strings, "锁没释放"


class TestSummaryIsAppendedAtomically:
    async def test_summary_accumulates_without_read_modify_write(self, manager):
        """
        摘要原先是"读出来拼一下再写回去"——同样是一次读改写，
        两个压缩交错时会丢掉一份。改成 APPEND 之后每一份都在。
        """
        _install_summariser(manager, text="第一段摘要")
        await _fill(manager, MemoryManager.COMPRESS_AT)

        _install_summariser(manager, text="第二段摘要")
        await _fill(manager, MemoryManager.COMPRESS_AT, prefix="again")

        summary = await manager._redis.get(manager._summary_key("u1", "c1"))
        assert "第一段摘要" in summary and "第二段摘要" in summary
