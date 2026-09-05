"""
Prometheus 业务指标（Grafana 面板的数据源）。

单独成模块的原因：api/main.py 在测试里会被反复 reimport（鉴权配置是
模块级读取的），如果指标定义在 main.py 里，第二次 import 会因为
重复注册同名时序直接抛错。core/metrics.py 只会被 Python 缓存一次。

面板对应关系（backend/config/grafana/dashboards/ai-helpdesk.json）：
  QPS / p95 延迟   ← http_requests_total / http_request_duration_seconds
  LLM token 花费   ← llm_tokens_total{agent, direction}
  限流命中         ← rate_limit_hits_total
"""
from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP 请求数",
    ["method", "path", "status"],
)

HTTP_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP 请求耗时",
    ["method", "path"],
    # /chat 的耗时由 LLM 决定，桶要铺到几十秒
    buckets=(0.005, 0.025, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 40.0, 80.0),
)

LLM_TOKENS = Counter(
    "llm_tokens_total",
    "LLM token 用量（成本核算）",
    ["agent", "direction"],  # direction: input / output
)

TICKET_NOTIFY_FAILURES = Counter(
    "ticket_notify_failures_total",
    "工单通知最终投递失败的次数（已重试到上限，或状态码表明重试无意义）。"
    "**这个数不为零意味着有客户被告知转人工、而值班的人没收到通知**——"
    "适合直接配成告警，不需要再看别的指标。",
)

TICKET_NOTIFY_PENDING = Gauge(
    "ticket_notify_pending",
    "待投递的工单通知条数。持续不降说明接收端在拒收或不可达；"
    "接收端恢复之后它会自己回到 0（后台补投循环会把积压捡回来）。",
)

# **不要给它加 user_id 标签。** 原来有，而 user_id 是无界的：每来一个新用户
# 就多一条时序，永不回收（Prometheus 的时序在 retention 到期前一直占内存和磁盘）。
# 而且被限流的往往正是刷接口的那一类调用方——它们制造的 user_id 最多。
#
# 需要知道"是谁被限流了"就去看日志：那一行带 user_id，而日志有轮转，指标没有。
# 面板上的表达式 `sum(rate(rate_limit_hits_total[5m]))` 本来就把标签聚掉了，
# 去掉标签不影响它。
RATE_LIMIT_HITS = Counter(
    "rate_limit_hits_total",
    "被限流拒绝的请求数",
)


# Agent 单次请求延迟。两处刻意和原来不同：
#
#  1. 桶显式给，不用 prometheus_client 的默认值。默认桶是给"秒级、亚秒为主"
#     的指标设计的，最大 10 —— 而这个指标的单位是毫秒，真实值 6000~22000。
#     于是每一条真实延迟都落进 +Inf，histogram_quantile 算出来是 +Inf，
#     p95 面板看着有线其实没有数据。
#  2. 在每次请求结束时观测**这一次**的延迟。原来是 Monitor 每 10s 观测一次
#     stats.avg_ms —— 那是"滚动平均的重采样"，不是延迟分布：同一个请求会被
#     重复计入几十次，而一个慢请求之后的快请求根本不会单独出现。
#     实测 technical_0 的 9 次真实观测 sum=135072，正好是 avg_ms 15008 的 9 倍。
#
# 上界取到 60000：LLM_TIMEOUT_S 默认 60，超过这个数请求已经被超时掐掉了。
AGENT_LATENCY_MS = Histogram(
    "agent_latency_ms",
    "Agent 单次请求延迟（毫秒）",
    ["agent"],
    buckets=(250, 500, 1000, 2000, 4000, 6000, 8000, 12000,
             16000, 20000, 30000, 45000, 60000, float("inf")),
)


def record_agent_latency(agent: str, latency_ms: float) -> None:
    """Agent 一次请求结束时记录延迟。成功失败都记——失败也占用了这段时间。"""
    AGENT_LATENCY_MS.labels(agent=agent).observe(latency_ms)


def record_llm_tokens(agent: str, input_tokens: int, output_tokens: int) -> None:
    """LLM 调用后记录 token 用量。usage 缺失时传 0，不抛错。"""
    if input_tokens:
        LLM_TOKENS.labels(agent=agent, direction="input").inc(input_tokens)
    if output_tokens:
        LLM_TOKENS.labels(agent=agent, direction="output").inc(output_tokens)


# 没匹配到路由时用的占位标签值。**不要退回原始 URL**——那是无界的：
# 任何扫描器打一串不存在的路径就能让时序数量无限增长，而这类流量恰恰最多。
# 路由模板（`/tickets/{ticket_id}`）是有界的，原始路径不是。
UNMATCHED_PATH = "<unmatched>"


def observe_http(method: str, path: str, status: int, duration_s: float) -> None:
    """HTTP 中间件出口调用。path 必须是**路由模板**，见 UNMATCHED_PATH。"""
    HTTP_REQUESTS.labels(method=method, path=path, status=str(status)).inc()
    HTTP_DURATION.labels(method=method, path=path).observe(duration_s)
