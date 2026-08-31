"""
一轮对话的事件日志：骨架这一层（app/events/）。

- 记录器：seq 按发生顺序、快照、截断、不抛、跟着 contextvar 进任务；
- 写入器：攒批、队列满了丢弃并计数、重试不写出重复行、重试用尽丢弃并计数、停机限时写完；
- 存储：写进去读得回、按实例隔离、保留期只动本实例的过期行、清空原文后结构不变。
  Postgres 那一份在设置了 TEST_POSTGRES_DSN 时跑，和进程内那一份跑同一组断言；
- 审计和工单的新列：老库补列、按实例读；
- 接口：日志关着时只多一个 turn_id；一轮由 turn_started 和终结事件包住；流式中途断开、
  编排器抛错；实例没设时日志不开；同一会话两轮重叠。

全部离线：模型是 conftest 的脚本化假模型，存储是临时目录里的 SQLite 和进程内缓冲。
"""
import asyncio
import importlib
import json
import os
import re
import secrets
import sqlite3
import sys
import time
import uuid
from enum import Enum

import pytest
from conftest import ScriptedModel, text_step
from prometheus_client import REGISTRY

from app.agents.agent_orchestrator import AgentOrchestrator, AgentType
from app.core.intent_recognizer import IntentCategory, UrgencyLevel
from app.events import EVENT_TYPES, NOOP, TERMINAL_TYPES, EventLog, EventLogConfig, bind, events
from app.events.recorder import (
    MAX_EVENT_BODY_BYTES,
    MAX_TURN_BODY_BYTES,
    EventLogStats,
    TurnRecorder,
    new_turn_id,
)
from app.events.retention import DAY_S, Retention
from app.events.store import MemoryEventStore, PgEventStore
from app.events.types import Event
from app.events.writer import EventWriter
from app.memory.conversation_memory import MemoryContext
from app.storage.conversation_log import ConversationLog
from app.tickets.ticket_store import TicketStore
from app.tools.tool_manager import ToolResult

TEST_PG_DSN = os.getenv("TEST_POSTGRES_DSN", "").strip()

# 只会出现在原话和 user_id 里的字串：结构（data）里一处都不许有
CANARY_MESSAGE = "SO-7731 柜门金丝雀补件到哪了"
CANARY_USER = "user-canary-5c1e"


def _turn(mode: str = "full", *, sink=None, stats: EventLogStats | None = None, request_id: str = "r1"):
    stats = stats if stats is not None else EventLogStats()
    sink = sink if sink is not None else MemoryEventStore("staff", stats=stats)
    turn = TurnRecorder(new_turn_id(), instance="staff", conv_id="c1", request_id=request_id,
                        mode=mode, sink=sink, stats=stats)
    return turn, sink, stats


async def _recorded(store, turn_id: str) -> list[dict]:
    return [e.to_dict() for e in await store.events_of(turn_id)]


# ── 事件类型 ──────────────────────────────────────────────────────────────────

def test_the_event_types_are_a_fixed_list():
    """读的一方按类型投影，拼错一个字母不会报错，只会让某个字段悄悄变空。"""
    assert EVENT_TYPES == {
        "turn_started", "memory_loaded", "intent_voted", "prefetch_planned", "tool_started",
        "tool_call", "model_call", "routed", "agent_started", "agent_fallback",
        "preamble_discarded", "loop_ended", "loop_fallback", "guard_replaced", "agent_answered",
        "handoff", "ticket_created", "turn_completed", "turn_failed", "turn_aborted",
    }
    assert TERMINAL_TYPES == {"turn_completed", "turn_failed", "turn_aborted"} <= EVENT_TYPES


# ── 记录器 ────────────────────────────────────────────────────────────────────

async def test_seq_follows_the_order_things_happened():
    turn, store, _ = _turn()
    for kind in ("turn_started", "memory_loaded", "intent_voted", "routed", "turn_completed"):
        turn.emit(kind, {"step": kind})

    recorded = await store.events_of(turn.turn_id)
    assert [e.seq for e in recorded] == [0, 1, 2, 3, 4]
    assert [e.type for e in recorded] == ["turn_started", "memory_loaded", "intent_voted",
                                          "routed", "turn_completed"]
    assert all(a.t_ms <= b.t_ms and a.ts <= b.ts for a, b in zip(recorded, recorded[1:]))
    assert {(e.turn_id, e.instance, e.conv_id, e.request_id, e.v) for e in recorded} == {
        (turn.turn_id, "staff", "c1", "r1", 1)}
    assert turn.terminal == "turn_completed"


async def test_an_event_is_a_snapshot_of_the_moment_it_was_emitted():
    """预取的 trace 字典之后还会被 info.update(...) 改；存引用会记成改过之后的样子。"""
    turn, store, _ = _turn()
    trace = {"outcome": "found", "sources": [{"doc_id": "d1"}]}
    body = {"content": "片段原文"}
    turn.emit("tool_call", {"trace": trace}, body)
    trace["outcome"] = "changed"
    trace["sources"].append({"doc_id": "d2"})
    body["content"] = "改过了"

    [event] = await _recorded(store, turn.turn_id)
    assert event["data"]["trace"] == {"outcome": "found", "sources": [{"doc_id": "d1"}]}
    assert event["body"] == {"content": "片段原文"}


async def test_emit_never_raises():
    """记日志不能打断回答：序列化失败、类型拼错、写入器抛错，都只计数。"""
    class Unprintable:
        def __str__(self):
            raise RuntimeError("boom")

        __repr__ = __str__

    class BrokenSink:
        def put(self, event):
            raise RuntimeError("down")

    turn, store, stats = _turn()
    turn.emit("routed", {"bad": Unprintable()})
    turn.emit("not_a_type", {})
    TurnRecorder(new_turn_id(), instance="staff", conv_id="c1", mode="full",
                 sink=BrokenSink(), stats=stats).emit("turn_started", {})
    assert stats.dropped == {"serialize": 3}

    turn.emit("turn_completed", {"outcome": "answered"})        # 之后照常
    assert [e.type for e in await store.events_of(turn.turn_id)] == ["turn_completed"]


async def test_long_text_is_cut_to_fit_and_short_fields_stay_whole():
    turn, store, _ = _turn()
    turn.emit("tool_call", {"tool": "knowledge_search"},
              {"args": {"query": "W3012 门板"}, "content": "长" * 100_000})

    [event] = await store.events_of(turn.turn_id)
    assert len(event.body.encode("utf-8")) <= MAX_EVENT_BODY_BYTES
    decoded = event.to_dict()
    assert decoded["body"]["args"] == {"query": "W3012 门板"}
    assert decoded["body"]["content"].startswith("长" * 1000)
    assert decoded["body"]["content"].endswith("…[截断]")
    assert decoded["data"] == {"tool": "knowledge_search", "truncated": True}


