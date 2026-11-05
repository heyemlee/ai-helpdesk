"""
Schema 检索的 Embedding：怎么配、怎么在页面上换、换的时候和换了之后出事怎么办。

- 页面保存的 Embedding 是单独一节：存主链路时它原样留着，反过来也一样；
- 配了远端 Embedding，检索索引用它建；建不起来就退回本地向量，服务照常起，并说出原因；
- 问题里的关键词合成一次 Embedding 请求；Embedding 半路挂了，这个问题只少了向量召回；
- 页面上换 Embedding：先测试、再用新模型把整个索引建好、再保存、最后换上——
  任何一步失败，正在用的索引和保存的文件都不动；
- Key 留空只能接着用同一台主机上已有的 Key；Key 不出现在响应和日志里。

模型服务一律是假的（共享 HTTP Client 换成 MockTransport），一个字节都出不了本机。
"""
import hashlib
import json
import os
import random
import stat
import tempfile
import unittest
from pathlib import Path

import httpx

from ai_sql_agent.core import get_settings
from ai_sql_agent.core.model_settings import (
    clear_embedding,
    clear_store,
    load_store,
    save_embedding,
    save_store,
)
from test_model_settings import (
    ENV_KEY,
    OTHER,
    OTHER_KEY,
    PAGE_KEY,
    RELAY,
    REMOTE,
    _Environment,
    chat_reply,
    page_body,
)

DIMS = 64


def fake_vector(text, dimensions):
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    generator = random.Random(seed)
    return [generator.uniform(-1.0, 1.0) for _ in range(dimensions)]


def embeddings_reply(request, dimensions=None):
    body = json.loads(request.content)
    size = dimensions or body.get("dimensions") or DIMS
    return httpx.Response(200, json={
        "object": "list",
        "data": [
            {"object": "embedding", "index": index, "embedding": fake_vector(text, size)}
            for index, text in enumerate(body["input"])
        ],
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    })


def is_embedding(request):
    return request.url.path.endswith("/embeddings")


def embedding_body(**overrides):
    body = {"base_url": OTHER, "api_key": OTHER_KEY, "model": "text-embedding-3-small", "dimensions": DIMS}
    body.update(overrides)
    return body


class _EmbeddingEnvironment(_Environment):
    """假模型服务：/embeddings 按请求的维度给向量；`self.embedding_failure` 设了就按它失败。"""

    embedding_failure = None

    def respond(self, request, index):
        if is_embedding(request):
            if self.embedding_failure is not None:
                return self.embedding_failure(request)
            return embeddings_reply(request)
        return chat_reply()

    def embedding_requests(self):
        return [request for request in self.service.requests if is_embedding(request)]

    def remote_env(self, **extra):
        return self.setenv(
            AI_SQL_EMBEDDING_BACKEND="remote",
            AI_SQL_EMBEDDING_URL=f"{RELAY}/v1/embeddings",
            AI_SQL_EMBEDDING_API_KEY=ENV_KEY,
            AI_SQL_EMBEDDING_MODEL="text-embedding-3-small",
            AI_SQL_EMBEDDING_DIMENSIONS=str(DIMS),
            AI_SQL_EMBEDDING_BATCH_SIZE="50",
            **extra,
        )

    def ask(self, client, question="查询准时率低于0.9的供应商名称"):
        answered = client.post("/query", json={"user_id": "u", "session_id": "s", "query": question})
        self.assertEqual(answered.status_code, 200, answered.text)
        return answered.json()


# ----------------------------------------------------------------------
# 存取：两节互不影响
# ----------------------------------------------------------------------


