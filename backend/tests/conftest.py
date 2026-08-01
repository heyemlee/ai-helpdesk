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


@pytest.fixture(autouse=True)
def _fresh_llm_guards():
    """
    模型凭证不可用保护的状态是进程级的（core/llm_guard），按"地址 + 凭证"登记。
    测试里到处用同一把 "test-key"，一个用例把它判成不可用，后面所有用例的
    模型调用都会被挡——所以每个用例开始前清空登记表。
    """
    from app.core import llm_guard

    llm_guard.forget_all()
    yield
    llm_guard.forget_all()


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


# ── 假的模型中继 ──────────────────────────────────────────────────────────────

RELAY_OK_BODY = {
    "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-test",
    "content": [{"type": "text", "text": "好的"}],
    "stop_reason": "end_turn", "stop_sequence": None,
    "usage": {"input_tokens": 1, "output_tokens": 1},
}
# 中继停用 Key 之后实际回的那个错误体：没有标准的 error.type 字段
RELAY_DISABLED_BODY = {"error": "Invalid API key", "message": "API key is disabled"}


class FakeRelay:
    """
    接在 SDK 传输层上的假中继：SDK 走完自己的全套流程（拼请求、判状态码、决定
    重不重试、抛它自己的异常类），但不开任何套接字。

    `sent()` 数的是**真的发出去了**的请求——被凭证保护挡下的调用到不了这里，
    所以"后续环节不再出网"可以直接用这个数来断言。

    回什么按"主机 + 凭证"定，一个中继能同时扮演被停用的 Key 和还能用的 Key。
    """

    def __init__(self):
        self.requests: list[tuple[str, str]] = []            # (主机, 凭证)
        self.max_retries = 0                                 # SDK 自带的重试次数
        self._rules: list[tuple[str | None, str | None, int, object, dict]] = []

    def reject(self, key: str | None = None, *, host: str | None = None, status: int = 401,
               body: object = None, headers: dict | None = None) -> None:
        """之后匹配 key/host 的请求回这个状态。key、host 留空表示不限。后加的规则优先。"""
        self._rules.insert(0, (host, key, status,
                               RELAY_DISABLED_BODY if body is None else body, headers or {}))

    def allow(self, key: str | None = None, *, host: str | None = None,
              text: str | None = None) -> None:
        """之后匹配的请求回 200。`text` 是模型的正文——结构化调用要的是一段 JSON。"""
        body = RELAY_OK_BODY if text is None else {
            **RELAY_OK_BODY, "content": [{"type": "text", "text": text}]}
        self._rules.insert(0, (host, key, 200, body, {}))

    def sent(self, key: str | None = None, host: str | None = None) -> int:
        return sum(1 for h, k in self.requests
                   if (key is None or k == key) and (host is None or h == host))

    def __call__(self, request):
        key = request.headers.get("x-api-key") or \
            request.headers.get("authorization", "").removeprefix("Bearer ")
        host = request.url.host
        self.requests.append((host, key))
        for rule_host, rule_key, status, body, headers in self._rules:
            if rule_host in (None, host) and rule_key in (None, key):
                if isinstance(body, (dict, list)):
                    return _httpx2.Response(status, json=body, headers=headers)
                return _httpx2.Response(status, text=str(body), headers=headers)
        return _httpx2.Response(200, json=RELAY_OK_BODY)


@pytest.fixture
def relay(monkeypatch) -> FakeRelay:
    """
    把之后造出来的所有模型客户端都接到一个 FakeRelay 上。

    换的是 `llm_utils.anthropic_client_kwargs`——所有客户端都从
    `create_llm_client` 出来，而它的参数只从这一个函数来。SDK 自带的重试设成 0：
    429/5xx 的用例不该为了 SDK 的退避真的睡上几秒（401/403 它本来就不重试，
    有单独一条用例在默认重试次数下确认这一点）。
    """
    from app.core import llm_utils

    fake = FakeRelay()
    original = llm_utils.anthropic_client_kwargs

    def _kwargs(api_key, base_url=None):
        kwargs = original(api_key, base_url)
        kwargs["http_client"] = _httpx2.AsyncClient(transport=_httpx2.MockTransport(fake))
        kwargs["max_retries"] = fake.max_retries
        return kwargs

    monkeypatch.setattr(llm_utils, "anthropic_client_kwargs", _kwargs)
    return fake
