"""
跨会话记忆的开关（ENABLE_CROSS_SESSION_MEMORY）。

情景记忆和用户画像只按 user_id 区分。没开令牌鉴权时 user_id 取自请求体，而面向
客户的实例和员工实例共用同一套 Redis / Postgres——客户在请求里填一个员工的
user_id（控制台默认是 u1001），就能把员工对话的摘要和画像带进自己的上下文，
员工查过的订单也就跟着出去了。把客户和订单数据隔开靠的是"客户实例不接
ai-sql-agent"，这条路正好绕过它。

关掉之后这两级**既不读也不写**。不写同样要紧：只关读的话，客户仍能往员工的
画像里写东西，再被员工实例读进上下文。
"""
import importlib
import sys

import pytest
from conftest import FakeRedis

import app.memory.conversation_memory as memory_module
from app.memory.conversation_memory import MemoryManager, MsgRole, UserProfilePayload

# 别的会话留下的东西：一个员工查过的订单
_STAFF_SUMMARY = "员工查过 J26-00136 的欠款，还差 12,000 元"
_STAFF_PROFILE = '{"preferences": [], "products": ["J26-00136"], "issue_types": ["欠款"]}'


class _Store:
    """预先放着"别的会话"的情景记忆和画像，并记下每一次调用。"""

    def __init__(self):
        self.calls: list[str] = []

    async def episodic_search(self, user_id, embedding, top_k):
        self.calls.append("episodic_search")
        return [_STAFF_SUMMARY]

    async def episodic_add(self, *args):
        self.calls.append("episodic_add")

    async def profile_get(self, user_id):
        self.calls.append("profile_get")
        return _STAFF_PROFILE

    async def profile_set(self, *args):
        self.calls.append("profile_set")


class _Embedder:
    async def embed(self, text):
        return [0.0]


def _manager(cross_session: bool) -> MemoryManager:
    m = MemoryManager.__new__(MemoryManager)
    m._redis = FakeRedis()
    m._store = _Store()
    m._embedder = _Embedder()
    m._client = None
    m._model = "test-model"
    m._cross_session = cross_session
    return m


def _stub_models(monkeypatch) -> list[str]:
    """把画像提炼和压缩摘要那两次模型调用换掉，返回被调用过的名字。"""
    called: list[str] = []

    async def _parse(*args, **kwargs):
        called.append("profile")
        return UserProfilePayload(preferences=[], products=["J26-00136"], issue_types=[])

    async def _create(*args, **kwargs):
        called.append("summary")
        return type("R", (), {"content": [type("B", (), {"type": "text", "text": "摘要：客户在问交期"})()]})()

    monkeypatch.setattr(memory_module, "structured_parse", _parse)
    monkeypatch.setattr(memory_module, "structured_create", _create)
    return called


class TestSwitchedOffNothingIsReadFromOtherSessions:
    async def test_someone_elses_history_and_profile_stay_out_of_the_context(self):
        """**核心用例。** 客户填了员工的 user_id，新会话里也拿不到员工那边的东西。"""
        manager = _manager(cross_session=False)

        ctx = await manager.get_context("u1001", "a-new-conversation", query="我之前问过什么")

        assert ctx.relevant_history == []
        assert ctx.user_profile == {}
        assert "J26-00136" not in ctx.to_prompt_text()

    async def test_the_store_is_not_even_asked(self):
        """不是"查了再丢掉"：存储一次都不碰，也就不存在哪条路径忘了丢。"""
        manager = _manager(cross_session=False)

        await manager.get_context("u1001", "a-new-conversation", query="我之前问过什么")

        assert manager._store.calls == []

    async def test_switched_on_the_same_request_does_recall_it(self):
        """对照：差别确实来自开关，不是这个假存储本来就查不出东西。"""
        manager = _manager(cross_session=True)

        ctx = await manager.get_context("u1001", "a-new-conversation", query="我之前问过什么")

        assert ctx.relevant_history == [_STAFF_SUMMARY]
        assert ctx.user_profile["products"] == ["J26-00136"]

    async def test_the_current_conversation_is_still_remembered(self):
        """关的只是跨会话那两级，同一段对话里说过的话照样在。"""
        manager = _manager(cross_session=False)
        await manager.add_message("customer-1", "c1", MsgRole.USER, "我的柜子什么时候到")

        ctx = await manager.get_context("customer-1", "c1", query="大概几号")

        assert [m.content for m in ctx.recent_messages] == ["我的柜子什么时候到"]


