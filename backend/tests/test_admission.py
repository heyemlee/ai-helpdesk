"""
模型调用的准入控制（core/admission）和按 run 记下的工具结果（app/runs/memo）。

- 令牌桶：补充的算式、拿完就等、等不起就在本地判过载（不出网）；所有调用都经过
  GuardedLLMClient，所以挡在那一层就挡住了全部；
- "这一轮不出网"（降级最后一档）：同一份模型配置的保护状态不动，只挡绑定了它的那一轮；
- 工具结果：第一次执行记下，重跑时同样的调用不再执行；失败的不记。

Redis 里那只共享的桶在 test_runs_backends.py 里测。
"""
import asyncio

import pytest

from app.core import admission
from app.core.admission import Admission, Limits, estimate_tokens, refill, shed
from app.core.llm_guard import GuardedLLMClient, LLMGuard, LLMUnavailableError
from app.runs.memo import MemoryMemo, RunScope, bind_run, deterministic_id
from app.tools.tool_manager import Tool, ToolManager


def test_refill_adds_by_elapsed_time_and_stops_at_capacity():
    assert refill(0, last_ms=0, now_ms=500, rate_per_ms=0.01, capacity=10) == 5
    assert refill(8, last_ms=0, now_ms=10_000, rate_per_ms=0.01, capacity=10) == 10
    assert refill(3, last_ms=100, now_ms=50, rate_per_ms=0.01, capacity=10) == 3     # 钟往回走：不补也不扣


def test_limits_from_env():
    limits = Limits.from_env({"AI_HELPDESK_LLM_RPM": "120", "AI_HELPDESK_LLM_TPM": "x",
                              "AI_HELPDESK_LLM_ADMISSION_WAIT_S": "5"})
    assert (limits.rpm, limits.tpm, limits.max_wait_s, limits.active) == (120, 0, 5.0, True)
    assert not Limits.from_env({}).active


def test_the_estimate_counts_the_prompt_and_the_output_ceiling():
    kwargs = {"system": "s" * 100, "messages": [{"role": "user", "content": "问" * 200}], "max_tokens": 300}
    assert estimate_tokens(kwargs) == int(300 * 0.5) + 300
    assert estimate_tokens({"messages": []}) == 1024                 # 没给 max_tokens：按默认上限


async def test_without_limits_nothing_waits():
    assert await Admission(Limits()).acquire("p", 10_000) == 0.0


async def test_an_empty_bucket_makes_the_call_wait_or_rejects_it_locally():
    strict = Admission(Limits(rpm=2, max_wait_s=0.0))
    await strict.acquire("p", 1)
    await strict.acquire("p", 1)
    with pytest.raises(LLMUnavailableError) as rejected:
        await strict.acquire("p", 1)
    assert rejected.value.reason == "overloaded" and rejected.value.sent is False

    patient = Admission(Limits(rpm=1200, max_wait_s=1.0))           # 每 50ms 补一个
    for _ in range(1200):
        await patient.acquire("p", 1)
    assert 0.0 < await patient.acquire("p", 1) < 0.5


async def test_a_bigger_than_bucket_call_is_charged_as_a_full_bucket():
    """单次估算比整桶还大时按整桶算：不然这次调用永远等不到令牌。"""
    admission = Admission(Limits(tpm=100, max_wait_s=0.0))
    assert await admission.acquire("p", 5000) == 0.0


class _Inner:
    def __init__(self):
        self.calls = 0
        self.messages = self

    async def create(self, **kwargs):
        self.calls += 1
        return type("Message", (), {"usage": None, "content": []})()


async def test_every_model_call_goes_through_the_bucket():
    inner = _Inner()
    client = GuardedLLMClient(inner, LLMGuard("openai @ relay.example", provider="openai"))
    previous = admission.current()
    admission.install(Admission(Limits(rpm=1, max_wait_s=0.0)))
    try:
        await client.messages.create(model="m", messages=[{"role": "user", "content": "hi"}])
        with pytest.raises(LLMUnavailableError):
            await client.messages.create(model="m", messages=[{"role": "user", "content": "hi"}])
    finally:
        admission.install(previous)
    assert inner.calls == 1                                         # 第二次没有出网
    assert client.guard.blocked_reason() == ""                      # 本地判的过载不动这份配置的状态


