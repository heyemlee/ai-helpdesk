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
from prometheus_client import Counter, Histogram

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

RATE_LIMIT_HITS = Counter(
    "rate_limit_hits_total",
    "被限流拒绝的请求数",
    ["user_id"],
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


def observe_http(method: str, path: str, status: int, duration_s: float) -> None:
    """HTTP 中间件出口调用。path 用路由模板（/tickets/{ticket_id}）控制基数。"""
    HTTP_REQUESTS.labels(method=method, path=path, status=str(status)).inc()
    HTTP_DURATION.labels(method=method, path=path).observe(duration_s)
