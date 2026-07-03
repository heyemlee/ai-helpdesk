"""测试公共设施。所有测试都不发真实网络请求。"""
# 和 api/main.py 顶部同一件事，也必须同样排在所有 import httpx 之前：让全进程的
# httpx 解析到 httpx2（anthropic 1.x 的 HTTP 层）。conftest 由 pytest 最先导入，
# 所以放这里能覆盖到随后加载的测试模块。
#
# 不这么做测试仍然全绿——但每个 import httpx 的测试模块都会打一条"调用过晚"的
# 告警，把生产环境真正出问题时的那条信号淹掉。
import httpx2 as _httpx2

_httpx2.alias_httpx()

import pathlib  # noqa: E402
import sys  # noqa: E402

import pytest  # noqa: E402

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