class EmbeddingStoreTest(unittest.TestCase):
    CHAT = {"model_provider": "openai", "openai_api_key": PAGE_KEY, "openai_base_url": RELAY}
    EMBEDDING = {"base_url": OTHER, "api_key": OTHER_KEY, "model": "text-embedding-3-small", "dimensions": 1024}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = str(Path(self._tmp.name) / "state" / "model_settings.json")

    def test_saving_or_clearing_one_section_keeps_the_other(self):
        save_store(self.path, self.CHAT, saved_by="a")
        save_embedding(self.path, self.EMBEDDING, saved_by="b")
        loaded = load_store(self.path)
        self.assertEqual(loaded["values"]["openai_api_key"], PAGE_KEY)
        self.assertEqual(loaded["embedding"]["values"], self.EMBEDDING)
        self.assertEqual(loaded["embedding"]["saved_by"], "b")

        save_store(self.path, {**self.CHAT, "chat_model": "gpt-6-sol"}, saved_by="c")
        self.assertEqual(load_store(self.path)["embedding"]["values"], self.EMBEDDING)

        self.assertTrue(clear_store(self.path))
        loaded = load_store(self.path)
        self.assertNotIn("values", loaded)
        self.assertNotIn("problem", loaded, "只剩 Embedding 一节不算坏文件")
        self.assertEqual(loaded["embedding"]["values"], self.EMBEDDING)

        self.assertTrue(clear_embedding(self.path))
        self.assertFalse(Path(self.path).exists(), "两节都没了，文件也就没了")
        self.assertFalse(clear_embedding(self.path))

    def test_clearing_the_embedding_keeps_the_chat_section(self):
        save_store(self.path, self.CHAT)
        save_embedding(self.path, self.EMBEDDING)
        self.assertTrue(clear_embedding(self.path))
        loaded = load_store(self.path)
        self.assertEqual(loaded["values"]["openai_base_url"], RELAY)
        self.assertNotIn("embedding", loaded)

    def test_the_file_stays_readable_by_the_service_account_only(self):
        save_embedding(self.path, self.EMBEDDING)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_a_damaged_embedding_section_does_not_take_the_chat_section_with_it(self):
        Path(self.path).parent.mkdir(parents=True)
        for broken in (
            {"values": {"base_url": "http://169.254.169.254", "api_key": "k" * 20, "model": "m", "dimensions": 8}},
            {"values": {"base_url": OTHER, "api_key": "", "model": "m", "dimensions": 8}},
            {"values": {"base_url": OTHER, "api_key": "k" * 20, "model": "m", "dimensions": 0}},
            {"values": "nope"},
        ):
            Path(self.path).write_text(json.dumps({"values": self.CHAT, "embedding": broken}), encoding="utf-8")
            loaded = load_store(self.path)
            self.assertNotIn("embedding", loaded, broken)
            self.assertTrue(loaded["embedding_problem"], broken)
            self.assertEqual(loaded["values"]["model_provider"], "openai", broken)


# ----------------------------------------------------------------------
# 配置：页面保存的 Embedding 用它自己的地址和 Key
# ----------------------------------------------------------------------


class EmbeddingPrecedenceTest(_EmbeddingEnvironment):
    def test_a_saved_embedding_switches_retrieval_to_it_with_its_own_key(self):
        self.setenv(AI_SQL_OPENAI_API_KEY=ENV_KEY)
        save_embedding(str(self.store_path), embedding_body(base_url=f"{OTHER}/v1"))
        settings = self.setenv()
        self.assertEqual(settings.embedding_backend, "remote")
        self.assertEqual(settings.embedding_url, f"{OTHER}/v1/embeddings")
        self.assertEqual(settings.embedding_dimensions, DIMS)
        self.assertEqual(settings.resolved_embedding_api_key(), OTHER_KEY)
        self.assertEqual(settings.server_side.resolved_embedding_api_key(), ENV_KEY, "服务端那份不动")
        self.assertEqual(settings.server_side.embedding_backend, "local")
        self.assertEqual(settings.resolved_chat_api_key(), "", "主链路不受 Embedding 那一节影响")


# ----------------------------------------------------------------------
# 检索：用远端向量建索引、一个问题一次请求、出事退回
# ----------------------------------------------------------------------


