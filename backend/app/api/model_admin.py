"""
模型连接的管理接口：看现在用的是哪一份、保存前先试一下、换一份、恢复为服务端配置。

    GET    /admin/model                    两组连接的现状（不含 Key）、能不能改、向量切换的进度
    POST   /admin/model/chat/models        这个对话连接有哪些模型（给页面的下拉框），不保存
    POST   /admin/model/chat/test          用提交的对话连接发两条极短的请求（普通 + 结构化输出），不保存
    PUT    /admin/model/chat               保存并立刻生效（不用重启）
    DELETE /admin/model/chat               丢掉页面保存的那一组，回到 `.env`
    POST   /admin/model/embedding/models   这个向量连接有哪些向量模型，不保存
    POST   /admin/model/embedding/test     用提交的向量连接算一个向量，报回实际维度，不保存
    PUT    /admin/model/embedding          测试通过后开始切换：重算全部向量、整批换上、再生效
    DELETE /admin/model/embedding          切换回 `.env` 里的向量配置（同样要重算）

改模型连接等于决定「每一条客户消息、连同知识库内容，发给谁」，所以门槛比别的管理接口高一档。
和 ai-sql-agent 的 `/admin/model` 是同一个思路，按这里的部署方式调整过：

- 配了 `AI_HELPDESK_API_KEY` 的实例：要带员工钥匙，和其它写接口一样（require_api_key）。
- 员工实例没有钥匙，靠端口只绑本机挡外人。但同一台机器上的浏览器打开的任何网页都能往
  127.0.0.1 发请求，而后端的 CORS 默认全放开——不另外拦，一个恶意网页就能把模型连接
  改到它自己的地址，从此每一条客户消息都送给它。所以**要求请求是本机控制台发来的**：
  `Host` 是本机的名字（挡 DNS 重绑定：那时 `Host` 还是攻击者的域名），浏览器发来的
  `Origin` 也得是本机（挡跨站请求）。控制台的 nginx 把浏览器的 `Host` 原样转给后端
  （frontend/docker/templates/default.conf.template），所以经过它也认得出来。
  判断不出来的一律按不能改处理，页面退成只读并说明原因。
- 面向客户的实例设了 `AI_HELPDESK_MODEL_SETTINGS_EDITABLE=false`，只读；它的入口 nginx
  本来也只放行 /chat 和 /health。

Key 只进不出：没有任何响应、日志、报错里带它，状态里只有「配没配」和末四位。
请求体不用 pydantic 模型接——校验失败时 FastAPI 会把提交的值（包括 Key）原样放进 422 里。
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from anthropic import APIConnectionError as AnthropicConnectionError
from anthropic import APITimeoutError as AnthropicTimeoutError
from anthropic import AsyncAnthropic
from fastapi import APIRouter, HTTPException, Request, Response
from openai import APIConnectionError as OpenAIConnectionError
from openai import APITimeoutError as OpenAITimeoutError
from pydantic import ValidationError

from app.core import llm_guard
from app.core import model_settings as ms
from app.core.embedder import Embedder
from app.core.llm_utils import (
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_OPENAI_MODEL,
    STRUCTURED_MAX_TOKENS,
    extract_structured_text,
    structured_parse,
)
from app.core.model_runtime import ModelRuntime, chat_config_from, embedder_for
from app.core.openai_compat import AsyncOpenAICompat, GatewayResponseError, schema_enforced_for

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/model", tags=["模型连接"])

# 测试连接时一条请求最多等多久。比正式调用短：测试是人在页面前等着看结果。
PROBE_TIMEOUT_S = 25.0
# 列模型只是问一句「有哪些模型」，不花 token，等得更短。
LIST_TIMEOUT_S = 15.0
MAX_LISTED_MODELS = 500

# 按名字猜哪些是向量模型、哪些不能拿来对话。只用来给下拉框筛选：猜错了无非列表里
# 多一项或少一项，页面上永远可以手动填模型名，保存前的测试连接才是准的。
_EMBEDDING_NAME = re.compile(r"embed|bge|gte-|m3e|(^|[/-])e5-", re.IGNORECASE)
_NOT_CHAT_NAME = re.compile(
    r"embed|bge|gte-|m3e|(^|[/-])e5-|rerank|whisper|tts|speech|transcri|audio|dall-e|image|"
    r"moderation|realtime|sora|stable-diffusion|flux|kolors|video",
    re.IGNORECASE,
)

DEFAULT_EMBEDDING = {"base_url": ms.OFFICIAL_BASE_URL["openai"], "model": "text-embedding-3-small", "dim": 1536}

_runtime: ModelRuntime | None = None


def install(runtime: ModelRuntime | None) -> None:
    """api/main.py 装配完组件后调用。没装之前所有接口返回 503。"""
    global _runtime
    _runtime = runtime


def _require_runtime() -> ModelRuntime:
    if _runtime is None:
        raise HTTPException(503, "服务未就绪")
    return _runtime


# ── 谁能改 ────────────────────────────────────────────────────────────────────

def _is_loopback_name(host: str) -> bool:
    host = host.strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return bool((mapped or address).is_loopback)


def edit_problem(request: Request) -> str:
    """这个请求不能改模型连接的原因；能改返回空串。原因是代号，页面按它配文案。"""
    if not ms.settings_path():
        return "disabled"
    if not ms.editable_here():
        return "read_only_instance"
    host = urlsplit(f"//{request.headers.get('host', '')}").hostname or ""
    if not _is_loopback_name(host):
        return "not_local"
    origin = request.headers.get("origin")
    if origin is not None:
        origin_host = urlsplit(origin).hostname or ""
        if not _is_loopback_name(origin_host):
            return "cross_origin"
    return ""


def _require_edit(request: Request) -> None:
    problem = edit_problem(request)
    if problem:
        raise HTTPException(403, {"code": problem, "message": "这个请求不能修改模型连接。"})


def _saved_by(request: Request) -> str:
    """记一笔是谁存的。员工实例没有账号体系，能记下的只有请求从哪来。"""
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "")
    return f"console@{ip}" if ip else "console"


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError:
        raise HTTPException(400, {"code": "invalid_body", "message": "请求体不是合法的 JSON。"}) from None
    if not isinstance(body, dict):
        raise HTTPException(400, {"code": "invalid_body", "message": "请求体要是一个对象。"})
    return body


def _invalid(error: ms.ModelSettingsError) -> HTTPException:
    return HTTPException(422, {"code": error.code, "message": error.message})


# ── 现状 ──────────────────────────────────────────────────────────────────────

def describe(runtime: ModelRuntime, request: Request) -> dict[str, Any]:
    """两组连接现在用的是哪一份。永远不含 Key。"""
    problem = edit_problem(request)
    chat = runtime.chat
    chat_base = chat.get("base_url") or ms.OFFICIAL_BASE_URL.get(chat["provider"], "")
    embedder = runtime.embedder
    status = embedder.status()
    store_problems = (ms.load_store(runtime.path).get("problems") or {}) if runtime.path else {}
    return {
        "enabled": bool(runtime.path),
        "editable": not problem,
        "reason": problem,
        "chat": {
            "source": chat.get("source", "env"),
            "provider": chat["provider"],
            "base_url": chat_base,
            "model": chat["model"],
            "key_set": bool(chat.get("api_key")),
            "key_hint": ms.key_hint(chat.get("api_key", "")),
            "saved_at": chat.get("saved_at", ""),
            "saved_by": chat.get("saved_by", ""),
        },
        "embedding": {
            **runtime.embedding_meta,
            "backend": status["backend"],
            "available": status["available"],
            "base_url": embedder._base_url or "",
            "model": status["model"],
            "dim": embedder.dim,
            "query_prefix": embedder._query_prefix,
            "key_set": bool(embedder._api_key),
            "key_hint": ms.key_hint(embedder._api_key or ""),
        },
        "switch": runtime.job.public(),
        "problems": store_problems,
        "defaults": {
            "chat": {
                "openai": {"base_url": ms.OFFICIAL_BASE_URL["openai"], "model": DEFAULT_OPENAI_MODEL},
                "anthropic": {"base_url": ms.OFFICIAL_BASE_URL["anthropic"], "model": DEFAULT_ANTHROPIC_MODEL},
            },
            "embedding": DEFAULT_EMBEDDING,
        },
    }


# ── 提交的连接 → 一份完整的候选 ───────────────────────────────────────────────

def chat_connection(body: dict[str, Any], runtime: ModelRuntime) -> dict[str, Any]:
    """
    页面提交的对话连接（协议、地址、Key）→ 校验过的一份（含 Key）。

    Key 没填表示"沿用现在这一把"——只在协议和主机都没换时才允许：换了主机还沿用，
    等于把现有的 Key 交给一台新主机。
    """
    provider = ms.clean_provider(body.get("provider"))
    base_url, warnings = ms.validate_base_url(body.get("base_url") or ms.OFFICIAL_BASE_URL[provider])
    key = ms.clean_api_key(body.get("api_key"))
    if not key:
        current = runtime.chat
        current_base = current.get("base_url") or ms.OFFICIAL_BASE_URL.get(current["provider"], "")
        if current["provider"] != provider or not ms.same_address(current_base, base_url):
            raise ms.ModelSettingsError(
                "api_key_required", "接口地址换了主机或换了协议，要重新填 Key。"
            )
        key = current["api_key"]
    return {"provider": provider, "base_url": base_url, "api_key": key, "warnings": warnings}


def chat_candidate(body: dict[str, Any], runtime: ModelRuntime) -> dict[str, Any]:
    """页面提交的对话连接 → 一份完整的候选（连接 + 模型名）。"""
    connection = chat_connection(body, runtime)
    return {**connection, "model": ms.clean_model_name(body.get("model"))}


def embedding_connection(body: dict[str, Any], runtime: ModelRuntime) -> dict[str, Any]:
    """页面提交的向量连接（地址、Key）→ 校验过的一份（含 Key）。规则同 chat_connection。"""
    base_url, warnings = ms.validate_base_url(body.get("base_url") or DEFAULT_EMBEDDING["base_url"])
    key = ms.clean_api_key(body.get("api_key"))
    if not key:
        current = runtime.embedder
        if not current._api_key or not ms.same_address(current._base_url or "", base_url):
            raise ms.ModelSettingsError("api_key_required", "接口地址换了主机，要重新填 Key。")
        key = current._api_key
    return {"base_url": base_url, "api_key": key, "warnings": warnings}


def embedding_candidate(body: dict[str, Any], runtime: ModelRuntime) -> dict[str, Any]:
    """页面提交的向量连接 → 一份完整的候选（连接 + 模型名、维度、查询前缀）。"""
    connection = embedding_connection(body, runtime)
    return {
        **connection,
        "model": ms.clean_model_name(body.get("model")),
        "dim": ms.clean_dim(body.get("dim")),
        "query_prefix": ms.clean_query_prefix(body.get("query_prefix")),
    }


# ── 测试连接 ──────────────────────────────────────────────────────────────────

def _scrub(text: str, key: str) -> str:
    text = str(text or "")
    return text.replace(key, "***") if key else text


def _provider_message(ex: BaseException) -> str:
    """服务商自己的那句报错。SDK 异常带 body，httpx 的状态错误带响应体。"""
    body = getattr(ex, "body", None)
    if body is None and isinstance(ex, httpx.HTTPStatusError):
        try:
            body = ex.response.json()
        except ValueError:
            body = ex.response.text
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if body.get("message"):
            return str(body["message"])
    if isinstance(body, str) and body.strip():
        return body.strip()
    return str(ex)


def outcome_of(ex: BaseException) -> tuple[str, int | None]:
    """一次失败 → (结果代号, HTTP 状态)。代号是页面配文案用的，和 llm_guard 的口径一致。"""
    if isinstance(ex, GatewayResponseError):
        return "not_api_response", 200
    if isinstance(ex, ValidationError):
        return "not_structured", 200
    if isinstance(ex, (OpenAITimeoutError, AnthropicTimeoutError, httpx.TimeoutException)):
        return "timeout", None
    if isinstance(ex, (OpenAIConnectionError, AnthropicConnectionError, httpx.TransportError)):
        return "connection_failed", None
    status = getattr(ex, "status_code", None)
    if status is None and isinstance(ex, httpx.HTTPStatusError):
        status = ex.response.status_code
        # llm_guard.classify 按属性取 status_code 和 body，给它补上
        ex.status_code = status  # type: ignore[attr-defined]
        try:
            ex.body = ex.response.json()  # type: ignore[attr-defined]
        except ValueError:
            ex.body = ex.response.text  # type: ignore[attr-defined]
    verdict = llm_guard.classify(ex)
    if verdict is not None:
        return {
            llm_guard.REASON_CREDENTIAL: "auth_failed",
            llm_guard.REASON_PERMISSION: "permission_denied",
            llm_guard.REASON_FORBIDDEN: "permission_denied",
            llm_guard.REASON_QUOTA: "quota_exhausted",
            llm_guard.REASON_RATE_LIMIT: "rate_limited",
        }.get(verdict.reason, "unexpected"), verdict.status_code
    if status == 404:
        return "model_not_found", status
    if status in (400, 422):
        return "bad_request", status
    if isinstance(status, int) and status >= 500:
        return "server_error", status
    return "unexpected", status if isinstance(status, int) else None


def _failure(check: str, ex: BaseException, key: str, started: float) -> dict[str, Any]:
    outcome, status = outcome_of(ex)
    return {
        "check": check,
        "outcome": outcome,
        "status": status,
        "detail": _scrub(_provider_message(ex), key)[:300],
        "latency_ms": round((time.monotonic() - started) * 1000),
    }


def _probe_client(candidate: dict[str, Any]) -> Any:
    """测试用的客户端：不套凭证保护（测试失败不能把正在用的连接记成"已被拒绝"），不重试。"""
    kwargs = {"api_key": candidate["api_key"], "base_url": candidate["base_url"],
              "timeout": PROBE_TIMEOUT_S, "max_retries": 0}
    return AsyncOpenAICompat(**kwargs) if candidate["provider"] == "openai" else AsyncAnthropic(**kwargs)


async def probe_chat(candidate: dict[str, Any]) -> dict[str, Any]:
    """
    两条极短的真实请求：一条普通对话，一条结构化输出。

    只测第一条不够：这个服务里意图识别、改写、重排、判分全靠结构化输出，有的兼容网关
    普通对话通、结构化输出不认——那样保存之后回答还能出来，但那四处会全部静默降级。
    """
    from app.tools.tool_manager import QueryRewritePayload

    client = _probe_client(candidate)
    key = candidate["api_key"]
    checks: list[dict[str, Any]] = []

    started = time.monotonic()
    try:
        resp = await client.messages.create(
            model=candidate["model"], max_tokens=1024,
            messages=[{"role": "user", "content": "这是一次连接测试，请只回复两个字：可以"}],
            output_config={"effort": "low"},
        )
        extract_structured_text(resp)
        checks.append({"check": "chat", "outcome": "ok", "status": 200, "detail": "",
                       "latency_ms": round((time.monotonic() - started) * 1000)})
    except GatewayResponseError as ex:
        checks.append(_failure("chat", ex, key, started))
    except ValueError as ex:
        checks.append({"check": "chat", "outcome": "empty_response", "status": 200,
                       "detail": str(ex)[:300], "latency_ms": round((time.monotonic() - started) * 1000)})
    except Exception as ex:  # noqa: BLE001
        checks.append(_failure("chat", ex, key, started))

    warnings = list(candidate["warnings"])
    if checks[0]["outcome"] in ("ok", "empty_response"):
        started = time.monotonic()
        try:
            payload = await structured_parse(
                client, output_format=QueryRewritePayload, model=candidate["model"],
                max_tokens=STRUCTURED_MAX_TOKENS,
                messages=[{"role": "user", "content": "把“柜子缺了一块门板怎么补”改写成两条检索子查询。"}],
            )
            outcome = "ok" if payload.queries else "empty_response"
            checks.append({"check": "structured", "outcome": outcome, "status": 200, "detail": "",
                           "latency_ms": round((time.monotonic() - started) * 1000)})
            if not schema_enforced_for(client, candidate["model"]):
                # 通是通了，但网关没有按 Schema 约束：结果是按提示词要来、再逐条校验的
                warnings.append("schema_not_enforced")
        except (ValidationError, GatewayResponseError) as ex:
            checks.append(_failure("structured", ex, key, started))
        except ValueError as ex:
            # structured_parse 的两种报错：预算被推理吃光，或者回了文字却校验不过
            outcome = "empty_response" if "max_tokens" in str(ex) else "not_structured"
            checks.append({"check": "structured", "outcome": outcome, "status": 200,
                           "detail": str(ex)[:300], "latency_ms": round((time.monotonic() - started) * 1000)})
        except Exception as ex:  # noqa: BLE001
            checks.append(_failure("structured", ex, key, started))
    else:
        checks.append({"check": "structured", "outcome": "skipped", "status": None, "detail": "",
                       "latency_ms": 0})

    return {"ok": all(c["outcome"] == "ok" for c in checks), "checks": checks, "warnings": warnings}


async def probe_embedding(candidate: dict[str, Any]) -> dict[str, Any]:
    """算一个向量，报回模型实际输出的维度。填了维度而对不上，按失败算。"""
    key = candidate["api_key"]
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
            resp = await client.post(
                f"{ms.api_root(candidate['base_url'])}/embeddings",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": candidate["model"], "input": [f"{candidate['query_prefix']}连接测试"]},
            )
            resp.raise_for_status()
            dim = len(resp.json()["data"][0]["embedding"])
    except (KeyError, IndexError, TypeError, ValueError) as ex:
        check = {"check": "embedding", "outcome": "unexpected", "status": 200,
                 "detail": f"返回的不是 OpenAI 兼容的向量格式：{type(ex).__name__}",
                 "latency_ms": round((time.monotonic() - started) * 1000)}
        return {"ok": False, "checks": [check], "dim": 0, "warnings": candidate["warnings"]}
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "checks": [_failure("embedding", ex, key, started)], "dim": 0,
                "warnings": candidate["warnings"]}

    latency = round((time.monotonic() - started) * 1000)
    if candidate["dim"] and candidate["dim"] != dim:
        check = {"check": "embedding", "outcome": "dim_mismatch", "status": 200,
                 "detail": f"填的是 {candidate['dim']} 维，模型实际返回 {dim} 维", "latency_ms": latency}
        return {"ok": False, "checks": [check], "dim": dim, "warnings": candidate["warnings"]}
    check = {"check": "embedding", "outcome": "ok", "status": 200, "detail": "", "latency_ms": latency}
    return {"ok": True, "checks": [check], "dim": dim, "warnings": candidate["warnings"]}


# ── 可选的模型 ────────────────────────────────────────────────────────────────

def _model_names(payload: Any) -> list[str]:
    """
    `GET /models` 的回答 → 模型名。OpenAI 和 Anthropic 都是 `{"data": [{"id": ...}]}`；
    有的中转站直接回数组、或者把名字放在 `name` / `model` 里。认不出、写法不合规的条目跳过。
    """
    items = payload.get("data", payload.get("models")) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise ValueError("回答里没有模型列表")
    names: list[str] = []
    for item in items:
        name = (item.get("id") or item.get("name") or item.get("model")) if isinstance(item, dict) else item
        if not isinstance(name, str):
            continue
        try:
            names.append(ms.clean_model_name(name))
        except ms.ModelSettingsError:
            continue
    return names


def _suits(kind: str, name: str) -> bool:
    if kind == "embedding":
        return bool(_EMBEDDING_NAME.search(name)) and "rerank" not in name.lower()
    return not _NOT_CHAT_NAME.search(name)


async def list_models(connection: dict[str, Any], kind: str) -> dict[str, Any]:
    """
    问接口有哪些模型，给页面的下拉框用。只读：不发对话、不花 token、不保存。

    读不到（中转站没开这个接口、Key 不对……）不算接口错误：页面照样能手动填模型名，
    所以和测试连接一样只报结果代号。按名字筛出适合这一组的；一个也筛不出来时给全部，
    让人自己挑。
    """
    key = connection["api_key"]
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=LIST_TIMEOUT_S) as client:
            if connection.get("provider") == "anthropic":
                resp = await client.get(
                    f"{connection['base_url'].rstrip('/')}/v1/models", params={"limit": 1000},
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                )
            else:
                resp = await client.get(f"{ms.api_root(connection['base_url'])}/models",
                                        headers={"Authorization": f"Bearer {key}"})
            resp.raise_for_status()
            names = _model_names(resp.json())
    except (ValueError, TypeError) as ex:
        # 200 但不是 JSON 或没有列表：多半是网页（地址少了 /v1）或者这家没有列模型的接口
        return {"ok": False, "models": [], "total": 0, "outcome": "not_api_response", "status": 200,
                "detail": _scrub(str(ex), key)[:300]}
    except Exception as ex:  # noqa: BLE001
        failure = _failure("models", ex, key, started)
        outcome = "list_unsupported" if failure["status"] in (404, 405) else failure["outcome"]
        return {"ok": False, "models": [], "total": 0, "outcome": outcome, "status": failure["status"],
                "detail": failure["detail"]}

    unique = sorted(set(names), key=str.lower)
    suited = [name for name in unique if _suits(kind, name)] or unique
    return {"ok": True, "models": suited[:MAX_LISTED_MODELS], "total": len(unique), "outcome": "ok",
            "status": resp.status_code, "detail": ""}


# ── 接口 ──────────────────────────────────────────────────────────────────────

@router.get("")
async def model_status(request: Request, response: Response):
    runtime = _require_runtime()
    # 切换进度要轮询，别让浏览器或代理拿缓存糊弄
    response.headers["Cache-Control"] = "no-store"
    return describe(runtime, request)


@router.post("/chat/models")
async def chat_models(request: Request):
    """这个对话连接有哪些模型。Key 留空的规则和测试连接一样：只在主机没换时沿用现在那把。"""
    runtime = _require_runtime()
    _require_edit(request)
    body = await _json_object(request)
    try:
        connection = chat_connection(body, runtime)
    except ms.ModelSettingsError as ex:
        raise _invalid(ex) from None
    return await list_models(connection, "chat")


@router.post("/chat/test")
async def test_chat(request: Request):
    runtime = _require_runtime()
    _require_edit(request)
    body = await _json_object(request)
    try:
        candidate = chat_candidate(body, runtime)
    except ms.ModelSettingsError as ex:
        raise _invalid(ex) from None
    return await probe_chat(candidate)


@router.put("/chat")
async def save_chat(request: Request):
    """
    保存对话连接并立刻生效。先测一次：测不通就不存——存进去的是一份坏连接，
    每一条客户消息都会失败。服务商临时故障而确认要存时，带 `force: true`。
    """
    runtime = _require_runtime()
    _require_edit(request)
    body = await _json_object(request)
    try:
        candidate = chat_candidate(body, runtime)
    except ms.ModelSettingsError as ex:
        raise _invalid(ex) from None
    result = await probe_chat(candidate)
    if not result["ok"] and not body.get("force"):
        raise HTTPException(422, {"code": "test_failed", "message": "连接测试没通过，没有保存。",
                                  "test": result})

    previous = runtime.chat
    values = {name: candidate[name] for name in ms.FIELDS["chat"]}
    entry = ms.save_section(runtime.path, "chat", values, saved_by=_saved_by(request))
    store = ms.load_store(runtime.path)
    runtime.apply_chat(chat_config_from(store))
    runtime._stamp = ms.file_stamp(runtime.path)
    logger.info(
        f"model_settings.saved section=chat provider={candidate['provider']} "
        f"host={ms.origin_of(candidate['base_url'])} model={candidate['model']} "
        f"key_changed={candidate['api_key'] != previous.get('api_key')} by={entry['saved_by']}"
    )
    return {**describe(runtime, request), "test": result}


@router.delete("/chat")
async def clear_chat(request: Request):
    """恢复为服务端配置（`.env`）。`.env` 本身没配好时不允许——恢复之后对话就全断了。"""
    runtime = _require_runtime()
    _require_edit(request)
    try:
        env_cfg = chat_config_from({})
    except RuntimeError as ex:
        raise HTTPException(409, {"code": "env_incomplete",
                                  "message": f"服务端没有可用的对话模型配置：{ex}"}) from None
    ms.clear_section(runtime.path, "chat")
    runtime.apply_chat(env_cfg)
    runtime._stamp = ms.file_stamp(runtime.path)
    logger.info(f"model_settings.cleared section=chat by={_saved_by(request)}")
    return describe(runtime, request)


@router.post("/embedding/models")
async def embedding_models(request: Request):
    """这个向量连接有哪些向量模型。向量接口一律按 OpenAI 兼容的写法问。"""
    runtime = _require_runtime()
    _require_edit(request)
    body = await _json_object(request)
    try:
        connection = embedding_connection(body, runtime)
    except ms.ModelSettingsError as ex:
        raise _invalid(ex) from None
    return await list_models(connection, "embedding")


@router.post("/embedding/test")
async def test_embedding(request: Request):
    runtime = _require_runtime()
    _require_edit(request)
    body = await _json_object(request)
    try:
        candidate = embedding_candidate(body, runtime)
    except ms.ModelSettingsError as ex:
        raise _invalid(ex) from None
    return await probe_embedding(candidate)


@router.put("/embedding", status_code=202)
async def save_embedding(request: Request):
    """
    测试通过后开始切换。返回 202：重算向量在后台跑，进度看 GET /admin/model 的 `switch`。
    切换成功之前，正在用的还是旧模型——库、配置、检索都没动。
    """
    runtime = _require_runtime()
    _require_edit(request)
    if runtime.switching:
        raise HTTPException(409, {"code": "switch_in_progress", "message": "上一次切换还没结束。"})
    body = await _json_object(request)
    try:
        candidate = embedding_candidate(body, runtime)
    except ms.ModelSettingsError as ex:
        raise _invalid(ex) from None
    result = await probe_embedding(candidate)
    if not result["ok"]:
        raise HTTPException(422, {"code": "test_failed", "message": "连接测试没通过，没有切换。",
                                  "test": result})

    values = {name: candidate[name] for name in ms.FIELDS["embedding"]}
    values["dim"] = result["dim"]
    target = embedder_for(values)
    saved_by = _saved_by(request)
    try:
        runtime.start_embedding_switch(target, values=values, saved_by=saved_by)
    except ms.ModelSettingsError as ex:
        raise HTTPException(409, {"code": ex.code, "message": ex.message}) from None
    logger.info(
        f"model_settings.switch_started section=embedding host={ms.origin_of(values['base_url'])} "
        f"model={values['model']} dim={values['dim']} by={saved_by}"
    )
    return {**describe(runtime, request), "test": result}


@router.delete("/embedding", status_code=202)
async def clear_embedding(request: Request):
    """切换回 `.env` 里的向量配置。和保存一样要重算全部向量，所以也是后台任务。"""
    runtime = _require_runtime()
    _require_edit(request)
    if runtime.switching:
        raise HTTPException(409, {"code": "switch_in_progress", "message": "上一次切换还没结束。"})
    target = Embedder.from_env()
    if target.signature == runtime.embedder.signature:
        # 现在用的就是 .env 那一份（比如页面保存的和 .env 恰好一样）：不用重算，只删文件里那组
        ms.clear_section(runtime.path, "embedding")
        # 向量空间一样，但 Key、地址、查询前缀可能是页面上改过的：运行中的连接也要换回 .env
        runtime.embedder.adopt(target)
        runtime.embedding_meta = {"source": "env", "saved_at": "", "saved_by": ""}
        runtime._stamp = ms.file_stamp(runtime.path)
        return describe(runtime, request)
    try:
        runtime.start_embedding_switch(target, values=None, saved_by=_saved_by(request))
    except ms.ModelSettingsError as ex:
        raise HTTPException(409, {"code": ex.code, "message": ex.message}) from None
    logger.info(f"model_settings.switch_started section=embedding restore=env by={_saved_by(request)}")
    return describe(runtime, request)
