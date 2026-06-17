"""流式链路：按句策略守卫、run_stream 事件序列、与非流式的口径一致性。"""

from conftest import make_request

from agents.agent_orchestrator import AgentOrchestrator, AgentType
from core.answer_policy import AnswerPolicy, PolicyStreamGuard
from core.intent_recognizer import IntentCategory

KB = "标准交期以工厂排产为准。补件确认后 7 个工作日内从工厂发出。"


def stub_stream(orch: AgentOrchestrator, chunks, fail: bool = False):
    """把所有 Agent 的底层流式调用换成固定分片，不发网络请求。"""
    for agents in orch._pool.values():
        for agent in agents:
            async def _stream(req, skill_prompt="", _c=chunks, _f=fail):
                if _f:
                    raise RuntimeError("模拟流式故障")
                for c in _c:
                    yield c
            agent._stream_llm = _stream
    return orch


class TestPolicyStreamGuard:
    def test_emits_only_complete_sentences(self):
        guard = PolicyStreamGuard(AnswerPolicy(), [KB])
        assert guard.feed("好的，") == ""          # 半句先攒着
        assert guard.feed("我帮你查。") == "好的，我帮你查。"

    def test_blocks_unsourced_promise_mid_stream(self):
        guard = PolicyStreamGuard(AnswerPolicy(), [KB])
        out = guard.feed("补件会在3个工作日") + guard.feed("内送到。")
        assert "3个工作日" not in out
        assert "不能在未确认前承诺" in out
        assert [v.rule for v in guard.violations] == ["lead_time_commitment"]

    def test_allows_sourced_statement(self):
        guard = PolicyStreamGuard(AnswerPolicy(), [KB])
        out = guard.feed("补件确认后 7 个工作日内从工厂发出。")
        assert "7 个工作日" in out
        assert guard.violations == []

    def test_flush_checks_trailing_fragment(self):
        """没有句末标点的收尾片段同样要过策略，不能直接放行。"""
        guard = PolicyStreamGuard(AnswerPolicy(), [KB])
        guard.feed("我保证24小时内一定解决")
        out = guard.flush()
        assert "24" not in out
        assert guard.violations

    def test_flush_is_idempotent(self):
        guard = PolicyStreamGuard(AnswerPolicy(), [KB])
        guard.feed("你好")
        assert guard.flush() == "你好"
        assert guard.flush() == ""


class TestRunStream:
    async def _collect(self, orch, req):
        events = []
        async for e in orch.run_stream(req):
            events.append(e)
        return events

    async def test_routing_event_comes_before_any_text(self):
        orch = stub_stream(AgentOrchestrator(api_key="k"), ["订单", "已发货。"])
        events = await self._collect(orch, make_request("订单进度", IntentCategory.ORDER_STATUS))
        kinds = [e["type"] for e in events]
        assert kinds[0] == "routing"
        assert kinds[-1] == "result"
        assert events[0]["primary_agent"] == AgentType.ORDER.value

    async def test_deltas_concatenate_to_final_response(self):
        orch = stub_stream(AgentOrchestrator(api_key="k"), ["您好，", "已收到。"])
        events = await self._collect(orch, make_request("订单进度", IntentCategory.ORDER_STATUS))
        deltas = "".join(e["text"] for e in events if e["type"] == "delta")
        result = events[-1]["result"]
        assert deltas == result.response

    async def test_policy_violation_recorded_in_result(self):
        orch = stub_stream(AgentOrchestrator(api_key="k"), ["补件会在3个工作日内送到。"])
        req = make_request("补件进度", IntentCategory.MISSING_PART)
        req.grounding_sources = [KB]
        events = await self._collect(orch, req)
        result = events[-1]["result"]
        assert result.policy_violations, "流式路径漏掉了策略拦截"
        deltas = "".join(e["text"] for e in events if e["type"] == "delta")
        assert "3个工作日" not in deltas

    async def test_stream_failure_falls_back_to_handoff(self):
        """流式已经吐出去的内容无法撤回，失败时应转人工而不是重跑出第二个答案。"""
        orch = stub_stream(AgentOrchestrator(api_key="k"), [], fail=True)
        events = await self._collect(orch, make_request("订单进度", IntentCategory.ORDER_STATUS))
        result = events[-1]["result"]
        assert result.handoff is True
        assert result.escalated is True

    async def test_handoff_intent_skips_llm_entirely(self):
        orch = stub_stream(AgentOrchestrator(api_key="k"), ["不该出现"])
        events = await self._collect(orch, make_request("转人工", IntentCategory.HUMAN_HANDOFF))
        result = events[-1]["result"]
        assert result.handoff is True
        assert "不该出现" not in result.response

    async def test_clarification_path(self):
        orch = stub_stream(AgentOrchestrator(api_key="k"), ["不该出现"])
        events = await self._collect(
            orch, make_request("嗯嗯这个那个", IntentCategory.OTHER, confidence=0.2))
        result = events[-1]["result"]
        assert result.needs_clarification is True
        assert "不该出现" not in result.response
