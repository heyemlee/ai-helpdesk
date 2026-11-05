"""
只给本机用的实例（员工实例）：挡掉别的网站借员工的浏览器发来的请求。

员工实例没有 API key，靠端口只绑 127.0.0.1 挡住别的机器。可员工自己的浏览器就在这台机器上，
它打开的任何网站都能往 127.0.0.1:8080、控制台的 /api/ 发请求：

- **跨站请求**：浏览器给每个请求带 `Sec-Fetch-Site`。别的网站发来的是 cross-site，本机别的端口上的
  页面是 same-site，这两种一律拒绝。响应读不读得到归 CORS 管（compose 里员工实例的 CORS 是关的），
  这一条挡的是"读不到响应、但照样会执行"的请求。
- **DNS rebinding**：网站把自己的域名改解析到 127.0.0.1，浏览器就把请求发到这里，而且算同源，
  响应读得到。这时 Host 还是那个外部域名，所以按 Host 白名单挡。

curl、Prometheus、容器健康检查不是浏览器：不带 `Sec-Fetch-Site`，Host 是 localhost 或 compose 里的
服务名，照常放行。Host 谁都能伪造，所以这一层只防浏览器，代替不了鉴权。

`AI_HELPDESK_ALLOWED_HOSTS` 没设就不启用：本地开发、测试和客户实例都不受影响。
"""
from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

ALLOWED_HOSTS_ENV = "AI_HELPDESK_ALLOWED_HOSTS"
# same-site 也拒：本机别的端口上的页面（比如另一个开发服务）同样不该替员工发请求
_REFUSED_SITES = frozenset({"cross-site", "same-site"})


def normalize_host(raw: str) -> str:
    """Host 头 → 小写主机名，去掉端口和末尾的点；IPv6 去掉方括号（`[::1]:8080` → `::1`）。"""
    host = raw.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end != -1 else host
    if host.count(":") == 1:
        host = host.partition(":")[0]
    return host.rstrip(".")


def allowed_hosts_from_env(env: Mapping[str, str] | None = None) -> frozenset[str]:
    """逗号分隔的主机名。没设或是空的，返回空集合，表示不启用。"""
    raw = (os.environ if env is None else env).get(ALLOWED_HOSTS_ENV, "")
    return frozenset(host for host in map(normalize_host, raw.split(",")) if host)


def install(app: Starlette, env: Mapping[str, str] | None = None) -> frozenset[str]:
    """
    设了 `AI_HELPDESK_ALLOWED_HOSTS` 就装上，返回白名单（空集合 = 没装）。

    要在其它中间件都注册完之后再调：后注册的在外层，这样被拒的请求进不了 CORS 和请求日志。
    """
    hosts = allowed_hosts_from_env(env)
    if hosts:
        app.add_middleware(BrowserGuard, allowed_hosts=hosts)
    return hosts


class BrowserGuard:
    """纯 ASGI 中间件：不缓冲响应，/chat/stream 这样的流式响应原样透传。"""

    def __init__(self, app: ASGIApp, allowed_hosts: Iterable[str]) -> None:
        self.app = app
        self.allowed_hosts = frozenset(host for host in map(normalize_host, allowed_hosts) if host)
        if not self.allowed_hosts:
            raise ValueError("BrowserGuard 至少要放行一个主机名")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # lifespan 照常；后端没有 WebSocket
            await self.app(scope, receive, send)
            return
        host: str | None = None
        site: str | None = None
        for name, value in scope["headers"]:
            if name == b"host" and host is None:
                host = value.decode("latin-1")
            elif name == b"sec-fetch-site" and site is None:
                site = value.decode("latin-1").strip().lower()
        if normalize_host(host or "") not in self.allowed_hosts:
            # 421 Misdirected Request：这个服务不认这个主机名。控制台的 nginx 也是这么回的
            await _refuse(scope, receive, send, 421, "Host 不在白名单里", f"host={host!r}")
            return
        if site in _REFUSED_SITES:
            await _refuse(scope, receive, send, 403, "不接受别的网站发来的请求", f"sec-fetch-site={site!r}")
            return
        await self.app(scope, receive, send)


async def _refuse(scope: Scope, receive: Receive, send: Send, status: int, detail: str, seen: str) -> None:
    # 路径和请求头都是对方给的：用 repr 记、截短，换行之类塞不进日志
    logger.warning(
        "拒绝了一个浏览器请求（%d）：%s %r %s",
        status,
        scope.get("method", ""),
        scope.get("path", "")[:200],
        seen[:200],
    )
    await JSONResponse({"detail": detail}, status_code=status)(scope, receive, send)
