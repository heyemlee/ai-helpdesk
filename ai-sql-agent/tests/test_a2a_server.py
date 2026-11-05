"""
A2A 服务端：契约 a2a-streaming-contract.md（v1.3）里能在单测里钉住的部分。

起整个 app（演示库 + Mock 模型），和 test_api_observability 同一套搭法。
"""
import json
import unittest
import uuid
from unittest import mock

from fastapi import HTTPException
from fastapi.testclient import TestClient

from ai_sql_agent.api.app import app
from ai_sql_agent.core import get_settings


# 会话 id 每次运行都要新的：runtime_data/memory.db 跨运行持久化，复用同一个
# session 时 ai-sql-agent 会认出「这问题本会话已经答过」，路由到「解释上一条结果」
# 而不是重新查库——那是它的设计，不是故障，但会让断言查库结果的用例第二次跑就挂。
SESSION = f"s_a2a_{uuid.uuid4().hex[:8]}"


def _rpc(method, text="查询准时率低于0.9的供应商名称", user_id="u_a2a", session=None, **extra):
    return {"jsonrpc": "2.0", "id": "req-1", "method": method,
            "params": {"message": {"kind": "message", "role": "user", "messageId": "m1",
                                   "contextId": session or SESSION,
                                   "parts": [{"kind": "text", "text": text}],
                                   "metadata": {"user_id": user_id}, **extra}}}


def _events(resp):
    """把 SSE 正文拆成 result 事件列表。"""
    out = []
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            out.append(json.loads(line[5:].strip())["result"])
    return out


