"""
工具循环核心（agents/tool_loop.py）：边界、出错、去重、并发、强制作答。

用脚本化的假模型（conftest.ScriptedModel）和假工具，不发网络请求，也不碰编排器——
接进专家之后的行为见 test_agent_tool_loop.py。
"""
import asyncio

from conftest import Block, ScriptedModel, text_step, tool_step
from pydantic import BaseModel, Field

from app.agents.tool_loop import (
    LoopBudget,
    LoopConfig,
    LoopEnd,
    LoopTool,
    StepEnd,
    ToolEnd,
    ToolOutcome,
    TurnContext,
    run_tool_loop,
)


class LookupArgs(BaseModel):
    query: str = Field(min_length=1)


class Lookup(LoopTool):
    """假工具：记下查了什么；可以设成会失败、要拒绝、要等信号、或是慢工具。"""

    name = "lookup"
    description = "查资料"
    params = LookupArgs

    def __init__(self, name="", *, evidence=True, fail=False, refusal="", slow=False, timeout_s=0.0,
                 gate=None, started=None, on_run=None):
        if name:
            self.name = name
        self.calls: list[str] = []
        self.evidence, self.fail, self.refusal = evidence, fail, refusal
        self.slow, self.timeout_s = slow, timeout_s
        self.gate, self.started, self.on_run = gate, started, on_run

    def refuse(self, turn):
        return self.refusal

    async def run(self, args, turn):
        self.calls.append(args.query)
        if self.on_run:
            self.on_run()
        if self.started is not None:
            self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("下游挂了")
        text = f"[资料] {args.query}：3个工作日内送到。"
        return ToolOutcome(content=text, evidence=text if self.evidence else "", outcome="found")


def _turn(*tools, **budget):
    config = LoopConfig(budget=LoopBudget(**budget), allow={"order": tuple(t.name for t in tools)})
    return TurnContext(user_id="u1", conv_id="c1", tools={t.name: t for t in tools}, config=config)


async def _run(model, turn, clock=None):
    kwargs = {"clock": clock} if clock else {}
    return [e async for e in run_tool_loop(
        model, model="m", system="s", messages=[{"role": "user", "content": "SO-1 补件到哪了"}],
        tools=list(turn.tools.values()), turn=turn, agent="order", max_tokens=256, **kwargs,
    )]


def _results(model, request_index):
    """第 n 次请求里交回给模型的那批 tool_result。"""
    return model.requests[request_index]["messages"][-1]["content"]


async def test_answers_in_one_call_when_the_material_is_enough():
    tool = Lookup()
    turn = _turn(tool)
    model = ScriptedModel(text_step("好的，", "已安排。"))
    events = await _run(model, turn)

    assert [type(e).__name__ for e in events] == ["TextDelta", "TextDelta", "StepEnd", "LoopEnd"]
    assert tool.calls == []
    [request] = model.requests
    assert request["tool_choice"] == {"type": "auto"}
    assert [t["name"] for t in request["tools"]] == ["lookup"]
    assert request["tools"][0]["input_schema"]["required"] == ["query"]
    assert turn.usage() == {"model_calls": 1, "input_tokens": 20, "output_tokens": 8}
    assert events[-1].forced == ""


async def test_a_tool_result_goes_back_under_the_same_id():
    tool = Lookup()
    turn = _turn(tool)
    model = ScriptedModel(tool_step(("call_1", "lookup", {"query": "SO-1 补件"})),
                          text_step("3个工作日内送到。"))
    events = await _run(model, turn)

    assert tool.calls == ["SO-1 补件"]
    assert len(model.requests[0]["messages"]) == 1                 # 调用方传进来的列表没被改
    sent = model.requests[1]["messages"]
    assert sent[-2]["role"] == "assistant" and sent[-2]["content"][0].type == "tool_use"
    assert sent[-1] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "call_1", "content": "[资料] SO-1 补件：3个工作日内送到。"},
    ]}
    [end] = [e for e in events if isinstance(e, ToolEnd)]
    assert (end.record["actor"], end.record["evidence"], end.record["dedup"]) == ("model", True, False)
    assert turn.trajectory == [end.record]
    assert [e.called_tools for e in events if isinstance(e, StepEnd)] == [True, False]
    assert (events[-1].model_calls, events[-1].tool_calls) == (2, 1)


