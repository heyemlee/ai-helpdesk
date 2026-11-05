"""
取消：检查点、执行器中断、吞异常的地方、入口的收尾，以及"断开即取消"开关的两种状态。

取消的语义（docs/limitations.md「取消」一节）：下一个检查点生效；SQL 立即中断；
正在进行的那一次模型调用打断不了，跑完之后结果丢弃、不再发新的调用。
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from ai_sql_agent.core import ModelTransport, RetryPolicy, get_settings
from ai_sql_agent.core.model_access import ModelAccessRegistry
from ai_sql_agent.core.run_context import (
    CancelToken,
    RunCancelled,
    RunContext,
    cancellable_sleep,
    check_cancelled,
    use_run,
)
from ai_sql_agent.core.trace import stage
from ai_sql_agent.execution import SQLiteMCPExecutor
from ai_sql_agent.execution.objects import MCPExecutionRequest
from ai_sql_agent.retrieval.access import ALLOW_ALL
from ai_sql_agent.auth import authorization_scope
from ai_sql_agent.runs import RunManager, SQLiteRunStore
from ai_sql_agent.runs.disconnect import watch_disconnect


def _big_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(3000)])
    conn.commit()
    conn.close()


# 三表笛卡尔积：2.7e10 行，不打断的话要跑到执行时限
_SLOW_SQL = "SELECT count(*) FROM t a, t b, t c"


class CancelTokenTest(unittest.TestCase):
    def test_first_cancel_wins_and_callbacks_run_once(self):
        token, called = CancelToken(), []
        remove = token.on_cancel(lambda: called.append("a"))
        token.on_cancel(lambda: called.append("b"))
        remove()
        self.assertTrue(token.cancel("api"))
        self.assertFalse(token.cancel("shutdown"))
        self.assertEqual((token.reason, called), ("api", ["b"]))
        late = []
        token.on_cancel(lambda: late.append(1))
        self.assertEqual(late, [1], "已经取消时注册的回调立刻调用")
        with self.assertRaises(RunCancelled) as raised:
            token.raise_if_cancelled()
        self.assertEqual(raised.exception.reason, "api")

    def test_run_cancelled_is_not_a_runtime_or_model_error(self):
        from ai_sql_agent.core.errors import ModelCallError

        # 路由会把 RuntimeError 当模型失败退回规则版；pipeline 会把 ModelCallError 转去降级重放
        self.assertFalse(issubclass(RunCancelled, (RuntimeError, ValueError, ModelCallError)))

    def test_outside_a_run_checkpoints_do_nothing(self):
        check_cancelled()
        slept = []
        cancellable_sleep(0.25, slept.append)
        self.assertEqual(slept, [0.25])

    def test_backoff_wakes_up_as_soon_as_the_run_is_cancelled(self):
        run = RunContext(run_id="r")
        threading.Timer(0.05, run.token.cancel, args=("api",)).start()
        started = time.monotonic()
        with use_run(run), self.assertRaises(RunCancelled):
            cancellable_sleep(5.0)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_deadline_turns_into_a_cancel_at_the_next_checkpoint(self):
        run = RunContext(run_id="r", deadline=time.monotonic() - 1)
        with use_run(run), self.assertRaises(RunCancelled) as raised:
            check_cancelled()
        self.assertEqual(raised.exception.reason, "deadline")


class SqliteExecutorCancelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "big.db"
        _big_database(self.db)
        self.executor = SQLiteMCPExecutor("big_db", self.db, max_execution_seconds=30)

    def tearDown(self):
        self.tmp.cleanup()

    def _execute(self, run):
        with use_run(run), authorization_scope(ALLOW_ALL, user_id="u"):
            return self.executor.execute(MCPExecutionRequest(database="big_db", sql=_SLOW_SQL))

    def test_a_running_statement_is_interrupted_within_milliseconds(self):
        run = RunContext(run_id="r")
        threading.Timer(0.2, run.token.cancel, args=("api",)).start()
        started = time.monotonic()
        result = self._execute(run)
        elapsed = time.monotonic() - started
        self.assertFalse(result.success)
        self.assertEqual(result.error, "查询已取消。", "取消和超时要分得清")
        self.assertLess(elapsed, 2.0)

    def test_an_already_cancelled_run_never_reaches_the_database(self):
        run = RunContext(run_id="r")
        run.token.cancel("api")
        with mock.patch.object(SQLiteMCPExecutor, "_connect", side_effect=AssertionError("连了库")):
            with self.assertRaises(RunCancelled):
                self._execute(run)

    def test_timeouts_still_read_as_timeouts(self):
        executor = SQLiteMCPExecutor("big_db", self.db, max_execution_seconds=0.2)
        with use_run(RunContext(run_id="r")), authorization_scope(ALLOW_ALL, user_id="u"):
            result = executor.execute(MCPExecutionRequest(database="big_db", sql=_SLOW_SQL))
        self.assertIn("超时", result.error)


PG_DSN = os.getenv("AI_SQL_PGVECTOR_DSN", "")
# 4 亿行的笛卡尔积：只读关卡放行（pg_sleep 不放行），不打断要跑好几秒
_SLOW_PG_SQL = "SELECT count(*) FROM generate_series(1, 20000) a, generate_series(1, 20000) b"


@unittest.skipUnless(PG_DSN, "未配置 AI_SQL_PGVECTOR_DSN，跳过 PostgreSQL 取消测试")
class PostgresExecutorCancelTest(unittest.TestCase):
    """执行中被取消：向服务端发取消请求（cancel_safe），语句立刻中断，文案和超时分开。"""

    def test_a_running_statement_is_cancelled_on_the_server(self):
        from ai_sql_agent.execution import PostgresMCPExecutor

        executor = PostgresMCPExecutor("pg_cancel_db", PG_DSN, max_execution_seconds=30)
        run = RunContext(run_id="r")
        threading.Timer(0.3, run.token.cancel, args=("api",)).start()
        started = time.monotonic()
        try:
            with use_run(run), authorization_scope(ALLOW_ALL, user_id="u"):
                result = executor.execute(MCPExecutionRequest(database="pg_cancel_db", sql=_SLOW_PG_SQL))
        finally:
            executor.close()
        self.assertFalse(result.success)
        self.assertEqual(result.error, "查询已取消。")
        self.assertLess(time.monotonic() - started, 5.0)

    def test_an_already_cancelled_run_never_borrows_a_connection(self):
        from ai_sql_agent.execution import PostgresMCPExecutor

        executor = PostgresMCPExecutor("pg_cancel_db", PG_DSN)
        run = RunContext(run_id="r")
        run.token.cancel("api")
        try:
            with mock.patch.object(executor._pool, "acquire", side_effect=AssertionError("借了连接")):
                with use_run(run), authorization_scope(ALLOW_ALL, user_id="u"), \
                        self.assertRaises(RunCancelled):
                    executor.execute(MCPExecutionRequest(database="pg_cancel_db", sql="SELECT 1"))
        finally:
            executor.close()


class TransportCancelTest(unittest.TestCase):
    def _transport(self, handler, *, sleeper=time.sleep):
        return ModelTransport(
            "coder",
            api_key="test-key",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            retry_policy=RetryPolicy(max_retries=3, backoff_base_seconds=5.0, backoff_max_seconds=5.0),
            sleeper=sleeper,
            access_registry=ModelAccessRegistry(),
        )

    def test_no_request_is_sent_once_the_run_is_cancelled(self):
        sent = []
        transport = self._transport(lambda request: sent.append(request) or httpx.Response(200, json={}))
        run = RunContext(run_id="r")
        run.token.cancel("api")
        with use_run(run), self.assertRaises(RunCancelled):
            transport.post_json("http://model.test/v1/chat/completions", {"model": "m"}, model="m")
        self.assertEqual(sent, [])

    def test_a_cancel_during_backoff_stops_the_retries(self):
        sent = []

        def handler(request):
            sent.append(request)
            return httpx.Response(503, json={"error": "busy"})

        transport = self._transport(handler)
        run = RunContext(run_id="r")
        threading.Timer(0.1, run.token.cancel, args=("api",)).start()
        started = time.monotonic()
        with use_run(run), self.assertRaises(RunCancelled):
            transport.post_json("http://model.test/v1/chat/completions", {"model": "m"}, model="m")
        self.assertLess(time.monotonic() - started, 2.0, "退避 5 秒，取消要在退避中途生效")
        self.assertEqual(len(sent), 1, "取消之后不再发重试")


class SwallowSitesTest(unittest.TestCase):
    """吞掉异常、而且有副作用的三处：取消必须原样穿过去，副作用一个都不能留下。"""

    def test_keyword_extractor_does_not_latch_to_rules_on_cancel(self):
        from ai_sql_agent.pipeline.local_clients import HybridKeywordExtractor

        class Cancelling:
            def extract(self, query, glossary=""):
                raise RunCancelled("api")

        extractor = HybridKeywordExtractor(model_extractor=Cancelling())
        with self.assertRaises(RunCancelled):
            extractor.extract("查询供应商")
        self.assertFalse(extractor._model_failed)
        self.assertIsNotNone(extractor._model_extractor)

    def test_embedding_client_does_not_cool_down_on_cancel(self):
        from ai_sql_agent.retrieval.embedding_client import RemoteEmbeddingClient

        client = RemoteEmbeddingClient(url="http://emb.test/v1/embeddings", api_key="k",
                                       model="m", dimensions=4)
        with mock.patch.object(client, "_request", side_effect=RunCancelled("api")):
            with self.assertRaises(RunCancelled):
                client.embed_texts(["供应商"])
        self.assertTrue(client.available, "取消不是服务故障，不能进冷却")

    def test_self_check_does_not_report_not_run_on_cancel(self):
        from ai_sql_agent.validation import SemanticSelfChecker

        class Cancelling:
            def generate(self, prompt):
                raise RunCancelled("api")

        with self.assertRaises(RunCancelled):
            SemanticSelfChecker(Cancelling()).run("查询供应商", "SELECT 1")


class PipelineCancelTest(unittest.TestCase):
    """取消之后不进降级、不重放历史 SQL，run 记为 cancelled。"""

    def test_a_cancel_mid_generation_stops_the_pipeline_without_degrading(self):
        from ai_sql_agent.generation import CoderModelClient
        from ai_sql_agent.pipeline import Text2SQLPipeline

        history = mock.Mock(return_value=[("erp_db", "SELECT 1")])
        pipeline = Text2SQLPipeline(sql_history_lookup=history)
        with tempfile.TemporaryDirectory() as tmp:
            manager = RunManager(SQLiteRunStore(Path(tmp) / "runs.db"))
            run = manager.start(entry="http_query", caller="c", user_id="u", session_id="s",
                                query="查询准时率低于0.9的供应商名称")
            original = CoderModelClient.generate_sql

            def generate_then_cancel(self, prompt):
                run.token.cancel("api")  # 模拟取消在生成途中到达：这一次调用照常返回
                return original(self, prompt)

            with mock.patch.object(CoderModelClient, "generate_sql", generate_then_cancel), \
                 mock.patch.object(Text2SQLPipeline, "_degrade", side_effect=AssertionError("走了降级")):
                with self.assertRaises(RunCancelled):
                    with manager.running(run):
                        pipeline.run("查询准时率低于0.9的供应商名称", user_id="u")
            history.assert_not_called()
            stored = manager.get(run.run_id)
            self.assertEqual((stored["status"], stored["cancel_reason"]), ("cancelled", "api"))
            events = manager.events(run.run_id)
            self.assertFalse(any(event["type"] == "tool.sql" for event in events),
                             "生成之后被取消：SQL 不该再执行")
            finished = [event for event in events if event["type"] == "stage.finished"]
            self.assertEqual(finished[-1]["attrs"]["status"], "ok", "进行中的那一步照常跑完")


class WatchDisconnectTest(unittest.TestCase):
    def test_disconnect_signals_once_and_still_waits_for_the_work(self):
        class Request:
            polls = 0

            async def is_disconnected(self):
                Request.polls += 1
                return Request.polls >= 2

        calls = []

        async def work():
            await asyncio.sleep(0.15)
            return "done"

        result = asyncio.run(watch_disconnect(Request(), work(), lambda: calls.append(1),
                                              poll_seconds=0.01))
        self.assertEqual((result, calls), ("done", [1]))

    def test_no_signal_when_the_client_stays(self):
        class Request:
            async def is_disconnected(self):
                return False

        calls = []

        async def work():
            return 42

        self.assertEqual(asyncio.run(watch_disconnect(Request(), work(), lambda: calls.append(1))), 42)
        self.assertEqual(calls, [])


class A2ACancelTest(unittest.TestCase):
    """
    A2A 流式：中途经 /runs 取消；"断开即取消"开关的两种状态。

    TestClient 会把整个响应收完才返回，模拟不了"流到一半"，所以和 test_stream_resources 一样
    直接驱动 `_stream_events` 这个生成器；app 的状态（审计、run 管理器）用 lifespan 建好的那一份。
    """

    @classmethod
    def setUpClass(cls):
        from ai_sql_agent.api.app import app

        cls.app = app
        cls.ctx = TestClient(app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def setUp(self):
        settings = get_settings()
        self._saved = (settings.cancel_on_disconnect, self.app.state.service)
        self.started = threading.Event()
        test = self

        class SlowService:
            """每 20 ms 过一个阶段边界，最多 10 秒；被取消就在下一个边界停。"""

            def run(self, **_kwargs):
                test.started.set()
                for _ in range(500):
                    with stage("retrieval"):
                        time.sleep(0.02)
                raise AssertionError("应该已经被取消")

        self.app.state.service = SlowService()

    def tearDown(self):
        settings = get_settings()
        settings.cancel_on_disconnect, self.app.state.service = self._saved

    def _stream(self, after_first_frame):
        """跑一条流；收到首帧后调 `after_first_frame(run_id, generator)`，它返回 True 就当客户端走了。"""
        from ai_sql_agent.a2a import server as a2a
        from ai_sql_agent.auth.objects import Principal

        app = self.app

        class Request:
            pass

        request = Request()
        request.app = app

        async def consume():
            permit = a2a._admission()
            permit.try_acquire()
            run_id = uuid.uuid4().hex
            generator = a2a._stream_events(
                request, "r", Principal.anonymous_principal("u_cancel"),
                a2a.ParsedMessage("查询准时率低于0.9的供应商名称", f"c_{run_id[:8]}", "u_cancel", "m1"),
                run_id, permit,
            )
            frames = []
            async for chunk in generator:
                frames.append(json.loads(chunk[len("data: "):])["result"])
                if len(frames) == 1 and await after_first_frame(run_id, generator):
                    break
            return run_id, frames, permit.active

        return asyncio.run(consume())

    def _wait_status(self, run_id, wanted, timeout=5.0):
        deadline = time.monotonic() + timeout
        run = None
        while time.monotonic() < deadline:
            run = self.app.state.runs.get(run_id)
            if run and run.get("status") == wanted:
                return run
            time.sleep(0.05)
        self.fail(f"run {run_id} 没有变成 {wanted}：{run}")

    def test_cancelling_a_live_stream_ends_it_with_a_canceled_terminal(self):
        async def cancel_via_http(run_id, _generator):
            self.assertTrue(await asyncio.to_thread(self.started.wait, 5))
            response = await asyncio.to_thread(self.client.post, f"/runs/{run_id}/cancel")
            self.assertEqual(response.status_code, 202, response.text)
            return False

        run_id, frames, _active = self._stream(cancel_via_http)
        terminal = frames[-1]
        # 契约 v1.13 §4.4：被本连接以外的途径取消发 canceled（v1.12 时只能用 failed + error 表达）。
        # metadata.error 里的 cancelled: 是取消原因，state 用的是 A2A 规范的拼写 canceled。
        self.assertEqual((terminal["kind"], terminal["final"]), ("status-update", True))
        self.assertEqual(terminal["status"]["state"], "canceled")
        self.assertEqual(terminal["metadata"]["error"], "cancelled: api")
        self.assertFalse(any(frame["kind"] == "artifact-update" for frame in frames))

        run = self._wait_status(run_id, "cancelled")
        self.assertEqual(run["cancel_reason"], "api")
        audit = self.app.state.audit.get_by_trace(run["trace_id"]) if run["trace_id"] else None
        rows = [row for row in self.app.state.audit.recent(limit=20, user_id="u_cancel")
                if row.get("run_id") == run_id]
        self.assertEqual([row["status_code"] for row in rows], [499], "取消照写一条审计，状态码 499")
        del audit

    def _disconnect(self):
        async def leave(_run_id, generator):
            self.assertTrue(await asyncio.to_thread(self.started.wait, 5))
            await generator.aclose()  # 客户端断开：服务端的生成器被收掉，走 finally
            return True

        run_id, _frames, active = self._stream(leave)
        # 首帧之后立刻断开也要把并发名额还回去（以前首帧的 yield 在 try 外面，名额会漏）
        self.assertEqual(active, 0)
        return run_id

    def test_disconnect_cancels_the_run_when_the_switch_is_on(self):
        get_settings().cancel_on_disconnect = True
        run_id = self._disconnect()
        run = self._wait_status(run_id, "cancelled")
        self.assertEqual(run["cancel_reason"], "client_disconnected")

    def test_disconnect_cancellation_is_on_by_default(self):
        from ai_sql_agent.core.settings import SqlAgentSettings

        self.assertIs(SqlAgentSettings.model_fields["cancel_on_disconnect"].default, True,
                      "契约 v1.13 §5.4：调用方断开后，我方在下一个检查点停止")

    def test_a_cancelled_send_returns_a_canceled_task(self):
        from ai_sql_agent.a2a import server as a2a

        def cancelled(*_args, **_kwargs):
            raise RunCancelled("api")

        body = {"jsonrpc": "2.0", "id": "req-1", "method": "message/send",
                "params": {"message": {"kind": "message", "role": "user", "messageId": "m1",
                                       "contextId": f"c_send_{uuid.uuid4().hex[:8]}",
                                       "parts": [{"kind": "text", "text": "查询准时率低于0.9的供应商名称"}],
                                       "metadata": {"user_id": "u_cancel"}}}}
        with mock.patch.object(a2a, "_run_bound", cancelled):
            response = self.client.post("/a2a", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        task = response.json()["result"]
        # 契约 v1.13 §4.4：send 返回的 Task 和流式终态用同一套取值
        self.assertEqual(task["status"]["state"], "canceled")
        self.assertEqual(task["metadata"]["error"], "cancelled: api")
        self.assertEqual(task["artifacts"], [])

    def test_disconnect_leaves_the_run_alone_when_the_switch_is_off(self):
        get_settings().cancel_on_disconnect = False
        run_id = self._disconnect()
        time.sleep(0.3)
        self.assertEqual(self.app.state.runs.get(run_id)["status"], "running",
                         "开关关掉就回到契约 v1.12 §5.4 的行为：断开后不中止")
        # 收尾：别让这个慢 run 一直跑到测试结束
        self.assertEqual(self.app.state.runs.cancel(run_id, "api"), "cancel_requested")
        self._wait_status(run_id, "cancelled")


if __name__ == "__main__":
    unittest.main()
