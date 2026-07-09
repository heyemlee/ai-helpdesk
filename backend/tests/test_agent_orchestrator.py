"""多 Agent 编排：领域打分路由、主+辅协作、澄清、置信度转人工、兜底降级。"""
import pytest
from conftest import make_request, stub_agents

from agents.agent_orchestrator import (
    SPECIALIST_AGENTS,
    AgentOrchestrator,
    AgentType,
)
from core.intent_recognizer import IntentCategory

# ── 专家阵容 ──────────────────────────────────────────────────────────────────

def test_three_specialists_plus_general_fallback(orchestrator):
    assert set(SPECIALIST_AGENTS) == {AgentType.TECHNICAL, AgentType.BILLING, AgentType.ORDER}
    for agent_type in SPECIALIST_AGENTS:
        assert orchestrator._pool[agent_type], f"{agent_type.value} 没有可用实例"
    assert orchestrator._pool[AgentType.GENERAL], "缺少兜底 Agent"


def test_handoff_is_not_a_pooled_agent(orchestrator):
    """
    转人工不该在 Agent 池里占一个空位。

    之前 primary_agent=ESCALATION 但池里没有该实例，_execute 会静默降级到
    GeneralAgent，响应里 agent_type=general 而 primary_agent=escalation 自相矛盾。
    """
    assert AgentType.HANDOFF not in orchestrator._pool


# ── 领域打分路由 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message,intent,expected", [
    ("订单什么时候发货", IntentCategory.ORDER_STATUS, AgentType.ORDER),
    ("物流一直不更新", IntentCategory.LOGISTICS, AgentType.ORDER),
    ("这个月被重复扣款了", IntentCategory.PAYMENT_ISSUE, AgentType.BILLING),
    ("帮我开发票", IntentCategory.INVOICE, AgentType.BILLING),
    ("App 一直崩溃", IntentCategory.TECHNICAL_CRASH, AgentType.TECHNICAL),
    ("无法登录，报401", IntentCategory.TECHNICAL_LOGIN, AgentType.TECHNICAL),
    ("你好", IntentCategory.GREETING, AgentType.GENERAL),
    ("想咨询一下会员权益", IntentCategory.QUERY, AgentType.GENERAL),
])
def test_domain_score_picks_primary_agent(orchestrator, message, intent, expected):
    decision = orchestrator._route_decision(make_request(message, intent))
    assert decision.primary_agent == expected


def test_domain_scores_are_bounded(orchestrator):
    """分数必须截断到 [0,1]，否则辅助 Agent 的相对门槛永远够不到。"""
    scores = orchestrator._domain_scores(
        make_request("无法登录 报401 一直崩溃 闪退 接口报错", IntentCategory.TECHNICAL_LOGIN)
    )
    assert all(0.0 <= v <= 1.0 for v in scores.values())


def test_cross_domain_promotes_supporting_agent(orchestrator):
    decision = orchestrator._route_decision(
        make_request("我登录不上，而且这个月被重复扣款了", IntentCategory.TECHNICAL_LOGIN)
    )

    assert decision.primary_agent == AgentType.TECHNICAL
    assert AgentType.BILLING in decision.supporting_agents
    assert decision.multi_agent is True


def test_single_domain_has_no_supporting_agents(orchestrator):
    decision = orchestrator._route_decision(
        make_request("帮我开发票", IntentCategory.INVOICE)
    )
    assert decision.supporting_agents == []


def test_general_never_becomes_a_supporting_agent(orchestrator):
    decision = orchestrator._route_decision(
        make_request("订单发货了吗，顺便问下会员权益", IntentCategory.ORDER_STATUS)
    )
    assert AgentType.GENERAL not in decision.supporting_agents


def test_entities_contribute_to_domain_score(orchestrator):
    with_code = orchestrator._domain_scores(
        make_request("这个接口挂了", IntentCategory.TECHNICAL, entities={"error_code": ["500"]})
    )
    without = orchestrator._domain_scores(
        make_request("这个接口挂了", IntentCategory.TECHNICAL)
    )
    assert with_code[AgentType.TECHNICAL] > without[AgentType.TECHNICAL]


def test_routing_reason_records_all_domain_scores(orchestrator):
    decision = orchestrator._route_decision(
        make_request("帮我开发票", IntentCategory.INVOICE)
    )
    for agent_type in SPECIALIST_AGENTS:
        assert agent_type.value in decision.reason


# ── 主 + 辅并行协作 ───────────────────────────────────────────────────────────

async def test_multi_agent_run_merges_primary_and_supporting(orchestrator):
    stub_agents(orchestrator)
    req = make_request("我登录不上，而且这个月被重复扣款了", IntentCategory.TECHNICAL_LOGIN)

    result = await orchestrator.run(req)

    assert result.primary_agent == AgentType.TECHNICAL
    assert result.supporting_agents == [AgentType.BILLING]
    assert "主处理" in result.response and "辅助处理" in result.response
    assert set(result.agent_types) == {AgentType.TECHNICAL, AgentType.BILLING}


