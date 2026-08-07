"""
控制台配置模型连接：存哪、谁能改、Key 去哪、保存后怎么生效、两个实例怎么跟上。

全部离线：对话模型的测试连接打桩在探测函数上，向量模型是接在 HTTP 客户端上的假端点，
存储是临时 SQLite。
"""
import asyncio
import json
import os
import stat
from types import SimpleNamespace

import httpx
import pytest
from openai import AuthenticationError, NotFoundError, RateLimitError
from starlette.requests import Request

from app.api import model_admin
from app.core import model_settings as ms
from app.core.embedder import Embedder, EmbeddingUnavailable
from app.core.model_runtime import ModelRuntime, chat_config_from, embedder_from
from app.core.openai_compat import AsyncOpenAICompat
from app.storage.vector_store import EMBEDDING_SIGNATURE_KEY, LocalStore, check_embedding_signature
from app.tools.knowledge_base import KnowledgeBase

KEY = "sk-proj-test-0123456789abcd"
OTHER_KEY = "sk-proj-other-9876543210wxyz"


# ── 夹具 ──────────────────────────────────────────────────────────────────────

class _Component:
    """记下 reconfigure_llm 收到了什么，代替编排器、工具管理、记忆、评测。"""

    def __init__(self):
        self.calls = []

    def reconfigure_llm(self, client, model):
        self.calls.append((client, model))


class _FakeEmbeddingAPI:
    def __init__(self, dim=8):
        self.dim = dim
        self.status = 200
        self.requests = []

    def vector(self, text):
        return Embedder.local_embedding(f"remote::{text}", self.dim)

    def handle(self, request):
        texts = json.loads(request.content)["input"]
        self.requests.append(texts)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": f"Incorrect API key provided: {KEY}",
                                                               "type": "invalid_request_error"}})
        return httpx.Response(200, json={"data": [{"index": i, "embedding": self.vector(t)}
                                                  for i, t in enumerate(texts)]})


@pytest.fixture
def api(monkeypatch):
    """假的 /embeddings。只接管没指定 transport 的客户端——测试用的 ASGI 客户端照常走它自己的。"""
    fake = _FakeEmbeddingAPI()
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(fake.handle))
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return fake


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = str(tmp_path / "runtime" / "model_settings.json")
    monkeypatch.setenv("AI_HELPDESK_MODEL_SETTINGS_PATH", path)
    monkeypatch.delenv("AI_HELPDESK_MODEL_SETTINGS_EDITABLE", raising=False)
    for name in ("EMBEDDING_BASE_URL", "EMBEDDING_API_KEY", "EMBEDDING_MODEL", "EMBEDDING_DIM"):
        monkeypatch.delenv(name, raising=False)
    return path


@pytest.fixture
async def store(tmp_path):
    local = LocalStore(db_path=str(tmp_path / "vec.db"))
    kb = KnowledgeBase(store=local, embedder=Embedder())
    await kb.add_documents_async([
        {"title": "安装调整", "content": "柜门关不严绝大多数是铰链未调到位"},
        {"title": "送货预约", "content": "送货需提前预约并具备卸货条件"},
    ])
    await local.episodic_add("e1", "u1", "c1", "客户问过补件进度", "", Embedder.local_embedding("x"))
    await check_embedding_signature(local, Embedder())
    yield local
    await local.close()


def _runtime(path, store, embedder=None, components=None, editable=True):
    stored = ms.load_store(path)
    embedder_obj, meta = embedder_from(stored)
    return ModelRuntime(
        path=path, editable=editable, chat=chat_config_from(stored),
        embedder=embedder or embedder_obj, embedding_meta=meta, store=store,
        llm_components=components if components is not None else [_Component()],
        llm_info={},
    )


def _client(runtime, monkeypatch, *, host="127.0.0.1:8080", chat_ok=True):
    """挂上管理接口的最小应用。对话模型的测试连接打桩：不发任何请求。"""
    from fastapi import FastAPI

    async def fake_probe(candidate):
        outcome = "ok" if chat_ok else "auth_failed"
        return {"ok": chat_ok, "warnings": candidate["warnings"],
                "checks": [{"check": "chat", "outcome": outcome, "status": 200 if chat_ok else 401,
                            "detail": "", "latency_ms": 1}]}

    monkeypatch.setattr(model_admin, "probe_chat", fake_probe)
    app = FastAPI()
    app.include_router(model_admin.router)
    model_admin.install(runtime)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{host}")


# ── 存取与校验 ────────────────────────────────────────────────────────────────

def test_feature_is_off_without_a_settings_path(monkeypatch):
    monkeypatch.delenv("AI_HELPDESK_MODEL_SETTINGS_PATH", raising=False)
    assert ms.settings_path() == ""
    assert ms.load_store("") == {}


