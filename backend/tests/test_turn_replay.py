"""
从事件重放一轮（app/events/replay.py）和运维脚本（app/events/ops.py、scripts/turn_events.py、
scripts/replay_turns.py）。

- 重建出来的请求能自证：初始 messages 的哈希等于 agent_started 里记的；改一处输入就对不上；
- 循环状态：重建出来的 messages 就是模型下一步真正收到的那一份；
- 守卫重放：代码不变时替换结果和记录一字不差；守卫改了，差异能报出来；
- 路由重放：代码不变时没有差异；门槛改了，会变的轮次能报出来；
- 导出的用例能通过 load_cases 和 validate；
- check 报得出没有终结事件、seq 有缺口的轮次；forget 只删本实例的、不给 --yes 不删。

全部离线：模型是脚本化的假模型；answer 重放只验"不给 --yes 只打印计划"。
"""
import importlib.util
import json
import pathlib
import time

import pytest
from conftest import ScriptedModel, text_step, tool_step
from test_turn_model_calls import MESSAGE, _loop_chat, _loop_desk
from test_turn_projection import CANARY, ORDER_DATA, _answer, _chat, _desk, _Intent, _stream

from app.agents.agent_orchestrator import AgentOrchestrator, AgentType
from app.core.answer_policy import AnswerPolicy
from app.core.evidence import normalize_delegation_result
from app.core.intent_recognizer import IntentCategory
from app.evaluation.trajectory import load_cases
from app.events.ops import check_turns, forget
from app.events.recorder import new_turn_id
from app.events.replay import ReplayError, export_case, rebuild_loop_state, rebuild_request, replay_guard, replay_route
from app.events.store import MemoryEventStore
from app.events.types import ABANDONED_SEQ, Event

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _plain(value):
    """SDK 的块对象和事件里的 dict 比：都变成 dict。"""
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return {k: _plain(v) for k, v in vars(value).items()}
    return value


# ── 重建请求 ──────────────────────────────────────────────────────────────────

async def test_the_rebuilt_request_proves_itself(monkeypatch):
    main, log = await _desk(monkeypatch, _Intent(IntentCategory.MISSING_PART))
    answer, recorded = await _chat(main, log)

    rebuilt = rebuild_request(recorded)
    assert rebuilt.verified is True and rebuilt.notes == []
    assert rebuilt.agent.startswith("order_")
    req = rebuilt.request
    assert (req.message, req.user_id, req.intent, req.entities) == (
        CANARY["message"], CANARY["user"], IntentCategory.MISSING_PART, {"order_id": [CANARY["entity"]]})
    assert len(req.grounding_sources) == 2                     # 知识 + 查到的业务数据

    # 输入改一处（比如记忆上下文），哈希就对不上：那是"用当前代码重放旧输入"
    tampered = [{**e, "body": {**e["body"], "text": "多了一段记忆"}} if e["type"] == "memory_loaded" else e
                for e in recorded]
    changed = rebuild_request(tampered)
    assert changed.verified is False and changed.notes


async def test_a_parallel_turn_and_a_stream_turn_prove_themselves_too(monkeypatch):
    main, log = await _desk(monkeypatch, _Intent(IntentCategory.DAMAGE_DEFECT))
    _, recorded = await _chat(main, log, "门板有划痕，而且尾款还多收了")
    assert rebuild_request(recorded).verified is True
    _, streamed = await _stream(main, log)
    assert rebuild_request(streamed).verified is True


async def test_a_purged_turn_cannot_be_rebuilt(monkeypatch):
    main, log = await _desk(monkeypatch, _Intent(IntentCategory.MISSING_PART))
    _, recorded = await _chat(main, log)
    with pytest.raises(ReplayError):
        rebuild_request([{**e, "body": None} for e in recorded])


# ── 循环状态 ──────────────────────────────────────────────────────────────────

