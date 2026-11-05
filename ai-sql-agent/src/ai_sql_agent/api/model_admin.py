"""
模型连接的管理接口：看现在用的是哪一份、保存前先试一下、换一份、退回服务端配置。

    GET    /admin/model                 现在用的模型连接（不含 Key），以及这个请求能不能改
    POST   /admin/model/models          这个连接的接口提供哪些模型（给页面的下拉框），不保存
    POST   /admin/model/test            拿提交的连接各发一条极短的请求，不保存
    PUT    /admin/model                 保存并立刻生效（不用重启）
    DELETE /admin/model                 丢掉页面保存的那一份，回到环境变量和 `.env`
    POST   /admin/model/embedding/models 这个 Embedding 接口提供哪些向量模型，不保存
    POST   /admin/model/embedding/test  拿提交的 Embedding 连接算一段文本的向量，不保存
    PUT    /admin/model/embedding       先用它把 Schema 检索索引整个建好，再保存、换上
    DELETE /admin/model/embedding       回到服务端配置的 Embedding（同样先建好索引再换）

Embedding 和主链路分开存、分开恢复。换 Embedding 不是改个配置就完：新旧模型的向量不在
一个空间里，检索索引要整个重算。所以保存前先测试，测试通过后用新模型把索引建出来，
建成了才写文件、换上；中途任何一步失败，正在用的索引和保存的文件都不动。

改模型连接等于决定「每一个问题、连同表结构和查出来的数据，发给谁」，所以门槛比别的
运维接口高一档：

- 开了鉴权的实例：管理员 Key，和其他 `/admin` 接口一样。
- 没开鉴权的实例（本地开发，匿名即管理员）：只接受**本机直接发来**的请求——
  连接来自回环地址、`Host` 是本机名字、没有经过代理的痕迹、浏览器请求必须同源。
  这样局域网里的别人、被打开的恶意网页（DNS 重绑定 / 跨站请求）都改不了它。
  判断不出来的一律按「不是本机」处理，页面退成只读并说明原因。

Key 只进不出：没有任何响应、日志、报错里带它，状态里只有「配没配」和末四位。
请求体不用 pydantic 模型接——校验失败时 FastAPI 会把提交的值原样放进 422 里。
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response

from ai_sql_agent.auth import Principal
from ai_sql_agent.core import (
    CIRCUIT_REGISTRY,
    MODEL_ACCESS,
    ModelAccessRegistry,
    ModelAuthError,
    ModelBadRequestError,
    ModelCallError,
    ModelConnectionError,
    ModelQuotaExhaustedError,
    ModelRateLimitError,
    ModelResponseError,
    ModelServerError,
    ModelTimeoutError,
    ModelTransport,
    RetryPolicy,
    SqlAgentSettings,
    build_chat_payload,
    get_http_client,
    get_logger,
    get_settings,
    log_event,
    reload_model_settings,
)
from ai_sql_agent.core.model_access import REASON_PERMISSION_DENIED, SCOPE_CREDENTIAL
from ai_sql_agent.core.transport import _classify_status, forget_parameter_adaptations
from ai_sql_agent.core.model_settings import (
    OFFICIAL_BASE_URL,
    PROVIDERS,
    ModelSettingsError,
    bump_connection_version,
    clean_api_key,
    clean_dimensions,
    clean_model_name,
    clear_embedding,
    clear_store,
    key_field,
    key_hint,
    origin_of,
    same_address,
    save_embedding,
    save_store,
    url_field,
    validate_base_url,
)
from ai_sql_agent.core.settings import (
    CHAT_PATH,
    DEFAULT_CHAT_MODEL,
    DEFAULT_CODER_MODEL,
    build_endpoint,
)

from .security import require_admin

_logger = get_logger("api.model_admin")

router = APIRouter(prefix="/admin/model")

MAX_BODY_BYTES = 8 * 1024
REQUEST_FIELDS = frozenset({"provider", "base_url", "api_key", "chat_model", "coder_model"})

PROBE_COMPONENT = "connection_test"
PROBE_TIMEOUT_SECONDS = 20.0
PROBE_MAX_TOKENS = 32
PROBE_PROMPT = "Reply with the single word: ok"

PROBE_OUTCOMES = (
    "ok",
    "auth",
    "quota",
    "model_denied",
    "forbidden",
    "rate_limited",
    "timeout",
    "unreachable",
    "not_found",
    "rejected",
    "server_error",
    "unexpected_response",
    "dimension_mismatch",
    "unsupported",
    "failed",
)
"""连接测试里一个模型的结果只会是这几种。页面按它们配文案（`settings.model.outcome.*`）。
`dimension_mismatch` 只出现在 Embedding 的测试里：服务返回的维度和填写的不一样。
`unsupported` 只出现在读模型列表时：这个接口没有列模型的功能（404 / 405）。"""

LIST_TIMEOUT_SECONDS = 15.0
"""读模型列表只是问一句「有哪些模型」，不花 token，比连接测试等得更短。"""
MAX_LISTED_MODELS = 500

# 按名字猜哪些是向量模型、哪些不能拿来对话。只用来给下拉框筛选：猜错了无非列表里
# 多一项或少一项，页面上永远可以手动填模型名，连接测试才是准的。
_EMBEDDING_NAME = re.compile(r"embed|bge|gte-|m3e|(^|[/-])e5-", re.IGNORECASE)
_NOT_CHAT_NAME = re.compile(
    r"embed|bge|gte-|m3e|(^|[/-])e5-|rerank|whisper|tts|speech|transcri|audio|dall-e|image|"
    r"moderation|realtime|sora|stable-diffusion|flux|kolors|video",
    re.IGNORECASE,
)

EMBEDDING_REQUEST_FIELDS = frozenset({"base_url", "api_key", "model", "dimensions"})
EMBEDDING_PROBE_TEXT = "供应商准时交付率"

DEFAULT_EMBEDDING = {
    "base_url": OFFICIAL_BASE_URL["openai"],
    "model": "text-embedding-3-small",
    "dimensions": 1024,
}
"""
页面上 Embedding 表单的默认值。维度默认 1024：OpenAI 的 text-embedding-3 和百炼的
text-embedding-v4 都能按请求的维度输出，1024 也是本地哈希向量和 pgvector 表一直用的维度，
不用为了换模型去改表。"""

# 只有这几种说明「问题在这个模型名上」：第一个模型是因为别的原因没通的话，第二个不用再试。
_MODEL_SPECIFIC = frozenset({"not_found", "model_denied", "forbidden", "rejected"})

_FORWARDING_HEADERS = (
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-real-ip",
    "via",
)

# 保存、恢复都要「写文件 + 换配置 + 重建几个对象」，两个请求交错着做会留下对不上的状态。
_apply_lock = threading.Lock()


# ----------------------------------------------------------------------
# 谁能改
# ----------------------------------------------------------------------


def _is_loopback_address(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return bool((mapped or address).is_loopback)


def _is_loopback_name(host: str) -> bool:
    host = host.lower().rstrip(".")
    return host == "localhost" or host.endswith(".localhost") or _is_loopback_address(host)


def _host_and_port(netloc: str, scheme: str) -> Optional[Tuple[str, int]]:
    try:
        parts = urlsplit(f"//{netloc}")
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname:
        return None
    return parts.hostname.lower(), port or (443 if scheme == "https" else 80)


def local_request_problem(request: Request) -> str:
    """
    这个请求是不是本机直接发来的。是 → 空串；不是 → 哪一条没过（记日志用，不给调用方）。

    四条都要过：

    1. 连接的对端是回环地址；
    2. `Host` 是本机的名字——恶意网页用 DNS 重绑定把自己的域名指到 127.0.0.1 时，
       连接确实来自本机的浏览器，但 `Host` 还是它的域名；
    3. 没有代理转发留下的头——反向代理和隧道跟服务在同一台机器上时，对端也是回环地址；
    4. 浏览器发来的（带 `Origin`）必须同源。

    同机反向代理如果既把 `Host` 改写成回环地址、又不加任何转发头（nginx 的默认配置就是这样），
    这里分辨不出来：把没开鉴权的实例暴露出去之前，应当先开鉴权，或者关掉这个功能。
    """
    client = request.client.host if request.client else ""
    if not _is_loopback_address(client):
        return "remote_client"

    scheme = request.url.scheme
    host = _host_and_port(request.headers.get("host", ""), scheme)
    if host is None or not _is_loopback_name(host[0]):
        return "foreign_host"

    if any(name in request.headers for name in _FORWARDING_HEADERS):
        return "forwarded"

    origin = request.headers.get("origin")
    if origin is not None:
        parts = urlsplit(origin)
        source = _host_and_port(parts.netloc, parts.scheme)
        if parts.scheme != scheme or source is None or source != host:
            return "cross_origin"
    if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        return "cross_origin"
    return ""


@dataclass(frozen=True)
class ModelAdminAccess:
    principal: Principal
    reason: str = ""
    """空串 = 可以改；`disabled`（这台服务没开这个功能）/ `local_only`（要在本机上改）= 只能看。"""
    detail: str = ""

    @property
    def may_edit(self) -> bool:
        return not self.reason


def model_admin_access(request: Request) -> ModelAdminAccess:
    """FastAPI 依赖：先过管理员这一关（401 / 403 在这里抛），再判断这个请求能不能改。"""
    principal = require_admin(request)
    if not get_settings().model_settings_file:
        return ModelAdminAccess(principal, "disabled")
    if not request.app.state.auth.enabled:
        problem = local_request_problem(request)
        if problem:
            return ModelAdminAccess(principal, "local_only", problem)
    return ModelAdminAccess(principal)


DENIED = {
    "disabled": "这台服务没有开启从页面配置模型连接（AI_SQL_MODEL_SETTINGS_ENABLED）。",
    "local_only": (
        "这台服务没有开鉴权，模型连接只能在服务所在的那台机器上、通过 localhost 修改。"
        "要远程修改，请开启鉴权并使用管理员 Key。"
    ),
}


def _require_edit(request: Request, access: ModelAdminAccess) -> None:
    if access.may_edit:
        return
    log_event(
        _logger,
        logging.WARNING,
        "model_settings.denied",
        reason=access.reason,
        check=access.detail,
        caller=access.principal.caller,
        client=request.client.host if request.client else "",
        method=request.method,
    )
    raise HTTPException(
        status_code=403, detail={"code": access.reason, "message": DENIED[access.reason]}
    )


# ----------------------------------------------------------------------
# 请求体 → 一份待用的连接
# ----------------------------------------------------------------------


def _invalid(error: ModelSettingsError) -> HTTPException:
    return HTTPException(status_code=400, detail={"code": error.code, "message": error.message})


async def _json_object(request: Request) -> Dict[str, Any]:
    """读请求体：只收不大的 JSON 对象。报错里不带提交的内容。"""
    media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(
            status_code=415,
            detail={"code": "unsupported_media_type", "message": "请求体要用 application/json。"},
        )
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BODY_BYTES:
            raise HTTPException(
                status_code=413, detail={"code": "too_large", "message": "请求体太大。"}
            )
    try:
        document = json.loads(bytes(raw))
    except ValueError:
        document = None
    if not isinstance(document, dict):
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_json", "message": "请求体要是一个 JSON 对象。"},
        )
    return document


def _text(body: Dict[str, Any], name: str) -> str:
    value = body.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ModelSettingsError("invalid_request", f"{name} 要是字符串。")
    return value


def paired_address(settings: SqlAgentSettings, provider: str) -> str:
    """
    现有的那把 Key 是配给哪个地址的。

    正在用的这家协议，看的是请求实际发往的端点——服务端可以用 `AI_SQL_CHAT_URL`
    把它指到和 BASE_URL 不同的主机上，Key 一直是发给那台主机的。
    """
    if provider == settings.model_provider:
        return settings.chat_url
    return settings.base_url_of(provider)


@dataclass
class Candidate:
    """一份校验过、Key 也已经确定下来的模型连接：可以拿去试，也可以保存。"""

    provider: str
    base_url: str
    api_key: str
    chat_model: str
    coder_model: str
    key_changed: bool = False
    warnings: List[str] = field(default_factory=list)

    @property
    def chat_url(self) -> str:
        return build_endpoint(self.base_url, CHAT_PATH[self.provider])

    def values(self) -> Dict[str, str]:
        return {
            "model_provider": self.provider,
            key_field(self.provider): self.api_key,
            url_field(self.provider): self.base_url,
            "chat_model": self.chat_model,
            "coder_model": self.coder_model,
        }


def resolve_candidate(body: Dict[str, Any], settings: SqlAgentSettings) -> Candidate:
    """
    请求体 → `Candidate`。不成立时抛 `ModelSettingsError`。

    Key 留空表示「接着用现有的」，但只有接口地址没换主机时才行：
    现有的 Key 不会被发到一个新的地址去，换地址必须同时把 Key 填上。
    """
    if not set(body) <= REQUEST_FIELDS:
        raise ModelSettingsError("invalid_request", "请求里有不认识的字段。")

    provider = _text(body, "provider").strip().lower()
    if provider not in PROVIDERS:
        raise ModelSettingsError("invalid_provider", "协议只能是 anthropic 或 openai。")

    base_url, warnings = validate_base_url(
        _text(body, "base_url").strip() or OFFICIAL_BASE_URL[provider]
    )
    typed_key = clean_api_key(_text(body, "api_key"))
    chat_model = (
        clean_model_name(_text(body, "chat_model"), "规划模型") or DEFAULT_CHAT_MODEL[provider]
    )
    coder_model = (
        clean_model_name(_text(body, "coder_model"), "SQL 模型") or DEFAULT_CODER_MODEL[provider]
    )

    existing = settings.api_key_of(provider)
    api_key = typed_key
    if not api_key:
        if not existing:
            raise ModelSettingsError("key_required", "请填写 API Key。")
        if not same_address(base_url, paired_address(settings, provider)):
            raise ModelSettingsError(
                "key_required",
                "接口地址换了主机，需要同时填写 API Key——现有的 Key 不会被发到新的地址。",
            )
        api_key = existing

    return Candidate(
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        chat_model=chat_model,
        coder_model=coder_model,
        key_changed=bool(typed_key) and typed_key != existing,
        warnings=warnings,
    )


@dataclass
class EmbeddingCandidate:
    """一份校验过、Key 也已经确定下来的 Embedding 连接。"""

    base_url: str
    api_key: str
    model: str
    dimensions: int
    key_changed: bool = False
    warnings: List[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return build_endpoint(self.base_url, "embeddings")

    def values(self) -> Dict[str, Any]:
        return {
            "base_url": self.base_url,
            "api_key": self.api_key,
            "model": self.model,
            "dimensions": self.dimensions,
        }


def reusable_embedding_keys(settings: SqlAgentSettings) -> List[Tuple[str, str]]:
    """
    Key 留空时可以接着用的那几把，以及各自配给了哪台主机：`[(主机, Key)]`。

    - 现在 Embedding 用的那把，配给的是 Embedding 端点那台主机；
    - 主链路（对话模型）用的那把，配给的是对话端点那台主机。

    两边都是 OpenAI 的话就不用把同一把 Key 填两遍；换了主机则必须重填。
    """
    pairs: List[Tuple[str, str]] = []
    embedding_key = settings.resolved_embedding_api_key()
    if embedding_key:
        pairs.append((origin_of(settings.embedding_url), embedding_key))
    chat_key = settings.resolved_chat_api_key()
    if chat_key:
        pairs.append((origin_of(settings.chat_url), chat_key))
    return [(address, key) for address, key in pairs if address]


def resolve_embedding_candidate(body: Dict[str, Any], settings: SqlAgentSettings) -> EmbeddingCandidate:
    """请求体 → `EmbeddingCandidate`。不成立时抛 `ModelSettingsError`。规则和主链路一样。"""
    if not set(body) <= EMBEDDING_REQUEST_FIELDS:
        raise ModelSettingsError("invalid_request", "请求里有不认识的字段。")

    base_url, warnings = validate_base_url(
        _text(body, "base_url").strip() or DEFAULT_EMBEDDING["base_url"]
    )
    typed_key = clean_api_key(_text(body, "api_key"))
    model = clean_model_name(_text(body, "model"), "Embedding 模型") or DEFAULT_EMBEDDING["model"]
    raw_dimensions = body.get("dimensions")
    dimensions = (
        DEFAULT_EMBEDDING["dimensions"]
        if raw_dimensions in (None, "", 0)
        else clean_dimensions(raw_dimensions)
    )

    target = origin_of(base_url)
    reusable = next((key for address, key in reusable_embedding_keys(settings) if address == target), "")
    api_key = typed_key or reusable
    if not api_key:
        raise ModelSettingsError(
            "key_required",
            "请填写 API Key——这个地址上没有可以接着用的 Key，现有的 Key 不会被发到新的地址。",
        )
    return EmbeddingCandidate(
        base_url=base_url,
        api_key=api_key,
        model=model,
        dimensions=dimensions,
        key_changed=bool(typed_key) and typed_key != settings.resolved_embedding_api_key(),
        warnings=warnings,
    )


# ----------------------------------------------------------------------
# 现在用的是哪一份
# ----------------------------------------------------------------------


def _embedding_base(settings: SqlAgentSettings) -> str:
    """Embedding 端点去掉末尾的 `/embeddings`，就是表单里填的那个接口地址。"""
    url = settings.embedding_url.rstrip("/")
    return url[: -len("/embeddings")] if url.endswith("/embeddings") else url


def describe_embedding(
    settings: SqlAgentSettings, access: ModelAdminAccess, pipeline: Any = None
) -> Dict[str, Any]:
    """
    Embedding 的现状。不含 Key。

    `backend` 是配置要的，`active` 是 Schema 检索索引实际在用的——配的是远端、启动时它
    调不通，检索就会退回本地哈希向量，`fallback` 里是原因。
    """
    from ai_sql_agent.pipeline.embedding import LOCAL_DIMENSIONS

    remote = settings.embedding_backend == "remote"
    page = settings.page_store.get("embedding") or {}
    active = dict(getattr(pipeline, "embedding_status", None) or {})
    status: Dict[str, Any] = {
        "source": "page" if page else "server",
        "backend": settings.embedding_backend,
        "model": settings.embedding_model if remote else "local_hash",
        "dimensions": settings.embedding_dimensions if remote else LOCAL_DIMENSIONS,
        "key_configured": remote and bool(settings.resolved_embedding_api_key()),
        "active": {
            name: active[name] for name in ("backend", "model", "dimensions") if name in active
        },
        "fallback": active.get("fallback", ""),
    }
    if not access.may_edit:
        return status

    server = settings.server_side
    server_remote = server.embedding_backend == "remote"
    status.update(
        base_url=_embedding_base(settings) if remote else "",
        endpoint=settings.embedding_url if remote else "",
        key_hint=key_hint(settings.resolved_embedding_api_key()) if remote else "",
        # 这几台主机上有现成的 Key：表单里填的地址落在其中之一，Key 就可以留空
        reusable_key_addresses=sorted({address for address, _ in reusable_embedding_keys(settings)}),
        saved_at=page.get("saved_at", ""),
        saved_by=page.get("saved_by", ""),
        store_problem=settings.page_store.get("embedding_problem", ""),
        server={
            "backend": server.embedding_backend,
            "model": server.embedding_model if server_remote else "local_hash",
            "dimensions": server.embedding_dimensions if server_remote else LOCAL_DIMENSIONS,
        },
        defaults=dict(DEFAULT_EMBEDDING),
    )
    return status


def describe(
    settings: SqlAgentSettings, access: ModelAdminAccess, pipeline: Any = None
) -> Dict[str, Any]:
    """
    模型连接的现状。不含 Key。

    接口地址、Key 的末四位、是谁保存的，只给能改它的请求——没开鉴权的实例上，
    局域网里的人只看得到「用的是哪家、哪个模型、配没配 Key」。
    """
    provider = settings.model_provider
    page = settings.page_store
    status: Dict[str, Any] = {
        "editable": access.may_edit,
        "reason": access.reason,
        "source": "page" if page.get("values") else "server",
        "provider": provider,
        "chat_model": settings.chat_model,
        "coder_model": settings.coder_model,
        "key_configured": settings.has_model_credentials,
        "embedding": describe_embedding(settings, access, pipeline),
    }
    if not access.may_edit:
        return status

    server = settings.server_side
    standard_endpoint = build_endpoint(settings.base_url_of(provider), CHAT_PATH[provider])
    status.update(
        base_url=settings.base_url_of(provider),
        endpoint=settings.chat_url,
        # 服务端用 AI_SQL_CHAT_URL 指了一个非标准路径：页面只能按「接口地址 + 标准路径」保存
        custom_endpoint=settings.chat_url != standard_endpoint,
        key_hint=key_hint(settings.resolved_chat_api_key()),
        saved_at=page.get("saved_at", ""),
        saved_by=page.get("saved_by", ""),
        store_problem=page.get("problem", ""),
        server={
            "provider": server.model_provider,
            "chat_model": server.chat_model,
            "coder_model": server.coder_model,
            "key_configured": server.has_model_credentials,
        },
        # 页面切换协议时拿它预填：这家协议现在配的地址、有没有可以接着用的 Key
        providers={
            name: {
                "base_url": settings.base_url_of(name) or OFFICIAL_BASE_URL[name],
                "official_base_url": OFFICIAL_BASE_URL[name],
                "chat_model": settings.chat_model if name == provider else DEFAULT_CHAT_MODEL[name],
                "coder_model": (
                    settings.coder_model if name == provider else DEFAULT_CODER_MODEL[name]
                ),
                "key_configured": bool(settings.api_key_of(name)),
                "key_address": origin_of(paired_address(settings, name)),
            }
            for name in PROVIDERS
        },
        # 这把 Key 已经被模型服务拒过的话，在这里就能看到，不用等下一个问题失败
        access_faults=MODEL_ACCESS.snapshot(),
    )
    return status


# ----------------------------------------------------------------------
# 试一下
# ----------------------------------------------------------------------


def _outcome_of(error: ModelCallError) -> str:
    if isinstance(error, ModelQuotaExhaustedError):
        return "quota"
    if isinstance(error, ModelAuthError):
        if error.block_scope == SCOPE_CREDENTIAL:
            return "auth"
        return "model_denied" if error.block_reason == REASON_PERMISSION_DENIED else "forbidden"
    if isinstance(error, ModelRateLimitError):
        return "rate_limited"
    if isinstance(error, ModelTimeoutError):
        return "timeout"
    if isinstance(error, ModelConnectionError):
        return "unreachable"
    if isinstance(error, ModelBadRequestError):
        return "not_found" if error.status_code == 404 else "rejected"
    if isinstance(error, ModelServerError):
        return "server_error"
    if isinstance(error, ModelResponseError):
        return "unexpected_response"
    return "failed"


def probe(candidate: Candidate, *, client: Optional[httpx.Client] = None) -> Dict[str, Any]:
    """
    拿这份连接真的调一次模型：两个模型名各发一条极短的请求（同名只发一条）。

    走的是和正式调用同一个 `ModelTransport`——同样的请求头、请求体、端点拼法，
    所以「这里通了」说明的就是「正式调用会通」。只有三处不同：不重试；
    用一张自己的访问状态表（试一把坏 Key 不该把正在用的连接记成「已被拒绝」，
    也不该被那张表里的旧记录挡住）；不看熔断器的旧状态。

    结果里只有分类、状态码和服务端错误里那句给人看的话，不带整个响应体。
    """
    transport = ModelTransport(
        PROBE_COMPONENT,
        api_key=candidate.api_key,
        provider=candidate.provider,
        timeout=PROBE_TIMEOUT_SECONDS,
        retry_policy=RetryPolicy(max_retries=0),
        access_registry=ModelAccessRegistry(),
        client=client,
    )
    targets: List[Tuple[List[str], str]] = [(["chat"], candidate.chat_model)]
    if candidate.coder_model == candidate.chat_model:
        targets[0][0].append("coder")
    else:
        targets.append((["coder"], candidate.coder_model))

    results: List[Dict[str, Any]] = []
    for roles, model in targets:
        transport.circuit.reset()
        payload = build_chat_payload(
            [{"role": "user", "content": PROBE_PROMPT}],
            model=model,
            temperature=0.0,
            stream=False,
            provider=candidate.provider,
            max_tokens=PROBE_MAX_TOKENS,
            # 和正式调用带一样的参数：模型不认的话在这里就按正式调用的办法改掉再试
            reasoning_effort=get_settings().reasoning_effort,
        )
        entry: Dict[str, Any] = {"roles": roles, "model": model, "status_code": None, "message": ""}
        started = time.perf_counter()
        try:
            reply = transport.post_chat(candidate.chat_url, payload, model=model)
        except ModelCallError as exc:
            entry.update(
                ok=False,
                outcome=_outcome_of(exc),
                status_code=exc.status_code,
                message=exc.upstream_message,
            )
        else:
            # 回了 200 的 JSON，但长得不像对话接口的响应：多半是地址填到了别的服务上
            looks_like_chat = isinstance(reply, dict) and (
                isinstance(reply.get("choices"), list) or isinstance(reply.get("content"), list)
            )
            entry.update(
                ok=looks_like_chat,
                outcome="ok" if looks_like_chat else "unexpected_response",
                status_code=200,
            )
        entry["duration_ms"] = round((time.perf_counter() - started) * 1000)
        results.append(entry)
        if not entry["ok"] and entry["outcome"] not in _MODEL_SPECIFIC:
            break

    transport.circuit.reset()
    return {
        "ok": len(results) == len(targets) and all(item["ok"] for item in results),
        "provider": candidate.provider,
        "endpoint": candidate.chat_url,
        "results": results,
        "warnings": candidate.warnings,
    }


def _first_vector(reply: Any) -> Optional[List[float]]:
    """`/embeddings` 响应里的第一个向量；长得不像就返回 None。"""
    try:
        vector = reply["data"][0]["embedding"]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(vector, list) or not vector:
        return None
    return vector


def probe_embedding(
    candidate: EmbeddingCandidate, *, client: Optional[httpx.Client] = None
) -> Dict[str, Any]:
    """
    拿这份 Embedding 连接真的算一段文本的向量。和主链路的连接测试一样：走正式调用的那个
    `ModelTransport`、不重试、用自己的一张访问状态表，结果里不带整个响应体。

    向量的维度必须和填写的一样：有的服务不认 `dimensions` 参数，照自己的默认维度返回——
    那样建出来的索引和配置对不上，pgvector 表也建不起来。
    """
    transport = ModelTransport(
        PROBE_COMPONENT,
        api_key=candidate.api_key,
        provider="openai",
        timeout=PROBE_TIMEOUT_SECONDS,
        retry_policy=RetryPolicy(max_retries=0),
        access_registry=ModelAccessRegistry(),
        client=client,
    )
    transport.circuit.reset()
    entry: Dict[str, Any] = {
        "roles": ["embedding"],
        "model": candidate.model,
        "status_code": None,
        "message": "",
        "dimensions": 0,
    }
    started = time.perf_counter()
    try:
        reply = transport.post_json(
            candidate.url,
            {
                "model": candidate.model,
                "input": [EMBEDDING_PROBE_TEXT],
                "dimensions": candidate.dimensions,
                "encoding_format": "float",
            },
            model=candidate.model,
        )
    except ModelCallError as exc:
        entry.update(
            ok=False,
            outcome=_outcome_of(exc),
            status_code=exc.status_code,
            message=exc.upstream_message,
        )
    else:
        vector = _first_vector(reply)
        if vector is None:
            entry.update(ok=False, outcome="unexpected_response", status_code=200)
        elif len(vector) != candidate.dimensions:
            entry.update(ok=False, outcome="dimension_mismatch", status_code=200, dimensions=len(vector))
        else:
            entry.update(ok=True, outcome="ok", status_code=200, dimensions=len(vector))
    entry["duration_ms"] = round((time.perf_counter() - started) * 1000)
    transport.circuit.reset()
    return {
        "ok": entry["ok"],
        "endpoint": candidate.url,
        "results": [entry],
        "warnings": candidate.warnings,
    }


# ----------------------------------------------------------------------
# 有哪些模型可选
# ----------------------------------------------------------------------


def _model_names(document: Any) -> List[str]:
    """
    `GET /models` 的回答 → 模型名。OpenAI 和 Anthropic 都是 `{"data": [{"id": ...}]}`；
    有的中转站直接回数组、或者把名字放在 `name` / `model` 里。认不出、写法不合规的条目跳过。
    """
    items = document.get("data", document.get("models")) if isinstance(document, dict) else document
    if not isinstance(items, list):
        raise ValueError("回答里没有模型列表")
    names: List[str] = []
    for item in items:
        name = (item.get("id") or item.get("name") or item.get("model")) if isinstance(item, dict) else item
        if not isinstance(name, str):
            continue
        try:
            cleaned = clean_model_name(name, "模型")
        except ModelSettingsError:
            continue
        if cleaned:
            names.append(cleaned)
    return names


def _suits(kind: str, name: str) -> bool:
    if kind == "embedding":
        return bool(_EMBEDDING_NAME.search(name)) and "rerank" not in name.lower()
    return not _NOT_CHAT_NAME.search(name)


def list_models(
    provider: str, base_url: str, api_key: str, kind: str, *, client: Optional[httpx.Client] = None
) -> Dict[str, Any]:
    """
    问接口有哪些模型，给页面的下拉框用。只读：不调模型、不花 token、不保存。

    读不到（中转站没开这个接口、Key 不对……）不算接口错误：页面照样能手动填模型名，
    所以和连接测试一样只报结果分类、状态码和服务端那句话。按名字筛出适合这一处的
    （`chat` / `embedding`）；一个也筛不出来时给全部，让人自己挑。
    """
    if provider == "anthropic":
        headers = {"x-api-key": api_key, "anthropic-version": get_settings().anthropic_version}
        params: Optional[Dict[str, Any]] = {"limit": 1000}
    else:
        headers = {"Authorization": f"Bearer {api_key}"}
        params = None
    result: Dict[str, Any] = {
        "ok": False, "models": [], "total": 0, "outcome": "failed", "status_code": None, "message": "",
    }
    started = time.perf_counter()
    try:
        response = (client or get_http_client()).get(
            build_endpoint(base_url, "models"), headers=headers, params=params, timeout=LIST_TIMEOUT_SECONDS
        )
    except httpx.TimeoutException:
        result["outcome"] = "timeout"
    except httpx.HTTPError:
        result["outcome"] = "unreachable"
    else:
        result["status_code"] = response.status_code
        if response.status_code >= 400:
            error = _classify_status(response, component=PROBE_COMPONENT, model="", api_key=api_key)
            result["outcome"] = "unsupported" if response.status_code in (404, 405) else _outcome_of(error)
            result["message"] = error.upstream_message
        else:
            try:
                names = _model_names(response.json())
            except ValueError:
                # 200 但不是 JSON 或没有列表：多半是网页（地址不对）
                result["outcome"] = "unexpected_response"
            else:
                unique = sorted(set(names), key=str.lower)
                suited = [name for name in unique if _suits(kind, name)] or unique
                result.update(ok=True, outcome="ok", models=suited[:MAX_LISTED_MODELS], total=len(unique))
    result["duration_ms"] = round((time.perf_counter() - started) * 1000)
    return result


# ----------------------------------------------------------------------
# 让它生效
# ----------------------------------------------------------------------


def apply_model_connection(app: FastAPI) -> SqlAgentSettings:
    """
    让刚保存（或刚丢掉）的模型连接立刻生效。

    模型 Client 自己会跟上（`core.model_client`）。这里处理的是那些在启动时
    按「有没有凭证」做过一次选择的地方——启动时没有 Key、后来在页面上配了的话，
    它们会一直停在规则版：意图路由与数据问答、对话摘要、抽词。
    凭证换了，针对旧凭证记下的拒绝和熔断状态也一并清掉。
    """
    settings = reload_model_settings()
    bump_connection_version()
    CIRCUIT_REGISTRY.reset_all()
    MODEL_ACCESS.reset_all()
    forget_parameter_adaptations()

    pipeline = getattr(app.state, "pipeline", None)
    memory = getattr(app.state, "memory", None)
    reenable = getattr(getattr(pipeline, "keyword_extractor", None), "reset_model", None)
    if callable(reenable):
        reenable()
    refresh = getattr(memory, "refresh_summarizer", None)
    if callable(refresh):
        refresh()
    if pipeline is not None and memory is not None:
        from ai_sql_agent.pipeline import DynamicSqlAgentService

        # 请求是在处理时才读 app.state.service 的：正在跑的请求用完旧的那个，新的请求用新的
        app.state.service = DynamicSqlAgentService(pipeline, memory)
    return settings


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _pipeline(request: Request) -> Any:
    return getattr(request.app.state, "pipeline", None)


@router.get("")
def model_status(
    request: Request, response: Response, access: ModelAdminAccess = Depends(model_admin_access)
) -> Dict[str, Any]:
    """现在用的模型连接（主链路和 Embedding），以及这个请求能不能改它。"""
    _no_store(response)
    return describe(get_settings(), access, _pipeline(request))


@router.post("/models")
def list_chat_models(
    request: Request,
    response: Response,
    access: ModelAdminAccess = Depends(model_admin_access),
    body: Dict[str, Any] = Depends(_json_object),
) -> Dict[str, Any]:
    """
    这个连接的接口提供哪些模型（`GET /models`），给页面的下拉框用。不调模型、不保存。

    Key 留空的规则和连接测试一样：接口地址换了主机就必须填。
    """
    _no_store(response)
    _require_edit(request, access)
    try:
        candidate = resolve_candidate(body, get_settings())
    except ModelSettingsError as error:
        raise _invalid(error) from None

    result = list_models(candidate.provider, candidate.base_url, candidate.api_key, "chat")
    log_event(
        _logger,
        logging.INFO,
        "model_settings.listed",
        caller=access.principal.caller,
        provider=candidate.provider,
        endpoint=origin_of(candidate.base_url),
        outcome=result["outcome"],
        count=len(result["models"]),
    )
    return result


@router.post("/test")
def test_model(
    request: Request,
    response: Response,
    access: ModelAdminAccess = Depends(model_admin_access),
    body: Dict[str, Any] = Depends(_json_object),
) -> Dict[str, Any]:
    """
    拿提交的连接试一下，不保存。会真的调用模型（每个模型名一条极短的请求）。

    Key 留空 = 用现有的那把，规则和保存时一样：接口地址换了主机就必须填。
    """
    _no_store(response)
    _require_edit(request, access)
    try:
        candidate = resolve_candidate(body, get_settings())
    except ModelSettingsError as error:
        raise _invalid(error) from None

    report = probe(candidate)
    log_event(
        _logger,
        logging.INFO,
        "model_settings.tested",
        caller=access.principal.caller,
        provider=candidate.provider,
        endpoint=origin_of(candidate.chat_url),
        ok=report["ok"],
        outcomes=[item["outcome"] for item in report["results"]],
    )
    return report


@router.put("")
def save_model(
    request: Request,
    response: Response,
    access: ModelAdminAccess = Depends(model_admin_access),
    body: Dict[str, Any] = Depends(_json_object),
) -> Dict[str, Any]:
    """
    保存模型连接并立刻生效。之后它压过环境变量和 `.env`，直到有人调 DELETE。

    这里不验证连接能不能用——先调 `/admin/model/test`。保存一份不通的连接不会让服务挂掉：
    问答会照「模型不可用」降级，直到换成一份能用的。
    """
    _no_store(response)
    _require_edit(request, access)
    with _apply_lock:
        before = get_settings()
        try:
            candidate = resolve_candidate(body, before)
            save_store(
                before.model_settings_file, candidate.values(), saved_by=access.principal.caller
            )
        except ModelSettingsError as error:
            raise _invalid(error) from None
        except OSError as error:
            log_event(
                _logger, logging.ERROR, "model_settings.store_failed", error=type(error).__name__
            )
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "store_unwritable",
                    "message": "写不了保存模型连接的文件（AI_SQL_MODEL_SETTINGS_PATH），没有改动。",
                },
            ) from None
        settings = apply_model_connection(request.app)

    log_event(
        _logger,
        logging.WARNING,
        "model_settings.saved",
        caller=access.principal.caller,
        client=request.client.host if request.client else "",
        provider=candidate.provider,
        endpoint=origin_of(candidate.chat_url),
        chat_model=candidate.chat_model,
        coder_model=candidate.coder_model,
        key_changed=candidate.key_changed,
        previous_provider=before.model_provider,
        previous_endpoint=origin_of(before.chat_url),
    )
    return {
        "saved": True,
        "warnings": candidate.warnings,
        "status": describe(settings, access, _pipeline(request)),
    }


@router.delete("")
def clear_model(
    request: Request,
    response: Response,
    access: ModelAdminAccess = Depends(model_admin_access),
) -> Dict[str, Any]:
    """丢掉页面保存的模型连接，回到环境变量和 `.env` 给出的那一份，立刻生效。"""
    _no_store(response)
    _require_edit(request, access)
    with _apply_lock:
        before = get_settings()
        try:
            cleared = clear_store(before.model_settings_file)
        except OSError as error:
            log_event(
                _logger, logging.ERROR, "model_settings.store_failed", error=type(error).__name__
            )
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "store_unwritable",
                    "message": "删不掉保存模型连接的文件（AI_SQL_MODEL_SETTINGS_PATH），没有改动。",
                },
            ) from None
        settings = apply_model_connection(request.app)

    log_event(
        _logger,
        logging.WARNING,
        "model_settings.cleared",
        caller=access.principal.caller,
        client=request.client.host if request.client else "",
        had_saved_settings=cleared,
        provider=settings.model_provider,
        endpoint=origin_of(settings.chat_url),
    )
    return {"cleared": cleared, "status": describe(settings, access, _pipeline(request))}


# ----------------------------------------------------------------------
# Embedding
# ----------------------------------------------------------------------


def _require_pipeline(request: Request) -> Any:
    pipeline = _pipeline(request)
    if pipeline is None:
        raise HTTPException(
            status_code=503, detail={"code": "not_ready", "message": "服务还没有启动完。"}
        )
    return pipeline


@router.post("/embedding/models")
def list_embedding_models(
    request: Request,
    response: Response,
    access: ModelAdminAccess = Depends(model_admin_access),
    body: Dict[str, Any] = Depends(_json_object),
) -> Dict[str, Any]:
    """这个 Embedding 接口提供哪些向量模型。Key 留空的规则和 Embedding 的连接测试一样。"""
    _no_store(response)
    _require_edit(request, access)
    try:
        candidate = resolve_embedding_candidate(body, get_settings())
    except ModelSettingsError as error:
        raise _invalid(error) from None

    result = list_models("openai", candidate.base_url, candidate.api_key, "embedding")
    log_event(
        _logger,
        logging.INFO,
        "model_settings.embedding_listed",
        caller=access.principal.caller,
        endpoint=origin_of(candidate.base_url),
        outcome=result["outcome"],
        count=len(result["models"]),
    )
    return result


@router.post("/embedding/test")
def test_embedding(
    request: Request,
    response: Response,
    access: ModelAdminAccess = Depends(model_admin_access),
    body: Dict[str, Any] = Depends(_json_object),
) -> Dict[str, Any]:
    """拿提交的 Embedding 连接算一段文本的向量，不保存。Key 留空的规则和保存时一样。"""
    _no_store(response)
    _require_edit(request, access)
    try:
        candidate = resolve_embedding_candidate(body, get_settings())
    except ModelSettingsError as error:
        raise _invalid(error) from None

    report = probe_embedding(candidate)
    log_event(
        _logger,
        logging.INFO,
        "model_settings.embedding_tested",
        caller=access.principal.caller,
        endpoint=origin_of(candidate.url),
        model=candidate.model,
        dimensions=candidate.dimensions,
        ok=report["ok"],
        outcome=report["results"][0]["outcome"],
    )
    return report


@router.put("/embedding")
def save_embedding_model(
    request: Request,
    response: Response,
    access: ModelAdminAccess = Depends(model_admin_access),
    body: Dict[str, Any] = Depends(_json_object),
) -> Dict[str, Any]:
    """
    换 Embedding：测试 → 用新模型把 Schema 检索索引整个建好 → 保存 → 换上。

    要几秒到几十秒（Schema 里每张表、每个字段都要算一次向量）。测试没通过、索引建不出来、
    文件写不了，都在换上之前停下：正在用的索引和保存的文件都不动。
    """
    from ai_sql_agent.pipeline.embedding import remote_embedding_client

    _no_store(response)
    _require_edit(request, access)
    pipeline = _require_pipeline(request)
    with _apply_lock:
        before = get_settings()
        try:
            candidate = resolve_embedding_candidate(body, before)
        except ModelSettingsError as error:
            raise _invalid(error) from None

        report = probe_embedding(candidate)
        if not report["ok"]:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "test_failed",
                    "message": "连接测试没有通过，没有保存。",
                    "test": report,
                },
            )

        client = remote_embedding_client(
            url=candidate.url,
            api_key=candidate.api_key,
            model=candidate.model,
            dimensions=candidate.dimensions,
            settings=before,
        )
        started = time.perf_counter()
        try:
            service = pipeline.build_schema_retrieval_service(client)
        except ModelCallError as error:
            log_event(
                _logger,
                logging.ERROR,
                "model_settings.embedding_index_failed",
                endpoint=origin_of(candidate.url),
                model=candidate.model,
                error_category=error.category,
            )
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "index_failed",
                    "message": (
                        "用新模型重建检索索引时 Embedding 服务出错，没有保存，正在用的索引没有改动。"
                        + (f" 服务端说：{error.upstream_message}" if error.upstream_message else "")
                    ),
                },
            ) from None
        rebuild = {
            "texts": client.embedded_count,
            "duration_ms": round((time.perf_counter() - started) * 1000),
        }

        try:
            save_embedding(
                before.model_settings_file, candidate.values(), saved_by=access.principal.caller
            )
        except ModelSettingsError as error:
            raise _invalid(error) from None
        except OSError as error:
            log_event(
                _logger, logging.ERROR, "model_settings.store_failed", error=type(error).__name__
            )
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "store_unwritable",
                    "message": "写不了保存模型连接的文件（AI_SQL_MODEL_SETTINGS_PATH），没有改动。",
                },
            ) from None
        settings = reload_model_settings()
        pipeline.install_schema_retrieval_service(
            service, reason=f"embedding:{access.principal.caller}"
        )

    log_event(
        _logger,
        logging.WARNING,
        "model_settings.embedding_saved",
        caller=access.principal.caller,
        client=request.client.host if request.client else "",
        endpoint=origin_of(candidate.url),
        model=candidate.model,
        dimensions=candidate.dimensions,
        key_changed=candidate.key_changed,
        previous_backend=before.embedding_backend,
        previous_model=before.embedding_model,
        rebuilt_texts=rebuild["texts"],
        rebuild_ms=rebuild["duration_ms"],
    )
    return {
        "saved": True,
        "warnings": candidate.warnings,
        "test": report,
        "rebuild": rebuild,
        "status": describe(settings, access, pipeline),
    }


@router.delete("/embedding")
def clear_embedding_model(
    request: Request,
    response: Response,
    access: ModelAdminAccess = Depends(model_admin_access),
) -> Dict[str, Any]:
    """
    丢掉页面保存的 Embedding，回到服务端配置的那一种，并按它重建检索索引。

    服务端配的是远端、而它调不通时，和启动时一样退回本地哈希向量（状态里的 `fallback`）。
    """
    _no_store(response)
    _require_edit(request, access)
    pipeline = _require_pipeline(request)
    with _apply_lock:
        before = get_settings()
        try:
            cleared = clear_embedding(before.model_settings_file)
        except OSError as error:
            log_event(
                _logger, logging.ERROR, "model_settings.store_failed", error=type(error).__name__
            )
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "store_unwritable",
                    "message": "改不了保存模型连接的文件（AI_SQL_MODEL_SETTINGS_PATH），没有改动。",
                },
            ) from None
        settings = reload_model_settings()
        if cleared:
            pipeline.install_schema_retrieval_service(
                pipeline.build_schema_retrieval_service(None),
                reason=f"embedding_restored:{access.principal.caller}",
            )

    log_event(
        _logger,
        logging.WARNING,
        "model_settings.embedding_cleared",
        caller=access.principal.caller,
        client=request.client.host if request.client else "",
        had_saved_settings=cleared,
        backend=settings.embedding_backend,
        **({"fallback": pipeline.embedding_status.get("fallback", "")} if cleared else {}),
    )
    return {"cleared": cleared, "status": describe(settings, access, pipeline)}
