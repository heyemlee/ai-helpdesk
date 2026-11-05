"""
OpenTelemetry 链路追踪。

调用方（ai-helpdesk 的员工实例）的 httpx 已经被 OTel 插桩，发过来的请求头里带着 W3C
`traceparent`。这里把它接上：

- 每个请求一个 SERVER span，父节点是调用方发请求的那一跳（`server_span`，由 API 的中间件调）；
- 每个阶段（`trace.stage()`）一个子 span；
- 每次模型调用、每次 SQL 执行各一个子 span（`timed_event_span`，挂在运行事件上，见
  `run_context.emit`）。

于是在 Tempo 里，一轮对话从客服一直到每一步 SQL 是同一条链路。

自己的 16 位 trace_id（审计、run 记录、日志里一直用的那个）不换，记成 span 属性
`ai_sql.trace_id`；run 的 id 记成 `ai_sql.run_id`。JSON 日志里另记一份 OTel 的 trace id
（`otel_trace_id`），Grafana 里可以从 SQL Agent 的日志跳到 Tempo。

span 里不放问题原文、SQL 和错误原文：那些在审计和运行事件里，权限和保留期都在那边管。

只在配置了 `OTEL_EXPORTER_OTLP_ENDPOINT` 时安装 TracerProvider、导出 span；没配时 tracer 是
OTel 的空实现，开销可以忽略。没配时也照样读 `traceparent`：日志里的 `otel_trace_id` 仍是
调用方那条链路的 id，两边的日志对得上。
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Mapping, Optional

from opentelemetry import propagate, trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

_logger = logging.getLogger("ai_sql.telemetry")

TRACER_NAME = "ai_sql_agent"
_tracer = trace.get_tracer(TRACER_NAME)
_provider: Any = None

# 计了时的运行事件 → span 名，以及哪些属性带进 span。原文类的（sql、error、问题）一律不带
_TIMED_EVENTS: Dict[str, tuple] = {
    "model.call": (
        "model.call",
        ("component", "model", "status", "attempt", "streamed", "error_category", "status_code",
         "prompt_tokens", "completion_tokens", "total_tokens"),
    ),
    "tool.sql": (
        "sql.execute",
        ("database", "status", "row_count", "truncated"),
    ),
}


def setup_telemetry(release: str = "") -> bool:
    """
    配了 `OTEL_EXPORTER_OTLP_ENDPOINT` 就装上 TracerProvider，span 批量发给 Collector。返回是否启用。

    服务名取 `OTEL_SERVICE_NAME`，没设时是 `ai-sql-agent`。重复调用不会叠加。
    `release` 是没有 `AI_SQL_RELEASE` 时用的版本号。
    """
    global _provider
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        return False
    if _provider is not None:
        return True

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({
        "service.name": os.getenv("OTEL_SERVICE_NAME", "").strip() or "ai-sql-agent",
        # 镜像构建时记下的提交号（Dockerfile 的 AI_SQL_RELEASE），没有就用包版本
        "service.version": os.getenv("AI_SQL_RELEASE", "").strip() or release or "dev",
    }))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _provider = provider
    _logger.info("OTel 追踪已启用 → %s", endpoint)
    return True


def shutdown_telemetry() -> None:
    """进程收尾：把还没发出去的 span 发完。没启用时什么都不做。"""
    global _provider
    if _provider is None:
        return
    try:
        _provider.shutdown()
    except Exception as exc:  # pragma: no cover - 收尾不能抛
        _logger.warning("OTel 收尾失败：%s", exc)
    _provider = None


@contextmanager
def server_span(method: str, path: str, headers: Mapping[str, str]) -> Iterator[Span]:
    """
    一个请求的 SERVER span，父节点取自请求头里的 `traceparent`（没有就是一条新链路）。

    退出时**不结束** span：流式响应（A2A 的 message/stream）在中间件返回之后才开始干活，
    span 要等响应体发完再结束，由调用方 `end_server_span()`。
    """
    parent = propagate.extract(headers)
    with _tracer.start_as_current_span(
        f"{method} {path}",
        context=parent,
        kind=SpanKind.SERVER,
        end_on_exit=False,
        record_exception=False,
        set_status_on_exception=False,
        attributes={"http.request.method": method, "url.path": path},
    ) as current:
        yield current


def end_server_span(span: Span, *, method: str, route: str, status_code: int,
                    error: Optional[BaseException] = None) -> None:
    """
    请求结束：span 名换成路由模板（`POST /runs/{run_id}/cancel`，不用原始路径，免得名字跟着 id
    变），补上状态码，5xx 和异常标成错误，然后结束 span。
    """
    if span.is_recording():
        span.update_name(f"{method} {route}")
        span.set_attribute("http.route", route)
        span.set_attribute("http.response.status_code", status_code)
        if error is not None:
            span.set_status(Status(StatusCode.ERROR, type(error).__name__))
        elif status_code >= 500:
            span.set_status(Status(StatusCode.ERROR))
    span.end()


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Span]:
    """
    一个内部 span，挂在当前 span 下面。异常照常往上抛，状态由调用方自己标（取消不算错误）。

    值为 None 的属性不写。
    """
    with _tracer.start_as_current_span(
        name,
        record_exception=False,
        set_status_on_exception=False,
        attributes={key: value for key, value in attributes.items() if value is not None},
    ) as current:
        yield current


def mark_error(current: Span, error: BaseException) -> None:
    """把一个 span 标成失败。只记异常类型，不记异常原文（里面可能有 SQL、模型输出）。"""
    if current.is_recording():
        current.set_status(Status(StatusCode.ERROR, type(error).__name__))
        current.set_attribute("error.type", type(error).__name__)


def timed_event_span(event_type: str, attrs: Mapping[str, Any]) -> None:
    """
    一条计了时的运行事件（模型调用 `model.call`、SQL 执行 `tool.sql`）→ 一个已经结束的子 span。

    事件在调用结束时才发、带着 `duration_ms`，所以 span 的开始时间往回推这么多。挂在当前 span
    （发事件时所在的那个阶段）下面。别的事件类型、没有 `duration_ms` 的，什么都不做。
    """
    spec = _TIMED_EVENTS.get(event_type)
    if spec is None:
        return
    duration_ms = attrs.get("duration_ms")
    if not isinstance(duration_ms, (int, float)) or isinstance(duration_ms, bool):
        return
    name, keys = spec
    end = time.time_ns()
    start = end - int(max(duration_ms, 0) * 1_000_000)
    attributes = {f"ai_sql.{key}": _attribute(attrs[key]) for key in keys if attrs.get(key) is not None}
    current = _tracer.start_span(name, start_time=start, attributes=attributes)
    if attrs.get("status") == "error":
        current.set_status(Status(StatusCode.ERROR, str(attrs.get("error_category") or "")))
    current.end(end_time=end)


def tag_current_span(key: str, value: Any) -> None:
    """给当前 span 加一个属性（比如请求的 SERVER span 上的 run_id）。不在 span 里时什么都不做。"""
    current = trace.get_current_span()
    if value is not None and current.is_recording():
        current.set_attribute(key, _attribute(value))


def current_otel_trace_id() -> str:
    """当前 span 的 OTel trace id（32 位十六进制），日志关联用。不在任何链路里时是空串。"""
    context = trace.get_current_span().get_span_context()
    if context.is_valid:
        return format(context.trace_id, "032x")
    return ""


def _attribute(value: Any) -> Any:
    """span 属性只收 str / bool / int / float（及其列表），别的转成字符串。"""
    if isinstance(value, (str, bool, int, float)):
        return value
    return str(value)