def _loop_script():
    return ScriptedModel(
        tool_step(("call_k", "knowledge_search", {"query": MESSAGE}),
                  ("call_o", "order_data_query", {"query": "SO-1 补件发货日期"}), text=("我先查一下",)),
        text_step("已经查到 SO-1 补件的安排。"),
    )


async def test_the_loop_state_is_what_the_model_saw_next(monkeypatch):
    model = _loop_script()
    main, log = await _loop_desk(monkeypatch, model)
    _, recorded = await _loop_chat(main, log)
    agent = next(e["agent"] for e in recorded if e["type"] == "agent_started")

    state = rebuild_loop_state(recorded, agent)
    assert _plain(state.messages) == _plain(model.requests[1]["messages"])    # 第二步真正收到的
    assert (state.steps, state.tool_calls, state.delegations, state.in_flight) == (2, 1, 1, [])
    assert ("knowledge_search", MESSAGE.casefold(), 3) in state.memo          # 规则预取查过的
    assert state.sent_chars == 0                                              # 非流式：没往外发过字

    # 进程挂在工具跑到一半：发起了、没有结果的那一个看得出来
    unfinished = [e for e in recorded if not (e["type"] == "tool_call" and e["call_id"] == "call_o")]
    assert rebuild_loop_state(unfinished, agent).in_flight == ["call_o"]


# ── 守卫和路由 ────────────────────────────────────────────────────────────────

async def test_guard_replay_matches_the_record_and_notices_a_changed_rule(monkeypatch):
    main, log = await _desk(monkeypatch, _Intent(IntentCategory.MISSING_PART), data=False, models={
        AgentType.ORDER: _answer("补件运费是 680 元，三个工作日内送到。")})
    _, recorded = await _chat(main, log)
    [replay] = replay_guard(recorded)
    assert replay.recorded and replay.same

    _, streamed = await _stream(main, log)
    [replay] = replay_guard(streamed)
    assert replay.recorded and replay.same

    [changed] = replay_guard(recorded, AnswerPolicy(enabled=False))           # 守卫被改得什么都不拦
    assert not changed.same and changed.replayed == []


async def test_guard_replay_follows_the_loop_step_by_step(monkeypatch):
    """循环里中途加出处、每步作废半句：重放按记录的顺序来，结果和当时一字不差。"""
    model = ScriptedModel(
        tool_step(("call_o", "order_data_query", {"query": "SO-1 补件发货日期"}), text=("我先查一下",)),
        text_step("SO-1 的补件已排产，三个工作日内发出。运费是 680 元。"),
    )
    main, log = await _loop_desk(monkeypatch, model)
    _, recorded = await _loop_chat(main, log)
    [replay] = replay_guard(recorded)
    assert replay.same


async def test_route_replay_reports_only_what_would_change(monkeypatch):
    main, log = await _desk(monkeypatch, _Intent(IntentCategory.DAMAGE_DEFECT))
    _, recorded = await _chat(main, log, "门板有划痕，而且尾款还多收了")
    orchestrator = AgentOrchestrator(api_key="replay-no-calls")

    replay = replay_route(recorded, orchestrator)
    assert replay.differences == [] and replay.recorded["path"] == "parallel"

    monkeypatch.setattr(AgentOrchestrator, "SUPPORTING_MIN_SCORE", 0.99)     # 改了协作门槛
    changed = replay_route(recorded, orchestrator)
    assert "path" in changed.differences and changed.replayed["path"] == "single"


async def test_route_replay_skips_turns_routed_by_an_outage(monkeypatch):
    main, log = await _desk(monkeypatch, _Intent(IntentCategory.MISSING_PART), outage="quota_exhausted")
    _, recorded = await _chat(main, log)
    assert replay_route(recorded, AgentOrchestrator(api_key="replay-no-calls")) is None


# ── 导出 ──────────────────────────────────────────────────────────────────────