class RetrievalEmbeddingTest(_EmbeddingEnvironment):
    def test_the_index_is_built_with_the_remote_model_and_a_question_costs_one_request(self):
        self.remote_env()
        client = self.app_client()
        status = client.app.state.pipeline.embedding_status
        self.assertEqual((status["backend"], status["model"], status["dimensions"], status["fallback"]),
                         ("remote", "text-embedding-3-small", DIMS, ""))
        built = len(self.embedding_requests())
        self.assertGreater(built, 0)
        self.assertEqual({request.headers["authorization"] for request in self.embedding_requests()},
                         {f"Bearer {ENV_KEY}"})

        self.ask(client)
        during_question = self.embedding_requests()[built:]
        self.assertEqual(len(during_question), 1, "全部关键词合成一次请求")
        self.assertGreater(len(json.loads(during_question[0].content)["input"]), 1)

        self.ask(client)
        self.assertEqual(len(self.embedding_requests()), built + 1, "同样的关键词第二次直接用缓存")

    def test_an_unreachable_embedding_at_startup_falls_back_to_local_and_says_why(self):
        self.embedding_failure = lambda request: httpx.Response(503, json={"error": {"message": "overloaded"}})
        self.remote_env()
        client = self.app_client()
        status = client.app.state.pipeline.embedding_status
        self.assertEqual(status["backend"], "local")
        self.assertIn("ModelServerError", status["fallback"])

        described = client.get("/admin/model").json()["embedding"]
        self.assertEqual((described["backend"], described["active"]["backend"]), ("remote", "local"))
        self.assertTrue(described["fallback"])
        self.assertTrue(self.ask(client)["answer"], "退回本地向量照样能答")

    def test_an_embedding_that_breaks_mid_question_only_drops_the_vector_route(self):
        self.remote_env()
        client = self.app_client()
        built = len(self.embedding_requests())
        self.embedding_failure = lambda request: httpx.Response(500, json={"error": {"message": "boom"}})

        with self.assertLogs("ai_sql", level="WARNING") as logs:
            answer = self.ask(client)
        self.assertTrue(answer["answer"])
        self.assertTrue(any("retrieval.embedding_unavailable" in line for line in logs.output))
        failed = len(self.embedding_requests()) - built
        self.assertGreaterEqual(failed, 1)

        self.ask(client, "各车间上个月的产量和合格率")
        self.assertEqual(len(self.embedding_requests()) - built, failed, "冷却期内不再去撞")

    def test_a_manual_reload_rebuilds_even_when_the_schema_did_not_change(self):
        client = self.app_client()
        pipeline = client.app.state.pipeline
        before = pipeline.schema_retrieval_service
        self.assertEqual(pipeline.reload_schema(reason="manual:admin"), [])
        self.assertIsNot(pipeline.schema_retrieval_service, before, "手动重建一定重建")
        rebuilt = pipeline.schema_retrieval_service
        self.assertEqual(pipeline.reload_schema(reason="watcher"), [])
        self.assertIs(pipeline.schema_retrieval_service, rebuilt, "轮询只在结构变了时重建")

        reloaded = client.post("/admin/schema/reload")
        self.assertEqual(reloaded.status_code, 200, reloaded.text)
        self.assertIsNot(pipeline.schema_retrieval_service, rebuilt)


class RepairFollowsTheConnectionTest(_EmbeddingEnvironment):
    def test_repair_uses_the_model_once_a_key_is_saved_after_startup(self):
        client = self.app_client()
        repair = client.app.state.pipeline.sql_repair
        self.assertFalse(repair._model_available())
        self.assertEqual(client.put("/admin/model", json=page_body()).status_code, 200)
        self.assertTrue(repair._model_available(), "不用等一次 SQL 生成才发现有 Key 了")


# ----------------------------------------------------------------------
# 接口：测试、保存并重建、恢复
# ----------------------------------------------------------------------