def test_each_section_is_saved_on_its_own_and_the_file_is_private(settings_file):
    ms.save_section(settings_file, "chat", {"provider": "openai", "base_url": "https://api.openai.com/v1",
                                            "api_key": KEY, "model": "gpt-5.6-luna"}, saved_by="console@x")
    ms.save_section(settings_file, "embedding", {"base_url": "https://api.openai.com/v1", "api_key": KEY,
                                                 "model": "text-embedding-3-small", "dim": 1536,
                                                 "query_prefix": ""})
    assert stat.S_IMODE(os.stat(settings_file).st_mode) == 0o600     # 文件里有明文 Key

    store = ms.load_store(settings_file)
    assert store["chat"]["values"]["model"] == "gpt-5.6-luna"
    assert store["chat"]["saved_by"] == "console@x"
    assert store["embedding"]["values"]["dim"] == 1536

    assert ms.clear_section(settings_file, "chat") is True
    assert "chat" not in ms.load_store(settings_file) and "embedding" in ms.load_store(settings_file)
    assert ms.clear_section(settings_file, "embedding") is True
    assert not os.path.exists(settings_file)                          # 两组都没了，文件也删掉


def test_a_broken_section_is_dropped_with_a_reason_and_the_other_kept(settings_file):
    os.makedirs(os.path.dirname(settings_file), exist_ok=True)
    with open(settings_file, "w", encoding="utf-8") as f:
        json.dump({"chat": {"values": {"provider": "openai", "base_url": "https://api.openai.com/v1",
                                       "model": "gpt-5.6-luna"}},   # 少了 Key
                   "embedding": {"values": {"base_url": "https://api.openai.com/v1", "api_key": KEY,
                                            "model": "text-embedding-3-small", "dim": 1536}}}, f)
    store = ms.load_store(settings_file)
    assert "chat" not in store and "embedding" in store
    assert "Key" in store["problems"]["chat"]


@pytest.mark.parametrize("url,code", [
    ("http://169.254.169.254/v1", "forbidden_address"),          # 云主机元数据服务
    ("http://2852039166/v1", "forbidden_address"),               # 躲检查的数字主机名
    ("https://user:pw@api.openai.com/v1", "invalid_base_url"),
    ("https://api.openai.com/v1?x=1", "invalid_base_url"),
    ("ftp://api.openai.com", "invalid_base_url"),
])
def test_addresses_that_must_not_receive_a_key_are_refused(url, code):
    with pytest.raises(ms.ModelSettingsError) as raised:
        ms.validate_base_url(url)
    assert raised.value.code == code


def test_plaintext_http_to_a_public_host_is_allowed_but_flagged():
    assert ms.validate_base_url("http://relay.example.com/v1/")[1] == ["plaintext_http"]
    assert ms.validate_base_url("http://127.0.0.1:11434/v1") == ("http://127.0.0.1:11434/v1", [])


# ── 谁能改 ────────────────────────────────────────────────────────────────────