async def test_a_turn_stops_keeping_text_after_a_megabyte_but_keeps_every_event():
    turn, store, _ = _turn()
    for _ in range(25):
        turn.emit("model_call", {"purpose": "answer"}, {"output": "字" * 20_000})   # 每条约 60 KB

    recorded = await store.events_of(turn.turn_id)
    assert len(recorded) == 25
    kept = [e for e in recorded if e.body is not None]
    dropped = [e.to_dict()["data"] for e in recorded if e.body is None]
    assert dropped and all(d == {"purpose": "answer", "body_dropped": True} for d in dropped)
    assert sum(len(e.body.encode("utf-8")) for e in kept) <= MAX_TURN_BODY_BYTES + MAX_EVENT_BODY_BYTES


async def test_meta_mode_keeps_the_structure_only():
    turn, store, _ = _turn("meta")
    turn.emit("turn_started", {"transport": "chat"}, {"message": "原话"})
    [event] = await _recorded(store, turn.turn_id)
    assert event["data"] == {"transport": "chat"} and event["body"] is None


async def test_values_jsonb_refuses_are_cleaned_up_front():
    """JSONB 不收 \\u0000、NaN、Infinity：一条这样的事件会让同一批的两百条一起写不进去。"""
    class Color(Enum):
        RED = "red"

    turn, store, _ = _turn()
    turn.emit("routed", {"scores": {Color.RED: float("nan"), "b": float("inf")},
                         "path": Color.RED, "agents": ("order", "billing"), "ok": True},
              {"text": "前\x00后"})
    [event] = await store.events_of(turn.turn_id)
    assert "\\u0000" not in event.body and "NaN" not in event.data and "Infinity" not in event.data
    decoded = event.to_dict()
    assert decoded["data"] == {"scores": {"red": None, "b": None}, "path": "red",
                               "agents": ["order", "billing"], "ok": True}
    assert decoded["body"] == {"text": "前后"}


async def test_ids_and_text_from_the_client_are_cleaned_before_they_reach_the_database():
    """
    conv_id 和原话都是客户端带来的：NUL、孤立代理字符 Postgres 存不下，超长的 conv_id 会撑破
    索引项。一条这样的事件以前会让同一批里别的轮次一起写不进去。
    """
    stats = EventLogStats()
    store = MemoryEventStore("staff", stats=stats)
    turn = TurnRecorder(new_turn_id(), instance="staff", conv_id="c\x00" + "x" * 9000 + "\ud800",
                        mode="full", sink=store, stats=stats)
    turn.emit("turn_started", {"note": "坏\ud800字"}, {"message": "前\x00后\udfff"}, call_id="\ud83d" * 300)

    [event] = await store.events_of(turn.turn_id)
    assert event.conv_id == "c" + "x" * 199 and len(event.call_id) == 200
    for text in (event.conv_id, event.call_id, event.data, event.body):
        text.encode("utf-8")                             # 编得成 UTF-8，没有 NUL
        assert "\x00" not in text and "\\u0000" not in text
    assert event.to_dict()["body"]["message"].startswith("前后")


async def test_request_id_is_cut_to_64_characters():
    """request_id 在客户实例上由客户端决定（X-Request-ID），只截不信。"""
    turn, store, _ = _turn(request_id="x" * 300)
    turn.emit("turn_started", {})
    [event] = await store.events_of(turn.turn_id)
    assert event.request_id == "x" * 64


def test_turn_ids_are_hex_and_sort_by_time():
    first = new_turn_id()
    time.sleep(0.002)
    second = new_turn_id()
    assert re.fullmatch(r"[0-9a-f]{26}", first) and first < second


async def test_events_follow_the_turn_into_tasks_and_nowhere_else():
    """contextvar：建任务时复制 context，所以准备任务、并行专家、轮次之后的后台任务都带着它。"""
    assert events() is NOOP
    turn, store, _ = _turn()

    async def child():
        events().emit("routed", {"path": "single"})

    with bind(turn):
        assert events() is turn
        await asyncio.create_task(child())
    assert events() is NOOP
    await asyncio.create_task(child())                      # 绑定之外：什么都不记
    assert [e.type for e in await store.events_of(turn.turn_id)] == ["routed"]


async def test_off_records_nothing_but_still_hands_out_a_turn_id():
    turn = EventLog.disabled().open_turn(conv_id="c1")
    turn.emit("turn_started", {"transport": "chat"}, {"message": "原话"})
    turn.emit("turn_completed", {})
    assert re.fullmatch(r"[0-9a-f]{26}", turn.turn_id)
    assert turn.events == [] and not turn.recording
    assert turn.terminal == "turn_completed"                 # 流式的 finally 靠它判断，关着也要准
    turn.close()


async def test_totals_add_up_model_and_tool_calls():
    turn, _, _ = _turn()
    turn.emit("model_call", {"usage": {"input_tokens": 100, "output_tokens": 7}})
    turn.emit("model_call", {"usage": {"input_tokens": 50, "output_tokens": 3}})
    turn.emit("model_call", {"status": "blocked"})          # 被保护挡下：没有用量
    turn.emit("tool_call", {"tool": "knowledge_search"})
    assert turn.totals() == {"model_calls": 3, "input_tokens": 150, "output_tokens": 10, "tool_calls": 1}


def _sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


async def test_overlapping_turns_in_one_conversation_are_noted():
    log = await EventLog.open(EventLogConfig(mode="full", instance="staff"))
    before = _sample("turns_concurrent_total")

    first = log.open_turn(conv_id="c-overlap")
    second = log.open_turn(conv_id="c-overlap")
    elsewhere = log.open_turn(conv_id="c-elsewhere")
    assert (first.concurrent, second.concurrent, elsewhere.concurrent) == ([], [first.turn_id], [])

    first.close()
    second.close()
    first.close()                                           # 重复调不出错
    later = log.open_turn(conv_id="c-overlap")
    assert later.concurrent == []
    assert _sample("turns_concurrent_total") - before == 1
    later.close()
    elsewhere.close()


# ── 写入器 ────────────────────────────────────────────────────────────────────

