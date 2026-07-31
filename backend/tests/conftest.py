"""测试公共设施。所有测试都不发真实网络请求。"""
# 和 api/main.py 顶部同一件事，也必须同样排在所有 import httpx 之前：让全进程的
# httpx 解析到 httpx2（anthropic 1.x 的 HTTP 层）。conftest 由 pytest 最先导入，
# 所以放这里能覆盖到随后加载的测试模块。
#
# 不这么做测试仍然全绿——但每个 import httpx 的测试模块都会打一条"调用过晚"的
# 告警，把生产环境真正出问题时的那条信号淹掉。
import httpx2 as _httpx2

_httpx2.alias_httpx()

import pytest  # noqa: E402

from app.agents.agent_orchestrator import AgentOrchestrator, Request  # noqa: E402
from app.core.intent_recognizer import IntentCategory, IntentRecognizer  # noqa: E402


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
