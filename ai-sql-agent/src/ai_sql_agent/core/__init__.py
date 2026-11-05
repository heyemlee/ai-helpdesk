"""
AI SQL Agent 横切基础设施：配置、日志与链路追踪、模型调用治理。

刻意不引入 Agent 框架：编排逻辑留在 ai_sql_agent.pipeline，这里只解决
「生产可运维性」——配置收口、结构化日志、超时重试、错误分类、用量统计。
"""

from .circuit import REGISTRY as CIRCUIT_REGISTRY
from .circuit import CircuitState
from .errors import (
    SqlAgentError,
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
from .logging_setup import configure_logging, get_logger, log_event
from .model_access import MODEL_ACCESS, ModelAccessRegistry
from .metrics import REGISTRY, render_metrics
from .settings import (
    SqlAgentSettings,
    get_settings,
    reload_model_settings,
    reload_settings,
)
from .trace import (
    get_trace_id,
    new_trace_id,
    record_usage,
    stage,
    usage_scope,
    usage_summary,
    use_trace_id,
)
from .transport import (
    ModelTransport,
    RetryPolicy,
    build_chat_payload,
    extract_chat_content,
    get_http_client,
)

__all__ = [
    "SqlAgentError",
    "CIRCUIT_REGISTRY",
    "CircuitState",
    "MODEL_ACCESS",
    "ModelAccessRegistry",
    "SqlAgentSettings",
    "ModelAuthError",
    "ModelBadRequestError",
    "ModelCallError",
    "ModelCircuitOpenError",
    "ModelConnectionError",
    "ModelCredentialUnavailableError",
    "ModelQuotaExhaustedError",
    "ModelRateLimitError",
    "ModelResponseError",
    "ModelServerError",
    "ModelTimeoutError",
    "ModelTransport",
    "REGISTRY",
    "RetryPolicy",
    "build_chat_payload",
    "configure_logging",
    "extract_chat_content",
    "get_http_client",
    "get_logger",
    "get_settings",
    "get_trace_id",
    "log_event",
    "new_trace_id",
    "record_usage",
    "render_metrics",
    "reload_model_settings",
    "reload_settings",
    "stage",
    "usage_scope",
    "usage_summary",
    "use_trace_id",
]
