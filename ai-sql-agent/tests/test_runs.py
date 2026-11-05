"""
运行记录（run）与事件日志：事件编号与截断、存储、生命周期、孤儿清理，以及 HTTP 这一层。

库调用（benchmark、脚本直接调 pipeline）没有 run，所有钩子都是空操作——那一面由
`test_prompt_snapshot.py` 守着：有没有这层，模型收到的东西逐项相同。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from ai_sql_agent.core import reload_settings
from ai_sql_agent.core.metrics import render_metrics
from ai_sql_agent.core.run_context import RunCancelled, current_run, emit, stage_frame
from ai_sql_agent.core.trace import stage
from ai_sql_agent.runs import RunManager, RunRecorder, SQLiteRunStore
from ai_sql_agent.runs.events import RunEvent, cap_attrs
from ai_sql_agent.runs.store import orphans


def _dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


class CapAttrsTest(unittest.TestCase):
    def test_long_strings_are_truncated_and_say_by_how_much(self):
        capped = cap_attrs({"sql": "x" * 9000, "note": "y" * 2000}, max_bytes=100_000)
        self.assertTrue(capped["sql"].startswith("x" * 8192))
        self.assertIn("+808 chars", capped["sql"])
        self.assertIn("+976 chars", capped["note"])

    def test_oversized_attrs_drop_the_largest_fields_and_record_which(self):
        capped = cap_attrs({"small": 1, "big": "z" * 1000, "bigger": "w" * 1024}, max_bytes=200)
        self.assertEqual(capped["small"], 1)
        self.assertIn("bigger", capped["_dropped"])
        self.assertNotIn("bigger", capped)

    def test_unknown_objects_become_strings(self):
        capped = cap_attrs({"value": object(), "nested": {"set": {1, 2}}}, max_bytes=10_000)
        self.assertIsInstance(capped["value"], str)
        self.assertEqual(sorted(capped["nested"]["set"]), [1, 2])


class _Store(SQLiteRunStore):
    """可以按需让写入失败的 SQLite 存储。"""

    fail = False

    def append_events(self, events):
        if self.fail:
            raise OSError("disk full")
        super().append_events(events)


class RunRecorderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = _Store(Path(self.tmp.name) / "runs.db")
        self.store.create_run({"run_id": "r1", "query": "q"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_sequence_is_strictly_increasing_and_flushes_at_stage_end(self):
        recorder = RunRecorder("r1", self.store, max_attr_bytes=16384)
        recorder("stage.started", "routing", None, {})
        self.assertEqual(self.store.list_events("r1"), [], "stage 没结束之前只在缓冲里")
        recorder("model.call", "routing", None, {"status": "ok"})
        recorder("stage.finished", "routing", None, {"status": "ok"})
        events = self.store.list_events("r1")
        self.assertEqual([event["seq"] for event in events], [1, 2, 3])
        self.assertEqual([event["type"] for event in events],
                         ["stage.started", "model.call", "stage.finished"])
        self.assertEqual(self.store.get_run("r1")["last_seq"], 3)

    def test_a_failing_store_stops_persisting_but_never_raises(self):
        recorder = RunRecorder("r1", self.store, max_attr_bytes=16384)
        self.store.fail = True
        recorder("stage.finished", "routing", None, {})
        self.assertFalse(recorder.persist)
        self.store.fail = False
        recorder("stage.finished", "retrieval", None, {})
        self.assertEqual(self.store.list_events("r1"), [], "一次失败之后这个 run 不再落库，免得日志刷屏")
        self.assertIn("ai_sql_run_events_dropped_total", render_metrics())

    def test_subscribers_see_every_event_even_when_storage_is_off(self):
        seen = []
        recorder = RunRecorder("r1", self.store, max_attr_bytes=16384, subscribers=[seen.append])
        recorder.persist = False
        recorder("stage.started", "routing", 2, {"database": "erp_db"})
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], RunEvent)
        self.assertEqual((seen[0].stage, seen[0].step), ("routing", 2))


class SQLiteRunStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SQLiteRunStore(Path(self.tmp.name) / "runs.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip_and_pagination(self):
        self.store.create_run({"run_id": "r1", "trace_id": "t1", "entry": "http_query",
                               "caller": "c", "user_id": "u", "session_id": "s", "query": "q"})
        self.store.append_events([
            RunEvent(run_id="r1", seq=index, ts="2026-01-01T00:00:00.000Z", type="stage.started",
                     stage="routing", attrs={"index": index})
            for index in range(1, 6)
        ])
        self.store.finish_run("r1", status="completed", usage={"total_tokens": 7},
                              outcome={"execution_status": "success"})
        run = self.store.get_run("r1")
        self.assertEqual((run["status"], run["entry"], run["last_seq"]), ("completed", "http_query", 5))
        self.assertEqual(run["usage"], {"total_tokens": 7})
        self.assertEqual(run["outcome"], {"execution_status": "success"})
        self.assertTrue(run["finished_at"])
        first = self.store.list_events("r1", after_seq=0, limit=2)
        rest = self.store.list_events("r1", after_seq=first[-1]["seq"], limit=10)
        self.assertEqual([event["seq"] for event in first + rest], [1, 2, 3, 4, 5])
        self.assertEqual(rest[0]["attrs"], {"index": 3})

    def test_duplicate_events_are_ignored(self):
        self.store.create_run({"run_id": "r1", "query": "q"})
        event = RunEvent(run_id="r1", seq=1, ts="2026-01-01T00:00:00.000Z", type="run.started")
        self.store.append_events([event])
        self.store.append_events([event])
        self.assertEqual(len(self.store.list_events("r1")), 1)

    def test_purge_only_removes_runs_finished_before_the_cutoff(self):
        for run_id in ("old", "new", "live"):
            self.store.create_run({"run_id": run_id, "query": "q"})
            self.store.append_events([RunEvent(run_id=run_id, seq=1, ts="t", type="run.started")])
        self.store.finish_run("old", status="completed")
        self.store.finish_run("new", status="completed")
        with self.store._connect() as conn:
            conn.execute("UPDATE ai_sql_runs SET finished_at = '2020-01-01T00:00:00.000Z' WHERE run_id = 'old'")
        removed = self.store.purge_finished_before("2025-01-01T00:00:00.000Z")
        self.assertEqual(removed, 1)
        self.assertIsNone(self.store.get_run("old"))
        self.assertEqual(self.store.list_events("old"), [])
        self.assertIsNotNone(self.store.get_run("new"))
        self.assertIsNotNone(self.store.get_run("live"), "还在跑的 run 不按保留期删")


class OrphanTest(unittest.TestCase):
    def test_runs_of_a_dead_local_process_are_orphans(self):
        host = socket.gethostname()
        runs = [
            {"run_id": "dead", "owner": f"{host}:{_dead_pid()}", "updated_at": "2099-01-01T00:00:00Z"},
            {"run_id": "mine", "owner": f"{host}:{os.getpid()}", "updated_at": "2000-01-01T00:00:00Z"},
            {"run_id": "remote-stale", "owner": "elsewhere:1", "updated_at": "2000-01-01T00:00:00Z"},
            {"run_id": "remote-fresh", "owner": "elsewhere:1", "updated_at": "2099-01-01T00:00:00Z"},
        ]
        self.assertEqual(sorted(orphans(runs, stale_seconds=3600)), ["dead", "remote-stale"])

    def test_sweep_marks_them_interrupted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteRunStore(Path(tmp) / "runs.db")
            store.create_run({"run_id": "dead", "query": "q",
                              "owner": f"{socket.gethostname()}:{_dead_pid()}"})
            manager = RunManager(store)
            live = manager.start(entry="http_query", caller="c", user_id="u", session_id="s", query="q")
            self.assertEqual(manager.sweep_orphans(), 1)
            self.assertEqual(store.get_run("dead")["status"], "interrupted")
            self.assertEqual(store.get_run(live.run_id)["status"], "running",
                             "本进程正在跑的 run 不能被当成孤儿")


class RunManagerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SQLiteRunStore(Path(self.tmp.name) / "runs.db")
        self.manager = RunManager(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def _start(self):
        return self.manager.start(entry="http_query", caller="c", user_id="u",
                                  session_id="s", query="查询供应商")

    def test_a_normal_run_completes_with_its_outcome_and_events_in_order(self):
        run = self._start()
        with self.manager.running(run):
            self.assertIs(current_run(), run)
            with stage("routing"):
                emit("model.call", component="thinking", status="ok")
            with stage("execution", step=1, database="erp_db"):
                emit("tool.sql", database="erp_db", status="ok", sql="SELECT 1")
            run.set_outcome(execution_status="success", degraded=False)
        self.assertIsNone(current_run())

        stored = self.store.get_run(run.run_id)
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["outcome"]["execution_status"], "success")
        events = self.store.list_events(run.run_id)
        self.assertEqual([event["seq"] for event in events], list(range(1, len(events) + 1)))
        types = [event["type"] for event in events]
        self.assertEqual(types[0], "run.started")
        self.assertEqual(types[-1], "run.finished")
        self.assertEqual(events[-1]["attrs"]["status"], "completed")
        model_call = next(event for event in events if event["type"] == "model.call")
        self.assertEqual(model_call["stage"], "routing", "事件知道自己发生在哪个阶段")
        sql = next(event for event in events if event["type"] == "tool.sql")
        self.assertEqual((sql["stage"], sql["step"]), ("execution", 1), "执行器的事件继承阶段的步骤号")
        started = next(event for event in events
                       if event["type"] == "stage.started" and event["stage"] == "execution")
        self.assertEqual(started["step"], 1)
        self.assertEqual(started["attrs"], {"database": "erp_db"}, "step 只在信封里，不在 attrs 里重复")
        self.assertIn('ai_sql_runs_total{entry="http_query",status="completed"}', render_metrics())

    def test_an_exception_fails_the_run_and_still_propagates(self):
        run = self._start()
        with self.assertRaises(ValueError):
            with self.manager.running(run):
                with stage("planning"):
                    raise ValueError("bad plan")
        stored = self.store.get_run(run.run_id)
        self.assertEqual(stored["status"], "failed")
        self.assertIn("ValueError", stored["error"])
        finished = [event for event in self.store.list_events(run.run_id) if event["type"] == "stage.finished"]
        self.assertEqual(finished[-1]["attrs"]["status"], "error")

    def test_a_cancelled_run_is_recorded_as_cancelled_not_failed(self):
        run = self._start()
        with self.assertRaises(RunCancelled):
            with self.manager.running(run):
                self.assertEqual(self.manager.cancel(run.run_id, "api"), "cancel_requested")
                with stage("retrieval"):
                    pass  # 进入阶段之前就停：这个阶段根本不会开始
        stored = self.store.get_run(run.run_id)
        self.assertEqual((stored["status"], stored["cancel_reason"]), ("cancelled", "api"))
        types = [event["type"] for event in self.store.list_events(run.run_id)]
        self.assertIn("run.cancel_requested", types)
        self.assertNotIn("stage.started", types, "已取消就不再进入新的阶段")

    def test_cancel_reports_finished_and_unknown_runs(self):
        run = self._start()
        with self.manager.running(run):
            pass
        self.assertEqual(self.manager.cancel(run.run_id, "api"), "already_finished")
        self.assertEqual(self.manager.cancel("nope", "api"), "not_found")

    def test_owner_is_known_while_running_and_after(self):
        run = self._start()
        self.assertEqual(self.manager.owner_of(run.run_id), ("c", "u"))
        with self.manager.running(run):
            pass
        self.assertEqual(self.manager.owner_of(run.run_id), ("c", "u"))
        self.assertIsNone(self.manager.owner_of("nope"))

    def test_shutdown_interrupts_what_is_still_registered(self):
        run = self._start()
        self.manager.shutdown()
        self.assertEqual(self.store.get_run(run.run_id)["status"], "interrupted")
        self.assertTrue(run.token.cancelled)

    def test_library_calls_without_a_run_are_untouched(self):
        with stage_frame("execution", 1):
            emit("tool.sql", database="erp_db")  # 不在 run 里：什么都不发生
        with stage("routing"):
            pass


DSN = os.getenv("AI_SQL_PGVECTOR_DSN", "")


@unittest.skipUnless(DSN, "未配置 AI_SQL_PGVECTOR_DSN，跳过 PostgreSQL 运行记录存储测试")
class PostgresRunStoreTest(unittest.TestCase):
    """与 SQLite 同一套行为；CI 的 PostgreSQL 16 服务上跑，本机没有 DSN 时跳过。"""

    def test_round_trip_conflicts_interruption_and_purge(self):
        import uuid

        from ai_sql_agent.runs import PostgresRunStore

        store = PostgresRunStore(DSN)
        run_id, other = uuid.uuid4().hex, uuid.uuid4().hex
        try:
            store.create_run({"run_id": run_id, "trace_id": "t", "entry": "http_query",
                              "caller": "c", "user_id": "u", "session_id": "s", "query": "q"})
            events = [RunEvent(run_id=run_id, seq=index, ts="2026-01-01T00:00:00.000Z",
                               type="stage.started", stage="routing", attrs={"index": index})
                      for index in (1, 2, 3)]
            store.append_events(events)
            store.append_events(events[:1])  # 重复写入被忽略
            store.finish_run(run_id, status="completed", usage={"total_tokens": 3},
                             outcome={"execution_status": "success"})
            run = store.get_run(run_id)
            self.assertEqual((run["status"], run["last_seq"]), ("completed", 3))
            self.assertEqual((run["usage"], run["outcome"]),
                             ({"total_tokens": 3}, {"execution_status": "success"}))
            self.assertTrue(run["finished_at"].endswith("Z"))
            rest = store.list_events(run_id, after_seq=1, limit=10)
            self.assertEqual([event["seq"] for event in rest], [2, 3])
            self.assertEqual(rest[0]["attrs"], {"index": 2})

            store.create_run({"run_id": other, "query": "q", "owner": "elsewhere:1"})
            self.assertIn(other, [row["run_id"] for row in store.running_runs()])
            self.assertEqual(store.mark_interrupted([other], "interrupted: test"), 1)
            self.assertEqual(store.get_run(other)["status"], "interrupted")

            with store._conn() as conn:
                conn.execute("UPDATE ai_sql_runs SET finished_at = '2020-01-01T00:00:00Z' "
                             "WHERE run_id = %s", (run_id,))
            self.assertGreaterEqual(store.purge_finished_before("2021-01-01T00:00:00.000Z"), 1)
            self.assertIsNone(store.get_run(run_id))
            self.assertEqual(store.list_events(run_id), [])
        finally:
            with store._conn() as conn:
                conn.execute("DELETE FROM ai_sql_run_events WHERE run_id = ANY(%s)", ([run_id, other],))
                conn.execute("DELETE FROM ai_sql_runs WHERE run_id = ANY(%s)", ([run_id, other],))
            store.close()


class RunApiTest(unittest.TestCase):
    """HTTP：/query 带回 X-Run-Id，/runs 只给本人和管理员看，A2A 的 taskId 就是 run_id。"""

    ENV_KEYS = (
        "AI_SQL_AUTH_MODE",
        "AI_SQL_API_KEYS_FILE",
        "AI_SQL_AUDIT_DB_PATH",
        "AI_SQL_MEMORY_DB_PATH",
        "AI_SQL_RUNS_DB_PATH",
        "AI_SQL_SEMANTIC_DIR",
    )

    @classmethod
    def setUpClass(cls):
        from ai_sql_agent.api.app import app

        cls.tmpdir = tempfile.TemporaryDirectory()
        key_file = Path(cls.tmpdir.name) / "keys.json"
        key_file.write_text(json.dumps({
            "keys": [
                {"key": "admin-key", "caller": "ops", "user_id": "ops",
                 "roles": ["everything"], "is_admin": True},
                {"key": "alice-key", "caller": "console", "user_id": "alice", "roles": ["everything"]},
                {"key": "bob-key", "caller": "console", "user_id": "bob", "roles": ["everything"]},
            ],
            "policies": {"everything": {"allow_tables": ["*"]}},
        }), encoding="utf-8")
        cls._saved = {key: os.environ.get(key) for key in cls.ENV_KEYS}
        os.environ.update({
            "AI_SQL_AUTH_MODE": "api_key",
            "AI_SQL_API_KEYS_FILE": str(key_file),
            "AI_SQL_AUDIT_DB_PATH": str(Path(cls.tmpdir.name) / "audit.db"),
            "AI_SQL_MEMORY_DB_PATH": str(Path(cls.tmpdir.name) / "memory.db"),
            "AI_SQL_RUNS_DB_PATH": str(Path(cls.tmpdir.name) / "runs.db"),
            "AI_SQL_SEMANTIC_DIR": "",
        })
        reload_settings()
        cls.app = app
        cls.ctx = TestClient(app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)
        for key, value in cls._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reload_settings()
        cls.tmpdir.cleanup()

    @staticmethod
    def _key(value):
        return {"X-API-Key": value}

    def _ask(self, key="alice-key", question="查询准时率低于0.9的供应商名称"):
        user = {"alice-key": "alice", "bob-key": "bob", "admin-key": "ops"}[key]
        response = self.client.post("/query", headers=self._key(key), json={
            "query": question, "user_id": user, "session_id": f"s_{os.urandom(4).hex()}"})
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def test_query_returns_a_run_whose_events_tell_the_story(self):
        response = self._ask()
        run_id = response.headers["X-Run-Id"]
        run = self.client.get(f"/runs/{run_id}", headers=self._key("alice-key")).json()
        self.assertEqual((run["status"], run["entry"]), ("completed", "http_query"))
        self.assertEqual(run["trace_id"], response.json()["trace_id"])
        self.assertEqual(run["outcome"]["execution_status"], response.json()["execution_status"])
        self.assertNotIn("owner", run)

        events = self.client.get(f"/runs/{run_id}/events", headers=self._key("alice-key")).json()["events"]
        self.assertEqual([event["seq"] for event in events], list(range(1, len(events) + 1)))
        self.assertEqual(events[0]["type"], "run.started")
        self.assertEqual(events[-1]["type"], "run.finished")
        stages = [event["stage"] for event in events if event["type"] == "stage.started"]
        for expected in ("memory_context", "routing", "normalization", "retrieval", "planning",
                         "sql_generation", "execution"):
            self.assertIn(expected, stages)
        sql = [event for event in events if event["type"] == "tool.sql"]
        self.assertTrue(sql and sql[0]["attrs"]["sql"].upper().startswith("SELECT"))
        self.assertEqual(sql[0]["stage"], "execution")

        audit = self.app.state.audit.get_by_trace(run["trace_id"])
        self.assertEqual(audit["run_id"], run_id, "审计行和 run 互相找得到")

    def test_events_page_with_after(self):
        run_id = self._ask().headers["X-Run-Id"]
        first = self.client.get(f"/runs/{run_id}/events", params={"limit": 3},
                                headers=self._key("alice-key")).json()
        rest = self.client.get(f"/runs/{run_id}/events", params={"after": first["next_after"]},
                               headers=self._key("alice-key")).json()
        self.assertEqual([event["seq"] for event in first["events"]], [1, 2, 3])
        self.assertEqual(rest["events"][0]["seq"], 4)

    def test_someone_elses_run_is_not_found_but_an_admin_sees_it(self):
        run_id = self._ask("alice-key").headers["X-Run-Id"]
        for path in (f"/runs/{run_id}", f"/runs/{run_id}/events"):
            self.assertEqual(self.client.get(path, headers=self._key("bob-key")).status_code, 404)
            self.assertEqual(self.client.get(path, headers=self._key("admin-key")).status_code, 200)
        self.assertEqual(self.client.post(f"/runs/{run_id}/cancel", headers=self._key("bob-key")).status_code, 404)
        self.assertEqual(self.client.get(f"/runs/{run_id}").status_code, 401)

    def test_cancelling_a_finished_run_is_a_conflict(self):
        run_id = self._ask().headers["X-Run-Id"]
        response = self.client.post(f"/runs/{run_id}/cancel", headers=self._key("alice-key"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.client.post("/runs/nope/cancel", headers=self._key("alice-key")).status_code, 404)

    def test_a2a_task_id_is_the_run_id(self):
        from ai_sql_agent.core import get_settings

        saved = get_settings().a2a_streaming
        get_settings().a2a_streaming = True
        try:
            body = {"jsonrpc": "2.0", "id": "r", "method": "message/stream", "params": {"message": {
                "kind": "message", "role": "user", "messageId": "m1", "contextId": f"c_{os.urandom(4).hex()}",
                "parts": [{"kind": "text", "text": "查询库存低于安全库存的物料名称"}],
                "metadata": {"user_id": "alice"}}}}
            with self.client.stream("POST", "/a2a", json=body,
                                    headers={**self._key("alice-key"), "Accept": "text/event-stream"}) as response:
                response.read()
                frames = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
        finally:
            get_settings().a2a_streaming = saved
        task_id = frames[0]["result"]["id"]
        run = self.client.get(f"/runs/{task_id}", headers=self._key("alice-key")).json()
        self.assertEqual((run["entry"], run["status"]), ("a2a_stream", "completed"))


if __name__ == "__main__":
    unittest.main()
