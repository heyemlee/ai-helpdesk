"""
Prometheus 业务指标（Grafana 面板的数据源）。

单独成模块的原因：api/main.py 在测试里会被反复 reimport（鉴权配置是
模块级读取的），如果指标定义在 main.py 里，第二次 import 会因为
重复注册同名时序直接抛错。core/metrics.py 只会被 Python 缓存一次。

面板对应关系（deploy/grafana/dashboards/ai-helpdesk.json）：
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


# ── 工具循环（agents/tool_loop.py）────────────────────────────────────────────
#
# 开关打开之后看这几条：作答多花了几次模型调用、工具调用怎么收场、多常走到预算尽头、
# 多常因为网关不认工具退回单次生成。标签都有界：tool 只有本系统注册的那几个，模型
# 编出来的工具名一律记成 <unknown>，不让它把时序数撑开；outcome 是代码里的固定集合。
KNOWN_LOOP_TOOLS = frozenset({"knowledge_search", "order_data_query"})

AGENT_LOOP_MODEL_CALLS = Histogram(
    "agent_loop_model_calls",
    "工具循环里一个专家一轮作答用掉的模型调用次数",
    ["agent"],
    buckets=(1, 2, 3, 4, 5, 6, float("inf")),
)

AGENT_LOOP_TOOL_CALLS = Counter(
    "agent_loop_tool_calls_total",
    "工具循环里模型发起的工具调用，按工具和结局分；dedup 表示复用了本轮之前的结果",
    ["tool", "outcome"],
)

AGENT_LOOP_FORCED = Counter(
    "agent_loop_forced_total",
    "工具循环走到预算尽头、最后一步被要求直接作答的次数：model_calls / tool_calls / deadline",
    ["reason"],
)

AGENT_LOOP_DISCARDED = Counter(
    "agent_loop_discarded_preamble_total",
    "模型调工具前写了半句、被作废的次数。持续上升说明提示词里'调工具前不要先写回答'没管住",
    ["agent"],
)

AGENT_LOOP_FALLBACK = Counter(
    "agent_loop_fallback_total",
    "工具循环退回单次生成的次数。tools_unsupported：网关明确拒绝了工具调用",
    ["reason"],
)


def record_loop_tool(tool: str, outcome: str, dedup: bool = False) -> None:
    """工具循环里一次工具调用收场。"""
    label = tool if tool in KNOWN_LOOP_TOOLS else "<unknown>"
    AGENT_LOOP_TOOL_CALLS.labels(tool=label, outcome="dedup" if dedup else (outcome or "unknown")).inc()


def record_loop_end(agent: str, model_calls: int, forced: str) -> None:
    """一个专家的工具循环结束。forced 为空说明模型在预算内自己停下了。"""
    AGENT_LOOP_MODEL_CALLS.labels(agent=agent).observe(model_calls)
    if forced:
        AGENT_LOOP_FORCED.labels(reason=forced).inc()


def record_loop_discard(agent: str) -> None:
    AGENT_LOOP_DISCARDED.labels(agent=agent).inc()


def record_loop_fallback(reason: str) -> None:
    AGENT_LOOP_FALLBACK.labels(reason=reason).inc()


# ── 事件日志（app/events/）──────────────────────────────────────────────────────
#
# 写不进去时回答不受影响，日志会缺：靠这几条和 /health 的 event_log 发现。dropped 不为零、
# 队列深度持续不降，都说明库那边出了问题。reason 是固定集合：queue_full（队列满了）、
# db_error（重试用尽）、too_large（data 超过上限）、serialize（序列化失败）、shutdown（停机时
# 没写完）。
TURN_EVENTS_WRITTEN = Counter(
    "turn_events_written_total",
    "写进事件日志的事件条数",
)

TURN_EVENTS_DROPPED = Counter(
    "turn_events_dropped_total",
    "没写进事件日志、丢掉的事件条数，按原因分",
    ["reason"],
)

TURN_EVENTS_QUEUE_DEPTH = Gauge(
    "turn_events_queue_depth",
    "事件日志写入队列里还没写的条数",
)

TURN_EVENTS_WRITE_SECONDS = Histogram(
    "turn_events_write_seconds",
    "事件日志一批写入的耗时",
    buckets=(0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)

TURN_PROJECTION_MISMATCH = Counter(
    "turn_projection_mismatch_total",
    "一轮结束时，从事件投影出来的结果和响应对不上的次数（影子校验）。不为零说明有人改了"
    "响应字段或事件，忘了另一边",
)

TURNS_CONCURRENT = Counter(
    "turns_concurrent_total",
    "开始时同一会话里已经有另一轮在途的轮次数。后端没有串行化同一会话，"
    "这个数决定要不要加会话锁",
)


# ── 可恢复的运行（app/runs/）和模型准入（core/admission.py）────────────────────
#
# 排队模式（AI_HELPDESK_RUN_MODE=queued）下一轮对话是一个 run：API 落库、派发，worker 领走、
# 执行、写结果。这几条回答三个问题：排了多久、第一句话多久出来、出过多少次"换人接着跑"。
# 标签都是固定集合：status 是 run 的终态，kind 是这一次执行是怎么来的，level 是降级档位。
RUNS_SUBMITTED = Counter(
    "agent_runs_submitted_total",
    "提交的 run：accepted（新建）、deduplicated（同一个 Idempotency-Key 又来了一次）、"
    "rejected（积压超过上限，回了 429）",
    ["outcome"],
)

RUN_QUEUE_WAIT = Histogram(
    "agent_run_queue_wait_seconds",
    "run 从提交到第一次被 worker 领走等了多久",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
)

FIRST_DELTA_SECONDS = Histogram(
    "chat_first_delta_seconds",
    "一轮从开始到发出第一句回答用了多久。mode=inline 从请求进来算，mode=queued 从 run 提交算"
    "（含排队）：两种模式比的就是排队多花的那一截",
    ["mode"],
    buckets=(0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 20.0, 30.0, 45.0, 60.0, 90.0),
)

RUN_DURATION = Histogram(
    "agent_run_duration_seconds",
    "run 从提交到终态用了多久，按终态分",
    ["status"],
    buckets=(0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 45.0, 60.0, 90.0, 120.0, 300.0),
)

RUN_ATTEMPTS = Counter(
    "agent_run_attempts_total",
    "run 的执行次数，按来路分：first（第一次）、reclaimed（原来的 worker 失联，被别人接手）",
    ["kind"],
)

RUNS_FINISHED = Counter(
    "agent_runs_finished_total",
    "到达终态的 run：succeeded / failed（这一轮自己出错，不重试）/ dead（反复失联，进死信）",
    ["status"],
)

RUN_LEASE_LOST = Counter(
    "agent_run_lease_lost_total",
    "worker 续租时发现租约已经不是自己的了（被判失联、别人接手）。这一次执行的结果作废",
)

RUN_BACKLOG = Gauge(
    "agent_run_backlog",
    "派发队列里还没跑完的 run：没被领走的加上领走了还没确认的。KEDA 按同一个数扩缩 worker",
    ["instance"],
)

RUNS_IN_FLIGHT = Gauge(
    "agent_runs_in_flight",
    "这个 worker 进程手上正在跑的 run",
)

RUN_DEGRADED = Counter(
    "agent_run_degraded_total",
    "按积压降级执行的 run，按档位分：1 关工具循环，2 不改写不重排，3 不委派查库，4 不出网",
    ["level"],
)

LLM_ADMISSION_WAIT = Histogram(
    "llm_admission_wait_seconds",
    "一次模型调用在本地等令牌等了多久（令牌桶没开时不记）",
    buckets=(0.0, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 40.0),
)

LLM_ADMISSION_REJECTED = Counter(
    "llm_admission_rejected_total",
    "等令牌超过上限、在本地判过载、没有出网的模型调用次数",
)