class _Store:
    """
    记账的假存储。`insert` 照 Postgres 的 ON CONFLICT DO NOTHING 去重。

    fail：前几次直接抛；lost_reply：前几次其实写进去了、只是回包丢了（抛 ConnectionResetError）；
    hang：一直不返回。
    """

    def __init__(self, fail: int = 0, lost_reply: int = 0, hang: bool = False):
        self.fail, self.lost_reply, self.hang = fail, lost_reply, hang
        self.attempts = 0
        self.batches: list[list[Event]] = []
        self.rows: dict[tuple[str, int], Event] = {}

    async def insert(self, batch):
        self.attempts += 1
        if self.hang:
            await asyncio.sleep(3600)
        if self.fail:
            self.fail -= 1
            raise OSError("db down")
        for event in batch:
            self.rows.setdefault((event.turn_id, event.seq), event)
        if self.lost_reply:
            self.lost_reply -= 1
            raise ConnectionResetError("reply lost")
        self.batches.append(list(batch))


def _writer_turn(store, **kwargs):
    stats = EventLogStats()
    writer = EventWriter(store, stats, flush_s=0.01, backoff_s=0.001, **kwargs)
    turn = TurnRecorder(new_turn_id(), instance="staff", conv_id="c1", mode="full",
                        sink=writer, stats=stats)
    return writer, turn, stats


async def test_events_are_written_in_batches_and_flushed_on_close():
    store = _Store()
    writer, turn, stats = _writer_turn(store, batch_size=200)
    writer.start()
    for _ in range(450):
        turn.emit("model_call", {"purpose": "answer"})
    await writer.close(timeout_s=5)

    assert sum(len(b) for b in store.batches) == len(store.rows) == 450
    assert max(len(b) for b in store.batches) <= 200 and len(store.batches) >= 3
    assert (stats.written, stats.dropped) == (450, {})


async def test_a_full_queue_drops_and_counts():
    writer, turn, stats = _writer_turn(_Store(), max_queue=5)     # 没启动：队列只进不出
    for _ in range(8):
        turn.emit("model_call", {})
    assert writer.depth == 5 and stats.dropped == {"queue_full": 3}
    await writer.close(timeout_s=0.1)
    assert stats.dropped == {"queue_full": 3, "shutdown": 5}


async def test_retrying_after_a_lost_reply_does_not_write_rows_twice():
    store = _Store(lost_reply=1)
    writer, turn, stats = _writer_turn(store)
    writer.start()
    for _ in range(10):
        turn.emit("model_call", {})
    await writer.close(timeout_s=5)

    assert store.attempts == 2 and len(store.rows) == 10
    assert (stats.written, stats.dropped) == (10, {})
    assert stats.last_error == "ConnectionResetError"        # 只有类名：/health 匿名可读


async def test_retries_give_up_and_count_what_was_lost():
    store = _Store(fail=99)
    writer, turn, stats = _writer_turn(store, retries=3)
    writer.start()
    for _ in range(4):
        turn.emit("model_call", {})
    await writer.close(timeout_s=5)

    assert store.attempts == 4 and store.rows == {}
    assert (stats.written, stats.dropped, stats.last_error) == (0, {"db_error": 4}, "OSError")


class _RowError(Exception):
    """模仿驱动报的"这一行存不下"：带 SQLSTATE 22021（字符不在编码里）。"""

    sqlstate = "22021"


class _PickyStore(_Store):
    """有一行 conv_id 是 bad 的：带着它的批次整批报错，单独写它也报错。"""

    async def insert(self, batch):
        self.attempts += 1
        if any(e.conv_id == "bad" for e in batch):
            raise _RowError("invalid byte sequence")
        for event in batch:
            self.rows.setdefault((event.turn_id, event.seq), event)


async def test_a_row_the_database_refuses_does_not_take_the_batch_with_it():
    store = _PickyStore()
    stats = EventLogStats()
    writer = EventWriter(store, stats, flush_s=0.01, backoff_s=0.001)
    good = TurnRecorder(new_turn_id(), instance="staff", conv_id="c1", mode="full", sink=writer, stats=stats)
    bad = TurnRecorder(new_turn_id(), instance="staff", conv_id="c1", mode="full", sink=writer, stats=stats)
    bad.conv_id = "bad"                                   # 绕过清理，模拟漏网的一行
    good.emit("turn_started", {})
    bad.emit("turn_started", {})
    good.emit("turn_completed", {})
    writer.start()
    await writer.close(timeout_s=5)

    assert sorted(seq for (turn_id, seq) in store.rows if turn_id == good.turn_id) == [0, 1]
    assert (stats.written, stats.dropped) == (2, {"db_error": 1})
    assert store.attempts == 4                            # 整批一次，然后逐行三次；不按退避重试


async def test_shutdown_waits_a_bounded_time_for_a_stuck_database():
    writer, turn, stats = _writer_turn(_Store(hang=True))
    writer.start()
    for _ in range(3):
        turn.emit("model_call", {})
    started = time.monotonic()
    await writer.close(timeout_s=0.2)
    assert time.monotonic() - started < 2
    assert stats.dropped == {"shutdown": 3}

    turn.emit("model_call", {})                             # 关了之后来的：直接计数，不进队列
    assert stats.dropped == {"shutdown": 4}


# ── 存储：进程内和 Postgres 跑同一组断言 ──────────────────────────────────────

def _event(turn_id: str, seq: int, instance: str, conv_id: str, ts: float, body=None, **data) -> Event:
    return Event(turn_id=turn_id, seq=seq, instance=instance, conv_id=conv_id, request_id="r",
                 type="turn_started" if seq == 0 else "turn_completed", agent="", step=None,
                 call_id="", ts=ts, t_ms=float(seq), v=1,
                 data=json.dumps(data or {"seq": seq}), body=None if body is None else json.dumps(body))


