from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional

import httpx

from .circuit import REGISTRY as CIRCUIT_REGISTRY
from .errors import (
    ModelAuthError,
    ModelBadRequestError,
    ModelCallError,
    ModelCircuitOpenError,
    ModelConnectionError,
    ModelCredentialUnavailableError,
    ModelQuotaExhaustedError,
    ModelRateLimitError,
    ModelResponseError,
    ModelServerError,
    ModelTimeoutError,
)
from .logging_setup import get_logger, log_event
from .metrics import MODEL_CALLS, MODEL_TOKENS
from .model_access import (
    AMBIGUOUS_FORBIDDEN_HOLD_SECONDS,
    MODEL_ACCESS,
    REASON_CREDENTIAL_REJECTED,
    REASON_FORBIDDEN,
    REASON_PERMISSION_DENIED,
    REASON_QUOTA_EXHAUSTED,
    REASON_RATE_LIMITED,
    SCOPE_CREDENTIAL,
    SCOPE_MODEL,
    AccessBlock,
    AccessKey,
    ModelAccessRegistry,
    credential_fingerprint,
    endpoint_of,
)
from .run_context import cancellable_sleep, check_cancelled
from .run_context import emit as emit_run_event
from .settings import get_settings
from .trace import record_usage

_logger = get_logger("model")

_shared_client: Optional[httpx.Client] = None
_client_lock = threading.Lock()


def get_http_client() -> httpx.Client:
    """
    进程内共享的 HTTP Client，复用连接池。

    FastAPI 的同步端点跑在线程池里，多个请求会并发走到这里，
    加锁避免重复创建 Client（httpx.Client 本身是线程安全的）。
    """
    global _shared_client
    if _shared_client is None:
        with _client_lock:
            if _shared_client is None:
                _shared_client = httpx.Client(
                    limits=httpx.Limits(
                        max_connections=32, max_keepalive_connections=16
                    )
                )
    return _shared_client


def close_http_client() -> None:
    """关闭共享 Client，释放连接池（进程退出前调用）。"""
    global _shared_client
    with _client_lock:
        if _shared_client is not None:
            _shared_client.close()
            _shared_client = None


@dataclass
class RetryPolicy:
    """可重试错误的退避策略。"""

    max_retries: int = 2
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 8.0

    @classmethod
    def from_settings(cls) -> "RetryPolicy":
        settings = get_settings()
        return cls(
            max_retries=settings.http_max_retries,
            backoff_base_seconds=settings.http_backoff_base_seconds,
            backoff_max_seconds=settings.http_backoff_max_seconds,
        )

    def delay_for(self, attempt: int, retry_after: Optional[float] = None) -> float:
        """第 attempt 次失败后的等待秒数；服务端给了 Retry-After 就听它的。"""
        if retry_after is not None and retry_after > 0:
            return min(retry_after, self.backoff_max_seconds)
        exponential = self.backoff_base_seconds * (2 ** attempt)
        capped = min(exponential, self.backoff_max_seconds)
        # 抖动，避免多个并发请求在同一时刻重试把服务端再打挂一次
        return capped * random.uniform(0.5, 1.0)


def _parse_retry_after(response: httpx.Response) -> Optional[float]:
    """`Retry-After` 可以是秒数，也可以是一个 HTTP 日期；两种都换算成「还要等多少秒」。"""
    raw = response.headers.get("Retry-After", "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        moment = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        return None
    return max(0.0, moment.timestamp() - time.time())


# 响应体里出现这些字样，才算「明确」说了是哪一种拒绝。刻意只收指向明确的说法：
# 认不出来的一律按较轻的那种处理（普通限流 / 语义不明的 403），宁可多发一次，
# 也不把一次临时故障当成永久的。
_QUOTA_MARKERS = (
    "insufficient_quota",
    "insufficient_user_quota",
    "insufficient_balance",
    "insufficient balance",
    "insufficient credit",
    "billing_hard_limit_reached",
    "billing_not_active",
    "quota_exhausted",
    "credit balance is too low",
    "exceeded your current quota",
    "out of credits",
    "余额不足",
    "额度不足",
    "额度已用尽",
    "额度已耗尽",
    "配额已用尽",
    "配额已耗尽",
)
_CREDENTIAL_MARKERS = (
    "invalid api key",
    "invalid_api_key",
    "incorrect api key",
    "invalid x-api-key",
    "api key is disabled",
    "api key has been disabled",
    "api key disabled",
    "key is disabled",
    "api key not found",
    "invalid authentication",
    "authentication_error",
    "token is invalid",
    "invalid token",
    "token expired",
    "token has expired",
    "token revoked",
    "account_deactivated",
    "account deactivated",
    "account disabled",
    "account suspended",
    "令牌无效",
    "无效的令牌",
    "令牌已禁用",
    "令牌已过期",
    "密钥无效",
    "密钥已禁用",
)
_PERMISSION_MARKERS = (
    "permission_error",
    "permission denied",
    "model_not_allowed",
    "model not allowed",
    "not allowed to use model",
    "not allowed to access model",
    "does not have access to model",
    "no access to model",
    "not authorized to access this model",
    "无权使用",
    "无权访问",
    "没有权限",
)

_KEY_SHAPED = re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}|(?i:bearer)\s+[A-Za-z0-9._\-]{8,}")


