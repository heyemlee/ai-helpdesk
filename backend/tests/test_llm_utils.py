"""LLM 调用的公共约束：token 预算解释、结构化输出、effort 下发。"""
import pytest
from pydantic import BaseModel

from core import llm_utils
from core.llm_utils import structured_create, structured_parse


class _Payload(BaseModel):
    verdict: str


class _Usage:
    def __init__(self, output_tokens: int):
        self.output_tokens = output_tokens


class _Resp:
    """对齐 anthropic 的 ParsedMessage：parsed_output + stop_reason + usage。"""

    def __init__(self, parsed_output=None, stop_reason="end_turn", output_tokens=12):
        self.parsed_output = parsed_output
        self.stop_reason = stop_reason
        self.usage = _Usage(output_tokens)


class _FakeMessages:
    """记录每次调用的参数，返回预置响应。"""

    def __init__(self, resp: _Resp | None = None):
        self.resp = resp or _Resp()
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return "ok"

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        return self.resp


class _FakeClient:
    def __init__(self, resp: _Resp | None = None):
        self.messages = _FakeMessages(resp)


# ── effort 下发 ───────────────────────────────────────────────────────────────

async def test_structured_calls_carry_effort():
    """
    结构化调用统一带 effort，调用点不用各写一遍。

    这几个调用都是判别式任务（分类、排序、判分），不需要长推理。留着默认的
    高 effort 不只是贵：推理块会先吃掉 max_tokens，正文没产出就是一次静默降级。
    """
    client = _FakeClient()

    await structured_create(client, model="m", max_tokens=8, messages=[])

    assert client.messages.calls[0]["output_config"]["effort"] == llm_utils.STRUCTURED_EFFORT


async def test_caller_supplied_output_config_wins():
    """调用方自带 output_config 时按它来，不被默认值覆盖。"""
    client = _FakeClient()

    await structured_create(
        client, model="m", max_tokens=8, messages=[], output_config={"effort": "high"}
    )

    assert client.messages.calls[0]["output_config"]["effort"] == "high"


# ── 结构化输出 ────────────────────────────────────────────────────────────────

async def test_parse_returns_the_validated_model():
    client = _FakeClient(_Resp(parsed_output=_Payload(verdict="ok")))

    payload = await structured_parse(
        client, output_format=_Payload, model="m", max_tokens=8, messages=[]
    )

    assert payload.verdict == "ok"
    assert client.messages.calls[0]["output_format"] is _Payload


async def test_budget_exhausted_before_output_names_the_knob():
    """
    推理吃光预算是这条链路最阴险的失败：拿到一个合法响应，但正文是空的。

    报错必须点名该调哪个环境变量，否则调用方 except 里只会记下一句
    "解析失败"，跟真正的原因隔着好几层。
    """
    client = _FakeClient(_Resp(parsed_output=None, stop_reason="max_tokens", output_tokens=2048))

    with pytest.raises(ValueError, match="AI_HELPDESK_STRUCTURED_MAX_TOKENS"):
        await structured_parse(
            client, output_format=_Payload, model="m", max_tokens=8, messages=[]
        )


async def test_missing_parse_result_raises_instead_of_returning_none():
    """
    拿不到结果就抛。返回 None 会让调用方的降级路径变成隐式的——
    意图退回投票、重排退回原序都该是显式决定，不是这里悄悄给个空对象。
    """
    client = _FakeClient(_Resp(parsed_output=None))

    with pytest.raises(ValueError, match="_Payload"):
        await structured_parse(
            client, output_format=_Payload, model="m", max_tokens=8, messages=[]
        )


async def test_no_sampling_parameters_are_sent():
    """
    SDK 1.x 把 temperature/top_p/top_k 从签名里删了，再传是 TypeError 而不是 400。

    原来那套"探测 400 再降级"的兼容逻辑接不住 TypeError，会被调用点的
    except Exception 吃掉后照样静默降级——所以这里一个都不能传。
    """
    client = _FakeClient(_Resp(parsed_output=_Payload(verdict="ok")))

    await structured_create(client, model="m", max_tokens=8, messages=[])
    await structured_parse(
        client, output_format=_Payload, model="m", max_tokens=8, messages=[]
    )

    for call in client.messages.calls:
        assert not {"temperature", "top_p", "top_k"} & call.keys()


def test_structured_budget_leaves_room_for_reasoning_blocks():
    """预算调小会让推理模型在 thinking 阶段就耗尽，正文为空后静默降级。"""
    assert llm_utils.STRUCTURED_MAX_TOKENS >= 1024
    assert llm_utils.ANSWER_MAX_TOKENS >= 2048
