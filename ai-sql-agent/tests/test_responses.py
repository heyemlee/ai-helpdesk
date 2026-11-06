"""Offline acceptance for SQL's explicit Responses transport and settings."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from ai_sql_agent.core import ModelAccessRegistry, ModelTransport, RetryPolicy, usage_scope, usage_summary
from ai_sql_agent.core.errors import ModelBadRequestError, ModelRateLimitError, ModelResponseError, ModelTimeoutError
from ai_sql_agent.core.settings import SqlAgentSettings
from ai_sql_agent.core.transport import build_chat_payload


def response(**overrides):
    return {"status": "completed", "model": "gpt-6.1-sol", "output": [
        {"type": "reasoning", "id": "rs", "summary": [], "encrypted_content": "opaque"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "SELECT 1"}]}],
        "usage": {"input_tokens": 12, "output_tokens": 7, "total_tokens": 19,
                  "input_tokens_details": {"cached_tokens": 3}, "output_tokens_details": {"reasoning_tokens": 2}}, **overrides}


class ResponsesTest(unittest.TestCase):
    def transport(self, handle, retries=0):
        http = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(http.close)
        t = ModelTransport("responses_test", api_key="test-key", api_mode="responses", client=http,
                           retry_policy=RetryPolicy(max_retries=retries, backoff_base_seconds=0),
                           sleeper=lambda _: None, access_registry=ModelAccessRegistry())
        t.circuit.reset()
        return t

    def test_text_wire_and_usage(self):
        requests = []
        def handle(req):
            requests.append(json.loads(req.content))
            self.assertEqual(req.url.path, "/v1/responses")
            return httpx.Response(200, json=response())
        with usage_scope():
            result = self.transport(handle).chat("https://model.test/v1/responses", [
                {"role": "system", "content": "SQL only"}, {"role": "user", "content": "one"}], model="gpt-6.1-sol")
            summary = usage_summary()
        self.assertEqual(result, "SELECT 1")
        self.assertEqual(requests[0]["store"], False)
        self.assertNotIn("temperature", requests[0])
        self.assertIn("max_output_tokens", requests[0])
        self.assertEqual(summary["total_tokens"], 19)
        self.assertEqual(summary["cache_read_tokens"], 3)
        self.assertEqual(summary["reasoning_tokens"], 2)

    def test_structured_request_has_responses_schema(self):
        schema = {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"], "additionalProperties": False}
        body = build_chat_payload([], model="relay-alias", api_mode="responses", response_schema=schema,
                                  reasoning_effort="low", max_tokens=64)
        self.assertEqual(body["text"]["format"], {"type": "json_schema", "name": "result", "strict": True, "schema": schema})
        self.assertNotIn("response_format", body)
        self.assertEqual(body["reasoning"], {"effort": "low"})

    def test_invalid_outputs_do_not_become_sql(self):
        for overrides in [
            {"output": []}, {"output": [{"type": "message", "status": "incomplete", "content": [{"type": "output_text", "text": "SELECT"}]}]}, {"status": "failed"}, {"status": "in_progress"}, {"status": None},
            {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
            {"status": "incomplete", "incomplete_details": {"reason": "content_filter"}},
            {"output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]},
            {"output": [{"type": "function_call", "name": "unknown", "call_id": "c"}]},
        ]:
            with self.subTest(overrides=overrides), usage_scope():
                t = self.transport(lambda _: httpx.Response(200, json=response(**overrides)))
                with self.assertRaises(ModelResponseError):
                    t.chat("https://model.test/v1/responses", [], model="gpt-6.1-sol")
                self.assertEqual(usage_summary()["total_tokens"], 19)

    def test_no_chat_parameter_or_protocol_fallback(self):
        calls = []
        def handle(req):
            calls.append(req)
            return httpx.Response(400, json={"error": {"message": "reasoning unsupported"}})
        with self.assertRaises(ModelBadRequestError):
            self.transport(handle).chat("https://model.test/v1/responses", [], model="gpt-6.1-sol")
        self.assertEqual(len(calls), 1)
        self.assertIn("input", json.loads(calls[0].content))

    def test_timeout_is_typed_and_bounded(self):
        calls = []
        def handle(req):
            calls.append(req)
            raise httpx.ReadTimeout("slow", request=req)
        with self.assertRaises(ModelTimeoutError):
            self.transport(handle).chat("https://model.test/v1/responses", [], model="gpt-6.1-sol")
        self.assertEqual(len(calls), 1)

    def test_429_guard_prevents_repeated_requests(self):
        calls = []
        def handle(req):
            calls.append(req)
            return httpx.Response(429, headers={"Retry-After": "60"}, json={"error": {"message": "limited"}})
        t = self.transport(handle, retries=2)
        for _ in range(2):
            with self.assertRaises(ModelRateLimitError):
                t.chat("https://model.test/v1/responses", [], model="gpt-6.1-sol")
        self.assertEqual(len(calls), 1)

    def test_stream_usage_and_terminal(self):
        events = [{"type": "response.output_text.delta", "delta": "SELECT 1"},
                  {"type": "response.completed", "response": response()}]
        raw = "".join("data: " + json.dumps(e) + "\n\n" for e in events)
        t = self.transport(lambda _: httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text=raw))
        with usage_scope():
            self.assertEqual(list(t.chat_stream("https://model.test/v1/responses", [], model="gpt-6.1-sol")), ["SELECT 1"])
            self.assertEqual(usage_summary()["total_tokens"], 19)

    def test_disconnected_or_failed_stream_is_not_success_and_not_retried(self):
        for end in [None, "response.incomplete", "response.failed", "error"]:
            with self.subTest(end=end):
                calls = []
                events = [{"type": "response.output_text.delta", "delta": "SELECT"}]
                if end:
                    events.append({"type": end, "response": response(status=end.split(".")[-1])})
                def handle(req):
                    calls.append(req)
                    return httpx.Response(200, text="".join("data: " + json.dumps(e) + "\n\n" for e in events))
                with self.assertRaises(ModelResponseError):
                    list(self.transport(handle, retries=2).chat_stream("https://model.test/v1/responses", [], model="gpt-6.1-sol"))
                self.assertEqual(len(calls), 1)

    def test_protocol_selected_from_settings_not_model_name(self):
        cfg = SqlAgentSettings(_env_file=None, AI_SQL_OPENAI_API_MODE="responses", chat_model="relay-alias")
        self.assertTrue(cfg.chat_url.endswith("/responses"))
        cfg = SqlAgentSettings(_env_file=None, AI_SQL_OPENAI_API_MODE="chat", chat_model="gpt-6.1-sol")
        self.assertTrue(cfg.chat_url.endswith("/chat/completions"))

    def test_saved_protocol_roundtrip_and_actual_client(self):
        from ai_sql_agent.api.model_admin import resolve_candidate
        from ai_sql_agent.core import model_settings as ms
        from ai_sql_agent.generation.coder_client import CoderModelClient
        server = SqlAgentSettings(_env_file=None, OPENAI_API_KEY="test-key")
        candidate = resolve_candidate({"provider": "openai", "base_url": "https://model.test/v1",
            "api_key": "test-key", "chat_model": "gpt-6.1-sol", "coder_model": "gpt-6.1-sol", "api_mode": "responses"}, server)
        self.assertTrue(candidate.chat_url.endswith("/responses"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            path.write_text(json.dumps({"values": candidate.values()}))
            stored = ms.load_store(str(path))
        self.assertEqual(stored["values"]["openai_api_mode"], "responses")
        from ai_sql_agent.core.settings import apply_model_overrides

        cfg = apply_model_overrides(server, stored)
        self.assertTrue(cfg.chat_url.endswith("/responses"))
        self.assertEqual(cfg.embedding_url, server.embedding_url)
        with patch("ai_sql_agent.core.model_client.get_settings", return_value=cfg):
            conn = CoderModelClient()._connection()
        self.assertEqual(conn.transport.api_mode, "responses")
        self.assertTrue(conn.url.endswith("/responses"))

    def test_probe_rejects_empty_output(self):
        from ai_sql_agent.api.model_admin import Candidate, probe
        c = Candidate("openai", "https://model.test/v1", "test-key", "gpt-6.1-sol", "gpt-6.1-sol", api_mode="responses")
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response(output=[])))) as http:
            report = probe(c, client=http)
        self.assertFalse(report["ok"])
        self.assertEqual(report["api_mode"], "responses")

    def test_embedding_does_not_inherit_generation_protocol(self):
        cfg = SqlAgentSettings(_env_file=None, AI_SQL_OPENAI_API_MODE="responses")
        with patch("ai_sql_agent.core.transport.get_settings", return_value=cfg):
            with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": []}))) as http:
                t = ModelTransport("embedding", client=http)
                self.assertEqual(t.api_mode, "chat")
                self.assertEqual(t.post_json("https://model.test/v1/embeddings", {"input": ["test"]}), {"data": []})

    def test_contradictory_terminal_event_is_not_success(self):
        from ai_sql_agent.core.responses import stream_text
        raw = "data: " + json.dumps({"type": "response.failed", "response": response()}) + "\n\n"
        with self.assertRaises(ModelResponseError):
            list(stream_text(iter(raw.splitlines()), lambda _: None))