async def _store_contract(staff, customer, write):
    """write(store, events) 把事件放进去：进程内那一份直接 put，Postgres 那一份走 insert。"""
    now = time.time()
    conv = f"conv-{uuid.uuid4().hex[:8]}"
    old, recent, ancient, theirs = (new_turn_id() for _ in range(4))
    staff_events = [
        _event(old, 0, staff.instance, conv, now - 40 * DAY_S, {"message": "四十天前问的"}),
        _event(old, 1, staff.instance, conv, now - 40 * DAY_S + 5, {"response": "四十天前答的"}),
        _event(recent, 0, staff.instance, conv, now - 60, {"message": "刚才问的"}),
        _event(ancient, 0, staff.instance, conv, now - 200 * DAY_S, {"message": "两百天前"}),
    ]
    # 同一个 conv_id：客户自己挑的会话 id 撞上了员工的
    customer_events = [_event(theirs, 0, customer.instance, conv, now - 200 * DAY_S, {"message": "客户的"})]
    await write(staff, staff_events)
    await write(customer, customer_events)

    # 写进去读得回
    assert [e.to_dict() for e in await staff.events_of(recent)] == [staff_events[2].to_dict()]
    assert [e.seq for e in await staff.events_of(old)] == [0, 1]
    # 按时间段挑轮次（先开始的在前）、几轮一次取完：运维检查和重放用
    assert await staff.turn_ids_between(now - 50 * DAY_S) == [old, recent]
    by_turn = await staff.events_of_turns([old, recent, theirs])
    assert set(by_turn) == {old, recent} and [e.seq for e in by_turn[old]] == [0, 1]
    # 按实例隔离
    assert await staff.events_of(theirs) == []
    assert await staff.turn_ids_of(conv) == [recent, old, ancient]          # 新的在前
    assert await customer.turn_ids_of(conv) == [theirs]

    # 保留期：只动本实例、只动过期的；先删整行，再清剩下那些的原文
    retention = Retention(staff, body_days=30, row_days=180, stats=EventLogStats())
    assert await retention.run_once(now) == {"bodies_cleared": 2, "rows_deleted": 1}
    cleared = await staff.events_of(old)
    assert [e.body for e in cleared] == [None, None]
    assert [e.data for e in cleared] == [e.data for e in staff_events[:2]]   # 结构原样
    assert (await staff.events_of(recent))[0].body is not None
    assert await staff.events_of(ancient) == []
    [kept] = await customer.events_of(theirs)                               # 比 180 天还老，但不归它管
    assert kept.body is not None
    assert retention.health()["last_run"] == now

    # 删除请求：按 turn_started 原文里的 user_id 找会话，只删本实例的
    forget_conv, user = f"conv-forget-{uuid.uuid4().hex[:8]}", f"u-{uuid.uuid4().hex[:8]}"
    mine, others = new_turn_id(), new_turn_id()
    await write(staff, [_event(mine, 0, staff.instance, forget_conv, now, {"user_id": user, "message": "删掉我"})])
    await write(customer, [_event(others, 0, customer.instance, forget_conv, now, {"user_id": user})])
    assert await staff.conv_ids_of_user(user) == [forget_conv]
    assert await staff.delete_conversation(forget_conv) == 1
    assert await staff.events_of(mine) == [] and len(await customer.events_of(others)) == 1


async def test_memory_store_contract():
    stats = EventLogStats()
    staff, customer = MemoryEventStore("staff", stats=stats), MemoryEventStore("customer")

    async def write(store, batch):
        for event in batch:
            store.put(event)

    # 进程内那一份是一个进程一个实例；为了验隔离，两个实例的事件放进同一个缓冲
    customer._turns = staff._turns
    await _store_contract(staff, customer, write)
    assert stats.written == 5                            # 员工那边写进去的：四条加删除请求那一条


async def test_memory_store_keeps_the_latest_turns_only():
    store = MemoryEventStore("staff", max_turns=3)
    turn_ids = [new_turn_id() for _ in range(5)]
    for turn_id in turn_ids:
        store.put(_event(turn_id, 0, "staff", "c1", time.time()))
    assert set(await store.turn_ids_of("c1")) == set(turn_ids[2:])


@pytest.mark.skipif(not TEST_PG_DSN, reason="未设置 TEST_POSTGRES_DSN")
async def test_postgres_store_contract():
    from app.storage.pg import Database

    db = Database(TEST_PG_DSN)
    await db.connect(embedding_dim=256)
    try:
        # 共用的测试库里可能有别的数据：实例名每次换一个，保留期的计数才准
        tag = uuid.uuid4().hex[:8]
        staff, customer = PgEventStore(db, f"staff-{tag}"), PgEventStore(db, f"customer-{tag}")

        async def write(store, batch):
            await store.insert(batch)

        await _store_contract(staff, customer, write)

        # 同一批再写一次（第一次其实写进去了、只是回包丢了）：不写出重复行
        turn_id = new_turn_id()
        batch = [_event(turn_id, seq, staff.instance, "c-retry", time.time(), {"n": seq}) for seq in range(3)]
        await staff.insert(batch)
        await staff.insert(batch)
        assert [e.seq for e in await staff.events_of(turn_id)] == [0, 1, 2]

        # 写入器 → Postgres：走完整的一条路
        stats = EventLogStats()
        writer = EventWriter(staff, stats, flush_s=0.01)
        writer.start()
        turn = TurnRecorder(new_turn_id(), instance=staff.instance, conv_id="c-writer", mode="full",
                            sink=writer, stats=stats)
        turn.emit("turn_started", {"transport": "chat"}, {"message": "原话\x00带个零字符"})
        turn.emit("turn_completed", {"outcome": "answered", "score": float("nan")}, {"response": "答"})
        await writer.close(timeout_s=5)
        recorded = [e.to_dict() for e in await staff.events_of(turn.turn_id)]
        assert [(e["type"], e["body"]) for e in recorded] == [
            ("turn_started", {"message": "原话带个零字符"}), ("turn_completed", {"response": "答"})]
        assert recorded[1]["data"] == {"outcome": "answered", "score": None}

        # 建表语句在已有数据的库上再跑一遍不出错，数据还在
        await db._init_schema(256)
        assert len(await staff.events_of(turn.turn_id)) == 2
    finally:
        await db.close()