async def test_the_last_allowed_call_must_answer():
    tool = Lookup()
    turn = _turn(tool, max_model_calls=2)
    model = ScriptedModel(
        tool_step(("call_1", "lookup", {"query": "a"})),
        # 网关不认 tool_choice=none，最后一步还是调了工具：不执行，取已有正文
        tool_step(("call_2", "lookup", {"query": "b"}), text=["只能先答到这里。"]),
    )
    events = await _run(model, turn)

    assert model.requests[1]["tool_choice"] == {"type": "none"}
    assert tool.calls == ["a"]
    assert events[-1].forced == "model_calls"
    assert [e.called_tools for e in events if isinstance(e, StepEnd)] == [True, False]


async def test_calls_beyond_the_budget_are_refused_and_the_next_step_must_answer():
    tool = Lookup()
    turn = _turn(tool, max_tool_calls=1)
    model = ScriptedModel(
        tool_step(("call_1", "lookup", {"query": "a"}), ("call_2", "lookup", {"query": "b"})),
        text_step("好的。"),
    )
    events = await _run(model, turn)

    assert tool.calls == ["a"]
    refused = _results(model, 1)[1]
    assert refused["is_error"] is True and "次数" in refused["content"]
    assert model.requests[1]["tool_choice"] == {"type": "none"}
    assert events[-1].forced == "tool_calls"


async def test_bad_calls_come_back_as_errors_without_running():
    tool = Lookup()
    turn = _turn(tool)
    broken = Block(type="tool_use", id="call_3", name="lookup", input={},
                   parse_error="参数不是合法 JSON：Expecting value")
    model = ScriptedModel(
        tool_step(("call_1", "no_such_tool", {"query": "a"}), ("call_2", "lookup", {"query": ""}), broken),
        text_step("好的。"),
    )
    await _run(model, turn)

    assert tool.calls == []
    results = _results(model, 1)
    assert [r["tool_use_id"] for r in results] == ["call_1", "call_2", "call_3"]
    assert all(r["is_error"] for r in results)
    assert "no_such_tool" in results[0]["content"]
    assert "query" in results[1]["content"]
    assert "JSON" in results[2]["content"]
    assert [r["outcome"] for r in turn.trajectory] == ["unavailable_tool", "invalid_args", "invalid_args"]


async def test_the_policy_gate_stops_a_call_before_it_runs():
    tool = Lookup(refusal="报价不通过系统查询。")
    turn = _turn(tool)
    model = ScriptedModel(tool_step(("call_1", "lookup", {"query": "加岛台多少钱"})),
                          text_step("报价以正式报价单为准。"))
    await _run(model, turn)

    assert tool.calls == []
    assert _results(model, 1) == [{"type": "tool_result", "tool_use_id": "call_1",
                                   "content": "报价不通过系统查询。", "is_error": True}]


async def test_the_same_query_runs_once_including_what_the_rules_fetched():
    tool = Lookup()
    turn = _turn(tool)
    turn.remember(tool.memo_key(LookupArgs(query="SO-1 补件")),
                  ToolOutcome(content="预取的资料", evidence="预取的资料", outcome="found"))
    model = ScriptedModel(
        tool_step(("c1", "lookup", {"query": " so-1  补件 "}),     # 大小写、空白不同也算同一次
                  ("c2", "lookup", {"query": "SO-2"}), ("c3", "lookup", {"query": "SO-2"})),
        text_step("好的。"),
    )
    events = await _run(model, turn)

    assert tool.calls == ["SO-2"]
    first = _results(model, 1)[0]
    assert "预取的资料" in first["content"] and "复用" in first["content"]
    assert [r["dedup"] for r in turn.trajectory] == [True, False, True]
    assert events[-1].tool_calls == 1                               # 复用的不花预算