async def test_an_exported_case_loads_validates_and_stubs_the_recorded_data(monkeypatch, tmp_path):
    main, log = await _desk(monkeypatch, _Intent(IntentCategory.MISSING_PART))
    _, recorded = await _chat(main, log)

    case = export_case(recorded, case_id="X-1", category="B")
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"cases": [case]}, ensure_ascii=False), encoding="utf-8")
    [loaded], _ = load_cases(path)
    assert (loaded.message, loaded.intent_label, loaded.category) == (CANARY["message"], "missing_part", "B")
    [stub] = loaded.stubs
    assert stub["payload"]["answer"] == ORDER_DATA["answer"]
    assert normalize_delegation_result(stub["payload"], answer=stub["answer"])["selected_databases"] == ["wms_db"]


# ── 运维：check 和 forget ─────────────────────────────────────────────────────

def _event(turn_id: str, seq: int, kind: str, *, instance="staff", conv="c1", body=None) -> Event:
    return Event(turn_id=turn_id, seq=seq, instance=instance, conv_id=conv, request_id="r", type=kind, agent="",
                 step=None, call_id="", ts=time.time(), t_ms=float(seq), v=1, data="{}",
                 body=None if body is None else json.dumps(body))


async def test_check_reports_unfinished_turns_and_gaps():
    store = MemoryEventStore("staff")
    complete, unfinished, gapped = new_turn_id(), new_turn_id(), new_turn_id()
    for event in (_event(complete, 0, "turn_started"), _event(complete, 1, "turn_completed"),
                  _event(unfinished, 0, "turn_started"),
                  _event(gapped, 0, "turn_started"), _event(gapped, 2, "turn_completed")):
        store.put(event)

    report = await check_turns(store, since=0)
    assert (report["turns"], report["complete"], report["events"]) == (3, 1, 5)
    assert report["no_terminal"] == [unfinished] and report["seq_gaps"] == [gapped]
    assert report["abandoned"] == []


async def test_check_lists_turns_closed_by_a_later_attempt_apart_from_problems():
    """
    worker 被杀的那一轮：自己的事件停在半截，接手的一方在保留号上补了 turn_abandoned。
    它不算"没有终结事件"，保留号也不算 seq 的缺口；自己发了终结事件、又被补过的，算完整。
    """
    store = MemoryEventStore("staff")
    lost, finished_late = new_turn_id(), new_turn_id()
    for event in (_event(lost, 0, "turn_started"), _event(lost, 1, "routed"),
                  _event(lost, ABANDONED_SEQ, "turn_abandoned"),
                  _event(finished_late, 0, "turn_started"), _event(finished_late, 1, "turn_aborted"),
                  _event(finished_late, ABANDONED_SEQ, "turn_abandoned")):
        store.put(event)

    report = await check_turns(store, since=0)
    assert report["abandoned"] == [lost] and report["no_terminal"] == [] and report["seq_gaps"] == []
    assert report["complete"] == 1


async def test_forget_deletes_only_this_instance_and_only_with_yes():
    staff, customer = MemoryEventStore("staff"), MemoryEventStore("customer")
    customer._turns = staff._turns                       # 一张表、两个实例
    mine, theirs = new_turn_id(), new_turn_id()
    staff.put(_event(mine, 0, "turn_started", body={"user_id": "u-del", "message": "删掉我"}))
    staff.put(_event(theirs, 0, "turn_started", instance="customer", body={"user_id": "u-del"}))

    plan = await forget(staff, user_id="u-del")
    assert plan["conversations"] == ["c1"] and plan["dry_run"] and await staff.events_of(mine)

    done = await forget(staff, user_id="u-del", dry_run=False)
    assert done["deleted"] == {"c1": 1} and await staff.events_of(mine) == []
    assert len(await customer.events_of(theirs)) == 1    # 客户实例的同名会话不归它管