@pytest.mark.skipif(not TEST_PG_DSN, reason="未设置 TEST_POSTGRES_DSN")
async def test_postgres_audit_and_tickets_point_back_to_the_turn():
    from app.storage.conversation_log import PgConversationLog
    from app.storage.pg import Database
    from app.tickets.ticket_store import PgTicketStore

    db = Database(TEST_PG_DSN)
    await db.connect(embedding_dim=256)
    try:
        conv = f"conv-{uuid.uuid4().hex[:8]}"
        log = PgConversationLog(db)
        await db.execute(
            "INSERT INTO conversation_log (conv_id, user_id, role, content, created_at)"
            " VALUES ($1, 'u0', 'user', '加列之前', $2)", conv, time.time())
        await log.append(conv, "u1", "user", "员工这边", turn_id="t-staff", instance="staff")
        await log.append(conv, "u2", "user", "客户那边", turn_id="t-customer", instance="customer")

        staff_view = await log.get_conversation(conv, instance="staff")
        assert [(e.content, e.turn_id, e.instance) for e in staff_view] == [
            ("加列之前", "", ""), ("员工这边", "t-staff", "staff")]
        assert len(await log.get_conversation(conv)) == 3

        # 按用户删除事件时从审计找会话：本实例写的，加上分不出实例的老记录
        who, legacy, staff_conv, customer_conv = (f"u-{uuid.uuid4().hex[:8]}", f"{conv}-a", f"{conv}-b",
                                                  f"{conv}-c")
        await db.execute(
            "INSERT INTO conversation_log (conv_id, user_id, role, content, created_at)"
            " VALUES ($1, $2, 'user', '加列之前', $3)", legacy, who, time.time())
        await log.append(staff_conv, who, "user", "员工这边", turn_id="t-1", instance="staff")
        await log.append(staff_conv, who, "assistant", "回答", turn_id="t-1", instance="staff")
        await log.append(customer_conv, who, "user", "客户那边", turn_id="t-2", instance="customer")
        assert await log.conv_ids_of_user(who, "staff") == [legacy, staff_conv]
        assert await log.conv_ids_of_user(who, "customer") == [legacy, customer_conv]

        tickets = PgTicketStore(db)
        ticket = await tickets.create(conv_id=conv, user_id="u1", reason="用户要求人工", turn_id="t-staff")
        assert (await tickets.get(ticket.id)).turn_id == "t-staff"
        assert ticket.to_dict()["turn_id"] == "t-staff"
        assert (await tickets.update_status(ticket.id, "assigned")).turn_id == "t-staff"
        assert ticket.id in {t.id for t in await tickets.list()}

        # 两个实例共用这个库，总有一个先升级。旧镜像按 `SELECT *` 读这两张表、再按字段名构造
        # 对象：表上多一列它就报错。所以这两张表的列一列都不能多
        for table, columns in OLD_COLUMNS.items():
            rows = await db.fetch(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = current_schema() AND table_name = $1 ORDER BY ordinal_position", table)
            assert [r["column_name"] for r in rows] == columns, table

        # 审计的清理连旁边那张表一起清
        assert await log.purge_before(time.time() + 1) >= 3
        orphans = await db.fetchrow("SELECT COUNT(*) AS n FROM conversation_log_turns t"
                                    " LEFT JOIN conversation_log c ON c.seq = t.seq WHERE c.seq IS NULL")
        assert orphans["n"] == 0
    finally:
        await db.close()


# 加事件日志之前的列：旧镜像（4822cef、40e9547）就按这些字段构造 Ticket / LogEntry
OLD_COLUMNS = {
    "tickets": ["id", "conv_id", "user_id", "request_id", "reason", "intent", "urgency",
                "answer_confidence", "last_message", "status", "created_at", "updated_at"],
    "conversation_log": ["seq", "conv_id", "user_id", "request_id", "role", "content", "agent_type",
                         "created_at"],
}


@pytest.mark.skipif(not TEST_PG_DSN, reason="未设置 TEST_POSTGRES_DSN")
async def test_postgres_drops_only_the_rows_it_cannot_store():
    """绕过记录器的清理，直接塞两行 Postgres 存不下的：同一批里正常的那一轮照样写进去。"""
    from app.storage.pg import Database

    db = Database(TEST_PG_DSN)
    await db.connect(embedding_dim=256)
    try:
        store = PgEventStore(db, f"staff-{uuid.uuid4().hex[:8]}")
        stats = EventLogStats()
        writer = EventWriter(store, stats, flush_s=0.01, backoff_s=0.001)
        ok, nul, long = new_turn_id(), new_turn_id(), new_turn_id()
        now = time.time()
        writer.put(_event(ok, 0, store.instance, "c-ok", now, {"message": "正常"}))
        writer.put(_event(nul, 0, store.instance, "c\x00nul", now))
        # 随机的长串：压缩之后仍然超过索引项的上限（一串重复字符会被压得很小，存得进去）
        writer.put(_event(long, 0, store.instance, secrets.token_hex(4500), now))
        writer.put(_event(ok, 1, store.instance, "c-ok", now, {"response": "答"}))
        writer.start()
        await writer.close(timeout_s=10)

        assert [e.seq for e in await store.events_of(ok)] == [0, 1]
        assert (stats.written, stats.dropped) == (2, {"db_error": 2})
    finally:
        await db.close()


# ── 审计和工单：老库补列 ──────────────────────────────────────────────────────

_OLD_AUDIT = """
CREATE TABLE conversation_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, conv_id TEXT NOT NULL, user_id TEXT NOT NULL,
    request_id TEXT NOT NULL DEFAULT '', role TEXT NOT NULL, content TEXT NOT NULL,
    agent_type TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
);
INSERT INTO conversation_log (conv_id, user_id, request_id, role, content, agent_type, created_at)
VALUES ('c1', 'u0', 'r0', 'user', '加列之前', '', 1.0);
"""

_OLD_TICKETS = """
CREATE TABLE tickets (
    id TEXT PRIMARY KEY, conv_id TEXT NOT NULL, user_id TEXT NOT NULL,
    request_id TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL, intent TEXT NOT NULL DEFAULT '',
    urgency TEXT NOT NULL DEFAULT '', answer_confidence REAL NOT NULL DEFAULT 0.0,
    last_message TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open',
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
INSERT INTO tickets (id, conv_id, user_id, reason, created_at, updated_at)
VALUES ('tk_old', 'c1', 'u0', '加列之前的工单', 1.0, 1.0);
"""


def _old_database(path, script: str) -> None:
    db = sqlite3.connect(path)
    db.executescript(script)
    db.commit()
    db.close()


async def test_an_old_audit_log_gets_the_new_columns_and_reads_by_instance(tmp_path):
    path = tmp_path / "conversations.db"
    _old_database(path, _OLD_AUDIT)
    log = ConversationLog(db_path=str(path))
    await log.append("c1", "u1", "assistant", "员工这边", turn_id="t1", instance="staff")
    await log.append("c1", "u2", "user", "客户那边", turn_id="t2", instance="customer")

    staff = await log.get_conversation("c1", instance="staff")
    assert [(e.content, e.turn_id, e.instance) for e in staff] == [
        ("加列之前", "", ""), ("员工这边", "t1", "staff")]
    assert staff[1].to_dict()["turn_id"] == "t1"
    assert len(await log.get_conversation("c1")) == 3           # 不给实例：照旧全给
    await log.close()
    await ConversationLog(db_path=str(path)).close()           # 补列可以重复跑


async def test_an_old_ticket_table_gets_turn_id(tmp_path):
    path = tmp_path / "tickets.db"
    _old_database(path, _OLD_TICKETS)
    tickets = TicketStore(db_path=str(path))
    assert (await tickets.get("tk_old")).turn_id == ""
    ticket = await tickets.create(conv_id="c1", user_id="u1", reason="用户要求人工", turn_id="t1")
    assert (await tickets.get(ticket.id)).to_dict()["turn_id"] == "t1"
    await tickets.close()
    await TicketStore(db_path=str(path)).close()


# ── 配置 ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("env, mode, instance, reason", [
    ({}, "off", "", ""),
    ({"AI_HELPDESK_EVENT_LOG": "full"}, "off", "", "AI_HELPDESK_INSTANCE"),
    ({"AI_HELPDESK_EVENT_LOG": "full", "AI_HELPDESK_INSTANCE": "staff"}, "full", "staff", ""),
    ({"AI_HELPDESK_EVENT_LOG": "meta", "AI_HELPDESK_INSTANCE": "customer"}, "meta", "customer", ""),
    ({"AI_HELPDESK_EVENT_LOG": "on", "AI_HELPDESK_INSTANCE": " Staff "}, "full", "staff", ""),
    ({"AI_HELPDESK_EVENT_LOG": "verbose", "AI_HELPDESK_INSTANCE": "staff"}, "off", "staff", "不认识"),
    ({"AI_HELPDESK_EVENT_LOG": "full", "AI_HELPDESK_INSTANCE": "prod"}, "off", "", "AI_HELPDESK_INSTANCE"),
])
def test_the_switch_needs_an_instance(env, mode, instance, reason):
    """标错实例的后果是员工那边看得到客户的轮次，所以实例没设或写错时宁可不记。"""
    config = EventLogConfig.from_env(env)
    assert (config.mode, config.instance) == (mode, instance)
    assert reason in config.disabled_reason if reason else config.disabled_reason == ""


