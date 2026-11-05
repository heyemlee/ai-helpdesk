from __future__ import annotations

import threading
from typing import Dict, List, Optional, Sequence, Tuple

Labels = Tuple[Tuple[str, str], ...]


def _normalize(labels: Optional[Dict[str, str]]) -> Labels:
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    )


def _render_labels(labels: Labels, extra: Optional[Tuple[str, str]] = None) -> str:
    items = list(labels)
    if extra:
        items.append(extra)
    if not items:
        return ""
    body = ",".join(f'{name}="{_escape(value)}"' for name, value in items)
    return "{" + body + "}"


class Counter:
    """单调递增计数器。"""

    def __init__(self, name: str, help_text: str, label_names: Sequence[str] = ()):
        self.name = name
        self.help_text = help_text
        self.label_names = tuple(label_names)
        self._values: Dict[Labels, float] = {}
        self._lock = threading.Lock()

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = _normalize(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def collect(self) -> List[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} counter"]
        with self._lock:
            snapshot = dict(self._values)
        for labels, value in sorted(snapshot.items()):
            lines.append(f"{self.name}{_render_labels(labels)} {value:g}")
        return lines


class Gauge:
    """可增可减的瞬时值，支持带标签的多条序列。"""

    def __init__(self, name: str, help_text: str, label_names: Sequence[str] = ()):
        self.name = name
        self.help_text = help_text
        self.label_names = tuple(label_names)
        self._values: Dict[Labels, float] = {}
        self._lock = threading.Lock()

    def set(self, value: float) -> None:
        self.set_labeled(value)

    def set_labeled(self, value: float, **labels: str) -> None:
        key = _normalize(labels)
        with self._lock:
            self._values[key] = float(value)

    def collect(self) -> List[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} gauge"]
        with self._lock:
            snapshot = dict(self._values)
        for labels, value in sorted(snapshot.items()):
            lines.append(f"{self.name}{_render_labels(labels)} {value:g}")
        return lines


DEFAULT_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)


class Histogram:
    """
    直方图，用于算延迟分位（P95 由 Prometheus 侧的 histogram_quantile 计算）。

    桶是累积的：le="0.5" 的计数包含所有 ≤0.5 的观测，最后再补一个 +Inf。
    """

    def __init__(
        self,
        name: str,
        help_text: str,
        label_names: Sequence[str] = (),
        buckets: Sequence[float] = DEFAULT_BUCKETS,
    ):
        self.name = name
        self.help_text = help_text
        self.label_names = tuple(label_names)
        self.buckets = tuple(sorted(buckets))
        self._counts: Dict[Labels, List[int]] = {}
        self._sums: Dict[Labels, float] = {}
        self._totals: Dict[Labels, int] = {}
        self._lock = threading.Lock()

    def observe(self, value: float, **labels: str) -> None:
        key = _normalize(labels)
        with self._lock:
            counts = self._counts.setdefault(key, [0] * len(self.buckets))
            for index, upper in enumerate(self.buckets):
                if value <= upper:
                    counts[index] += 1
            self._sums[key] = self._sums.get(key, 0.0) + value
            self._totals[key] = self._totals.get(key, 0) + 1

    def collect(self) -> List[str]:
        lines = [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} histogram",
        ]
        with self._lock:
            keys = sorted(self._counts)
            counts = {key: list(self._counts[key]) for key in keys}
            sums = dict(self._sums)
            totals = dict(self._totals)

        for key in keys:
            for index, upper in enumerate(self.buckets):
                lines.append(
                    f"{self.name}_bucket"
                    f"{_render_labels(key, ('le', _format_bucket(upper)))} "
                    f"{counts[key][index]:g}"
                )
            lines.append(
                f"{self.name}_bucket{_render_labels(key, ('le', '+Inf'))} "
                f"{totals[key]:g}"
            )
            lines.append(f"{self.name}_sum{_render_labels(key)} {sums[key]:g}")
            lines.append(f"{self.name}_count{_render_labels(key)} {totals[key]:g}")
        return lines


def _format_bucket(value: float) -> str:
    return f"{value:g}"


class MetricsRegistry:
    """
    指标注册表 + Prometheus 文本输出。

    刻意不引 prometheus_client：需要的只是几个计数器和直方图，
    自己实现约百行、零依赖，输出格式与 Prometheus 抓取规范一致。
    """

    def __init__(self) -> None:
        self._metrics: List[object] = []

    def register(self, metric):
        self._metrics.append(metric)
        return metric

    def render(self) -> str:
        lines: List[str] = []
        for metric in self._metrics:
            lines.extend(metric.collect())
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        """仅用于测试。"""
        for metric in self._metrics:
            for attribute in ("_values", "_counts", "_sums", "_totals"):
                container = getattr(metric, attribute, None)
                if isinstance(container, dict):
                    container.clear()


REGISTRY = MetricsRegistry()

