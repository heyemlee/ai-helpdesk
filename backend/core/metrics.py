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