async def test_forget_by_user_also_finds_conversations_through_the_audit_log(tmp_path, capsys):
    """
    原文 30 天就清空：只靠 turn_started 的原文，更早的轮次找不到。审计日志里"用户 → 会话"一直在，
    按用户删除时也从那里找——本实例写的，加上分不出实例的老记录——审计本身只读不删。
    """
    from app.storage.conversation_log import ConversationLog

    turn_events = _script("turn_events")
    audit = ConversationLog(db_path=str(tmp_path / "conversations.db"))
    staff = MemoryEventStore("staff")
    expired, recent = new_turn_id(), new_turn_id()
    staff.put(_event(expired, 0, "turn_started", conv="c-old"))          # 原文已按保留期清空
    staff.put(_event(expired, 1, "turn_completed", conv="c-old"))
    staff.put(_event(recent, 0, "turn_started", conv="c-new", body={"user_id": "u-del", "message": "最近的"}))
    await audit.append("c-old", "u-del", "user", "早先的原话", turn_id=expired, instance="staff")
    await audit.append("c-legacy", "u-del", "user", "加事件日志之前")      # 没有实例：老记录
    await audit.append("c-customer", "u-del", "user", "客户实例的", instance="customer")
    await audit.append("c-someone", "u-other", "user", "别人的", instance="staff")
    try:
        plan = await forget(staff, user_id="u-del", audit=audit)
        assert plan["conversations"] == ["c-new", "c-old", "c-legacy"]
        assert plan["found_by"] == {"c-new": ["turn_started"], "c-old": ["conversation_log"],
                                    "c-legacy": ["conversation_log"]}
        assert await staff.events_of(expired)                            # 只是计划

        args = turn_events.parse(["forget", "--instance", "staff", "--user", "u-del", "--yes"])
        assert await turn_events.run(args, staff, audit) == 0
        done = json.loads(capsys.readouterr().out)
        assert done["deleted"] == {"c-new": 1, "c-old": 2, "c-legacy": 0}
        assert await staff.events_of(expired) == [] and await staff.events_of(recent) == []
        assert len(await audit.get_conversation("c-old")) == 1          # 审计不动
    finally:
        await audit.close()


# ── 两个脚本 ──────────────────────────────────────────────────────────────────

async def test_the_ops_script_shows_checks_and_forgets(monkeypatch, capsys):
    turn_events = _script("turn_events")
    main, log = await _desk(monkeypatch, _Intent(IntentCategory.MISSING_PART))
    answer, _ = await _chat(main, log)

    assert await turn_events.run(turn_events.parse(["show", "--instance", "staff", "--turn", answer["turn_id"]]),
                                 log.store) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["complete"] and shown["result"]["response"] == answer["response"]

    assert await turn_events.run(turn_events.parse(["check", "--instance", "staff"]), log.store) == 0
    assert json.loads(capsys.readouterr().out)["complete"] == 1

    assert await turn_events.run(turn_events.parse(["forget", "--instance", "staff", "--conv", "c1"]),
                                 log.store) == 0
    assert await log.store.events_of(answer["turn_id"])                     # 没给 --yes：只是计划
    capsys.readouterr()


async def test_the_replay_script_never_calls_the_model_without_yes(monkeypatch, capsys, tmp_path):
    replay_turns = _script("replay_turns")
    main, log = await _desk(monkeypatch, _Intent(IntentCategory.MISSING_PART))
    answer, _ = await _chat(main, log)
    turn = ["--instance", "staff", "--turn", answer["turn_id"]]

    for mode in ("route", "guard"):
        assert await replay_turns.run(replay_turns.parse([*turn, "--mode", mode]), log.store) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["compared"] == 1 and report["changed"] == []

    out = tmp_path / "exported.json"
    await replay_turns.run(replay_turns.parse([*turn, "--mode", "export", "--out", str(out)]), log.store)
    assert json.loads(capsys.readouterr().out)["cases"] == 1
    assert len(load_cases(out)[0]) == 1

    await replay_turns.run(replay_turns.parse([*turn, "--mode", "answer"]), log.store)
    plan = json.loads(capsys.readouterr().out)
    assert plan["plan"] == {"turns": 1, "estimated_model_calls": 1} and "turns" not in plan
