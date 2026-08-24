"""
OpenTelemetry 分布式追踪。

目标：看清一次 /chat 的完整耗时分布——意图识别、RAG 检索、Agent LLM 调用、
记忆读写各占多少。三层埋点：

  1. 自动埋点：FastAPI（HTTP span）、httpx（两个 LLM SDK 的底层请求）、
     redis（工作记忆/限流）、asyncpg（Postgres 存储）——四个库一行 instrument；
  2. 手动 span：intent.recognize / rag.retrieve / agent.handle / memory.get_context
     这类**逻辑阶段**，自动埋点看不出来的业务语义在这里；
  3. 日志关联：JsonFormatter 把当前 span 的 trace_id 写进每条 JSON 日志，
     Grafana 里 Loki 日志一键跳转 Tempo trace。

未配置 OTEL_EXPORTER_OTLP_ENDPOINT 时不安装 provider，span() 走 OTel 的
no-op tracer，零开销——demo 模式不需要起 collector 也能跑。
"""
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

logger = logging.getLogger(__name__)

_tracer = trace.get_tracer("ai-helpdesk")


def setup_telemetry(app=None) -> bool:
    """
    初始化 OTel。返回是否启用了真实导出。

    只在配置了 OTEL_EXPORTER_OTLP_ENDPOINT 时安装 TracerProvider；
    自动埋点（httpx/redis/asyncpg/fastapi）也只在启用后进行，
    避免 demo 模式白白付出拦截开销。
    """
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        logger.info("OTel 未配置（OTEL_EXPORTER_OTLP_ENDPOINT 为空），追踪关闭")
        return False

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({
        "service.name": os.getenv("OTEL_SERVICE_NAME", "ai-helpdesk"),
        "service.version": os.getenv("AI_HELPDESK_RELEASE", "dev"),
    }))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)

    global _tracer
    _tracer = trace.get_tracer("ai-helpdesk")

    # 自动埋点：失败不阻塞启动（某个库版本不兼容时只丢那一层的 span）
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument()
    except Exception as ex:
        logger.warning(f"httpx 自动埋点失败: {ex}")
    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        RedisInstrumentor().instrument()
    except Exception as ex:
        logger.warning(f"redis 自动埋点失败: {ex}")
    try:
        from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
        AsyncPGInstrumentor().instrument()
    except Exception as ex:
        logger.warning(f"asyncpg 自动埋点失败: {ex}")
    if app is not None:
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
            FastAPIInstrumentor.instrument_app(app)
        except Exception as ex:
            logger.warning(f"FastAPI 自动埋点失败: {ex}")

    logger.info(f"OTel 追踪已启用 → {endpoint}")
    return True


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """
    业务阶段 span。用法：

        with span("intent.recognize", strategy="fast_path"):
            ...

    未启用 OTel 时是 no-op tracer，开销可忽略。
    """
    with _tracer.start_as_current_span(name) as s:
        for key, value in attributes.items():
            if value is not None:
                s.set_attribute(key, value)
        yield s


def tag_current_span(key: str, value: Any) -> None:
    """给当前活动的 span 加一个属性（比如根 span 上的 helpdesk.turn_id）。没启用 OTel 时什么都不做。"""
    current = trace.get_current_span()
    if current.is_recording():
        current.set_attribute(key, value)


def current_trace_id() -> str | None:
    """当前活动 span 的 trace_id（十六进制），供日志关联。无活动 span 时 None。"""
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid and ctx.trace_id:
        return format(ctx.trace_id, "032x")
    return None