class EmbeddingAdminTest(_EmbeddingEnvironment):
    def test_status_says_what_is_in_use_and_never_the_key(self):
        self.remote_env()
        client = self.app_client()
        described = client.get("/admin/model").json()["embedding"]
        self.assertEqual(
            {name: described[name] for name in ("source", "backend", "model", "dimensions", "key_configured",
                                               "endpoint", "key_hint")},
            {"source": "server", "backend": "remote", "model": "text-embedding-3-small", "dimensions": DIMS,
             "key_configured": True, "endpoint": f"{RELAY}/v1/embeddings", "key_hint": ENV_KEY[-4:]},
        )
        self.assertEqual(described["active"], {"backend": "remote", "model": "text-embedding-3-small",
                                               "dimensions": DIMS})
        self.assertIn("https://relay.invalid", described["reusable_key_addresses"])
        self.assertEqual(described["defaults"]["dimensions"], 1024)
        self.assert_no_secret(client.get("/admin/model").text)

    def test_testing_reports_the_vector_size_and_saves_nothing(self):
        client = self.app_client()
        tested = client.post("/admin/model/embedding/test", json=embedding_body())
        self.assertEqual(tested.status_code, 200, tested.text)
        report = tested.json()
        self.assertTrue(report["ok"])
        self.assertEqual(report["endpoint"], f"{OTHER}/v1/embeddings")
        self.assertEqual((report["results"][0]["outcome"], report["results"][0]["dimensions"]), ("ok", DIMS))
        self.assertFalse(self.store_path.exists())
        self.assertEqual(get_settings().embedding_backend, "local")

    def test_a_service_that_ignores_the_requested_size_fails_the_test(self):
        self.embedding_failure = lambda request: embeddings_reply(request, dimensions=1536)
        client = self.app_client()
        result = client.post("/admin/model/embedding/test", json=embedding_body()).json()["results"][0]
        self.assertEqual((result["outcome"], result["dimensions"]), ("dimension_mismatch", 1536))

    def test_saving_rebuilds_the_index_then_switches_to_it(self):
        client = self.app_client()
        pipeline = client.app.state.pipeline
        before = pipeline.schema_retrieval_service
        self.assertEqual(pipeline.embedding_status["backend"], "local")

        saved = client.put("/admin/model/embedding", json=embedding_body())
        self.assertEqual(saved.status_code, 200, saved.text)
        result = saved.json()
        self.assertTrue(result["test"]["ok"])
        self.assertGreater(result["rebuild"]["texts"], 10)
        described = result["status"]["embedding"]
        self.assertEqual((described["source"], described["backend"], described["active"]["backend"],
                          described["key_hint"]), ("page", "remote", "remote", OTHER_KEY[-4:]))

        self.assertIsNot(pipeline.schema_retrieval_service, before)
        self.assertEqual(stat.S_IMODE(os.stat(self.store_path).st_mode), 0o600)
        self.assertEqual(load_store(str(self.store_path))["embedding"]["values"]["model"], "text-embedding-3-small")
        self.assertEqual({request.headers["authorization"] for request in self.embedding_requests()},
                         {f"Bearer {OTHER_KEY}"})
        self.assertEqual({request.url.host for request in self.embedding_requests()}, {"other.invalid"})

        asked = len(self.embedding_requests())
        self.ask(client)
        self.assertEqual(len(self.embedding_requests()), asked + 1, "换上之后的问题用新模型")

    def test_a_failed_test_saves_nothing_and_keeps_the_index(self):
        self.embedding_failure = lambda request: httpx.Response(
            401, json={"error": {"message": "Incorrect API key provided: sk-other-key-2222...cccc"}})
        client = self.app_client()
        before = client.app.state.pipeline.schema_retrieval_service
        refused = client.put("/admin/model/embedding", json=embedding_body())
        self.assertEqual(refused.status_code, 422, refused.text)
        detail = refused.json()["detail"]
        self.assertEqual((detail["code"], detail["test"]["results"][0]["outcome"]), ("test_failed", "auth"))
        self.assertIs(client.app.state.pipeline.schema_retrieval_service, before)
        self.assertFalse(self.store_path.exists())
        self.assert_no_secret(refused.text)

    def test_an_index_that_cannot_be_built_saves_nothing(self):
        # 测试只算一段文本，能过；建索引时一批几十段，服务端撑不住
        self.embedding_failure = lambda request: (
            embeddings_reply(request) if len(json.loads(request.content)["input"]) == 1
            else httpx.Response(500, json={"error": {"message": "batch too large"}})
        )
        client = self.app_client()
        before = client.app.state.pipeline.schema_retrieval_service
        failed = client.put("/admin/model/embedding", json=embedding_body())
        self.assertEqual(failed.status_code, 502, failed.text)
        self.assertEqual(failed.json()["detail"]["code"], "index_failed")
        self.assertIn("batch too large", failed.json()["detail"]["message"])
        self.assertIs(client.app.state.pipeline.schema_retrieval_service, before)
        self.assertFalse(self.store_path.exists())
        self.assertEqual(get_settings().embedding_backend, "local")

    def test_a_blank_key_is_only_reused_on_a_host_that_already_has_one(self):
        client = self.app_client()
        chat = page_body(provider="openai", base_url=f"{RELAY}/v1", chat_model="gpt-5.6-luna",
                         coder_model="gpt-5.6-luna")
        self.assertEqual(client.put("/admin/model", json=chat).status_code, 200)

        elsewhere = client.put("/admin/model/embedding", json=embedding_body(api_key=""))
        self.assertEqual(elsewhere.status_code, 400, elsewhere.text)
        self.assertEqual(elsewhere.json()["detail"]["code"], "key_required")

        same_host = client.put("/admin/model/embedding", json=embedding_body(base_url=f"{RELAY}/v1", api_key=""))
        self.assertEqual(same_host.status_code, 200, same_host.text)
        self.assertEqual({request.headers["authorization"] for request in self.embedding_requests()},
                         {f"Bearer {PAGE_KEY}"}, "对话模型那把 Key 只发给了它自己的主机")
        self.assertEqual(load_store(str(self.store_path))["values"]["openai_api_key"], PAGE_KEY,
                         "主链路那一节原样留着")

    def test_restoring_goes_back_to_the_server_embedding_and_rebuilds(self):
        client = self.app_client()
        client.put("/admin/model", json=page_body())
        client.put("/admin/model/embedding", json=embedding_body())
        installed = client.app.state.pipeline.schema_retrieval_service

        restored = client.delete("/admin/model/embedding")
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertTrue(restored.json()["cleared"])
        described = restored.json()["status"]["embedding"]
        self.assertEqual((described["source"], described["backend"], described["active"]["backend"]),
                         ("server", "local", "local"))
        self.assertIsNot(client.app.state.pipeline.schema_retrieval_service, installed)
        self.assertEqual(load_store(str(self.store_path))["values"]["anthropic_api_key"], PAGE_KEY)
        self.assertNotIn("embedding", load_store(str(self.store_path)))

        again = client.delete("/admin/model/embedding")
        self.assertFalse(again.json()["cleared"])

    def test_someone_else_on_the_network_cannot_change_it(self):
        client = self.app_client(**REMOTE)
        for response in (
            client.put("/admin/model/embedding", json=embedding_body()),
            client.post("/admin/model/embedding/test", json=embedding_body()),
            client.delete("/admin/model/embedding"),
        ):
            self.assertEqual(response.status_code, 403, response.text)
            self.assertEqual(response.json()["detail"]["code"], "local_only")
        self.assertEqual(self.embedding_requests(), [])
        self.assertNotIn("endpoint", client.get("/admin/model").json()["embedding"])

    def test_unknown_fields_and_bad_values_are_refused_without_echoing_them(self):
        client = self.app_client()
        for body, code in (
            ({**embedding_body(), "provider": "openai"}, "invalid_request"),
            (embedding_body(dimensions=-3), "invalid_dimensions"),
            (embedding_body(dimensions="lots"), "invalid_dimensions"),
            (embedding_body(base_url="http://169.254.169.254/latest"), "forbidden_address"),
            (embedding_body(model="text embedding"), "invalid_model"),
        ):
            refused = client.put("/admin/model/embedding", json=body)
            self.assertEqual(refused.status_code, 400, body)
            self.assertEqual(refused.json()["detail"]["code"], code, body)
            self.assert_no_secret(refused.text)
        self.assertEqual(self.embedding_requests(), [])

    def test_the_key_is_never_logged(self):
        client = self.app_client()
        with self.assertLogs("ai_sql", level="INFO") as logs:
            client.post("/admin/model/embedding/test", json=embedding_body())
            client.put("/admin/model/embedding", json=embedding_body())
            client.delete("/admin/model/embedding")
        self.assert_no_secret(*logs.output)
        self.assertTrue(any("model_settings.embedding_saved" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
