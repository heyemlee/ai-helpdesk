"""
请求追踪上下文与结构化日志。

核心问题：request_id 原来只活在 OrchestratorResult 里——没进日志、没进响应头，
用户报障时无法从一条日志串起"这次请求走了哪条路由、命中了哪次拦截"。

三件事：
  1. contextvar 保存当前请求的 request_id，跨 async 调用自动传播；
  2. RequestIdFilter 把 request_id 注入每条日志记录（无请求上下文时为 "-"）；
  3. JsonFormatter 输出单行 JSON 日志（LOG_FORMAT=json 时启用），
     ELK / Datadog / CloudWatch 可以直接按字段检索。

中间件在 api/main.py 里注册：优先复用调用方传入的 X-Request-ID
（网关一般会生成），没有则自己生成，并在响应头原样返回。
"""
import json
import logging
import time
import uuid
from contextvars import ContextVar

# 当前请求的 request_id。默认 "-" 表示不在请求上下文中（启动日志、后台任务等）。
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def current_request_id() -> str:
    return request_id_var.get()


class RequestIdFilter(logging.Filter):
    """把 contextvar 里的 request_id 挂到每条日志记录上。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """单行 JSON 日志。异常堆栈放进 exc_info 字段而不是多行文本。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": round(record.created, 3),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        # 关联 OTel trace：Grafana 里 Loki 日志按 trace_id 一键跳 Tempo。
        # OTel 未启用时 current_trace_id() 返回 None，字段不输出。
        try:
            from core.telemetry import current_trace_id
            trace_id = current_trace_id()
            if trace_id:
                payload["trace_id"] = trace_id
        except Exception:
            pass
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", fmt: str = "text") -> None:
    """
    统一配置根 logger。fmt="json" 时输出结构化日志，否则保持可读文本
    （文本格式也带 request_id，本地排障同样能串起一次请求）。
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    handler = logging.StreamHandler()
    handler.addFilter(RequestIdFilter())
    if fmt.lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] [%(request_id)s] %(name)s: %(message)s"
        ))
    root.handlers.clear()
    root.addHandler(handler)
