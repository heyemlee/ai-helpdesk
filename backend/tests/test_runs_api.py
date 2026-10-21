"""
排队模式下的接口（api/main.py + app/runs/）：真的编排器和整轮流程，存储、派发和帧流用进程内的实现。

- /chat/stream 提交成 run，worker 跑完，流里多一帧 run、每帧带 id、meta 和 result 带 run；
- 断线之后从 Last-Event-ID 接着读；
- worker 在提交点之后、落副作用的半路死掉：接手的那一遍不再作答，客户看到的、审计、记忆、
  工单都是同一个回答，各只有一份；
- 降级：最后一档不出网、不建工单；第 2 档检索走直检；
- 非流式 /chat 等结果；积压超限 429；进死信的 run 转人工；
- 读 run 要么是同一个人、要么拿着 run_id，别的实例读不到。
"""
import asyncio
import importlib
import json
import sys
import time
from types import SimpleNamespace

import pytest
from conftest import ScriptedModel, text_step
from fastapi import HTTPException
from redis.exceptions import ConnectionError as RedisConnectionError

from app.agents.agent_orchestrator import AgentOrchestrator, AgentType
from app.core.intent_recognizer import IntentCategory, UrgencyLevel
from app.events import EventLog, EventLogConfig
from app.memory.conversation_memory import MemoryContext
from app.runs.broker import MemoryBroker
from app.runs.channel import MemoryChannel
from app.runs.config import RunConfig
from app.runs.degrade import DegradePolicy
from app.runs.memo import MemoryMemo
from app.runs.records import DEAD, FAILED, RUNNING, SUCCEEDED
from app.runs.service import RunService
from app.runs.store import MemoryRunStore
from app.runs.worker import RunWorker
from app.storage.conversation_log import ConversationLog
from app.tickets.ticket_store import TicketStore
from app.tools.tool_manager import ToolResult

ANSWER = "补件已经登记，我们会尽快安排发出。"


class _Intent:
    def __init__(self, intent=IntentCategory.MISSING_PART):
        self.intent = intent
        self.intent_group = intent.value
        self.urgency = UrgencyLevel.LOW
        self.confidence = 0.9
        self.entities = {"order_id": ["SO-7731"]}
        self.fast_path = False
        self.strategy = "ensemble"
        self.source_scores: dict = {}
        self.latency_ms = 1.0


class _OnceMemory:
    """工作记忆的假实现：add_turn 按去重键只写一次，记下写了什么。"""

    cross_session = False

    def __init__(self):
        self.turns: list[tuple[str, str]] = []
        self.keys: set[str] = set()
        self.block_first = None
        # 接下来几次读、写记忆时 Redis 连不上
        self.context_failures = 0
        self.write_failures = 0

    async def get_context(self, user_id, conv_id, query=""):
        if self.context_failures:
            self.context_failures -= 1
            raise RedisConnectionError("Connection refused")
        return MemoryContext(recent_messages=[], relevant_history=[], user_profile={}, summary="")

    async def add_message(self, user_id, conv_id, role, content):
        self.turns.append((role.value, content))

    async def add_turn(self, user_id, conv_id, user_text, answer, *, dedup_key=""):
        if self.block_first is not None:
            gate, self.block_first = self.block_first, None
            await gate.wait()
        if self.write_failures:
            self.write_failures -= 1
            raise RedisConnectionError("Connection reset by peer")
        if dedup_key in self.keys:
            return False
        self.keys.add(dedup_key)
        self.turns.append((user_text, answer))
        return True

    async def update_profile(self, user_id, conv_id):
        return None


class _Tools:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def call(self, name, params, context=None, **_):
        self.calls.append(("call", name))
        return ToolResult(success=True, tool_name=name, data=[
            {"title": "补件流程", "content": "缺件补发在核实后安排。", "score": 0.6,
             "doc_id": "doc_1", "chunk_id": "c1", "chunk": 0, "retrieval": "hybrid"}])

    async def search_with_rewrite(self, name, query, top_k=5, context=None, **_):
        self.calls.append(("rewrite", name))
        return await self.call(name, {"query": query})