def scrub_secrets(text: str, api_key: str = "") -> str:
    """
    把凭证从一段要进日志 / 进异常信息的文字里拿掉。

    服务端的报错有时会把 Key 原样带回来（「Incorrect API key provided: sk-…」）。
    先按配置里的那把 Key 精确替换，再把长得像 Key 的片段盖掉。
    """
    if api_key and len(api_key) >= 4:
        text = text.replace(api_key, "<redacted>")
    return _KEY_SHAPED.sub("<redacted>", text)


def _mentions(text: str, markers) -> bool:
    return any(marker in text for marker in markers)


def upstream_error_message(text: str, limit: int = 300) -> str:
    """
    服务端错误响应里那句给人看的话。

    OpenAI 兼容与 Anthropic 的错误体都是 `{"error": {"message": …}}`，中转站常见的还有
    `{"error": "…"}` 和 `{"message": "…"}`。认不出结构就返回空：整个响应体不拿去给人看——
    它可能是网关的一整页 HTML，也可能根本不是模型服务回的。
    """
    try:
        document = json.loads(text)
    except ValueError:
        return ""
    if not isinstance(document, dict):
        return ""
    error = document.get("error")
    if isinstance(error, dict):
        message = error.get("message")
    elif isinstance(error, str):
        message = error
    else:
        message = document.get("message")
    if not isinstance(message, str):
        return ""
    return " ".join(message.split())[:limit]


def _classify_status(
    response: httpx.Response,
    *,
    component: str,
    model: str,
    api_key: str = "",
) -> ModelCallError:
    """
    HTTP 状态码 + 响应语义 → 有类型的错误，并标出它说明「哪个范围不能再用」。

    | 响应 | 错误 | 范围 | 永久 |
    | --- | --- | --- | --- |
    | 402，或写明额度 / 余额用尽 | `ModelQuotaExhaustedError` | 凭证 | 是 |
    | 401 | `ModelAuthError` | 凭证 | 是 |
    | 403，写明凭证无效 / 停用 | `ModelAuthError` | 凭证 | 是 |
    | 403，写明无权使用该模型 | `ModelAuthError` | 模型 | 是 |
    | 403，没说清楚 | `ModelAuthError` | 模型 | 否 |
    | 429（没写额度用尽） | `ModelRateLimitError` | — | 否，可退避重试 |
    """
    status = response.status_code
    text = scrub_secrets(response.text, api_key)
    body = text[:500]
    lowered = text[:4000].lower()
    kwargs = {
        "component": component,
        "model": model,
        "status_code": status,
        "upstream_message": upstream_error_message(text),
    }

    if status == 402 or (
        status in (400, 401, 403, 429) and _mentions(lowered, _QUOTA_MARKERS)
    ):
        return ModelQuotaExhaustedError(
            f"模型服务额度已用尽：{body}",
            block_scope=SCOPE_CREDENTIAL,
            block_reason=REASON_QUOTA_EXHAUSTED,
            block_permanent=True,
            **kwargs,
        )
    if status == 401:
        return ModelAuthError(
            f"模型服务鉴权失败：{body}",
            block_scope=SCOPE_CREDENTIAL,
            block_reason=REASON_CREDENTIAL_REJECTED,
            block_permanent=True,
            **kwargs,
        )
    if status == 403:
        about_credential = _mentions(lowered, _CREDENTIAL_MARKERS)
        about_permission = _mentions(lowered, _PERMISSION_MARKERS)
        if about_credential and not about_permission:
            return ModelAuthError(
                f"模型服务鉴权失败：{body}",
                block_scope=SCOPE_CREDENTIAL,
                block_reason=REASON_CREDENTIAL_REJECTED,
                block_permanent=True,
                **kwargs,
            )
        if about_permission:
            # 只说明这把凭证用不了**这个模型**；别的模型、别的配置不受影响
            return ModelAuthError(
                f"模型服务拒绝访问该模型：{body}",
                block_scope=SCOPE_MODEL,
                block_reason=REASON_PERMISSION_DENIED,
                block_permanent=True,
                **kwargs,
            )
        # 网关拦截页、地区限制、没说原因的拒绝：不知道是不是永久的，不当成永久
        return ModelAuthError(
            f"模型服务拒绝请求（403）：{body}",
            block_scope=SCOPE_MODEL,
            block_reason=REASON_FORBIDDEN,
            block_permanent=False,
            **kwargs,
        )
    if status == 429:
        return ModelRateLimitError(
            f"模型服务限流：{body}",
            retry_after=_parse_retry_after(response),
            **kwargs,
        )
    if status == 408:
        return ModelTimeoutError(f"模型服务返回超时：{body}", **kwargs)
    if 400 <= status < 500:
        return ModelBadRequestError(f"模型服务拒绝请求：{body}", **kwargs)
    return ModelServerError(f"模型服务错误：{body}", **kwargs)


