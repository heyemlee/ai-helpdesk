"""并发正确性：单实例 Pipeline / 单实例 Service 被多线程同时调用。"""

import unittest
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from ai_sql_agent.api.app import TRACE_HEADER, app

QUERIES = [
    ("查询准时率低于0.9的供应商名称", ["erp_db"]),
    ("查询实际成本超过预算的项目的采购总金额", ["pm_db", "erp_db"]),
]


class ConcurrentQueryTest(unittest.TestCase):
    """FastAPI 同步端点跑在线程池里，多请求共享同一个 Pipeline 实例。"""

    @classmethod
    def setUpClass(cls):
        cls.client_ctx = TestClient(app)
        cls.client = cls.client_ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_ctx.__exit__(None, None, None)

    def _post(self, index):
        query, _ = QUERIES[index % len(QUERIES)]
        return index, self.client.post(
            "/query",
            headers={TRACE_HEADER: f"trace-{index}"},
            json={
                "user_id": f"u_conc_{index}",
                "session_id": f"s_conc_{index}",
                "query": query,
            },
        )

    def test_parallel_requests_do_not_interfere(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(self._post, range(24)))

        for index, response in results:
            expected_query, expected_databases = QUERIES[index % len(QUERIES)]

            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()

            # trace_id 不串：每个请求拿回自己透传的那个
            self.assertEqual(body["trace_id"], f"trace-{index}")

            # 结果不串：命中的库与该请求的问题一致
            # 关心结果不串（该问题相关的库都在），不锁定候选集大小
            for expected in expected_databases:
                self.assertIn(expected, body["selected_databases"])
            self.assertTrue(body["step_logs"])
            for step in body["step_logs"]:
                self.assertTrue(step["success"], step["error"])


class ConcurrentPipelineTest(unittest.TestCase):
    """直接并发调用 Pipeline，绕过 HTTP 层。"""

    def test_shared_pipeline_instance_is_reentrant(self):
        from ai_sql_agent.pipeline import Text2SQLPipeline

        pipeline = Text2SQLPipeline()

        def run(index):
            query, expected = QUERIES[index % len(QUERIES)]
            result = pipeline.run(query)
            return expected, result

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(run, range(24)))

        for expected_databases, result in outcomes:
            for expected in expected_databases:
                self.assertIn(expected, result.selected_databases)
            for log in result.step_logs:
                self.assertTrue(log.execution_result["success"])


if __name__ == "__main__":
    unittest.main()