class TestSwitchedOffNothingIsWrittenForOtherSessions:
    async def test_no_profile_is_extracted_or_stored(self, monkeypatch):
        """
        不写和不读同样要紧：客户填员工的 user_id 聊一轮，不能把员工的画像盖掉，
        也不能往里塞东西等员工实例读进上下文。那次提炼用的模型调用也一并省掉。
        """
        called = _stub_models(monkeypatch)
        manager = _manager(cross_session=False)
        await manager.add_message("u1001", "c1", MsgRole.USER, "以后都按我说的格式回答")

        await manager.update_profile("u1001", "c1")

        assert "profile_set" not in manager._store.calls
        assert called == []

    async def test_switched_on_the_profile_is_stored(self, monkeypatch):
        """对照：开着的时候同样的调用是会写画像的。"""
        _stub_models(monkeypatch)
        manager = _manager(cross_session=True)
        await manager.add_message("u1001", "c1", MsgRole.USER, "J26-00136 的欠款还差多少")

        await manager.update_profile("u1001", "c1")

        assert "profile_set" in manager._store.calls

    async def test_compression_still_summarises_but_stores_no_episode(self, monkeypatch):
        """长对话照常压缩、照常留下本会话的摘要，只是不再存成跨会话可检索的情景记忆。"""
        called = _stub_models(monkeypatch)
        manager = _manager(cross_session=False)

        for i in range(MemoryManager.COMPRESS_AT):
            await manager.add_message("customer-1", "c1", MsgRole.USER, f"msg-{i}")

        assert called == ["summary"], "压缩没有跑，这条用例没验到东西"
        assert "摘要" in await manager._redis.get(manager._summary_key("customer-1", "c1"))
        assert len(await manager._get_working_memory("customer-1", "c1")) == MemoryManager.KEEP_RECENT
        assert "episodic_add" not in manager._store.calls

    async def test_switched_on_compression_stores_the_episode(self, monkeypatch):
        """对照：开着的时候压缩会把这一段存进情景记忆。"""
        _stub_models(monkeypatch)
        manager = _manager(cross_session=True)

        for i in range(MemoryManager.COMPRESS_AT):
            await manager.add_message("u1001", "c1", MsgRole.USER, f"msg-{i}")

        assert "episodic_add" in manager._store.calls


class TestTheSwitchReachesTheManager:
    def test_default_is_on(self):
        """不传就是开：员工实例不配这个变量，行为和加开关之前完全一致。"""
        manager = MemoryManager(store=_Store(), embedder=_Embedder(), api_key="test-key")
        assert manager.cross_session is True

    def test_constructor_argument_turns_it_off(self):
        manager = MemoryManager(store=_Store(), embedder=_Embedder(), api_key="test-key", cross_session=False)
        assert manager.cross_session is False


def _load_main(monkeypatch, value: str | None):
    """按指定环境变量重新导入 api.main——开关是模块级读取的。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("AI_HELPDESK_FRONTEND_DIST", "/nonexistent")
    if value is None:
        monkeypatch.delenv("ENABLE_CROSS_SESSION_MEMORY", raising=False)
    else:
        monkeypatch.setenv("ENABLE_CROSS_SESSION_MEMORY", value)
    sys.modules.pop("app.api.main", None)
    return importlib.import_module("app.api.main")


class TestTheSwitchIsReadFromTheEnvironment:
    def test_unset_means_on(self, monkeypatch):
        assert _load_main(monkeypatch, None)._CROSS_SESSION_MEMORY is True

    @pytest.mark.parametrize("value", ["true", "TRUE", "1", "on", ""])
    def test_these_leave_it_on(self, monkeypatch, value):
        assert _load_main(monkeypatch, value)._CROSS_SESSION_MEMORY is True

    @pytest.mark.parametrize("value", ["false", "False", " false ", "0", "no", "off", "OFF"])
    def test_every_way_of_writing_off_turns_it_off(self, monkeypatch, value):
        """
        这是个**关着才安全**的开关。只认 "false" 的话，写成 0 或 off 会被当成"开"，
        没有任何报错，而配置文件看起来是关的。
        """
        assert _load_main(monkeypatch, value)._CROSS_SESSION_MEMORY is False
