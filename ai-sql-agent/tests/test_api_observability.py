import unittest

from fastapi.testclient import TestClient

from ai_sql_agent.api.app import TRACE_HEADER, app


class ApiTraceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_ctx = TestClient(app)
        cls.client = cls.client_ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_ctx.__exit__(None, None, None)

    def test_health(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})

    def test_query_returns_trace_id_in_body_and_header(self):
        response = self.client.post(
            "/query",
            json={
                "user_id": "u_test",
                "session_id": "s_trace",
                "query": "查询准时率低于0.9的供应商名称",
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()

        self.assertTrue(body["trace_id"])
        self.assertEqual(response.headers[TRACE_HEADER], body["trace_id"])
        self.assertEqual(body["route"], "database_query")
        self.assertTrue(body["step_logs"])
        self.assertTrue(body["step_logs"][0]["success"])

    def test_incoming_trace_id_is_propagated(self):
        response = self.client.post(
            "/query",
            headers={TRACE_HEADER: "upstream-trace-1"},
            json={"user_id": "u_test", "session_id": "s_trace", "query": "你好"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["trace_id"], "upstream-trace-1")
        self.assertEqual(response.headers[TRACE_HEADER], "upstream-trace-1")

    def test_invalid_request_is_rejected(self):
        response = self.client.post(
            "/query", json={"user_id": "", "session_id": "s", "query": "x"}
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()