# ── 低置信度澄清 ──────────────────────────────────────────────────────────────

async def test_low_confidence_other_intent_asks_for_clarification(orchestrator):
    stub_agents(orchestrator)
    req = make_request("那个东西怎么弄啊", IntentCategory.OTHER, confidence=0.2)

    result = await orchestrator.run(req)

    assert result.needs_clarification is True
    assert result.handoff is False
    assert "订单物流" in result.response


async def test_confident_intent_skips_clarification(orchestrator):
    stub_agents(orchestrator)
    req = make_request("帮我开发票", IntentCategory.INVOICE, confidence=0.9)

    result = await orchestrator.run(req)

    assert result.needs_clarification is False


# ── 转人工 ────────────────────────────────────────────────────────────────────

async def test_explicit_handoff_intent_skips_llm_agents(orchestrator):
    stub_agents(orchestrator, fail_types=tuple(orchestrator._pool))  # 任何 Agent 被调用都会炸
    req = make_request("转人工", IntentCategory.HUMAN_HANDOFF)

    result = await orchestrator.run(req)

    assert result.handoff is True
    assert result.escalated is True
    assert result.agent_type == AgentType.HANDOFF
    assert result.primary_agent == AgentType.HANDOFF


async def test_critical_urgency_goes_straight_to_human(orchestrator):
    from core.intent_recognizer import UrgencyLevel

    stub_agents(orchestrator)
    req = make_request("紧急！系统全挂了", IntentCategory.TECHNICAL, urgency=UrgencyLevel.CRITICAL)

    result = await orchestrator.run(req)

    assert result.handoff is True
    assert "CRITICAL" in result.handoff_reason


async def test_low_intent_confidence_triggers_handoff(orchestrator):
    """意图置信度低于阈值时，即使 Agent 正常回复也要转人工，不硬答。"""
    stub_agents(orchestrator)
    req = make_request("发票的事", IntentCategory.INVOICE, confidence=0.3)

    result = await orchestrator.run(req)

    assert result.handoff is True
    assert "意图置信度" in result.handoff_reason


async def test_each_signal_is_compared_against_its_own_threshold(orchestrator):
    """
    三路置信度量纲不同：意图分被投票权重压缩后天然偏低，路由领域分接近 1，
    Agent 自评从 1.0 起扣。拿 min() 跟同一个阈值比会把正常流量误转人工。
    """
    stub_agents(orchestrator)
    # 意图 0.5 高于意图阈值 0.45，虽然它是三者里最弱的一环，也不该转人工
    result = await orchestrator.run(
        make_request("帮我开发票", IntentCategory.INVOICE, confidence=0.5)
    )

    assert result.handoff is False
    assert result.answer_confidence == pytest.approx(0.5, abs=1e-6)


async def test_answer_confidence_reports_the_weakest_signal(orchestrator):
    stub_agents(orchestrator)
    req = make_request("帮我开发票", IntentCategory.INVOICE, confidence=0.62)

    result = await orchestrator.run(req)

    assert result.answer_confidence == pytest.approx(0.62, abs=1e-6)


async def test_handoff_keeps_the_answer_already_produced(orchestrator):
    stub_agents(orchestrator, reply="这里是已经生成的回答")
    req = make_request("发票的事", IntentCategory.INVOICE, confidence=0.3)

    result = await orchestrator.run(req)

    assert "这里是已经生成的回答" in result.response
    assert "人工客服" in result.response


# ── 兜底降级 ──────────────────────────────────────────────────────────────────

async def test_specialist_failure_falls_back_to_general(orchestrator):
    stub_agents(orchestrator, fail_types=(AgentType.BILLING,))
    req = make_request("帮我开发票", IntentCategory.INVOICE)

    result = await orchestrator.run(req)

    assert result.agent_type == AgentType.GENERAL
    assert result.primary_agent == AgentType.BILLING  # 路由决策本身没变
    assert "抱歉" not in result.response


async def test_total_failure_hands_off_to_human(orchestrator):
    stub_agents(orchestrator, fail_types=(AgentType.BILLING, AgentType.GENERAL))
    req = make_request("帮我开发票", IntentCategory.INVOICE)

    result = await orchestrator.run(req)

    assert result.handoff is True
    assert "均执行失败" in result.handoff_reason


# ── 答案策略接入 ──────────────────────────────────────────────────────────────

async def test_policy_violation_is_blocked_and_surfaced(orchestrator):
    stub_agents(orchestrator, reply="退款会在 3 个工作日内到账。")
    req = make_request("退款多久到账", IntentCategory.REFUND)

    result = await orchestrator.run(req)

    assert result.policy_violations
    assert "3 个工作日内到账" not in result.response
    assert orchestrator.policy.blocked_total == 1


