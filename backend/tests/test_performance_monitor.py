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