class A2ATest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def setUp(self):
        get_settings().a2a_streaming = False

    # ── Agent Card 与开关（契约 §6.2）──

    def test_card_reflects_the_streaming_flag(self):
        card = self.client.get("/.well-known/agent.json").json()
        self.assertEqual(card["version"], "1.1.0")
        self.assertEqual(card["protocolVersion"], "0.2")
        self.assertFalse(card["capabilities"]["streaming"])
        self.assertFalse(card["capabilities"]["pushNotifications"])
        get_settings().a2a_streaming = True
        self.assertTrue(self.client.get("/.well-known/agent.json").json()["capabilities"]["streaming"])

    def test_stream_returns_32004_when_flag_is_off(self):
        """开关关着：-32004 在**流打开之前**、以普通 JSON 返回（§3.2 / §6.2）。"""
        r = self.client.post("/a2a", json=_rpc("message/stream"))
        self.assertIn("application/json", r.headers["content-type"])
        self.assertEqual(r.json()["error"]["code"], -32004)
        self.assertEqual(r.json()["id"], "req-1")

    # ── 刻意不做的方法（契约 §7）──

    def test_unsupported_methods_are_32004(self):
        for m in ("tasks/cancel", "tasks/resubscribe", "tasks/get"):
            self.assertEqual(self.client.post("/a2a", json=_rpc(m)).json()["error"]["code"], -32004, m)

    def test_unknown_method_is_32601(self):
        self.assertEqual(self.client.post("/a2a", json=_rpc("nope/what")).json()["error"]["code"], -32601)

    def test_bad_params_are_32602_json(self):
        body = {"jsonrpc": "2.0", "id": "x", "method": "message/send", "params": {"message": {"parts": []}}}
        r = self.client.post("/a2a", json=body)
        self.assertIn("application/json", r.headers["content-type"])
        self.assertEqual(r.json()["error"]["code"], -32602)

    def test_non_jsonrpc_body_is_32600(self):
        self.assertEqual(self.client.post("/a2a", json={"hello": 1}).json()["error"]["code"], -32600)

    # ── 限流与准入（契约 v1.13 §3.2 / §5.5）──

    def test_rate_limit_is_429_with_32029_before_any_stream(self):
        """限流 / 配额：429 + -32029，带 Retry-After，在流打开之前以普通 JSON 返回。"""

        def limited(_principal):
            raise HTTPException(status_code=429, detail="请求太频繁", headers={"Retry-After": "7"})

        get_settings().a2a_streaming = True
        with mock.patch.object(app.state.limiter, "check", limited):
            for method in ("message/send", "message/stream"):
                r = self.client.post("/a2a", json=_rpc(method))
                self.assertEqual(r.status_code, 429, method)
                self.assertIn("application/json", r.headers["content-type"], method)
                # 不是 -32001：那个码在 A2A 规范里是 TaskNotFoundError
                self.assertEqual(r.json()["error"]["code"], -32029, method)
                self.assertEqual(r.headers.get("retry-after"), "7", method)

    def test_full_stream_admission_is_429_with_32029(self):
        from ai_sql_agent.a2a import server as a2a

        # 准入按事件循环各持一个（_admission() 要在循环里调），这里直接换一个已经满了的
        full = a2a._Admission(limit=1)
        self.assertTrue(full.try_acquire())
        get_settings().a2a_streaming = True
        with mock.patch.object(a2a, "_admission", lambda: full):
            r = self.client.post("/a2a", json=_rpc("message/stream"))
        self.assertEqual(r.status_code, 429)
        self.assertIn("application/json", r.headers["content-type"])
        self.assertEqual(r.json()["error"]["code"], -32029)
        self.assertEqual(r.headers.get("retry-after"), "1")

    # ── message/send ──

    def test_send_returns_a_completed_task_with_isomorphic_artifact(self):
        r = self.client.post("/a2a", json=_rpc("message/send"))
        self.assertEqual(r.status_code, 200)
        task = r.json()["result"]
        self.assertEqual(task["kind"], "task")
        self.assertEqual(task["status"]["state"], "completed")
        self.assertEqual(task["contextId"], SESSION)
        parts = task["artifacts"][0]["parts"]
        text = next(p for p in parts if p["kind"] == "text")["text"]
        data = next(p for p in parts if p["kind"] == "data")["data"]
        self.assertTrue(text)
        # §4.4：data part 与 /query 的字段完全同构
        for key in ("route", "confidence", "queried_database", "has_risk", "degraded",
                    "degrade_reason", "selected_databases", "trace_id", "steps"):
            self.assertIn(key, data, key)
        self.assertEqual(data["route"], "database_query")
        self.assertTrue(data["steps"] and data["steps"][0]["success"])
        # 与 /query 跑同一个问题比对
        q = self.client.post("/query", json={
            "user_id": "u_a2a", "session_id": f"s_cmp_{uuid.uuid4().hex[:8]}",
            "query": "查询准时率低于0.9的供应商名称"}).json()
        self.assertEqual(data["selected_databases"], q["selected_databases"])
        self.assertEqual(data["steps"][0]["final_sql"], q["step_logs"][0]["final_sql"])

    def test_trace_id_header_is_propagated_into_task(self):
        r = self.client.post("/a2a", headers={"X-Trace-Id": "up-1"}, json=_rpc("message/send"))
        self.assertEqual(r.headers["X-Trace-Id"], "up-1")
        self.assertEqual(r.json()["result"]["metadata"]["trace_id"], "up-1")

    # ── message/stream（契约 §4.2 的顺序保证）──

    def test_stream_event_order_is_guaranteed(self):
        get_settings().a2a_streaming = True
        with self.client.stream("POST", "/a2a", json=_rpc("message/stream"),
                                headers={"Accept": "text/event-stream"}) as r:
            self.assertIn("text/event-stream", r.headers["content-type"])
            r.read()
            events = _events(r)

        kinds = [e["kind"] for e in events]
        self.assertEqual(kinds[0], "task", "首帧一定是 task")
        self.assertEqual(events[0]["status"]["state"], "submitted")
        finals = [e for e in events if e.get("kind") == "status-update" and e.get("final")]
        self.assertEqual(len(finals), 1, "终态有且只有一个")
        self.assertIs(events[-1], finals[0], "终态一定是最后一个")
        self.assertEqual(finals[0]["status"]["state"], "completed")
        self.assertIn("trace_id", finals[0]["metadata"])
        arts = [i for i, e in enumerate(events) if e["kind"] == "artifact-update"]
        self.assertEqual(len(arts), 1)
        self.assertLess(arts[0], len(events) - 1, "artifact 在终态之前")
        self.assertTrue(events[arts[0]]["lastChunk"])

        progress = [e for e in events if e["kind"] == "status-update" and not e.get("final")]
        self.assertTrue(progress, "查库路径至少有进度事件")
        stages = [e["metadata"]["stage"] for e in progress]
        # 契约面的 stage 名，不是内部名
        self.assertNotIn("sql_generation", stages)
        self.assertNotIn("memory_context", stages)
        self.assertTrue({"routing", "retrieval", "planning", "generation", "execution"} & set(stages))
        for e in progress:
            self.assertFalse(e["final"])
            self.assertEqual(e["status"]["state"], "working")
            self.assertIn("trace_id", e["metadata"])
            self.assertTrue(e["status"]["message"]["parts"][0]["text"])
        # 按 step 重复的 stage 带 step/database；顺序与 pipeline 一致
        exec_events = [e for e in progress if e["metadata"]["stage"] == "execution"]
        self.assertTrue(exec_events and "database" in exec_events[0]["metadata"])

    def test_stream_admission_failures_are_json_not_sse(self):
        """鉴权/参数错一律在流打开之前、普通 JSON（§3.2）。"""
        get_settings().a2a_streaming = True
        body = {"jsonrpc": "2.0", "id": "x", "method": "message/stream", "params": {"message": {"parts": []}}}
        r = self.client.post("/a2a", json=body, headers={"Accept": "text/event-stream"})
        self.assertIn("application/json", r.headers["content-type"])
        self.assertEqual(r.json()["error"]["code"], -32602)

    def test_mid_stream_failure_ends_with_a_failed_terminal(self):
        """流中途出错不靠断连表示失败：补一个 failed 终态再关（§5.2）。"""
        get_settings().a2a_streaming = True
        original = app.state.service.run

        def boom(**kw):
            raise RuntimeError("模拟中途炸了")
        app.state.service.run = boom
        try:
            with self.client.stream("POST", "/a2a", json=_rpc("message/stream")) as r:
                r.read()
                events = _events(r)
        finally:
            app.state.service.run = original
        self.assertEqual(events[0]["kind"], "task")
        last = events[-1]
        self.assertTrue(last["final"])
        self.assertEqual(last["status"]["state"], "failed")
        self.assertIn("模拟中途炸了", last["metadata"]["error"])
        self.assertFalse(any(e["kind"] == "artifact-update" for e in events))

    def test_stream_and_send_share_one_audit_shape(self):
        """
        §5.5：一次 stream = 一次 /query = 一条审计。

        user_id 必须每次运行都不一样。两轮都栽在这上面：
          · 一开始共用 u_a2a——/history 有 20 条上限，前面的用例攒满之后
            before 和 after 都是 20，断言永远不成立；
          · 改成固定的 u_audit_once 仍然不行——runtime_data/audit.db **跨运行
            持久化**，第二次跑就读到上一次留下的那条，`items == []` 挂掉。
        随机 id 才真的每次从零开始。
        """
        get_settings().a2a_streaming = True
        user = f"u_audit_{uuid.uuid4().hex[:8]}"
        self.assertEqual(self.client.get("/history", params={"user_id": user}).json()["items"], [])
        with self.client.stream("POST", "/a2a", json=_rpc("message/stream", user_id=user)) as r:
            r.read()
        items = self.client.get("/history", params={"user_id": user}).json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["route"], "database_query")


if __name__ == "__main__":
    unittest.main()