def _config(**overrides) -> RunConfig:
    values = dict(mode="queued", role="all", concurrency=2, lease_s=0.3, max_attempts=3, queue_max=50,
                  redispatch_after_s=0.2, retention_days=0, wait_s=5.0)
    values.update(overrides)
    return RunConfig(**values)


async def _queued(monkeypatch, tmp_path, *, steps=None, policy=None, start_worker=True, **config):
    settings = {"ANTHROPIC_API_KEY": "test-key", "AI_HELPDESK_API_KEY": "",
                "AI_HELPDESK_PROTECT_CHAT": "false", "AI_HELPDESK_FRONTEND_DIST": "/nonexistent",
                "AI_HELPDESK_INSTANCE": "staff", "AI_HELPDESK_EVENT_LOG": "full"}
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    for name in ("APP_ENV", "AI_HELPDESK_TOOL_LOOP", "POSTGRES_DSN", "AI_HELPDESK_RUN_MODE"):
        monkeypatch.delenv(name, raising=False)
    sys.modules.pop("app.api.main", None)
    main = importlib.import_module("app.api.main")

    orch = AgentOrchestrator(api_key="test-key")
    model = ScriptedModel(*(steps or [text_step(ANSWER)] * 6))
    for agent in orch._pool[AgentType.ORDER]:
        agent._client = model

    async def recognize(message, history=None):
        return _Intent(IntentCategory.HUMAN_HANDOFF if "转人工" in message else IntentCategory.MISSING_PART)

    orch.recognize_intent = recognize
    tickets = TicketStore(db_path=str(tmp_path / "tickets.db"))
    conv_log = ConversationLog(db_path=str(tmp_path / "conversations.db"))
    memory = _OnceMemory()
    tools = _Tools()
    log = await EventLog.open(EventLogConfig.from_env())
    for name, value in {
        "_orchestrator": orch, "_memory": memory, "_tool_manager": tools,
        "_sql_agent": None, "_ticket_store": tickets, "_ticket_notifier": None,
        "_conv_log": conv_log, "_user_auth": None, "_rate_limiter": None, "_event_log": log,
        "_RUN_CONFIG": _config(**config),
    }.items():
        monkeypatch.setattr(main, name, value)

    store, broker, channel = MemoryRunStore("staff"), MemoryBroker("staff"), MemoryChannel()
    runs = RunService(store=store, broker=broker, channel=channel, config=main._RUN_CONFIG, heartbeat_s=0.05)
    monkeypatch.setattr(main, "_runs", runs)
    desk = SimpleNamespace(main=main, store=store, broker=broker, channel=channel, memo=MemoryMemo(),
                           runs=runs, tickets=tickets, conv_log=conv_log, memory=memory, tools=tools,
                           model=model, log=log, workers=[], policy=policy)

    def new_worker(owner=None):
        worker = RunWorker(store=store, broker=broker, channel=channel, memo=desk.memo,
                           execute=main._execute_run, config=main._RUN_CONFIG, policy=policy,
                           on_dead=main._handoff_dead_run, owner=owner, block_ms=20, backlog_ttl_s=0.0)
        desk.workers.append(worker)
        return worker

    desk.new_worker = new_worker
    if start_worker:
        await new_worker("w1").start()
    return desk


async def _close(desk):
    for worker in desk.workers:
        if not worker.draining:
            await worker.drain(1.0)
    await asyncio.gather(*list(desk.main._background_tasks), return_exceptions=True)
    await desk.tickets.close()
    await desk.conv_log.close()


def _ask(main, message="补件到哪了", conv_id="c1"):
    return main.ChatRequest(message=message, user_id="u1", conv_id=conv_id)


def _frames(chunks):
    out = []
    for frame in "".join(chunks).split("\n\n"):
        lines = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
        if "event" in lines:
            out.append((lines["event"], json.loads(lines["data"]), lines.get("id")))
    return out


