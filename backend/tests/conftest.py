"""测试公共设施。所有测试都不发真实网络请求。"""
import pathlib
import sys

import pytest

_ROOT = str(pathlib.Path(__file__).parent.parent.resolve())
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agents.agent_orchestrator import AgentOrchestrator, Request  # noqa: E402
from core.intent_recognizer import IntentCategory, IntentRecognizer  # noqa: E402


@pytest.fixture
def recognizer() -> IntentRecognizer:
    """不带远端 Embedding 配置的识别器，Embedding 走本地向量。"""
    return IntentRecognizer(api_key="test-key")


@pytest.fixture
def orchestrator() -> AgentOrchestrator:
    return AgentOrchestrator(api_key="test-key")


def stub_agents(orch: AgentOrchestrator, reply: str = "好的，我来帮您处理。", fail_types=()):
    """
    把 Agent 池里所有实例的 LLM 调用替换成固定回复。

    fail_types 里的 AgentType 会抛异常，用来测试降级和转人工路径。
    """
    for agent_type, agents in orch._pool.items():
        for agent in agents:
            if agent_type in fail_types:
                async def _boom(req, skill_prompt="", _t=agent_type):
                    raise RuntimeError(f"{_t.value} 模拟故障")

                agent._call_llm = _boom
            else:
                async def _ok(req, skill_prompt="", _r=reply, _t=agent_type):
                    return f"[{_t.value}] {_r}"

                agent._call_llm = _ok
    return orch


def stub_intent(recog: IntentRecognizer, intent: IntentCategory, confidence: float = 0.9):
    """把三路融合里的 LLM 和 Embedding 换成确定结果，避免网络调用。"""
    async def _llm(message, history=None):
        return {"intent": intent, "confidence": confidence, "reasoning": "stub"}

    async def _emb(message):
        return {"intent": intent, "confidence": confidence}

    recog._llm_recognize = _llm
    recog._embedding_recognize = _emb
    return recog


def make_request(message: str, intent: IntentCategory, confidence: float = 0.9, **kwargs) -> Request:
    """构造一个已完成意图识别的编排请求，跳过网络调用。"""
    return Request(
        message=message,
        user_id="u1",
        conv_id="c1",
        intent=intent,
        intent_confidence=confidence,
        **kwargs,
    )
