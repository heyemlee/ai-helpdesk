import inspect

"""Monitor 告警阈值：默认值按真实模型延迟定，可用环境变量覆盖，路由惩罚与告警同一阈值。"""
import pytest

from monitor.performance_monitor import PerformanceMonitor, Severity


def _monitor() -> PerformanceMonitor:
    # __init__ 不碰 orchestrator / tool_manager，采集循环也不启动
    return PerformanceMonitor(orchestrator=None, tool_manager=None)


def test_default_agent_latency_threshold_fits_a_real_model():
    """3000ms 是夹具速度；真模型一轮 4~21s，那个阈值下八个实例全部常驻告警。"""
    threshold, severity, op = _monitor().THRESHOLDS["agent_avg_ms"]
    assert threshold >= 20000
    assert severity is Severity.WARNING and op == "greater_than"


def test_threshold_env_override(monkeypatch):
    monkeypatch.setenv("AI_HELPDESK_ALERT_AGENT_AVG_MS", "45000")
    m = _monitor()
    assert m.THRESHOLDS["agent_avg_ms"][0] == 45000.0
    assert PerformanceMonitor.THRESHOLDS["agent_avg_ms"][0] != 45000.0   # 类默认值不动


def test_bad_env_override_is_ignored(monkeypatch):
    monkeypatch.setenv("AI_HELPDESK_ALERT_AGENT_AVG_MS", "slow")
    assert _monitor().THRESHOLDS["agent_avg_ms"][0] == PerformanceMonitor.THRESHOLDS["agent_avg_ms"][0]


def test_routing_penalty_uses_the_same_latency_threshold(monkeypatch):
    """之前惩罚里写死 3000：告警阈值改了惩罚不跟着改，面板说正常、路由却还在降权。"""
    monkeypatch.setenv("AI_HELPDESK_ALERT_AGENT_AVG_MS", "10000")
    m = _monitor()
    assert m._routing_penalty(1.0, 9000) == 0.0
    assert m._routing_penalty(1.0, 14000) == pytest.approx(0.4)
    assert m._routing_penalty(1.0, 11000) == pytest.approx(0.1)


def test_healthy_real_model_latency_is_not_penalized():
    m = _monitor()
    for avg_ms in (3249.4, 7889.8, 15676.2, 20979.1):   # 实测的四个正常值
        assert m._routing_penalty(1.0, avg_ms) == 0.0


def test_check_threshold_appends_alert():
    m = _monitor()
    m._check_threshold("agent_success_rate", 0.5, "billing_0")
    assert len(m._alerts) == 1
    assert m._alerts[0].metric == "agent_success_rate:billing_0"
    m._check_threshold("agent_success_rate", 0.95, "billing_0")
    assert len(m._alerts) == 1


def test_busy_prometheus_port_does_not_kill_startup():
    """
    端口被占时 __init__ 必须活着回来。之前 start_http_server 的 OSError 一路冒到
    FastAPI lifespan，整个应用起不来——挂掉的只是个可选的监控侧车。
    """
    import socket

    # 必须绑 0.0.0.0：start_http_server 默认也绑 0.0.0.0，而 BSD 语义下
    # 0.0.0.0 与 127.0.0.1 算两个不同地址，两边都能绑上，撞不出冲突
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("0.0.0.0", 0))
    holder.listen(1)
    busy_port = holder.getsockname()[1]
    try:
        m = PerformanceMonitor(orchestrator=None, tool_manager=None, prometheus_port=busy_port)

        assert m.prometheus_error is not None          # 记下了原因，不是静默吞掉
        # 指标照常注册：API 端口上的 GET /metrics 读同一个全局 REGISTRY，
        # 独立 exporter 绑不上不该让它一起变空
        assert "agent_success_rate" in m._prom
    finally:
        holder.close()


def test_summary_reports_exporter_status():
    """/monitor 要能看出指标有没有在导出，而不是以为一切正常。"""
    m = _monitor()                                     # 不传端口 = 不启动这条路径
    m._orchestrator = type("O", (), {"get_stats": lambda self: {}})()
    m._tool_manager = type("T", (), {"get_stats": lambda self: {}})()

    prom = m.summary()["prometheus"]
    assert prom == {"exporter_ok": True, "error": None}


# ── 延迟直方图 ────────────────────────────────────────────────────────────────

def test_latency_buckets_cover_real_llm_latencies():
    """
    默认桶最大 10，而这个指标的单位是毫秒、真实值 6000~22000——每一条真实延迟
    都会落进 +Inf，histogram_quantile 算出来是 +Inf。面板上看着有线，其实没数据。
    """
    from core.metrics import AGENT_LATENCY_MS

    edges = [b for b in AGENT_LATENCY_MS._upper_bounds if b != float("inf")]
    assert max(edges) >= 45000, "上界要覆盖到 LLM_TIMEOUT_S 量级"
    # 真实观测集中在 6~22s，这一段必须有足够分辨率才算得出可用的 p95
    assert len([b for b in edges if 4000 <= b <= 24000]) >= 5


def test_monitor_does_not_resample_the_rolling_average():
    """
    原来 Monitor 每 10s observe 一次 stats.avg_ms：同一个请求被重复计入几十次，
    慢请求之后的快请求根本不会单独出现。延迟必须在请求结束时逐次记。
    """
    from monitor.performance_monitor import PerformanceMonitor

    src = inspect.getsource(PerformanceMonitor._collect)
    assert "agent_latency_ms" not in src, "Monitor 不该再自己观测延迟"


def test_each_request_is_recorded_once(orchestrator, monkeypatch):
    """_record 把本地统计和 Prometheus 观测收在一处，避免两边漂移。"""
    from agents import agent_orchestrator as ao
    from agents.agent_orchestrator import AgentType

    seen: list[tuple[str, float]] = []
    monkeypatch.setattr(ao, "record_agent_latency", lambda a, ms: seen.append((a, ms)))

    agent = orchestrator._pool[AgentType.GENERAL][0]
    before = agent.stats.total

    agent._record(ok=True, ms=8100.0)
    agent._record(ok=False, ms=15200.0)

    assert seen == [(agent.key, 8100.0), (agent.key, 15200.0)]
    assert agent.stats.total == before + 2