async def _read(response, stop_after=None):
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
        if stop_after is not None and chunk.startswith("id: ") and f"\nevent: {stop_after}\n" in chunk:
            break
    await response.body_iterator.aclose()
    return _frames(chunks)


async def _until(store, run_id, *statuses, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = await store.get(run_id)
        if run is not None and run.status in statuses:
            return run
        await asyncio.sleep(0.01)
    raise AssertionError(f"run {run_id} 没有到 {statuses}：{await store.get(run_id)}")


async def _audit(desk, conv_id="c1"):
    await asyncio.gather(*list(desk.main._background_tasks), return_exceptions=True)
    return [(e.role, e.content) for e in await desk.conv_log.get_conversation(conv_id, instance="staff")]


# ── 一轮的样子 ────────────────────────────────────────────────────────────────

async def test_a_queued_stream_carries_the_run_and_event_ids(monkeypatch, tmp_path):
    desk = await _queued(monkeypatch, tmp_path)
    try:
        frames = await _read(await desk.main.chat_stream(_ask(desk.main), None))
        kinds = [kind for kind, _, _ in frames]
        assert kinds[0] == "run" and kinds[1] == "attempt" and kinds[-1] == "result"
        assert {"meta", "routing", "delta"} <= set(kinds)
        run_id = frames[0][1]["run_id"]
        assert all(event_id for kind, _, event_id in frames if kind != "run")    # 都能续流

        meta = next(data for kind, data, _ in frames if kind == "meta")
        result = frames[-1][1]
        assert meta["run"] == result["run"] == {"run_id": run_id, "attempt": 1}
        assert result["degrade"]["level"] == 0
        assert result["response"] == ANSWER == "".join(d["text"] for k, d, _ in frames if k == "delta")

        run = await _until(desk.store, run_id, SUCCEEDED)
        assert run.turn_id == meta["turn_id"] == result["turn_id"]
        assert run.result["response"] == ANSWER and run.result["intent"] == meta["intent"]
        assert run.checkpoint["final"]["response"] == ANSWER
        assert await _audit(desk) == [("user", "补件到哪了"), ("assistant", ANSWER)]
        assert desk.memory.turns == [("补件到哪了", ANSWER)]
    finally:
        await _close(desk)


async def test_a_restored_turn_still_says_which_run_and_degrade_level_it_was(monkeypatch, tmp_path):
    """刷新之后工作台从事件日志重建这一轮：run 和降级档位要和当轮发出去的一样，不然重建出来的少了这两条说明。"""
    desk = await _queued(monkeypatch, tmp_path, policy=DegradePolicy(thresholds=(1, 99, 99, 99)))
    try:
        frames = await _read(await desk.main.chat_stream(_ask(desk.main), None))
        meta = next(data for kind, data, _ in frames if kind == "meta")
        result = frames[-1][1]
        projected = await desk.main.get_turn(result["turn_id"])
        assert projected["meta"]["run"] == meta["run"] and projected["meta"]["degrade"] == meta["degrade"]
        assert projected["result"]["run"] == result["run"] and projected["result"]["degrade"]["level"] == 1
    finally:
        await _close(desk)


async def test_a_client_that_drops_reconnects_with_the_last_event_id(monkeypatch, tmp_path):
    desk = await _queued(monkeypatch, tmp_path)
    try:
        first = await _read(await desk.main.chat_stream(_ask(desk.main), None), stop_after="meta")
        run_id = first[0][1]["run_id"]
        last_id = first[-1][2]
        await _until(desk.store, run_id, SUCCEEDED)            # 客户端走了，这一轮照样跑完

        rest = await _read(await desk.main.run_events(run_id, None, None, last_id))
        assert [k for k, _, _ in rest][-1] == "result"
        assert "meta" not in [k for k, _, _ in rest] and "run" not in [k for k, _, _ in rest]
        assert rest[-1][1]["response"] == ANSWER

        status = await desk.main.get_run(run_id, None)
        assert status["status"] == SUCCEEDED and status["attempts"][0]["outcome"] == SUCCEEDED
        assert "message" not in status
        with pytest.raises(HTTPException) as bad:
            await desk.main.run_events(run_id, None, None, "not-an-id")
        assert bad.value.status_code == 422
    finally:
        await _close(desk)


async def test_a_worker_dying_after_the_commit_point_does_not_answer_twice(monkeypatch, tmp_path):
    """
    第一遍答完、写了提交点、建了工单，写记忆时进程被杀。接手的那一遍照着提交点收尾：
    工单还是那一张，审计和记忆各一份，客户最后看到的就是第一遍定下来的那个回答。
    """
    # 专家答完，在最后一行声明要人工（[[handoff:yes]]）：这一轮既有模型写的回答，又要建工单
    desk = await _queued(monkeypatch, tmp_path, start_worker=False,
                         steps=[text_step("第一遍的回答。\n[[handoff:yes]]"), text_step("不该出现的第二遍。")])
    try:
        gate = asyncio.Event()                            # 永远不放行：第一遍卡在写记忆
        desk.memory.block_first = gate
        doomed = desk.new_worker("doomed")
        await doomed.start()
        response = await desk.main.chat_stream(_ask(desk.main, conv_id="c-crash"), None)
        reader = asyncio.create_task(_read(response))
        run_id = None
        for _ in range(500):
            runs = list(desk.store._runs.values())
            if runs and runs[0].checkpoint and await desk.tickets.list():
                run_id = runs[0].run_id
                break
            await asyncio.sleep(0.01)
        assert run_id, "第一遍没走到提交点"

        # 进程被杀：任务全部取消，不交还、不确认、不写结局
        for task in [*doomed._loops, *list(doomed._spawned)]:
            task.cancel()
        await asyncio.gather(*doomed._loops, *list(doomed._spawned), return_exceptions=True)
        doomed._draining = True
        assert (await desk.store.get(run_id)).status == RUNNING

        await desk.new_worker("survivor").start()
        frames = await asyncio.wait_for(reader, 10)
        done = await _until(desk.store, run_id, SUCCEEDED)

        assert done.attempt == 2 and done.result["response"].startswith("第一遍的回答。")
        assert desk.model.requests and len(desk.model.requests) == 1     # 第二遍一次模型都没调
        tickets = await desk.tickets.list()
        assert len(tickets) == 1 and done.result["ticket_id"] == tickets[0].id
        assert desk.memory.turns == [("补件到哪了", done.result["response"])]
        audit = await _audit(desk, "c-crash")
        assert audit == [("user", "补件到哪了"), ("assistant", done.result["response"])]

        # 客户端看到的：第二遍开始时一帧 attempt，之后是同一个回答（整段一个 delta）
        attempts = [d["attempt"] for k, d, _ in frames if k == "attempt"]
        assert attempts == [1, 2]
        after = frames[[k for k, _, _ in frames].index("attempt", 2):]
        assert "".join(d["text"] for k, d, _ in after if k == "delta") == done.result["response"]
        assert frames[-1][1]["run"]["resumed_from"] == done.checkpoint["turn_id"]
    finally:
        await _close(desk)


async def test_a_redis_blip_after_the_commit_point_is_retried_from_the_checkpoint(monkeypatch, tmp_path):
    """写记忆时 Redis 闪断：回答已经定了，交还重试，接手的那一遍按提交点收尾，不重新作答。"""
    desk = await _queued(monkeypatch, tmp_path, steps=[text_step("定下来的回答。"), text_step("不该出现的。")])
    try:
        desk.memory.write_failures = 1
        submitted = await desk.main.submit_run(_ask(desk.main, conv_id="c-blip"), None)
        done = await _until(desk.store, submitted["run_id"], SUCCEEDED)
        assert done.attempt == 2 and done.history[0]["outcome"] == "retry"
        assert done.result["response"] == "定下来的回答。" and len(desk.model.requests) == 1
        assert desk.memory.turns == [("补件到哪了", "定下来的回答。")]
        assert await _audit(desk, "c-blip") == [("user", "补件到哪了"), ("assistant", "定下来的回答。")]
    finally:
        await _close(desk)


async def test_a_redis_blip_before_the_answer_reruns_the_turn(monkeypatch, tmp_path):
    desk = await _queued(monkeypatch, tmp_path)
    try:
        desk.memory.context_failures = 1
        submitted = await desk.main.submit_run(_ask(desk.main, conv_id="c-early"), None)
        done = await _until(desk.store, submitted["run_id"], SUCCEEDED)
        assert done.attempt == 2 and done.checkpoint is not None and done.result["response"] == ANSWER
        assert await _audit(desk, "c-early") == [("user", "补件到哪了"), ("assistant", ANSWER)]
    finally:
        await _close(desk)


async def test_a_failure_that_does_not_go_away_ends_in_the_dead_letter(monkeypatch, tmp_path):
    desk = await _queued(monkeypatch, tmp_path, max_attempts=2)
    try:
        desk.memory.write_failures = 10
        submitted = await desk.main.submit_run(_ask(desk.main, conv_id="c-stuck"), None)
        done = await _until(desk.store, submitted["run_id"], DEAD)
        assert [h.get("outcome") for h in done.history] == ["retry", "retry", DEAD]
        assert done.result["handoff"] is True
    finally:
        await _close(desk)


async def test_an_error_in_the_turn_itself_is_still_final(monkeypatch, tmp_path):
    desk = await _queued(monkeypatch, tmp_path)
    try:
        async def broken(req):
            raise RuntimeError("编排器的 bug")
            yield  # noqa: B901 - 让它成为异步生成器

        monkeypatch.setattr(desk.main._orchestrator, "run_stream", broken)
        submitted = await desk.main.submit_run(_ask(desk.main, conv_id="c-bug"), None)
        done = await _until(desk.store, submitted["run_id"], FAILED)
        assert done.attempt == 1 and "编排器的 bug" in done.error
        assert await _audit(desk, "c-bug") == [("user", "补件到哪了")]
    finally:
        await _close(desk)


# ── 降级 ──────────────────────────────────────────────────────────────────────

async def test_the_last_degrade_level_answers_without_any_model_call_or_ticket(monkeypatch, tmp_path):
    desk = await _queued(monkeypatch, tmp_path, policy=DegradePolicy(thresholds=(1, 1, 1, 1)))
    try:
        frames = await _read(await desk.main.chat_stream(_ask(desk.main), None))
        result = frames[-1][1]
        assert result["service_unavailable"] is True and result["unavailable_reason"] == "overloaded"
        assert result["handoff"] is False and result["ticket_id"] is None
        assert result["degrade"]["level"] == 4 and "model_calls" in result["degrade"]["off"]
        assert desk.model.requests == [] and desk.tools.calls == []
        assert await desk.tickets.list() == []
    finally:
        await _close(desk)


async def test_level_two_searches_directly_and_level_three_stops_delegating(monkeypatch, tmp_path):
    desk = await _queued(monkeypatch, tmp_path, policy=DegradePolicy(thresholds=(1, 1, 99, 99)))
    try:
        frames = await _read(await desk.main.chat_stream(_ask(desk.main), None))
        meta = next(data for kind, data, _ in frames if kind == "meta")
        assert meta["degrade"]["level"] == 2
        assert meta["knowledge"]["mode"] == "direct"                 # 不改写、不重排
        assert ("rewrite", "knowledge_search") not in desk.tools.calls
    finally:
        await _close(desk)

    paused = desk.main._prefetch(DegradePolicy(thresholds=(1, 1, 1, 99)).level_for(5))
    assert paused.data_paused and not paused.will_query("SO-7731 到哪了")


# ── 非流式、背压、死信、读权限 ───────────────────────────────────────────────

async def test_plain_chat_waits_for_the_run(monkeypatch, tmp_path):
    desk = await _queued(monkeypatch, tmp_path)
    try:
        response = await desk.main.chat(_ask(desk.main), None)
        body = json.loads(response.body)
        assert body["response"] == ANSWER and body["run"]["attempt"] == 1
        assert body["intent"] == "missing_part" and body["conv_id"] == "c1"
    finally:
        await _close(desk)


async def test_a_full_queue_is_refused_with_retry_after(monkeypatch, tmp_path):
    desk = await _queued(monkeypatch, tmp_path, start_worker=False, queue_max=1)
    try:
        first = await desk.main.submit_run(_ask(desk.main, "第一条"), None, "key-1")
        with pytest.raises(HTTPException) as refused:
            await desk.main.chat_stream(_ask(desk.main, "第二条"), None)
        assert refused.value.status_code == 429 and int(refused.value.headers["Retry-After"]) >= 2
        # 客户端拿同一个 Idempotency-Key 重试第一条：拿回原来的 run，不算新负载，不被拒
        again = await desk.main.submit_run(_ask(desk.main, "第一条"), None, "key-1")
        assert again["run_id"] == first["run_id"]
    finally:
        await _close(desk)


async def test_the_same_idempotency_key_maps_to_one_run(monkeypatch, tmp_path):
    desk = await _queued(monkeypatch, tmp_path, start_worker=False)
    try:
        a = await desk.main.submit_run(_ask(desk.main), None, "retry-1")
        b = await desk.main.submit_run(_ask(desk.main), None, "retry-1")
        assert a["run_id"] == b["run_id"] and len(desk.store._runs) == 1
        with pytest.raises(HTTPException) as conflict:
            await desk.main.submit_run(_ask(desk.main, "另一句话"), None, "retry-1")
        assert conflict.value.status_code == 422
    finally:
        await _close(desk)


async def test_a_dead_run_is_handed_to_a_person(monkeypatch, tmp_path):
    desk = await _queued(monkeypatch, tmp_path, start_worker=False, max_attempts=1)
    try:
        submitted = await desk.main.submit_run(_ask(desk.main, conv_id="c-dead"), None)
        run_id = submitted["run_id"]
        await desk.store.claim(run_id, "crashed", 0.01)              # 领走之后就没了
        await asyncio.sleep(0.02)
        await desk.new_worker("w2").start()
        done = await _until(desk.store, run_id, DEAD)
        assert done.result["handoff"] is True and done.result["ticket_status"] == "open"
        [ticket] = await desk.tickets.list()
        assert ticket.id == done.result["ticket_id"] and "失联" in ticket.reason
        assert await _audit(desk, "c-dead") == [("user", "补件到哪了")]
        assert "人工" in done.result["response"]
    finally:
        await _close(desk)


async def test_a_run_from_another_instance_or_a_malformed_id_is_not_found(monkeypatch, tmp_path):
    desk = await _queued(monkeypatch, tmp_path, start_worker=False)
    try:
        customer = MemoryRunStore("customer")
        from app.runs.records import RunRecord, new_run_id
        other, _ = await customer.create(RunRecord(new_run_id(), "customer", "c9", "x", "客户的问题"))
        for run_id in (other.run_id, "../../etc", "0" * 31):
            with pytest.raises(HTTPException) as missing:
                await desk.main.get_run(run_id, None)
            assert missing.value.status_code == 404
    finally:
        await _close(desk)


async def test_probes(monkeypatch, tmp_path):
    desk = await _queued(monkeypatch, tmp_path, start_worker=False)
    try:
        assert (await desk.main.livez()) == {"status": "alive"}
        monkeypatch.setattr(desk.main, "_infra_redis", None)
        monkeypatch.setattr(desk.main, "_pg_database", None)
        ready = await desk.main.readyz()
        assert ready["ready"] is True and ready["mode"] == "queued"
        monkeypatch.setattr(desk.main, "_draining", True)
        assert (await desk.main.readyz()).status_code == 503
    finally:
        await _close(desk)