def test_days_and_the_read_switch():
    config = EventLogConfig.from_env({"AI_HELPDESK_EVENT_LOG_BODY_DAYS": "0",
                                      "AI_HELPDESK_EVENT_LOG_DAYS": "half a year",
                                      "AI_HELPDESK_EVENT_LOG_READ": "nope"})
    assert (config.body_days, config.row_days, config.read) == (0, 180, False)
    assert len(config.warnings) == 2                           # 认不出来的天数和读开关都说一声

    customer = EventLogConfig.from_env({"AI_HELPDESK_INSTANCE": "customer"})
    assert customer.read and any("READ=off" in w for w in customer.warnings)


# ── 接口 ──────────────────────────────────────────────────────────────────────

class _Intent:
    def __init__(self, intent=IntentCategory.MISSING_PART):
        self.intent = intent
        self.intent_group = intent.value
        self.urgency = UrgencyLevel.LOW
        self.confidence = 0.9
        self.entities = {"order_id": ["SO-7731"]}
        self.fast_path = True
        self.strategy = "ensemble"
        self.source_scores: dict = {}
        self.latency_ms = 1.0


class _Memory:
    cross_session = False

    def __init__(self, delay_s: float = 0.0):
        self.delay_s = delay_s

    async def get_context(self, user_id, conv_id, query=""):
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        return MemoryContext(recent_messages=[], relevant_history=[], user_profile={}, summary="")

    async def add_message(self, user_id, conv_id, role, content):
        return None

    async def update_profile(self, user_id, conv_id):
        return None


class _Tools:
    async def call(self, name, params, context=None, **_):
        return ToolResult(success=True, tool_name=name, data=[
            {"title": "补件流程", "content": "缺件补发在核实后安排。", "score": 0.6,
             "doc_id": "doc_1", "chunk_id": "c1", "chunk": 0, "retrieval": "hybrid"}])


async def _desk(monkeypatch, *, model=None, env=None, memory=None, tickets=None, conv_log=None):
    """api/main 接上假模型（订单专家）和进程内的事件日志。"""
    settings = {"ANTHROPIC_API_KEY": "test-key", "AI_HELPDESK_API_KEY": "",
                "AI_HELPDESK_PROTECT_CHAT": "false", "AI_HELPDESK_FRONTEND_DIST": "/nonexistent",
                "AI_HELPDESK_INSTANCE": "staff", "AI_HELPDESK_EVENT_LOG": "full", **(env or {})}
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    for name in ("APP_ENV", "AI_HELPDESK_TOOL_LOOP", "POSTGRES_DSN"):
        monkeypatch.delenv(name, raising=False)
    sys.modules.pop("app.api.main", None)
    main = importlib.import_module("app.api.main")

    orch = AgentOrchestrator(api_key="test-key")
    for agent in orch._pool[AgentType.ORDER]:
        agent._client = model or ScriptedModel(*[text_step("补件已经登记，三个工作日内发出。")] * 4)

    async def recognize(message, history=None):
        return _Intent(IntentCategory.HUMAN_HANDOFF if "转人工" in message else IntentCategory.MISSING_PART)

    orch.recognize_intent = recognize
    log = await EventLog.open(EventLogConfig.from_env())
    for name, value in {
        "_orchestrator": orch, "_memory": memory or _Memory(), "_tool_manager": _Tools(),
        "_sql_agent": None, "_ticket_store": tickets, "_ticket_notifier": None,
        "_conv_log": conv_log, "_user_auth": None, "_rate_limiter": None, "_event_log": log,
    }.items():
        monkeypatch.setattr(main, name, value)
    return main, log


def _ask(main, message: str = CANARY_MESSAGE, conv_id: str = "c1"):
    return main.ChatRequest(message=message, user_id=CANARY_USER, conv_id=conv_id)


def _frames(chunks: list[str]) -> list[tuple[str, dict]]:
    out = []
    for frame in "".join(chunks).split("\n\n"):
        lines = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
        if "event" in lines:
            out.append((lines["event"], json.loads(lines["data"])))
    return out


async def _stream(main, request) -> list[tuple[str, dict]]:
    response = await main.chat_stream(request, None)
    return _frames([chunk async for chunk in response.body_iterator])


async def test_with_the_log_off_the_response_only_gains_a_turn_id(monkeypatch):
    main, log = await _desk(monkeypatch, env={"AI_HELPDESK_EVENT_LOG": "off"})

    answer = await main.chat(_ask(main), None)
    frames = await _stream(main, _ask(main))
    meta = next(data for kind, data in frames if kind == "meta")
    result = next(data for kind, data in frames if kind == "result")

    assert re.fullmatch(r"[0-9a-f]{26}", answer.turn_id)
    assert meta["turn_id"] == result["turn_id"] != answer.turn_id
    assert not log.recording and await log.store.turn_ids_of("c1") == []
    assert log.stats.written == 0