async def test_shedding_blocks_only_the_turn_it_is_bound_to():
    guard = LLMGuard("anthropic @ relay.example", provider="anthropic")
    inner = _Inner()
    client = GuardedLLMClient(inner, guard)
    with shed():
        assert guard.blocked_reason() == "overloaded"
        with pytest.raises(LLMUnavailableError) as blocked:
            await client.messages.create(model="m", messages=[])
        assert blocked.value.sent is False

        async def other_task():                                     # 从这一轮建出去的任务也挡
            return guard.blocked_reason()
        assert await asyncio.create_task(other_task()) == "overloaded"
    assert guard.blocked_reason() == "" and inner.calls == 0
    await client.messages.create(model="m", messages=[])
    assert inner.calls == 1


# ── 工具结果按 run 记下来 ─────────────────────────────────────────────────────

def _counting_tool(name="order_data_query", fail=False):
    calls = []

    async def handler(params, context):
        calls.append(params)
        if fail:
            raise RuntimeError("下游挂了")
        return {"answer": f"SO-1 已出库（第 {len(calls)} 次查）"}

    tool = Tool(name=name, description="", handler=handler,
                schema={"type": "object", "properties": {"query": {"type": "string"}}},
                fallback=lambda params, context, error: {"unavailable": True, "error": error})
    return tool, calls


async def test_a_retry_replays_the_tool_result_of_the_previous_attempt():
    manager = ToolManager(api_key="test-key")
    tool, calls = _counting_tool()
    manager.register(tool)
    memo = MemoryMemo()

    with bind_run(RunScope("r1", 1, memo=memo)):
        first = await manager.call("order_data_query", {"query": "SO-1"})
    with bind_run(RunScope("r1", 2, memo=memo)):
        again = await manager.call("order_data_query", {"query": "SO-1"})
        different = await manager.call("order_data_query", {"query": "SO-2"})

    assert len(calls) == 2 and calls[1] == {"query": "SO-2"}
    assert again.data == first.data and again.replayed and not first.replayed
    assert not different.replayed
    with bind_run(RunScope("r2", 2, memo=memo)):                    # 别的 run：不复用
        assert not (await manager.call("order_data_query", {"query": "SO-1"})).replayed


async def test_a_failed_tool_call_is_not_remembered():
    manager = ToolManager(api_key="test-key")
    tool, calls = _counting_tool(fail=True)
    manager.register(tool)
    memo = MemoryMemo()
    with bind_run(RunScope("r1", 1, memo=memo)):
        assert (await manager.call("order_data_query", {"query": "SO-1"})).data["unavailable"]
    assert memo.entries("r1") == 0
    with bind_run(RunScope("r1", 2, memo=memo)):
        await manager.call("order_data_query", {"query": "SO-1"})
    assert len(calls) == 2                                           # 重跑时真的再试一次


async def test_outside_a_run_nothing_is_remembered():
    manager = ToolManager(api_key="test-key")
    tool, calls = _counting_tool()
    manager.register(tool)
    await manager.call("order_data_query", {"query": "SO-1"})
    await manager.call("order_data_query", {"query": "SO-1"})
    assert len(calls) == 2


def test_ids_derived_from_a_run_are_stable_and_scoped_by_instance():
    a = deterministic_id("tk_", RunScope("r1", 1, instance="staff"), "handoff")
    b = deterministic_id("tk_", RunScope("r1", 3, instance="staff"), "handoff")
    c = deterministic_id("tk_", RunScope("r1", 1, instance="customer"), "handoff")
    assert a == b != c and a.startswith("tk_") and len(a) == 15
