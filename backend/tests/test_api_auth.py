"""写操作端点的 API Key 鉴权。不启动 lifespan，只验证依赖是否生效。"""
import importlib
import sys

import pytest
from fastapi.testclient import TestClient


def _load_app(monkeypatch, api_key: str = "", protect_chat: str = "false"):
    """按指定环境变量重新导入 api.main —— 鉴权配置是模块级读取的。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("AI_HELPDESK_API_KEY", api_key)
    monkeypatch.setenv("AI_HELPDESK_PROTECT_CHAT", protect_chat)
    # 避免挂载前端产物干扰路由断言
    monkeypatch.setenv("AI_HELPDESK_FRONTEND_DIST", "/nonexistent")
    sys.modules.pop("app.api.main", None)
    return importlib.import_module("app.api.main")


# 这些端点会改变服务行为或产生真实成本，必须受保护
PROTECTED = [
    ("post", "/skills/reload", None),
    ("post", "/knowledge/add", {"documents": []}),
    ("post", "/eval/run", {}),
    ("post", "/eval/promote", None),
]


class TestApiKeyAuth:
    @pytest.mark.parametrize("method,path,body", PROTECTED)
    def test_rejects_without_key(self, monkeypatch, method, path, body):
        m = _load_app(monkeypatch, api_key="s3cret")
        # 刻意不进入 TestClient 的上下文管理器：一旦触发 lifespan 就会去连
        # Redis/Postgres。这里只验证鉴权依赖在业务代码之前就把请求拦下了。
        client = TestClient(m.app)
        resp = getattr(client, method)(path, json=body)
        assert resp.status_code == 401, f"{path} 未鉴权就放行了"

    @pytest.mark.parametrize("method,path,body", PROTECTED)
    def test_rejects_wrong_key(self, monkeypatch, method, path, body):
        m = _load_app(monkeypatch, api_key="s3cret")
        client = TestClient(m.app)
        resp = getattr(client, method)(path, json=body, headers={"X-API-Key": "wrong"})
        assert resp.status_code == 401

    def test_passes_auth_with_correct_key(self, monkeypatch):
        """正确 key 应该越过鉴权，落到业务层（服务未初始化 → 503）。"""
        m = _load_app(monkeypatch, api_key="s3cret")
        client = TestClient(m.app)
        resp = client.post("/skills/reload", headers={"X-API-Key": "s3cret"})
        assert resp.status_code != 401
        assert resp.status_code == 503

    def test_auth_disabled_when_key_unset(self, monkeypatch):
        m = _load_app(monkeypatch, api_key="")
        client = TestClient(m.app)
        resp = client.post("/skills/reload")
        assert resp.status_code != 401

    def test_chat_open_by_default(self, monkeypatch):
        m = _load_app(monkeypatch, api_key="s3cret", protect_chat="false")
        client = TestClient(m.app)
        resp = client.post("/chat", json={"message": "你好"})
        assert resp.status_code != 401

    def test_chat_protected_when_enabled(self, monkeypatch):
        m = _load_app(monkeypatch, api_key="s3cret", protect_chat="true")
        client = TestClient(m.app)
        resp = client.post("/chat", json={"message": "你好"})
        assert resp.status_code == 401
        resp = client.post("/chat", json={"message": "你好"}, headers={"X-API-Key": "s3cret"})
        assert resp.status_code != 401

    def test_read_only_endpoints_stay_open(self, monkeypatch):
        """/health 和 /metrics 不该被鉴权挡住——健康检查和抓取需要匿名访问。"""
        m = _load_app(monkeypatch, api_key="s3cret")
        client = TestClient(m.app)
        assert client.get("/health").status_code != 401
        assert client.get("/metrics").status_code == 200