async def test_a_turn_is_bracketed_by_started_and_completed(monkeypatch):
    main, log = await _desk(monkeypatch)
    answer = await main.chat(_ask(main), None)

    recorded = await _recorded(log.store, answer.turn_id)
    started, completed = recorded[0], recorded[-1]
    assert (started["type"], completed["type"]) == ("turn_started", "turn_completed")
    assert [e["type"] for e in recorded].count("turn_completed") == 1
    assert started["data"]["transport"] == "chat"
    assert started["data"]["loop"] == {"enabled": False} and started["data"]["concurrent"] == []
    assert started["data"]["release"] and "trace_id" in started["data"]
    assert started["body"] == {"user_id": CANARY_USER, "message": CANARY_MESSAGE}
    assert completed["data"]["outcome"] == "answered"
    assert completed["data"]["agent_type"] == answer.agent_type
    assert completed["data"]["latency_ms"] == answer.latency_ms
    assert completed["data"]["partial"] is False and completed["data"]["ticket_id"] is None
    assert completed["body"] == {"response": answer.response}
    assert {e["request_id"] for e in recorded} == {answer.request_id}
    # 结构里没有原话、user_id、回答：清空原文之后剩下的行指不到人
    structure = json.dumps([e["data"] for e in recorded], ensure_ascii=False)
    for text in (CANARY_MESSAGE, CANARY_USER, answer.response):
        assert text not in structure


async def test_the_stream_carries_the_turn_id_and_records_what_was_shown(monkeypatch):
    main, log = await _desk(monkeypatch, model=ScriptedModel(text_step("第一句。", "第二句。")))
    frames = await _stream(main, _ask(main))
    meta = next(data for kind, data in frames if kind == "meta")
    result = next(data for kind, data in frames if kind == "result")
    shown = "".join(data["text"] for kind, data in frames if kind == "delta")

    assert meta["turn_id"] == result["turn_id"]
    recorded = await _recorded(log.store, result["turn_id"])
    started, completed = recorded[0], recorded[-1]
    assert (started["type"], started["data"]["transport"]) == ("turn_started", "stream")
    assert completed["type"] == "turn_completed" and completed["body"] == {"response": shown}
    assert shown == result["response"] and completed["data"]["partial"] is False


async def test_a_handoff_ticket_and_the_audit_point_back_to_the_turn(monkeypatch, tmp_path):
    tickets = TicketStore(db_path=str(tmp_path / "tickets.db"))
    conv_log = ConversationLog(db_path=str(tmp_path / "conversations.db"))
    main, log = await _desk(monkeypatch, tickets=tickets, conv_log=conv_log)

    answer = await main.chat(_ask(main, "我要转人工"), None)
    await asyncio.gather(*list(main._background_tasks))

    assert answer.handoff and answer.ticket_id
    assert (await tickets.get(answer.ticket_id)).turn_id == answer.turn_id
    entries = await conv_log.get_conversation("c1", instance="staff")
    assert [(e.role, e.turn_id, e.instance) for e in entries] == [
        ("user", answer.turn_id, "staff"), ("assistant", answer.turn_id, "staff")]
    completed = (await _recorded(log.store, answer.turn_id))[-1]
    assert completed["data"]["outcome"] == "handoff"
    assert completed["data"]["ticket_id"] == answer.ticket_id
    await tickets.close()
    await conv_log.close()


async def test_the_conversation_endpoint_leaves_out_the_other_instance(monkeypatch, tmp_path):
    """两个实例共用审计表：客户自己挑的 conv_id 撞上员工的会话时，以前两边的记录混在一起返回。"""
    conv_log = ConversationLog(db_path=str(tmp_path / "conversations.db"))
    await conv_log.append("shared", "u0", "user", "加列之前")
    await conv_log.append("shared", "u1", "user", "员工这边", instance="staff")
    await conv_log.append("shared", "x9", "user", "客户那边", instance="customer")
    main, _ = await _desk(monkeypatch, conv_log=conv_log)

    got = await main.get_conversation("shared")
    assert [m["content"] for m in got["messages"]] == ["加列之前", "员工这边"]
    await conv_log.close()


async def _leave_after_the_first_sentence(main, request) -> list[tuple[str, dict]]:
    response = await main.chat_stream(request, None)
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
        if chunk.startswith("event: delta"):
            break
    await response.body_iterator.aclose()                   # 客户端断开：生成器被关掉
    return _frames(chunks)


async def test_a_client_leaving_mid_stream_is_recorded_as_aborted(monkeypatch):
    main, log = await _desk(monkeypatch, model=ScriptedModel(
        text_step("第一句已经发出去了。", "第二句还没来得及发。")))
    frames = await _leave_after_the_first_sentence(main, _ask(main))
    turn_id = next(data for kind, data in frames if kind == "meta")["turn_id"]
    sent = "".join(data["text"] for kind, data in frames if kind == "delta")
    recorded = await _recorded(log.store, turn_id)
    assert recorded[0]["type"] == "turn_started"
    # 作答在另一个任务里，客户端走了它可能还在跑：它的事件排在哪都有可能，终结事件只有这一条
    [aborted] = [e for e in recorded if e["type"] in TERMINAL_TYPES]
    assert aborted["type"] == "turn_aborted"
    assert aborted["data"] == {"stage": "answer", "sent_chars": len(sent)}


async def test_an_orchestrator_error_is_recorded_as_failed(monkeypatch):
    main, log = await _desk(monkeypatch)

    async def run(req):
        raise RuntimeError("编排器出错，回显了原话 SO-7731")

    async def run_stream(req):
        raise RuntimeError("流式编排器出错")
        yield  # noqa: B901 - 让它成为异步生成器

    monkeypatch.setattr(main._orchestrator, "run", run)
    monkeypatch.setattr(main._orchestrator, "run_stream", run_stream)

    with pytest.raises(RuntimeError):
        await main.chat(_ask(main, conv_id="c-fail"), None)
    frames = await _stream(main, _ask(main, conv_id="c-fail"))
    assert [kind for kind, _ in frames][-1] == "error"

    for turn_id in await log.store.turn_ids_of("c-fail"):
        recorded = await _recorded(log.store, turn_id)
        started, failed = recorded[0], recorded[-1]
        assert started["type"] == "turn_started" and failed["type"] == "turn_failed"
        assert failed["data"] == {"error_type": "RuntimeError", "stage": "answer"}
        assert "出错" in failed["body"]["error"]                # 异常信息可能回显输入：放原文那一半
    assert len(await log.store.turn_ids_of("c-fail")) == 2


# ── 审计：出错、中途断开的轮次也留档 ──────────────────────────────────────────
#
# 审计以前只在作答成功那一步写：出错的轮次、流式中途断开的轮次，客户的原话在审计里都找不到，
# 等于给了客户一个躲开留档的办法。现在收尾时补写客户的原话；客户已经看到的半截回答也写一条
# （作答 Agent 取路由给的主专家），一个字都没看到就只有原话。