async def test_sourced_commitment_passes_through(orchestrator):
    stub_agents(orchestrator, reply="退款审核通过后 7 个工作日内原路退回。")
    req = make_request(
        "退款多久到账",
        IntentCategory.REFUND,
        grounding_sources=["[知识库] 退款审核通过后 7 个工作日内原路退回。"],
    )

    result = await orchestrator.run(req)

    assert not result.policy_violations
    assert "7 个工作日内原路退回" in result.response


async def test_policy_violation_drags_confidence_toward_handoff(orchestrator):
    stub_agents(orchestrator, reply="退款 3 个工作日到账，费用 50 元马上退给你。")
    req = make_request("退款多久到账", IntentCategory.REFUND, confidence=0.8)

    result = await orchestrator.run(req)

    assert result.answer_confidence < 0.8


# ── 监控反馈 ──────────────────────────────────────────────────────────────────

def test_monitor_penalty_lowers_routing_score(orchestrator):
    agent = orchestrator._pool[AgentType.BILLING][0]
    agent.stats.total, agent.stats.success = 10, 10
    clean = agent.stats.routing_score()

    orchestrator.update_routing_penalties({"billing_0": 0.5})

    assert agent.stats.routing_score() < clean


def test_routing_config_snapshot(orchestrator):
    cfg = orchestrator.routing_config()

    assert cfg["specialists"] == [a.value for a in SPECIALIST_AGENTS]
    assert cfg["fallback_agent"] == AgentType.GENERAL.value
    assert cfg["intent"]["fine_grained_intents"] == 18


async def test_repeated_policy_violations_force_handoff(orchestrator):
    """一处无依据承诺先拦下改写；连着两处说明这轮回答整体不可信，直接转人工。"""
    stub_agents(
        orchestrator,
        reply="退款 3 个工作日到账。手续费一共 50 元，马上退给你。",
    )
    req = make_request("退款多久到账", IntentCategory.REFUND, confidence=0.9)

    result = await orchestrator.run(req)

    assert len(result.policy_violations) == 2
    assert result.handoff is True
    assert "答案策略拦截 2 处" in result.handoff_reason


# ── Agent 多实例与 Monitor 反馈闭环 ──────────────────────────────────────────

class TestAgentReplicas:
    """
    多实例存在的意义只有一个：让 Monitor 观测到的在线表现真正改变路由选择。
    单实例时 monitor_penalty 只是个数字，不会影响任何决策。
    """

    def test_pool_has_requested_replicas(self):
        orch = AgentOrchestrator(api_key="test-key", replicas=3)
        for agent_type in (AgentType.GENERAL, AgentType.TECHNICAL,
                           AgentType.BILLING, AgentType.ORDER):
            assert len(orch._pool[agent_type]) == 3
        keys = set(orch.get_stats())
        assert {"technical_0", "technical_1", "technical_2"} <= keys

    def test_replica_models_override(self):
        orch = AgentOrchestrator(
            api_key="test-key", model="base-model",
            replicas=2, replica_models=["strong-model", "cheap-model"],
        )
        assert orch._pool[AgentType.TECHNICAL][0].model == "strong-model"
        assert orch._pool[AgentType.TECHNICAL][1].model == "cheap-model"

    def test_replica_models_shorter_than_replicas_falls_back(self):
        orch = AgentOrchestrator(
            api_key="test-key", model="base-model",
            replicas=3, replica_models=["strong-model"],
        )
        models = [a.model for a in orch._pool[AgentType.BILLING]]
        assert models == ["strong-model", "base-model", "base-model"]

    def test_ties_round_robin_so_every_replica_gets_traffic(self):
        """冷启动时所有实例分数相同，必须轮询——否则 1 号实例永远没有统计。"""
        orch = AgentOrchestrator(api_key="test-key", replicas=2)
        picked = [orch._best_agent(AgentType.TECHNICAL).replica for _ in range(6)]
        assert set(picked) == {0, 1}, f"轮询失效，只选中了 {set(picked)}"

    def test_monitor_penalty_steers_traffic_away(self):
        """Monitor 判定 0 号实例劣化后，路由应当稳定倒向 1 号。"""
        orch = AgentOrchestrator(api_key="test-key", replicas=2)
        orch.update_routing_penalties({"technical_0": 0.8})

        picked = {orch._best_agent(AgentType.TECHNICAL).replica for _ in range(10)}
        assert picked == {1}, f"惩罚未生效，仍选中 {picked}"

    def test_single_replica_ignores_penalty(self):
        """单实例时没有可选项，惩罚不应该导致返回 None 而让请求无处可去。"""
        orch = AgentOrchestrator(api_key="test-key", replicas=1)
        orch.update_routing_penalties({"technical_0": 0.9})
        assert orch._best_agent(AgentType.TECHNICAL) is not None

    def test_penalty_is_clamped(self):
        orch = AgentOrchestrator(api_key="test-key", replicas=2)
        orch.update_routing_penalties({"technical_0": 5.0, "technical_1": -3.0})
        assert orch._pool[AgentType.TECHNICAL][0].stats.monitor_penalty == 0.9
        assert orch._pool[AgentType.TECHNICAL][1].stats.monitor_penalty == 0.0