async def test_calls_in_one_step_run_together_and_come_back_in_the_models_order():
    second_started = asyncio.Event()
    first = Lookup("first", gate=second_started)       # 要等第二个开跑才放行：串行执行会卡住
    second = Lookup("second", started=second_started)
    turn = _turn(first, second)
    model = ScriptedModel(
        tool_step(("c1", "first", {"query": "a"}), ("c2", "second", {"query": "b"})),
        text_step("好的。"),
    )
    events = await asyncio.wait_for(_run(model, turn), timeout=2)

    assert [e.call_id for e in events if isinstance(e, ToolEnd)] == ["c2", "c1"]     # 完成的顺序
    assert [r["tool_use_id"] for r in _results(model, 1)] == ["c1", "c2"]             # 交回的顺序
    assert [r["call_id"] for r in turn.trajectory] == ["c1", "c2"]


async def test_a_failing_tool_does_not_end_the_turn():
    tool = Lookup(fail=True)
    turn = _turn(tool)
    model = ScriptedModel(tool_step(("c1", "lookup", {"query": "a"})),
                          text_step("系统暂时查不到，建议人工核实。"))
    events = await _run(model, turn)

    [result] = _results(model, 1)
    assert result["is_error"] is True and "下游挂了" in result["content"]
    assert isinstance(events[-1], LoopEnd) and events[-1].model_calls == 2


async def test_slow_tools_are_limited_by_count_and_by_time():
    slow = Lookup(slow=True, timeout_s=40)
    turn = _turn(slow, max_delegations=1)
    model = ScriptedModel(
        tool_step(("c1", "lookup", {"query": "a"})),
        tool_step(("c2", "lookup", {"query": "b"})),
        text_step("好的。"),
    )
    await _run(model, turn)
    assert slow.calls == ["a"]
    assert "不能再查业务系统" in _results(model, 2)[0]["content"]

    # 剩余时间不够一次委派再加一步作答：一次都不让查
    slow = Lookup(slow=True, timeout_s=40)
    turn = _turn(slow, deadline_s=50)
    model = ScriptedModel(tool_step(("c1", "lookup", {"query": "a"})), text_step("好的。"))
    await _run(model, turn)
    assert slow.calls == []


async def test_running_out_of_time_forces_an_answer():
    now = [0.0]
    tool = Lookup(on_run=lambda: now.__setitem__(0, 11.0))
    turn = _turn(tool, deadline_s=10)
    model = ScriptedModel(tool_step(("c1", "lookup", {"query": "a"})), text_step("好的。"))
    events = await _run(model, turn, clock=lambda: now[0])

    assert model.requests[1]["tool_choice"] == {"type": "none"}
    assert events[-1].forced == "deadline"


def test_the_loop_is_off_unless_switched_on():
    assert LoopConfig.from_env({}) is None
    assert LoopConfig.from_env({"AI_HELPDESK_TOOL_LOOP": "off"}) is None
    config = LoopConfig.from_env({
        "AI_HELPDESK_TOOL_LOOP": "on",
        "AI_HELPDESK_TOOL_LOOP_MAX_CALLS": "0",
        "AI_HELPDESK_TOOL_LOOP_TOOLS": "technical=knowledge_search,order_data_query;general=",
    })
    assert config.budget.max_model_calls == 1                       # 至少要能作答一次
    assert config.allow["technical"] == ("knowledge_search", "order_data_query")
    assert config.allow["general"] == ()
    assert config.allow["order"] == ("knowledge_search", "order_data_query")   # 没提到的专家用默认


def test_tools_for_intersects_the_allowlist_with_what_this_instance_has():
    knowledge, data = Lookup("knowledge_search"), Lookup("order_data_query")
    turn = TurnContext(user_id="u", conv_id="c", tools={"knowledge_search": knowledge}, config=LoopConfig())
    assert turn.tools_for("order") == [knowledge]                    # 没接业务数据的实例
    turn.tools["order_data_query"] = data
    assert turn.tools_for("order") == [knowledge, data]
    assert turn.tools_for("technical") == [knowledge]
    assert turn.tools_for("unknown") == []