async def _audit(main, conv_log, conv_id: str) -> list[tuple[str, str, str, str]]:
    await asyncio.gather(*list(main._background_tasks))
    return [(e.role, e.content, e.agent_type, e.turn_id)
            for e in await conv_log.get_conversation(conv_id, instance="staff")]


async def test_a_completed_turn_is_audited_once(monkeypatch, tmp_path):
    conv_log = ConversationLog(db_path=str(tmp_path / "conversations.db"))
    main, _ = await _desk(monkeypatch, conv_log=conv_log)
    answer = await main.chat(_ask(main, conv_id="c-chat"), None)
    frames = await _stream(main, _ask(main, conv_id="c-stream"))
    result = next(data for kind, data in frames if kind == "result")

    assert await _audit(main, conv_log, "c-chat") == [
        ("user", CANARY_MESSAGE, "", answer.turn_id),
        ("assistant", answer.response, answer.agent_type, answer.turn_id)]
    assert await _audit(main, conv_log, "c-stream") == [
        ("user", CANARY_MESSAGE, "", result["turn_id"]),
        ("assistant", result["response"], result["agent_type"], result["turn_id"])]
    await conv_log.close()


async def test_a_failed_turn_still_leaves_the_customers_words_in_the_audit(monkeypatch, tmp_path):
    conv_log = ConversationLog(db_path=str(tmp_path / "conversations.db"))
    main, log = await _desk(monkeypatch, conv_log=conv_log)

    async def run(req):
        raise RuntimeError("编排器出错")

    async def run_stream(req):
        raise RuntimeError("流式编排器出错")
        yield  # noqa: B901 - 让它成为异步生成器

    monkeypatch.setattr(main._orchestrator, "run", run)
    monkeypatch.setattr(main._orchestrator, "run_stream", run_stream)
    with pytest.raises(RuntimeError):
        await main.chat(_ask(main, conv_id="c-fail"), None)
    await _stream(main, _ask(main, conv_id="c-fail"))

    # 两轮客户都什么也没看到：各留一条原话，没有助手那一条
    turn_ids = await log.store.turn_ids_of("c-fail")
    assert len(turn_ids) == 2
    assert sorted(await _audit(main, conv_log, "c-fail")) == sorted(
        ("user", CANARY_MESSAGE, "", turn_id) for turn_id in turn_ids)
    await conv_log.close()


async def test_a_stream_failing_halfway_keeps_the_half_the_customer_saw(monkeypatch, tmp_path):
    conv_log = ConversationLog(db_path=str(tmp_path / "conversations.db"))
    main, _ = await _desk(monkeypatch, conv_log=conv_log)

    async def run_stream(req):
        yield {"type": "routing", "primary_agent": "order", "supporting_agents": [],
               "reason": "规则命中", "confidence": 0.9}
        yield {"type": "delta", "text": "补件已经登记，"}
        raise RuntimeError("答到一半出错")

    monkeypatch.setattr(main._orchestrator, "run_stream", run_stream)
    frames = await _stream(main, _ask(main))
    turn_id = next(data for kind, data in frames if kind == "meta")["turn_id"]

    assert [kind for kind, _ in frames][-1] == "error"
    assert await _audit(main, conv_log, "c1") == [
        ("user", CANARY_MESSAGE, "", turn_id), ("assistant", "补件已经登记，", "order", turn_id)]
    await conv_log.close()


async def test_a_client_leaving_mid_stream_leaves_what_it_saw_in_the_audit(monkeypatch, tmp_path):
    conv_log = ConversationLog(db_path=str(tmp_path / "conversations.db"))
    main, _ = await _desk(monkeypatch, conv_log=conv_log, model=ScriptedModel(
        text_step("第一句已经发出去了。", "第二句还没来得及发。")))
    frames = await _leave_after_the_first_sentence(main, _ask(main))
    turn_id = next(data for kind, data in frames if kind == "meta")["turn_id"]
    primary = next(data for kind, data in frames if kind == "routing")["primary_agent"]

    assert await _audit(main, conv_log, "c1") == [
        ("user", CANARY_MESSAGE, "", turn_id), ("assistant", "第一句已经发出去了。", primary, turn_id)]
    await conv_log.close()


async def test_a_cancelled_chat_still_leaves_the_customers_words_in_the_audit(monkeypatch, tmp_path):
    """/chat 被取消（停机、客户端断开）时也一样：请求还在准备阶段，客户什么都没看到。"""
    conv_log = ConversationLog(db_path=str(tmp_path / "conversations.db"))
    main, log = await _desk(monkeypatch, conv_log=conv_log, memory=_Memory(delay_s=5))
    task = asyncio.create_task(main.chat(_ask(main), None))
    await asyncio.sleep(0.05)                                # 停在读记忆那一步
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    [turn_id] = await log.store.turn_ids_of("c1")
    assert await _audit(main, conv_log, "c1") == [("user", CANARY_MESSAGE, "", turn_id)]
    await conv_log.close()


async def test_without_an_instance_the_log_stays_off_and_health_says_why(monkeypatch):
    main, log = await _desk(monkeypatch, env={"AI_HELPDESK_INSTANCE": ""})
    answer = await main.chat(_ask(main), None)

    assert answer.turn_id and not log.recording and log.store is None
    health = (await main.health())["event_log"]
    assert health["mode"] == "off" and "AI_HELPDESK_INSTANCE" in health["disabled_reason"]


async def test_health_reports_what_the_log_wrote(monkeypatch):
    main, log = await _desk(monkeypatch)
    answer = await main.chat(_ask(main), None)
    health = (await main.health())["event_log"]
    assert {k: health[k] for k in ("mode", "instance", "read", "store", "queue_depth", "dropped")} == {
        "mode": "full", "instance": "staff", "read": True, "store": "memory",
        "queue_depth": 0, "dropped": 0}
    assert health["written"] == len(await log.store.events_of(answer.turn_id))
    assert health["retention"]["body_days"] == 30


async def test_two_overlapping_turns_in_one_conversation_are_flagged(monkeypatch):
    """后端不串行化同一会话，只靠前端禁用发送按钮。v1 只记下来，不拦。"""
    main, log = await _desk(monkeypatch, memory=_Memory(delay_s=0.05))
    first, second = await asyncio.gather(main.chat(_ask(main), None), main.chat(_ask(main), None))
    later = await main.chat(_ask(main), None)

    concurrent = {}
    for answer in (first, second, later):
        started = (await _recorded(log.store, answer.turn_id))[0]
        concurrent[answer.turn_id] = started["data"]["concurrent"]
    assert sorted([concurrent[first.turn_id], concurrent[second.turn_id]], key=len) == [
        [], [first.turn_id]]
    assert concurrent[later.turn_id] == []