REQUESTS = REGISTRY.register(
    Counter(
        "ai_sql_requests_total",
        "HTTP 请求总数",
        ("method", "path", "status"),
    )
)
REQUEST_DURATION = REGISTRY.register(
    Histogram(
        "ai_sql_request_duration_seconds",
        "HTTP 请求耗时",
        ("method", "path"),
    )
)
STAGE_DURATION = REGISTRY.register(
    Histogram(
        "ai_sql_stage_duration_seconds",
        "各处理阶段耗时（路由/检索/规划/生成/执行/修复）",
        ("stage", "status"),
    )
)
MODEL_CALLS = REGISTRY.register(
    Counter(
        "ai_sql_model_calls_total",
        "模型调用次数",
        ("component", "status", "error_category"),
    )
)
MODEL_TOKENS = REGISTRY.register(
    Counter(
        "ai_sql_model_tokens_total",
        "模型 Token 消耗",
        ("component", "kind"),
    )
)
SQL_EXECUTIONS = REGISTRY.register(
    Counter(
        "ai_sql_sql_executions_total",
        "SQL 执行次数",
        ("database", "status"),
    )
)
SQL_REPAIRS = REGISTRY.register(
    Counter("ai_sql_sql_repairs_total", "SQL 修复回路结果", ("result",))
)
ACCESS_DENIED = REGISTRY.register(
    Counter("ai_sql_access_denied_total", "数据权限拦截次数", ("database",))
)
CROSS_DB_VIOLATIONS = REGISTRY.register(
    Counter(
        "ai_sql_cross_database_references_total",
        "生成的 SQL 引用了目标库之外的表（跨库越界，已拦截重生成）",
        ("database",),
    )
)
PLAN_DB_CONFLICTS = REGISTRY.register(
    Counter(
        "ai_sql_plan_database_conflicts_total",
        "规划出的步骤声明的库里没有它处理对象引用的表（已触发重新规划）",
        (),
    )
)
SCHEMA_COMPACTED = REGISTRY.register(
    Counter(
        "ai_sql_schema_compacted_total",
        "Schema 上下文因超出 Token 预算改用精简渲染的次数",
        (),
    )
)
SCHEMA_COLUMNS_DROPPED = REGISTRY.register(
    Counter(
        "ai_sql_schema_columns_dropped_total",
        "因 Token 预算按相关性丢弃的字段数（丢了什么见 retrieval.schema_budget_applied 日志）",
        (),
    )
)
RESULT_ANOMALIES = REGISTRY.register(
    Counter(
        "ai_sql_result_anomalies_total",
        "执行结果软异常：空结果 / 基数异常 / 语义不一致",
        ("anomaly", "severity"),
    )
)
CIRCUIT_STATE = REGISTRY.register(
    Gauge(
        "ai_sql_circuit_state",
        "模型调用熔断器状态：0=闭合 1=半开 2=断开",
        ("component",),
    )
)
CIRCUIT_TRIPS = REGISTRY.register(
    Counter("ai_sql_circuit_trips_total", "熔断器跳闸次数", ("component",))
)
DEGRADED_RESPONSES = REGISTRY.register(
    Counter(
        "ai_sql_degraded_responses_total",
        "模型不可用触发的降级响应",
        ("reason", "failed_stage"),
    )
)
SCHEMA_RELOADS = REGISTRY.register(
    Counter("ai_sql_schema_reloads_total", "Schema 索引重建次数", ("result",))
)
LIMIT_REJECTIONS = REGISTRY.register(
    Counter("ai_sql_limit_rejections_total", "限流/配额拒绝次数", ("reason",))
)
FEEDBACK_SUBMITTED = REGISTRY.register(
    Counter(
        "ai_sql_feedback_total",
        "用户对回答的判定（对/错/说不准）；had_risk 区分当时有没有给风险提示",
        ("verdict", "had_risk"),
    )
)
RUNS_TOTAL = REGISTRY.register(
    Counter(
        "ai_sql_runs_total",
        "结束的 run 数（entry：http_query / a2a_send / a2a_stream）",
        ("entry", "status"),
    )
)
RUN_EVENTS_DROPPED = REGISTRY.register(
    Counter(
        "ai_sql_run_events_dropped_total",
        "因存储故障没能落库的 run 事件数（查询本身不受影响）",
        (),
    )
)
RUN_CANCELLATIONS = REGISTRY.register(
    Counter("ai_sql_run_cancellations_total", "收到的取消请求", ("reason",))
)
RUN_CANCEL_LATENCY = REGISTRY.register(
    Histogram(
        "ai_sql_run_cancel_latency_seconds",
        "从收到取消到 run 真正停下的耗时（检查点的实际粒度）",
        ("reason",),
        buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 15.0, 30.0, 60.0, 120.0),
    )
)
BUILD_INFO = REGISTRY.register(Gauge("ai_sql_up", "服务存活标记"))
BUILD_INFO.set(1)


def render_metrics() -> str:
    return REGISTRY.render()