def _request(host="127.0.0.1:5174", origin=None):
    headers = [(b"host", host.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    return Request({"type": "http", "method": "PUT", "path": "/admin/model/chat", "headers": headers,
                    "query_string": b"", "client": ("172.18.0.5", 50000)})


@pytest.mark.parametrize("host,origin,problem", [
    ("127.0.0.1:5174", None, ""),                                # 本机控制台，经前端 nginx 转发
    ("localhost:8080", "http://localhost:5174", ""),
    ("[::1]:8080", None, ""),
    ("evil.example", None, "not_local"),                         # DNS 重绑定：Host 还是对方的域名
    ("127.0.0.1:5174", "https://evil.example", "cross_origin"),  # 别的网页发来的跨站请求
])
def test_only_requests_from_the_local_console_may_edit(settings_file, host, origin, problem):
    assert model_admin.edit_problem(_request(host, origin)) == problem


def test_customer_facing_instance_is_read_only(settings_file, monkeypatch):
    monkeypatch.setenv("AI_HELPDESK_MODEL_SETTINGS_EDITABLE", "false")
    assert model_admin.edit_problem(_request()) == "read_only_instance"


# ── 对话连接：保存即生效 ──────────────────────────────────────────────────────

async def test_status_never_contains_the_key(settings_file, store, monkeypatch):
    ms.save_section(settings_file, "chat", {"provider": "openai", "base_url": "https://api.openai.com/v1",
                                            "api_key": KEY, "model": "gpt-5.6-luna"})
    runtime = _runtime(settings_file, store)
    async with _client(runtime, monkeypatch) as client:
        body = (await client.get("/admin/model")).json()
    assert body["chat"]["source"] == "page" and body["chat"]["key_hint"] == "abcd"
    assert KEY not in json.dumps(body)


async def test_saving_chat_applies_it_to_every_component_without_restart(settings_file, store, monkeypatch):
    components = [_Component(), _Component()]
    runtime = _runtime(settings_file, store, components=components)
    async with _client(runtime, monkeypatch) as client:
        resp = await client.put("/admin/model/chat", json={
            "provider": "openai", "base_url": "https://api.openai.com/v1", "api_key": KEY,
            "model": "gpt-5.6-luna"})
    assert resp.status_code == 200, resp.text
    assert KEY not in resp.text
    assert all(c.calls and c.calls[-1][1] == "gpt-5.6-luna" for c in components)
    assert runtime.chat["provider"] == "openai" and runtime.chat["source"] == "page"
    assert ms.load_store(settings_file)["chat"]["values"]["api_key"] == KEY


async def test_a_connection_that_fails_its_test_is_not_saved_unless_forced(settings_file, store, monkeypatch):
    runtime = _runtime(settings_file, store)
    body = {"provider": "openai", "base_url": "https://api.openai.com/v1", "api_key": KEY, "model": "gpt-x"}
    async with _client(runtime, monkeypatch, chat_ok=False) as client:
        refused = await client.put("/admin/model/chat", json=body)
        assert refused.status_code == 422 and refused.json()["detail"]["code"] == "test_failed"
        assert "chat" not in ms.load_store(settings_file)

        forced = await client.put("/admin/model/chat", json={**body, "force": True})
    assert forced.status_code == 200
    assert "chat" in ms.load_store(settings_file)


async def test_blank_key_reuses_the_current_one_only_on_the_same_host(settings_file, store, monkeypatch):
    ms.save_section(settings_file, "chat", {"provider": "openai", "base_url": "https://api.openai.com/v1",
                                            "api_key": KEY, "model": "gpt-5.6-luna"})
    runtime = _runtime(settings_file, store)
    async with _client(runtime, monkeypatch) as client:
        same_host = await client.put("/admin/model/chat", json={
            "provider": "openai", "base_url": "https://api.openai.com/v1/", "api_key": "",
            "model": "gpt-6-luna"})
        other_host = await client.put("/admin/model/chat", json={
            "provider": "openai", "base_url": "https://gateway.example.com/v1", "api_key": "",
            "model": "gpt-6-luna"})
    assert same_host.status_code == 200
    assert ms.load_store(settings_file)["chat"]["values"]["api_key"] == KEY
    # 换了主机还沿用现在的 Key，等于把它交给一台新主机
    assert other_host.status_code == 422
    assert other_host.json()["detail"]["code"] == "api_key_required"


async def test_restoring_chat_goes_back_to_env(settings_file, store, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-key-000000")
    ms.save_section(settings_file, "chat", {"provider": "openai", "base_url": "https://api.openai.com/v1",
                                            "api_key": KEY, "model": "gpt-5.6-luna"})
    components = [_Component()]
    runtime = _runtime(settings_file, store, components=components)
    async with _client(runtime, monkeypatch) as client:
        resp = await client.delete("/admin/model/chat")
    assert resp.status_code == 200
    assert resp.json()["chat"]["source"] == "env" and resp.json()["chat"]["provider"] == "anthropic"
    assert "chat" not in ms.load_store(settings_file)
    assert components[0].calls                                         # 换回 .env 那份也立刻生效


async def test_edits_from_another_site_are_refused(settings_file, store, monkeypatch):
    runtime = _runtime(settings_file, store)
    async with _client(runtime, monkeypatch) as client:
        resp = await client.put("/admin/model/chat", headers={"origin": "https://evil.example"}, json={
            "provider": "openai", "base_url": "https://evil.example/v1", "api_key": KEY, "model": "x"})
    assert resp.status_code == 403 and resp.json()["detail"]["code"] == "cross_origin"
    assert "chat" not in ms.load_store(settings_file)


# ── 测试连接 ──────────────────────────────────────────────────────────────────

class _FakeChatClient:
    def __init__(self, error=None):
        self.error = error
        self.messages = self

    async def create(self, **kwargs):
        if self.error:
            raise self.error
        return type("R", (), {"content": [type("B", (), {"type": "text", "text": "可以"})()],
                              "stop_reason": "end_turn"})()

    async def parse(self, **kwargs):
        from app.tools.tool_manager import QueryRewritePayload
        return type("R", (), {"parsed_output": QueryRewritePayload(queries=["门板缺件补发"]),
                              "stop_reason": "end_turn"})()


def _status_error(cls, status, message, body):
    response = httpx.Response(status, request=httpx.Request("POST", "https://api.openai.com/v1/x"))
    return cls(message, response=response, body=body)


async def test_chat_probe_checks_plain_and_structured_calls(monkeypatch):
    monkeypatch.setattr(model_admin, "_probe_client", lambda candidate: _FakeChatClient())
    result = await model_admin.probe_chat({"provider": "openai", "base_url": "https://api.openai.com/v1",
                                           "api_key": KEY, "model": "gpt-5.6-luna", "warnings": []})
    assert result["ok"] is True
    assert [c["check"] for c in result["checks"]] == ["chat", "structured"]


@pytest.mark.parametrize("error,outcome", [
    (_status_error(AuthenticationError, 401, f"Incorrect API key provided: {KEY}",
                   {"error": {"message": f"Incorrect API key provided: {KEY}", "type": "invalid_request_error"}}),
     "auth_failed"),
    (_status_error(RateLimitError, 429, "You exceeded your current quota",
                   {"error": {"message": "You exceeded your current quota", "type": "insufficient_quota"}}),
     "quota_exhausted"),
    (_status_error(NotFoundError, 404, "The model `gpt-x` does not exist",
                   {"error": {"message": "The model `gpt-x` does not exist", "type": "invalid_request_error"}}),
     "model_not_found"),
])
async def test_chat_probe_names_the_failure_and_never_echoes_the_key(monkeypatch, error, outcome):
    monkeypatch.setattr(model_admin, "_probe_client", lambda candidate: _FakeChatClient(error))
    result = await model_admin.probe_chat({"provider": "openai", "base_url": "https://api.openai.com/v1",
                                           "api_key": KEY, "model": "gpt-x", "warnings": []})
    assert result["ok"] is False
    assert result["checks"][0]["outcome"] == outcome
    assert result["checks"][1]["outcome"] == "skipped"     # 第一条就失败了，第二条不再发
    assert KEY not in json.dumps(result)


def _completion(text, finish_reason="stop"):
    message = SimpleNamespace(content=text, parsed=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
                           usage=SimpleNamespace(prompt_tokens=5, completion_tokens=5), model="gpt-5.6-luna")


class _Relay:
    """
    假的中转站（OpenAI 兼容）：`parse` 时把 response_format 丢掉、照常回一段文字（真实 SDK 在
    校验那一步抛 ValidationError）；`create` 按 `answer(kwargs)` 作答。
    """

    def __init__(self, answer):
        self.base_url = "https://relay.example/v1/"
        self.chat = SimpleNamespace(completions=self)
        self.answer = answer

    async def create(self, **kwargs):
        return self.answer(kwargs)

    async def parse(self, **kwargs):
        kwargs["response_format"].model_validate_json("1. 柜子门板缺失如何补件")


def _relay_client(answer):
    from app.core.openai_compat import _Messages

    client = AsyncOpenAICompat(api_key=KEY, base_url="https://relay.example/v1")
    client.messages = _Messages(_Relay(answer))
    return client


def _asked_for_schema(kwargs):
    return any("JSON Schema" in str(m.get("content", "")) for m in kwargs["messages"])


async def test_a_relay_that_drops_the_schema_passes_with_a_warning(monkeypatch):
    from app.core import openai_compat

    monkeypatch.setattr(openai_compat, "_SCHEMA_NOT_ENFORCED", set())
    client = _relay_client(lambda kwargs: _completion(
        '{"queries": ["门板缺件补发", "门板尺寸测量"]}' if _asked_for_schema(kwargs) else "可以"
    ))
    monkeypatch.setattr(model_admin, "_probe_client", lambda candidate: client)
    result = await model_admin.probe_chat({"provider": "openai", "base_url": "https://relay.example/v1",
                                           "api_key": KEY, "model": "gpt-5.6-luna", "warnings": []})
    assert result["ok"] is True
    assert [c["outcome"] for c in result["checks"]] == ["ok", "ok"]
    assert result["warnings"] == ["schema_not_enforced"]


async def test_a_relay_answer_in_the_wrong_shape_fails_the_structured_check(monkeypatch):
    from app.core import openai_compat

    monkeypatch.setattr(openai_compat, "_SCHEMA_NOT_ENFORCED", set())
    client = _relay_client(lambda kwargs: _completion("1. 门板缺件补发\n2. 门板尺寸测量"))
    monkeypatch.setattr(model_admin, "_probe_client", lambda candidate: client)
    result = await model_admin.probe_chat({"provider": "openai", "base_url": "https://relay.example/v1",
                                           "api_key": KEY, "model": "gpt-5.6-luna", "warnings": []})
    assert result["ok"] is False
    assert result["checks"][1]["outcome"] == "not_structured"
    assert "预算" not in json.dumps(result, ensure_ascii=False), "不能再说成「预算被推理用完了」"


async def test_a_web_page_is_reported_as_a_missing_v1(monkeypatch):
    client = _relay_client(lambda kwargs: "<!doctype html><html><title>Relay</title></html>")
    monkeypatch.setattr(model_admin, "_probe_client", lambda candidate: client)
    result = await model_admin.probe_chat({"provider": "openai", "base_url": "https://relay.example",
                                           "api_key": KEY, "model": "gpt-5.6-luna", "warnings": []})
    assert [c["outcome"] for c in result["checks"]] == ["not_api_response", "skipped"]
    assert "/v1" in result["checks"][0]["detail"]


async def test_embedding_probe_reports_the_real_dimension(api):
    result = await model_admin.probe_embedding({"base_url": "https://api.openai.com/v1", "api_key": KEY,
                                                "model": "text-embedding-3-small", "dim": 0,
                                                "query_prefix": "", "warnings": []})
    assert result["ok"] is True and result["dim"] == api.dim

    wrong = await model_admin.probe_embedding({"base_url": "https://api.openai.com/v1", "api_key": KEY,
                                               "model": "text-embedding-3-small", "dim": 1536,
                                               "query_prefix": "", "warnings": []})
    assert wrong["ok"] is False and wrong["checks"][0]["outcome"] == "dim_mismatch"

    api.status = 401
    denied = await model_admin.probe_embedding({"base_url": "https://api.openai.com/v1", "api_key": KEY,
                                                "model": "text-embedding-3-small", "dim": 0,
                                                "query_prefix": "", "warnings": []})
    assert denied["checks"][0]["outcome"] == "auth_failed"
    assert KEY not in json.dumps(denied)


# ── 可选的模型（下拉框）────────────────────────────────────────────────────────

LISTED = ["gpt-6-luna", "gpt-5.6-luna", "text-embedding-3-small", "Qwen/Qwen3-Embedding-0.6B",
          "BAAI/bge-reranker-v2-m3", "gpt-image-1", "gpt-4o-mini-tts", "whisper-1", "bad name!"]


class _FakeModelsAPI:
    def __init__(self):
        self.status = 200
        self.body: object = {"object": "list", "data": [{"id": name, "object": "model"} for name in LISTED]}
        self.requests: list[httpx.Request] = []

    def handle(self, request):
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": f"Incorrect API key provided: {KEY}"}})
        if isinstance(self.body, str):
            return httpx.Response(200, text=self.body, headers={"content-type": "text/html"})
        return httpx.Response(200, json=self.body)


@pytest.fixture
def models_api(monkeypatch):
    fake = _FakeModelsAPI()
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(fake.handle))
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return fake


async def test_model_lists_are_filtered_per_section_and_reuse_the_saved_key(settings_file, store, models_api,
                                                                           monkeypatch):
    ms.save_section(settings_file, "chat", {"provider": "openai", "base_url": "https://relay.example.com",
                                            "api_key": KEY, "model": "gpt-5.6-luna"})
    ms.save_section(settings_file, "embedding", {"base_url": "https://relay.example.com/v1", "api_key": KEY,
                                                 "model": "text-embedding-3-small", "dim": 8,
                                                 "query_prefix": ""})
    runtime = _runtime(settings_file, store)
    async with _client(runtime, monkeypatch) as client:
        chat = await client.post("/admin/model/chat/models", json={
            "provider": "openai", "base_url": "https://relay.example.com", "api_key": ""})
        embedding = await client.post("/admin/model/embedding/models", json={
            "base_url": "https://relay.example.com/v1", "api_key": ""})

    assert chat.status_code == 200, chat.text
    assert chat.json()["models"] == ["gpt-5.6-luna", "gpt-6-luna"]      # 向量、重排、图像、语音都不列
    assert chat.json()["total"] == 8                                     # 写法不合规的那条不算
    assert embedding.json()["models"] == ["Qwen/Qwen3-Embedding-0.6B", "text-embedding-3-small"]
    # 只写到主机的地址补 /v1；Key 留空 = 同一台主机上沿用保存的那把
    assert [str(r.url) for r in models_api.requests] == ["https://relay.example.com/v1/models"] * 2
    assert all(r.headers["authorization"] == f"Bearer {KEY}" for r in models_api.requests)
    assert KEY not in chat.text + embedding.text


async def test_listing_models_on_another_host_needs_its_own_key(settings_file, store, models_api, monkeypatch):
    ms.save_section(settings_file, "chat", {"provider": "openai", "base_url": "https://api.openai.com/v1",
                                            "api_key": KEY, "model": "gpt-5.6-luna"})
    runtime = _runtime(settings_file, store)
    async with _client(runtime, monkeypatch) as client:
        moved = await client.post("/admin/model/chat/models", json={
            "provider": "openai", "base_url": "https://gateway.example.com/v1", "api_key": ""})
        foreign = await client.post("/admin/model/chat/models", headers={"origin": "https://evil.example"},
                                    json={"provider": "openai", "base_url": "https://evil.example/v1",
                                          "api_key": ""})
    assert moved.status_code == 422 and moved.json()["detail"]["code"] == "api_key_required"
    assert foreign.status_code == 403 and foreign.json()["detail"]["code"] == "cross_origin"
    assert models_api.requests == []                                   # 两次都没有把 Key 发出去


@pytest.mark.parametrize("status,body,outcome", [
    (401, None, "auth_failed"),
    (404, None, "list_unsupported"),                                   # 中转站没开列模型的接口
    (200, "<!doctype html><title>Relay</title>", "not_api_response"),
    (200, {"message": "ok"}, "not_api_response"),
])
async def test_a_model_list_that_cannot_be_read_is_reported_not_raised(models_api, status, body, outcome):
    models_api.status = status
    if body is not None:
        models_api.body = body
    result = await model_admin.list_models(
        {"provider": "openai", "base_url": "https://relay.example.com/v1", "api_key": KEY, "warnings": []}, "chat")
    assert result["ok"] is False and result["models"] == [] and result["outcome"] == outcome
    assert KEY not in json.dumps(result)


async def test_anthropic_model_lists_use_its_own_headers(models_api):
    models_api.body = {"data": [{"type": "model", "id": "claude-sonnet-5-5"}, {"type": "model", "id": "claude-opus-5-5"}],
                       "has_more": False}
    result = await model_admin.list_models(
        {"provider": "anthropic", "base_url": "https://relay.example.com/api", "api_key": KEY, "warnings": []},
        "chat")
    assert result["models"] == ["claude-opus-5-5", "claude-sonnet-5-5"]
    request = models_api.requests[0]
    assert str(request.url) == "https://relay.example.com/api/v1/models?limit=1000"
    assert request.headers["x-api-key"] == KEY and request.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in request.headers


async def test_a_list_without_any_embedding_names_is_offered_whole(models_api):
    models_api.body = [{"id": "house-vectors-v2"}, "house-chat-v1"]          # 直接回数组的中转站
    result = await model_admin.list_models(
        {"provider": "openai", "base_url": "https://relay.example.com/v1", "api_key": KEY, "warnings": []},
        "embedding")
    assert result["ok"] is True and result["models"] == ["house-chat-v1", "house-vectors-v2"]


# ── 向量连接：切换、恢复、另一个实例跟上 ──────────────────────────────────────

async def test_saving_embedding_rebuilds_vectors_then_switches(settings_file, store, api, monkeypatch):
    runtime = _runtime(settings_file, store)
    shared = runtime.embedder
    async with _client(runtime, monkeypatch) as client:
        resp = await client.put("/admin/model/embedding", json={
            "base_url": "https://api.openai.com/v1", "api_key": KEY, "model": "text-embedding-3-small"})
        assert resp.status_code == 202, resp.text
        assert resp.json()["switch"]["state"] == "running"
        await runtime._task
        status = (await client.get("/admin/model")).json()

    assert status["switch"]["state"] == "succeeded" and status["switch"]["total"] == 3
    assert status["embedding"]["source"] == "page" and status["embedding"]["dim"] == api.dim
    assert (await store.vector_state())["kb_chunks"] == {"rows": 2, "dim": api.dim}
    assert await store.meta_get(EMBEDDING_SIGNATURE_KEY) == f"text-embedding-3-small/{api.dim}"
    assert shared.signature == f"text-embedding-3-small/{api.dim}"     # 原地换：各处拿的是同一个对象
    assert ms.load_store(settings_file)["embedding"]["values"]["dim"] == api.dim


async def test_a_failed_switch_changes_nothing(settings_file, store, api, monkeypatch):
    runtime = _runtime(settings_file, store)
    async with _client(runtime, monkeypatch) as client:
        resp = await client.put("/admin/model/embedding", json={
            "base_url": "https://api.openai.com/v1", "api_key": KEY, "model": "text-embedding-3-small"})
        assert resp.status_code == 202
        api.status = 503                                    # 测试通过之后、重算到一半服务挂了
        await runtime._task

    assert runtime.job.state == "failed"
    assert (await store.vector_state())["kb_chunks"]["dim"] == 256
    assert runtime.embedder.signature == "local_ngram/256"
    assert "embedding" not in ms.load_store(settings_file)


async def test_only_one_switch_runs_at_a_time(settings_file, store):
    runtime = _runtime(settings_file, store)
    runtime.job.state = "running"
    assert runtime.switching
    with pytest.raises(ms.ModelSettingsError):
        runtime.start_embedding_switch(Embedder(), values=None)


async def test_imports_are_paused_while_vectors_are_being_rebuilt(monkeypatch):
    """重算到一半进来的分片不会有新模型的向量，那次切换就换不上。"""
    from fastapi import HTTPException

    from app.api import main

    monkeypatch.setattr(main, "_model_runtime", type("R", (), {"switching": True})())
    with pytest.raises(HTTPException) as raised:
        await main._import_or_503(kb=None, docs=[{"title": "t", "content": "c"}])
    assert raised.value.status_code == 409


async def test_restoring_embedding_switches_back_to_env(settings_file, store, api, monkeypatch):
    runtime = _runtime(settings_file, store)
    async with _client(runtime, monkeypatch) as client:
        await client.put("/admin/model/embedding", json={
            "base_url": "https://api.openai.com/v1", "api_key": KEY, "model": "text-embedding-3-small"})
        await runtime._task
        resp = await client.delete("/admin/model/embedding")
        assert resp.status_code == 202
        await runtime._task

    assert runtime.embedder.signature == "local_ngram/256"
    assert (await store.vector_state())["kb_chunks"]["dim"] == 256
    assert "embedding" not in ms.load_store(settings_file)


async def test_the_other_instance_follows_chat_and_embedding(settings_file, store, api, monkeypatch):
    """
    员工实例保存，客户实例过几秒自己跟上。向量要等库里的向量已经换好才跟——
    不然就是拿新模型的查询向量去比旧库。
    """
    staff = _runtime(settings_file, store)
    customer_components = [_Component()]
    customer = _runtime(settings_file, store, components=customer_components, editable=False)

    async with _client(staff, monkeypatch) as client:
        await client.put("/admin/model/chat", json={
            "provider": "openai", "base_url": "https://api.openai.com/v1", "api_key": KEY,
            "model": "gpt-5.6-luna"})
    await customer.sync()
    assert customer.chat["model"] == "gpt-5.6-luna" and customer_components[0].calls

    # 配置文件先到、库还没换：客户实例不跟
    ms.save_section(settings_file, "embedding", {"base_url": "https://api.openai.com/v1", "api_key": KEY,
                                                 "model": "text-embedding-3-small", "dim": api.dim,
                                                 "query_prefix": ""})
    await customer.sync()
    assert customer.embedder.signature == "local_ngram/256"

    # 库换好之后跟上
    target = Embedder(base_url="https://api.openai.com/v1", api_key=KEY, model="text-embedding-3-small",
                      dim=api.dim)
    rows = await store.kb_embedding_rows()
    episodes = await store.episodic_embedding_rows()
    await store.replace_embeddings(
        api.dim,
        {r["id"]: api.vector(KnowledgeBase.embedding_text(r["title"], r["content"])) for r in rows},
        {r["id"]: api.vector(r["summary"]) for r in episodes},
        target.signature,
    )
    await customer.sync()
    assert customer.embedder.signature == target.signature


async def test_search_keeps_working_while_an_instance_lags_behind_a_switch(settings_file, store, api):
    """另一个实例刚把向量换成新维度，这个实例还没跟上：这几秒只走关键词，不报错。"""
    lagging = KnowledgeBase(store=store, embedder=Embedder())
    rows = await store.kb_embedding_rows()
    episodes = await store.episodic_embedding_rows()
    await store.replace_embeddings(
        api.dim,
        {r["id"]: api.vector(KnowledgeBase.embedding_text(r["title"], r["content"])) for r in rows},
        {r["id"]: api.vector(r["summary"]) for r in episodes},
        f"text-embedding-3-small/{api.dim}",
    )
    hits = await lagging.search_async("铰链怎么调", top_k=3)
    assert hits and hits[0]["title"] == "安装调整"


# ── 评审跟进：同一模型换连接、配置写不进去、切换瞬间的在途写入 ────────────────────

_REMOTE = {"base_url": "https://api.openai.com/v1", "api_key": KEY, "model": "text-embedding-3-small"}


async def test_the_other_instance_follows_a_new_key_host_or_prefix_on_the_same_model(
    settings_file, store, api, monkeypatch
):
    """
    Key 被停用后换一把、或者只改地址和查询前缀：模型和维度没变，向量不用重算，
    但客户实例必须用上新的连接——否则它一直拿作废的 Key 去查，检索悄悄退成只走关键词。
    """
    staff = _runtime(settings_file, store)
    customer = _runtime(settings_file, store, editable=False)
    async with _client(staff, monkeypatch) as client:
        await client.put("/admin/model/embedding", json=_REMOTE)
        await staff._task
        await customer.sync()
        assert customer.embedder._api_key == KEY

        await client.put("/admin/model/embedding", json={
            **_REMOTE, "api_key": OTHER_KEY, "base_url": "https://embeddings.example.org/v1",
            "query_prefix": "Query: "})
        await staff._task
    assert staff.job.state == "succeeded"
    await customer.sync()

    assert customer.embedder._api_key == OTHER_KEY
    assert customer.embedder._base_url == "https://embeddings.example.org/v1"
    assert customer.embedder._query_prefix == "Query: "
    assert customer.embedder.signature == staff.embedder.signature


async def test_restoring_embedding_with_the_same_model_really_uses_the_env_connection(
    settings_file, store, api, monkeypatch
):
    """页面存的和 .env 是同一个模型：恢复时不重算，但运行中的连接也得换回 .env 那一份。"""
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://env.example.org/v1")
    monkeypatch.setenv("EMBEDDING_API_KEY", KEY)
    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("EMBEDDING_DIM", str(api.dim))
    runtime = _runtime(settings_file, store)
    async with _client(runtime, monkeypatch) as client:
        resp = await client.put("/admin/model/embedding", json={
            **_REMOTE, "api_key": OTHER_KEY, "base_url": "https://page.example.org/v1"})
        assert resp.status_code == 202, resp.text
        await runtime._task
        assert runtime.embedder._api_key == OTHER_KEY

        resp = await client.delete("/admin/model/embedding")
        assert resp.status_code == 202, resp.text
        if runtime.switching:
            await runtime._task
        status = (await client.get("/admin/model")).json()

    assert status["embedding"]["source"] == "env"
    assert runtime.embedder._api_key == KEY
    assert runtime.embedder._base_url == "https://env.example.org/v1"


async def test_a_config_file_that_cannot_be_written_after_the_vectors_switched_is_reported_truthfully(
    settings_file, store, api, monkeypatch
):
    """
    向量已经在一个事务里换成新模型了，这时配置文件写不进去（磁盘满、挂载权限变了）：
    不能报"库没有改动"、也不能让运行中的实例继续用旧模型去查新向量。
    """
    runtime = _runtime(settings_file, store)
    real_save = ms.save_section

    def denied(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(ms, "save_section", denied)
    async with _client(runtime, monkeypatch) as client:
        await client.put("/admin/model/embedding", json=_REMOTE)
        await runtime._task

    signature = f"text-embedding-3-small/{api.dim}"
    assert await store.meta_get(EMBEDDING_SIGNATURE_KEY) == signature
    assert runtime.embedder.signature == signature          # 运行中的实例和库一致
    job = runtime.job.public()
    assert job["committed"] is True
    assert "没有改动" not in job["error"] and "Permission denied" in job["error"]

    # 文件恢复可写之后自己补写，重启就不会因为"库是新的、配置是旧的"起不来
    monkeypatch.setattr(ms, "save_section", real_save)
    await runtime.sync()
    saved = ms.load_store(settings_file)["embedding"]["values"]
    assert saved["model"] == "text-embedding-3-small"
    restarted, _ = embedder_from(ms.load_store(settings_file))
    await check_embedding_signature(store, restarted)


async def test_a_document_embedded_before_the_cutover_never_lands_with_old_vectors(
    settings_file, store, api, monkeypatch
):
    """导入里"算向量"和"写库"之间隔着切换：这篇不能带着旧模型的向量写进新库。"""
    runtime = _runtime(settings_file, store)
    kb = KnowledgeBase(store=store, embedder=runtime.embedder)
    embedded, release = asyncio.Event(), asyncio.Event()
    real_embed_many = runtime.embedder.embed_many

    async def held(texts):
        vectors = await real_embed_many(texts)
        embedded.set()
        await release.wait()
        return vectors

    monkeypatch.setattr(runtime.embedder, "embed_many", held)
    importing = asyncio.create_task(kb.import_documents([{"title": "补件流程", "content": "补件需要提供订单号和照片"}]))
    await embedded.wait()

    async with _client(runtime, monkeypatch) as client:
        await client.put("/admin/model/embedding", json=_REMOTE)
        await runtime._task
    assert runtime.job.state == "succeeded"

    release.set()
    with pytest.raises(EmbeddingUnavailable):
        await importing
    state = await store.vector_state()
    assert state["kb_chunks"] == {"rows": 2, "dim": api.dim}   # 没有多出一篇、也没有混进旧维度的行


async def test_an_episodic_summary_embedded_before_the_cutover_is_re_embedded(
    settings_file, store, api, monkeypatch
):
    from app.memory.conversation_memory import MemoryManager

    runtime = _runtime(settings_file, store)
    memory = MemoryManager(store=store, embedder=runtime.embedder, api_key="test-key")
    embedded, release = asyncio.Event(), asyncio.Event()
    real_embed = runtime.embedder.embed

    async def held(text):
        vector = await real_embed(text)
        embedded.set()
        await release.wait()
        return vector

    monkeypatch.setattr(runtime.embedder, "embed", held)
    storing = asyncio.create_task(memory._store_episodic("u2", "c2", "原文", "客户问了补件进度"))
    await embedded.wait()

    async with _client(runtime, monkeypatch) as client:
        await client.put("/admin/model/embedding", json=_REMOTE)
        await runtime._task

    monkeypatch.setattr(runtime.embedder, "embed", real_embed)   # 重算时用新的
    release.set()
    await storing
    dims = {
        row["summary"]: len(json.loads(row["embedding"]))
        for row in store._db.execute("SELECT summary, embedding FROM memory_episodic")
    }
    assert dims == {"客户问过补件进度": api.dim, "客户问了补件进度": api.dim}
