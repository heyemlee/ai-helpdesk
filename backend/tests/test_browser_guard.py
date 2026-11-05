"""
员工实例的浏览器防护（app/core/browser_guard.py）。

它挡的是员工自己的浏览器被别的网站借去发请求：Host 不在白名单的（DNS rebinding）回 421，
标明跨站的回 403。不是浏览器的调用方（curl、Prometheus、容器健康检查）不带 Sec-Fetch-Site，
用的是本机名或服务名，必须照常放行：放错了，监控和控制台就断了。
"""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.core.browser_guard import BrowserGuard, allowed_hosts_from_env, install, normalize_host

ALLOWED = ["localhost", "127.0.0.1", "::1", "ai-helpdesk", "ai-helpdesk-worker"]


async def _ok(request):
    return PlainTextResponse("ok")


async def _stream(request):
    async def chunks():
        yield b"data: 1\n\n"
        yield b"data: 2\n\n"

    return StreamingResponse(chunks(), media_type="text/event-stream")


def _guarded() -> BrowserGuard:
    app = Starlette(routes=[
        Route("/health", _ok),
        Route("/chat", _ok, methods=["POST"]),
        Route("/chat/stream", _stream),
    ])
    return BrowserGuard(app, allowed_hosts=ALLOWED)


@pytest.fixture
def client() -> TestClient:
    return TestClient(_guarded(), base_url="http://127.0.0.1:8080")


class TestCallersThatAreNotWebPagesGetThrough:
    @pytest.mark.parametrize("host", [
        "localhost:8000",        # 容器健康检查
        "127.0.0.1:8080",        # 宿主机上的 curl、verify.sh
        "[::1]:8080",
        "ai-helpdesk:8000",      # Prometheus
        "AI-Helpdesk-Worker:8000",
        "localhost.",
    ])
    def test_local_and_in_stack_names(self, client, host):
        assert client.get("/health", headers={"host": host}).status_code == 200

    @pytest.mark.parametrize("site", ["same-origin", "none"])
    def test_the_console_itself_and_a_typed_url(self, client, site):
        """控制台的页面经同源的 /api/ 转发过来是 same-origin；地址栏直接打开是 none。"""
        assert client.post("/chat", headers={"sec-fetch-site": site}).status_code == 200

    def test_a_stream_passes_through_unbuffered(self, client):
        response = client.get("/chat/stream")
        assert response.status_code == 200
        assert response.text == "data: 1\n\ndata: 2\n\n"


class TestRequestsAnotherSiteSendsThroughTheBrowser:
    @pytest.mark.parametrize("host", [
        "rebind.example:8080",
        "host.docker.internal:8080",
        "127.0.0.1.nip.io:8080",
        "localhost.evil.example",
        "ai-helpdesk.evil.example:8000",
    ])
    def test_a_host_outside_the_list_is_misdirected(self, client, host):
        response = client.get("/health", headers={"host": host})
        assert response.status_code == 421
        assert response.json() == {"detail": "Host 不在白名单里"}

    def test_a_request_without_a_host_is_misdirected(self):
        sent: list[dict] = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "GET", "path": "/health", "headers": []}
        asyncio.run(_guarded()(scope, receive, send))
        assert sent[0]["status"] == 421

    @pytest.mark.parametrize("site", ["cross-site", "same-site", "Cross-Site"])
    def test_a_cross_site_request_is_refused_before_it_runs(self, client, site):
        response = client.post("/chat", headers={"sec-fetch-site": site})
        assert response.status_code == 403
        assert response.json() == {"detail": "不接受别的网站发来的请求"}

    def test_it_refuses_even_where_cors_would_have_let_the_page_read_the_answer(self):
        """CORS 留着 * 也一样：防护在外层，跨站的预检和请求都到不了 CORS。"""
        app = FastAPI()
        app.add_api_route("/tickets", _ok)
        app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
        install(app, {"AI_HELPDESK_ALLOWED_HOSTS": "127.0.0.1"})
        client = TestClient(app, base_url="http://127.0.0.1:8080")
        cross_site = {"origin": "https://evil.example", "sec-fetch-site": "cross-site"}

        response = client.get("/tickets", headers=cross_site)
        assert response.status_code == 403
        assert "access-control-allow-origin" not in response.headers
        preflight = client.options(
            "/tickets", headers={**cross_site, "access-control-request-method": "POST"}
        )
        assert preflight.status_code == 403


class TestSetting:
    def test_without_it_nothing_is_installed(self):
        app = FastAPI()
        assert install(app, {}) == frozenset()
        assert install(app, {"AI_HELPDESK_ALLOWED_HOSTS": " , "}) == frozenset()
        assert app.user_middleware == []

    def test_it_goes_outside_the_middleware_registered_before_it(self):
        app = FastAPI()
        app.add_middleware(CORSMiddleware, allow_origins=["*"])
        assert install(app, {"AI_HELPDESK_ALLOWED_HOSTS": "localhost"}) == {"localhost"}
        assert app.user_middleware[0].cls is BrowserGuard

    def test_entries_are_normalized_like_the_host_header(self):
        env = {"AI_HELPDESK_ALLOWED_HOSTS": "LocalHost, [::1]:8080 ,ai-helpdesk,"}
        assert allowed_hosts_from_env(env) == {"localhost", "::1", "ai-helpdesk"}

    @pytest.mark.parametrize(("raw", "host"), [
        ("Example.COM:8080", "example.com"),
        ("[::1]:8080", "::1"),
        ("[::1]", "::1"),
        ("localhost.", "localhost"),
        ("", ""),
    ])
    def test_normalize_host(self, raw, host):
        assert normalize_host(raw) == host

    def test_an_empty_allowlist_is_a_mistake_not_a_wide_open_door(self):
        with pytest.raises(ValueError):
            BrowserGuard(_ok, allowed_hosts=[" ", ""])
