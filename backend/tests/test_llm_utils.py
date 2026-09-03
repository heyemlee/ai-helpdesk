"""LLM 调用的公共约束：token 预算解释、temperature 兼容降级。"""
import pytest

from core import llm_utils
from core.llm_utils import reset_temperature_support, structured_create, temperature_enabled


class _FakeMessages:
    """记录每次调用的参数；按需在第一次调用时抛出网关的 400。"""

    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None and len(self.calls) == 1:
            raise self.error
        return "ok"


class _FakeClient:
    def __init__(self, error: Exception | None = None):
        self.messages = _FakeMessages(error)


class _APIError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


_DEPRECATED = _APIError(
    "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
    "'message': '`temperature` is deprecated for this model.'}}"
)


@pytest.fixture(autouse=True)
def _restore_switch():
    reset_temperature_support(True)
    yield
    reset_temperature_support(True)


async def test_temperature_is_sent_when_the_model_accepts_it():
    client = _FakeClient()

    await structured_create(client, model="m", max_tokens=8, temperature=0.0, messages=[])

    assert client.messages.calls[0]["temperature"] == 0.0
    assert temperature_enabled() is True


async def test_model_rejecting_temperature_is_retried_without_it():
    """
    网关回 400 `temperature` is deprecated 时，结构化调用不能就这么失败。

    这些调用点（意图识别 / 记忆摘要 / 查询改写 / 重排 / Judge）每一个都在
    except 里静默降级，所以一旦这里失败，链路"看起来还在跑"，实际上
    意图识别只剩 Embedding 一路、Judge 全给默认分。
    """
    client = _FakeClient(error=_DEPRECATED)

    result = await structured_create(client, model="m", max_tokens=8, temperature=0.0, messages=[])

    assert result == "ok"
    assert len(client.messages.calls) == 2
    assert "temperature" not in client.messages.calls[1]
    assert temperature_enabled() is False


async def test_downgrade_is_remembered_for_the_process():
    """降级只探测一次，不能每条消息都白等一轮 400。"""
    first = _FakeClient(error=_DEPRECATED)
    await structured_create(first, model="m", max_tokens=8, temperature=0.0, messages=[])

    second = _FakeClient()
    await structured_create(second, model="m", max_tokens=8, temperature=0.0, messages=[])

    assert len(second.messages.calls) == 1
    assert "temperature" not in second.messages.calls[0]


async def test_other_errors_are_not_swallowed():
    """只兜 temperature 这一种 400，其余照常抛出去，别把真故障吃掉。"""
    client = _FakeClient(error=_APIError("400 - overloaded_error: upstream is busy"))

    with pytest.raises(_APIError):
        await structured_create(client, model="m", max_tokens=8, temperature=0.0, messages=[])

    assert len(client.messages.calls) == 1
    assert temperature_enabled() is True


def test_structured_budget_leaves_room_for_reasoning_blocks():
    """预算调小会让推理模型在 thinking 阶段就耗尽，正文为空后静默降级。"""
    assert llm_utils.STRUCTURED_MAX_TOKENS >= 1024
    assert llm_utils.ANSWER_MAX_TOKENS >= 2048