class ModelTransport:
    """
    模型 HTTP 调用统一入口。

    原先 6 个 Client 各自手写 urllib.request，没有重试、没有限流处理、
    错误全部塌缩成一句 RuntimeError。这里收敛为一处实现，负责：

    - 统一超时与连接池复用；
    - 429/5xx/超时/连接失败按指数退避重试（尊重 Retry-After）；
    - 错误按类别抛出（见 errors.py），上层可区分限流与参数错误；
    - 记录每次调用的结构化日志与 Token 用量；
    - 凭证被明确拒绝（或额度明确用尽）之后，同一组「服务地址 + 凭证」的调用
      在这里直接短路，不再发出去（见 model_access.py）。路由、抽词、规划、生成、
      自检、修复、摘要都走这一个入口，所以谁也没法绕过去把一次拒绝放大成几十次。
    """

    def __init__(
        self,
        component: str,
        *,
        api_key: str = "",
        timeout: Optional[float] = None,
        provider: str = "openai",
        retry_policy: Optional[RetryPolicy] = None,
        client: Optional[httpx.Client] = None,
        sleeper: Callable[[float], None] = time.sleep,
        access_registry: Optional[ModelAccessRegistry] = None,
    ):
        settings = get_settings()
        self.component = component
        self.api_key = api_key
        self.provider = provider or "openai"
        """`openai` 走 Bearer + chat/completions，`anthropic` 走 x-api-key + messages。
        Embedding / Rerank 只有 OpenAI 兼容协议，因此保持默认值。"""
        self.anthropic_version = settings.anthropic_version
        self.max_output_tokens = settings.max_output_tokens
        self.timeout = timeout if timeout is not None else settings.http_timeout_seconds
        self.retry_policy = retry_policy or RetryPolicy.from_settings()
        self._client = client
        self._sleep = sleeper
        self.circuit = CIRCUIT_REGISTRY.get(component)
        self.access = access_registry if access_registry is not None else MODEL_ACCESS
        """模型访问状态表。默认是进程内共用的那一张；测试可以换一张带假时钟的。"""
        self._credential = credential_fingerprint(api_key)

    @property
    def client(self) -> httpx.Client:
        return self._client or get_http_client()

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        if self.provider == "anthropic":
            headers = {
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": self.anthropic_version,
            }
        else:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }
        if extra:
            headers.update(extra)
        return headers

    def chat(
        self,
        url: str,
        messages: List[Dict[str, str]],
        *,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 0,
        response_schema: Optional[Dict[str, Any]] = None,
        schema_name: str = "result",
    ) -> str:
        """
        一次非流式对话，返回纯文本。两种协议的差异都收在这里。

        `response_schema` 只对 OpenAI 兼容协议生效：服务端按这份 JSON Schema 约束输出
        （strict 模式），返回的仍是文本，由调用方 `json.loads`。Anthropic 那边照旧
        靠提示词要求 JSON。
        """
        payload = build_chat_payload(
            messages,
            model=model,
            temperature=temperature,
            stream=False,
            provider=self.provider,
            max_tokens=max_tokens or self._output_budget(model),
            reasoning_effort=get_settings().reasoning_effort,
            response_schema=response_schema,
            schema_name=schema_name,
        )
        result = self.post_chat(url, payload, model=model)
        return extract_chat_content(result, component=self.component, model=model)

    def chat_stream(
        self,
        url: str,
        messages: List[Dict[str, str]],
        *,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 0,
    ) -> Iterator[str]:
        """流式对话，逐段 yield 文本增量。"""
        payload = build_chat_payload(
            messages,
            model=model,
            temperature=temperature,
            stream=True,
            provider=self.provider,
            max_tokens=max_tokens or self._output_budget(model),
            reasoning_effort=get_settings().reasoning_effort,
        )
        for _ in range(_MAX_PARAMETER_ADJUSTMENTS + 1):
            adapt_known_parameters(payload, url, model)
            emitted = 0
            try:
                for chunk in self.stream_sse(url, payload, model=model):
                    emitted += 1
                    yield chunk
                return
            except ModelBadRequestError as error:
                # 400 在第一段内容之前就回来了；已经吐过内容的流不能重放
                if emitted or not adapt_after_rejection(payload, url, model, error, self.component):
                    raise
        raise ModelBadRequestError(
            "模型服务一再拒绝请求参数，已放弃。", component=self.component, model=model
        )

    def post_chat(self, url: str, payload: Dict[str, Any], *, model: str) -> Dict[str, Any]:
        """
        发一次非流式对话请求。和 `post_json` 的区别：服务端说某个参数不认识、不支持时，
        去掉或换个名字再发一次，并记住这台服务上的这个模型，之后的请求直接按它认的发。

        这几个参数是「换了模型才会碰到」的那一类：OpenAI 的推理模型只认默认温度、
        输出上限叫 `max_completion_tokens`；兼容 OpenAI 的中转站和开源推理框架
        不一定认 `reasoning_effort` 和 `response_format`。为每个模型手写一张能力表
        跟不上模型更新，所以按服务端的原话来。
        """
        for _ in range(_MAX_PARAMETER_ADJUSTMENTS + 1):
            adapt_known_parameters(payload, url, model)
            try:
                return self.post_json(url, payload, model=model)
            except ModelBadRequestError as error:
                if not adapt_after_rejection(payload, url, model, error, self.component):
                    raise
        raise ModelBadRequestError(
            "模型服务一再拒绝请求参数，已放弃。", component=self.component, model=model
        )

    def _output_budget(self, model: str) -> int:
        """
        这次调用的输出上限。

        OpenAI 推理模型的上限连推理过程一起算：只给回答本身的量，推理一长就一个字
        也剩不下（`finish_reason=length`、正文为空）。所以给它单独一档。
        """
        if self.provider != "anthropic" and openai_reasoning_model(model):
            return get_settings().max_completion_tokens
        return self.max_output_tokens

    def post_json(
        self,
        url: str,
        payload: Dict[str, Any],
        *,
        model: str = "",
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """POST 一个 JSON 请求并返回解析后的响应体，失败按类别抛 ModelCallError。"""
        access_key = self._access_key(url)
        self._guard_access(access_key, model)
        self._guard_circuit(model)
        attempt = 0

        while True:
            # 取消检查点：每次尝试之前。正在进行的那一次打断不了（同步 httpx、共享客户端），
            # 最坏等它返回，结果丢弃。
            check_cancelled()
            if attempt:
                # 退避的这段时间里，别的调用可能已经确认这把凭证不能用了：
                # 重试之前再问一次，不要把一个已知的拒绝再发一遍。
                self._guard_access(access_key, model)
            started = time.perf_counter()
            try:
                response = self.client.post(
                    url,
                    content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers=self._headers(extra_headers),
                    timeout=self.timeout,
                )
                if response.status_code >= 400:
                    raise _classify_status(
                        response,
                        component=self.component,
                        model=model,
                        api_key=self.api_key,
                    )
                try:
                    result = response.json()
                except ValueError as exc:
                    raise ModelResponseError(
                        f"模型返回不是合法 JSON：{scrub_secrets(response.text, self.api_key)[:500]}",
                        component=self.component,
                        model=model,
                        status_code=response.status_code,
                    ) from exc

            except httpx.TimeoutException as exc:
                error: ModelCallError = ModelTimeoutError(
                    f"模型调用超时（{self.timeout}s）：{exc}",
                    component=self.component,
                    model=model,
                )
            except httpx.HTTPError as exc:
                error = ModelConnectionError(
                    f"模型调用连接失败：{exc}",
                    component=self.component,
                    model=model,
                )
            except ModelCallError as exc:
                error = exc
            else:
                duration_ms = round((time.perf_counter() - started) * 1000, 2)
                usage = self._record_usage(result, model)
                self.circuit.record_success()
                self.access.record_success(access_key, model)
                MODEL_CALLS.inc(
                    component=self.component, status="ok", error_category=""
                )
                log_event(
                    _logger,
                    logging.INFO,
                    "model.call",
                    component=self.component,
                    model=model,
                    status="ok",
                    attempt=attempt,
                    duration_ms=duration_ms,
                    **usage,
                )
                emit_run_event("model.call", component=self.component, model=model,
                               status="ok", attempt=attempt, duration_ms=duration_ms, **usage)
                return result

            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            can_retry = (
                error.retryable
                and attempt < self.retry_policy.max_retries
                and self._can_wait_for(error)
            )
            self._record_access_outcome(access_key, model, error)
            MODEL_CALLS.inc(
                component=self.component,
                status="error",
                error_category=error.category,
            )
            log_event(
                _logger,
                logging.WARNING if can_retry else logging.ERROR,
                "model.call",
                component=self.component,
                model=model,
                status="error",
                attempt=attempt,
                duration_ms=duration_ms,
                error_category=error.category,
                status_code=error.status_code,
                will_retry=can_retry,
                error=str(error),
            )
            emit_run_event("model.call", component=self.component, model=model,
                           status="error", attempt=attempt, duration_ms=duration_ms,
                           error_category=error.category, status_code=error.status_code,
                           will_retry=can_retry, error=str(error))

            if not can_retry:
                self._record_circuit_outcome(error)
                raise error

            # 退避可被取消打断
            cancellable_sleep(self.retry_policy.delay_for(attempt, error.retry_after), self._sleep)
            attempt += 1

    def stream_sse(
        self,
        url: str,
        payload: Dict[str, Any],
        *,
        model: str = "",
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Iterator[str]:
        """
        流式调用，逐段 yield `choices[0].delta.content`。

        流式响应一旦开始下发就不能安全重放，因此只对「建连阶段」的失败重试。
        """
        access_key = self._access_key(url)
        self._guard_access(access_key, model)
        self._guard_circuit(model)
        headers = self._headers(extra_headers)
        headers["Accept"] = "text/event-stream"
        attempt = 0

        while True:
            check_cancelled()
            if attempt:
                self._guard_access(access_key, model)
            started = time.perf_counter()
            emitted = 0
            try:
                with self.client.stream(
                    "POST",
                    url,
                    content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers=headers,
                    timeout=self.timeout,
                ) as response:
                    if response.status_code >= 400:
                        response.read()
                        raise _classify_status(
                            response,
                            component=self.component,
                            model=model,
                            api_key=self.api_key,
                        )
                    for chunk in _iter_sse_content(response.iter_lines()):
                        # 流式调用能在片段之间停下：退出 with 就关掉了这条连接
                        check_cancelled()
                        emitted += 1
                        yield chunk

            except httpx.TimeoutException as exc:
                error: ModelCallError = ModelTimeoutError(
                    f"模型流式调用超时（{self.timeout}s）：{exc}",
                    component=self.component,
                    model=model,
                )
            except httpx.HTTPError as exc:
                error = ModelConnectionError(
                    f"模型流式调用连接失败：{exc}",
                    component=self.component,
                    model=model,
                )
            except ModelCallError as exc:
                error = exc
            else:
                self.circuit.record_success()
                self.access.record_success(access_key, model)
                duration_ms = round((time.perf_counter() - started) * 1000, 2)
                log_event(
                    _logger,
                    logging.INFO,
                    "model.stream",
                    component=self.component,
                    model=model,
                    status="ok",
                    attempt=attempt,
                    duration_ms=duration_ms,
                )
                emit_run_event("model.call", component=self.component, model=model,
                               status="ok", attempt=attempt, duration_ms=duration_ms,
                               streamed=True)
                return

            # 已经吐过内容的流不能重放，否则调用方会看到重复片段
            can_retry = (
                error.retryable
                and attempt < self.retry_policy.max_retries
                and emitted == 0
                and self._can_wait_for(error)
            )
            self._record_access_outcome(access_key, model, error)
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            log_event(
                _logger,
                logging.WARNING if can_retry else logging.ERROR,
                "model.stream",
                component=self.component,
                model=model,
                status="error",
                attempt=attempt,
                duration_ms=duration_ms,
                error_category=error.category,
                status_code=error.status_code,
                will_retry=can_retry,
                error=str(error),
            )
            emit_run_event("model.call", component=self.component, model=model,
                           status="error", attempt=attempt, duration_ms=duration_ms,
                           streamed=True, error_category=error.category,
                           status_code=error.status_code, will_retry=can_retry,
                           error=str(error))
            if not can_retry:
                self._record_circuit_outcome(error)
                raise error

            cancellable_sleep(self.retry_policy.delay_for(attempt, error.retry_after), self._sleep)
            attempt += 1

    # ------------------------------------------------------------------
    # 凭证 / 额度 / 权限：已知不可用就不发
    # ------------------------------------------------------------------

    def _access_key(self, url: str) -> AccessKey:
        return AccessKey(
            endpoint=endpoint_of(url),
            provider=self.provider.lower(),
            credential=self._credential,
        )

    def _guard_access(self, key: AccessKey, model: str) -> None:
        """这组凭证（或这个模型）已知不可用时直接抛错，不发请求。"""
        entry = self.access.check(key, model)
        if entry is None:
            return

        error = self._short_circuit_error(entry, model)
        MODEL_CALLS.inc(
            component=self.component,
            status="error",
            error_category=error.category,
        )
        log_event(
            _logger,
            logging.WARNING,
            "model.short_circuited",
            component=self.component,
            model=model,
            cause=entry.reason,
            scope=entry.scope,
            permanent=entry.permanent,
            endpoint=key.endpoint_id,
            credential=key.credential,
        )
        raise error

    def _short_circuit_error(self, entry: AccessBlock, model: str) -> ModelCallError:
        kwargs = {
            "component": self.component,
            "model": model,
            "status_code": entry.status_code,
            "block_scope": entry.scope,
            "block_reason": entry.reason,
            "block_permanent": entry.permanent,
        }
        if entry.permanent:
            what = {
                REASON_QUOTA_EXHAUSTED: "模型服务额度已用尽",
                REASON_PERMISSION_DENIED: "这把凭证无权使用该模型",
            }.get(entry.reason, "模型凭证已被服务端拒绝")
            return ModelCredentialUnavailableError(
                f"{what}，本次调用未发出。修复凭证或额度后重启服务，"
                "或由管理员调用 POST /admin/circuit/reset 解除。",
                **kwargs,
            )

        remaining = round(entry.remaining(self.access.now()), 1)
        if entry.reason == REASON_RATE_LIMITED:
            # 仍然是限流：上层照「模型暂时不可用」降级，到点之后下一个请求自己会再试
            return ModelRateLimitError(
                f"模型服务限流，按 Retry-After 暂缓中（约 {remaining}s 后再试），本次调用未发出。",
                retry_after=remaining,
                **kwargs,
            )
        return ModelCredentialUnavailableError(
            f"模型服务刚刚拒绝了这个模型的请求（403），暂缓中（约 {remaining}s 后再试），本次调用未发出。",
            **kwargs,
        )

    def _can_wait_for(self, error: ModelCallError) -> bool:
        """
        服务端给了 Retry-After 时，只有等得起才在这次调用里重试。

        等不起（超过退避上限）就不重试：提前去试等于没有遵循 Retry-After，
        只会多挨一次限流。
        """
        if error.retry_after is None:
            return True
        return error.retry_after <= self.retry_policy.backoff_max_seconds

    def _record_access_outcome(self, key: AccessKey, model: str, error: ModelCallError) -> None:
        """按响应语义记下「哪个范围不能再用」。只在真的收到了服务端的响应时才会走到。"""
        if error.block_scope and error.block_permanent:
            if error.block_scope == SCOPE_MODEL and not model:
                # 不知道是哪个模型，就没法只拦这一个模型；不为此去拦整把凭证
                return
            entry = self.access.block(
                key,
                scope=error.block_scope,
                reason=error.block_reason,
                model=model,
                status_code=error.status_code,
                component=self.component,
            )
            log_event(
                _logger,
                logging.ERROR,
                "model.access_blocked",
                component=self.component,
                model=entry.model,
                scope=entry.scope,
                reason=entry.reason,
                status_code=error.status_code,
                endpoint=key.endpoint_id,
                credential=key.credential,
                recovery="restart or POST /admin/circuit/reset",
            )
            return

        if not model:
            return

        if isinstance(error, ModelAuthError):
            self.access.hold(
                key,
                reason=REASON_FORBIDDEN,
                seconds=AMBIGUOUS_FORBIDDEN_HOLD_SECONDS,
                model=model,
                status_code=error.status_code,
                component=self.component,
            )
        elif (
            isinstance(error, ModelRateLimitError)
            and error.retry_after is not None
            and not self._can_wait_for(error)
        ):
            self.access.hold(
                key,
                reason=REASON_RATE_LIMITED,
                seconds=error.retry_after,
                model=model,
                status_code=error.status_code,
                component=self.component,
            )

    # ------------------------------------------------------------------
    # 熔断
    # ------------------------------------------------------------------

    def _guard_circuit(self, model: str) -> None:
        """熔断器断开时直接抛错，不再发起请求也不再等超时。"""
        if self.circuit.allow():
            return

        MODEL_CALLS.inc(
            component=self.component,
            status="error",
            error_category="ModelCircuitOpenError",
        )
        log_event(
            _logger,
            logging.WARNING,
            "model.short_circuited",
            component=self.component,
            model=model,
        )
        raise ModelCircuitOpenError(
            f"{self.component} 模型调用已熔断，正在冷却，暂不发起请求。",
            component=self.component,
            model=model,
        )

    @staticmethod
    def _is_infrastructure_failure(error: ModelCallError) -> bool:
        """
        只有基础设施类失败才计入熔断。

        鉴权失败、参数错误是确定性的：它们本来就秒回，熔断没有意义，
        反而会让配置修好之后还要等冷却。额度耗尽同理（它不是 `ModelRateLimitError`）。
        """
        return isinstance(
            error,
            (
                ModelTimeoutError,
                ModelConnectionError,
                ModelServerError,
                ModelRateLimitError,
            ),
        )

    def _record_circuit_outcome(self, error: ModelCallError) -> None:
        if self._is_infrastructure_failure(error):
            self.circuit.record_failure()
        elif isinstance(error, (ModelAuthError, ModelQuotaExhaustedError)):
            # 凭证被拒、额度耗尽：链路是通的，但这**不是一次成功的调用**。
            # 不清失败计数、不合上熔断器；后续还发不发由 model_access 决定。
            self.circuit.record_inconclusive()
        else:
            # 参数错误、返回体解析失败：服务正常应答了，链路本身是通的
            self.circuit.record_success()

    def _record_usage(self, result: Dict[str, Any], model: str) -> Dict[str, int]:
        usage = result.get("usage") if isinstance(result, dict) else None
        if not isinstance(usage, dict):
            return {}
        # OpenAI 用 prompt/completion_tokens，Anthropic 用 input/output_tokens
        prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion_tokens = int(
            usage.get("completion_tokens") or usage.get("output_tokens") or 0
        )
        record_usage(
            component=self.component,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        MODEL_TOKENS.inc(prompt_tokens, component=self.component, kind="prompt")
        MODEL_TOKENS.inc(
            completion_tokens, component=self.component, kind="completion"
        )
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }


def _iter_sse_content(lines: Iterator[str]) -> Iterator[str]:
    """
    解析 SSE 流，只吐出增量文本（同时兼容 OpenAI 与 Anthropic 两种事件格式）。

    流以「输出到上限」结束时（OpenAI `finish_reason=length`、Anthropic
    `stop_reason=max_tokens`）在最后抛 `ModelResponseError`：前面吐出去的是半截，
    不能让调用方当成完整的规划去解析。
    """
    truncated = False
    for raw_line in lines:
        line = raw_line.strip()
        if not line or not line.startswith("data:"):
            continue
        data_text = line[len("data:"):].strip()
        if data_text == "[DONE]":
            break
        try:
            event = json.loads(data_text)
        except json.JSONDecodeError:
            continue

        # Anthropic：message_start / content_block_delta / message_delta / message_stop
        event_type = event.get("type")
        if event_type == "content_block_delta":
            text = (event.get("delta") or {}).get("text", "")
            if text:
                yield text
            continue
        if event_type == "message_delta":
            truncated = truncated or (event.get("delta") or {}).get("stop_reason") == "max_tokens"
            continue
        if event_type == "message_stop":
            break
        if event_type is not None and "choices" not in event:
            continue

        # OpenAI：choices[0].delta.content
        choices: List[Dict[str, Any]] = event.get("choices") or [{}]
        content = (choices[0].get("delta") or {}).get("content", "")
        if content:
            yield content
        truncated = truncated or choices[0].get("finish_reason") == "length"

    if truncated:
        raise ModelResponseError("模型输出到了长度上限，内容被截断（流式）。")


# ----------------------------------------------------------------------
# 请求体：两种协议，以及 OpenAI 推理模型的几处不同
# ----------------------------------------------------------------------

# OpenAI 的推理模型：GPT-5 起的 gpt-N 和 o 系列。前面可以带中转站常见的 `openai/` 这类前缀。
_OPENAI_REASONING = re.compile(r"(?:^|/)(?:gpt-(?:[5-9]|[1-9]\d)(?:[.\-]|$)|o[1-9](?:-|$))", re.I)


def openai_reasoning_model(model: str) -> bool:
    """
    OpenAI 的推理模型。它们和 gpt-4o 这一代有三处不同，请求体要跟着变：

    - 只认默认温度：带 `temperature=0` 整个请求被 400 拒绝；
    - 输出上限叫 `max_completion_tokens`，而且推理过程也算在里面；
    - 可以用 `reasoning_effort` 控制想多久——想得越久，第一个字来得越晚。

    认不出的模型名（中转站上的 Qwen、DeepSeek 等）按原来的方式发：带温度、不带上限。
    """
    return bool(_OPENAI_REASONING.search(model or ""))


_MAX_PARAMETER_ADJUSTMENTS = 4

# (端点, 模型) → 这台服务上的这个模型不认、要去掉或改名的参数。进程内记着，重启清空。
_ADJUSTMENTS: Dict[tuple, Dict[str, str]] = {}
_ADJUSTMENTS_LOCK = threading.Lock()

# 服务端说哪个参数不行时，怎么改。值为 "" 表示去掉，否则是改成的名字。
_ADJUSTABLE = (
    ("reasoning_effort", ""),
    ("temperature", ""),
    ("response_format", ""),
    ("max_completion_tokens", "max_tokens"),
    ("max_tokens", "max_completion_tokens"),
)


def adapt_known_parameters(payload: Dict[str, Any], url: str, model: str) -> None:
    """按之前记下的，把这个模型不认的参数先改掉。"""
    with _ADJUSTMENTS_LOCK:
        known = dict(_ADJUSTMENTS.get((endpoint_of(url), model), {}))
    for name, replacement in known.items():
        if name in payload:
            value = payload.pop(name)
            if replacement:
                payload[replacement] = value


def adapt_after_rejection(
    payload: Dict[str, Any],
    url: str,
    model: str,
    error: ModelCallError,
    component: str = "",
) -> bool:
    """
    400 是因为某个参数吗？是的话改掉它、记下来，返回 True（调用方再发一次）；否则 False。

    只认服务端原话里点了名的参数：一个与参数无关的 400（上下文太长、内容被拦）
    不该引起任何改动。
    """
    text = f"{error.upstream_message} {error}".lower()
    for name, replacement in _ADJUSTABLE:
        if name not in payload or name not in text:
            continue
        if name == "max_tokens" and "max_completion_tokens" in payload:
            continue
        with _ADJUSTMENTS_LOCK:
            _ADJUSTMENTS.setdefault((endpoint_of(url), model), {})[name] = replacement
        value = payload.pop(name)
        if replacement:
            payload[replacement] = value
        log_event(
            _logger,
            logging.WARNING,
            "model.parameter_adapted",
            component=component,
            model=model,
            parameter=name,
            replaced_by=replacement or None,
        )
        return True
    return False


def forget_parameter_adaptations() -> None:
    """清掉记下的参数改动（换模型连接时、测试里用）。"""
    with _ADJUSTMENTS_LOCK:
        _ADJUSTMENTS.clear()


def _anthropic_accepts_temperature(model: str) -> bool:
    """Claude 5 起 `temperature` 被废弃，带上这个字段整个请求会被 400 拒绝。

    老模型（claude-3-*/claude-sonnet-4 等）仍然接受，所以按模型名分流而不是
    一刀切删掉——本项目全链路用 temperature=0 求确定性，能带就带。
    认不出的模型名按「接受」处理，保持改动前的行为。
    """
    match = re.search(r"claude-(?:opus|sonnet|haiku|fable)-(\d+)", model or "")
    return not (match and int(match.group(1)) >= 5)


def build_chat_payload(
    messages: List[Dict[str, str]],
    *,
    model: str,
    temperature: float = 0.0,
    stream: bool = False,
    provider: str = "openai",
    max_tokens: int = 0,
    reasoning_effort: str = "",
    response_schema: Optional[Dict[str, Any]] = None,
    schema_name: str = "result",
) -> Dict[str, Any]:
    """按厂商协议构造请求体。

    OpenAI 兼容：`messages` 直接带 system 角色；推理模型（见 `openai_reasoning_model`）
    不带温度，改带 `max_completion_tokens` 和 `reasoning_effort`；给了 `response_schema`
    就要求按它输出（strict）。
    Anthropic Messages：system 要提到顶层字段，`max_tokens` 必填，
    且 Claude 5 起不能带 `temperature`。`reasoning_effort` 和 `response_schema` 不用。
    """
    if provider == "anthropic":
        system_parts = [
            str(m.get("content", ""))
            for m in messages
            if m.get("role") == "system"
        ]
        turns = [m for m in messages if m.get("role") != "system"]
        payload: Dict[str, Any] = {
            "model": model,
            "messages": turns,
            "max_tokens": max_tokens or get_settings().max_output_tokens,
            "stream": stream,
        }
        if _anthropic_accepts_temperature(model):
            payload["temperature"] = temperature
        if system_parts:
            payload["system"] = "\n\n".join(part for part in system_parts if part)
        return payload

    payload = {"model": model, "messages": messages, "stream": stream}
    if openai_reasoning_model(model):
        if max_tokens:
            payload["max_completion_tokens"] = max_tokens
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
    else:
        payload["temperature"] = temperature
    if response_schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": response_schema},
        }
    return payload


def extract_chat_content(result: Dict[str, Any], *, component: str, model: str) -> str:
    """
    从对话响应里取出文本内容（兼容 OpenAI 与 Anthropic 两种返回体）。

    输出到了长度上限（OpenAI `finish_reason=length`、Anthropic `stop_reason=max_tokens`）
    时抛 `ModelResponseError`，不把半截内容交出去：半条 SQL 会被当成完整的去执行，
    半份规划会被解析成少了几步的规划。推理模型把上限全用在推理上时正文是空的，也走这里。
    """
    if isinstance(result, dict) and result.get("choices"):
        choice = result["choices"][0] if isinstance(result["choices"][0], dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        if choice.get("finish_reason") == "length":
            raise ModelResponseError(
                "模型输出到了长度上限，内容被截断"
                f"（finish_reason=length，已有 {len(str(message.get('content') or ''))} 个字符）。",
                component=component,
                model=model,
            )
        refusal = message.get("refusal")
        if refusal and not message.get("content"):
            raise ModelResponseError(
                f"模型拒绝回答：{str(refusal)[:200]}", component=component, model=model
            )
    if (
        isinstance(result, dict)
        and isinstance(result.get("content"), list)
        and result.get("stop_reason") == "max_tokens"
    ):
        raise ModelResponseError(
            "模型输出到了长度上限，内容被截断（stop_reason=max_tokens）。",
            component=component,
            model=model,
        )
    try:
        if isinstance(result, dict) and result.get("choices"):
            return result["choices"][0]["message"]["content"].strip()
        # Anthropic：content 是内容块数组，取其中的 text 块
        if isinstance(result, dict) and isinstance(result.get("content"), list):
            text = "".join(
                str(block.get("text", ""))
                for block in result["content"]
                if isinstance(block, dict) and block.get("type") == "text"
            )
            if text.strip():
                return text.strip()
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise ModelResponseError(
            f"解析模型返回失败，原始返回：{str(result)[:500]}",
            component=component,
            model=model,
        ) from exc

    raise ModelResponseError(
        f"解析模型返回失败，原始返回：{str(result)[:500]}",
        component=component,
        model=model,
    )
