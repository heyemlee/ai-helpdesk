"""多 Agent 编排：领域打分路由、主+辅协作、澄清、置信度转人工、兜底降级。"""
import pytest
from conftest import make_request, stub_agents

from agents.agent_orchestrator import (
    _HANDOFF_NOTICE,
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
    ("这批柜子什么时候做好", IntentCategory.ORDER_STATUS, AgentType.ORDER),
    ("送货一直没安排", IntentCategory.DELIVERY, AgentType.ORDER),
    ("尾款被重复扣款了", IntentCategory.PAYMENT_ISSUE, AgentType.BILLING),
    ("这套报价多少", IntentCategory.QUOTE_PRICING, AgentType.BILLING),
    ("门板送来就是裂的", IntentCategory.DAMAGE_DEFECT, AgentType.TECHNICAL),
    ("柜门关不严，铰链松了", IntentCategory.INSTALLATION, AgentType.TECHNICAL),
    ("你好", IntentCategory.GREETING, AgentType.GENERAL),
    ("想问一下你们保修多久", IntentCategory.QUERY, AgentType.GENERAL),
])
def test_domain_score_picks_primary_agent(orchestrator, message, intent, expected):
    decision = orchestrator._route_decision(make_request(message, intent))
    assert decision.primary_agent == expected


def test_domain_scores_are_bounded(orchestrator):
    """分数必须截断到 [0,1]，否则辅助 Agent 的相对门槛永远够不到。"""
    scores = orchestrator._domain_scores(
        make_request("关不严 对不齐 有划痕 开裂 起泡 需要返工", IntentCategory.INSTALLATION)
    )
    assert all(0.0 <= v <= 1.0 for v in scores.values())


def test_cross_domain_promotes_supporting_agent(orchestrator):
    decision = orchestrator._route_decision(
        make_request("门板有划痕，而且尾款还多收了", IntentCategory.DAMAGE_DEFECT)
    )

    assert decision.primary_agent == AgentType.TECHNICAL
    assert AgentType.BILLING in decision.supporting_agents
    assert decision.multi_agent is True


def test_single_domain_has_no_supporting_agents(orchestrator):
    decision = orchestrator._route_decision(
        make_request("这套报价多少", IntentCategory.QUOTE_PRICING)
    )
    assert decision.supporting_agents == []


def test_general_never_becomes_a_supporting_agent(orchestrator):
    decision = orchestrator._route_decision(
        make_request("订单排产了吗，顺便问下保修范围", IntentCategory.ORDER_STATUS)
    )
    assert AgentType.GENERAL not in decision.supporting_agents


def test_entities_contribute_to_domain_score(orchestrator):
    with_code = orchestrator._domain_scores(
        make_request("这块板子对不上", IntentCategory.TECHNICAL, entities={"item_code": ["W3012"]})
    )
    without = orchestrator._domain_scores(
        make_request("这块板子对不上", IntentCategory.TECHNICAL)
    )
    assert with_code[AgentType.TECHNICAL] > without[AgentType.TECHNICAL]


def test_routing_reason_records_all_domain_scores(orchestrator):
    decision = orchestrator._route_decision(
        make_request("这套报价多少", IntentCategory.QUOTE_PRICING)
    )
    for agent_type in SPECIALIST_AGENTS:
        assert agent_type.value in decision.reason


# ── 主 + 辅并行协作 ───────────────────────────────────────────────────────────

async def test_multi_agent_run_merges_primary_and_supporting(orchestrator):
    stub_agents(orchestrator)
    req = make_request("门板有划痕，而且尾款还多收了", IntentCategory.DAMAGE_DEFECT)

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
    assert "订单交付" in result.response


async def test_confident_intent_skips_clarification(orchestrator):
    stub_agents(orchestrator)
    req = make_request("这套报价多少", IntentCategory.QUOTE_PRICING, confidence=0.9)

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
    req = make_request("报价的事", IntentCategory.QUOTE_PRICING, confidence=0.3)

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
        make_request("这套报价多少", IntentCategory.QUOTE_PRICING, confidence=0.5)
    )

    assert result.handoff is False
    assert result.answer_confidence == pytest.approx(0.5, abs=1e-6)


async def test_answer_confidence_reports_the_weakest_signal(orchestrator):
    stub_agents(orchestrator)
    req = make_request("这套报价多少", IntentCategory.QUOTE_PRICING, confidence=0.62)

    result = await orchestrator.run(req)

    assert result.answer_confidence == pytest.approx(0.62, abs=1e-6)


async def test_handoff_drops_the_draft_follow_up_questions(orchestrator):
    """
    已经建单转人工，就不该再让用户补资料。

    低置信度那一轮的草稿常常正好是一句追问（"具体想咨询什么内容呢？"），
    直接拼上转人工文案就是一条自相矛盾的回复：前半段让用户补充信息，
    后半段说已经转交人工——用户照着回复也没人接。
    """
    stub_agents(orchestrator, reply=(
        "我这边负责订单与交付。\n"
        "如果您已经下单，欢迎告诉我：\n"
        "- 订单号或柜号\n"
        "- 具体想咨询的内容\n"
        "需要我协助什么呢？"
    ))
    req = make_request("报价的事", IntentCategory.QUOTE_PRICING, confidence=0.3)

    result = await orchestrator.run(req)

    assert result.handoff is True
    assert _HANDOFF_NOTICE in result.response

    body = result.response.split(_HANDOFF_NOTICE)[0]
    assert "我这边负责订单与交付。" in body                  # 有信息量的部分保留
    for ask in ("欢迎告诉我", "订单号或柜号", "具体想咨询的内容", "需要我协助什么呢"):
        assert ask not in body, f"转人工后仍在向用户索取信息: {ask}"


async def test_handoff_drops_a_bare_checklist_of_requested_documents(orchestrator):
    """
    没有引导句、直接甩出来的待补资料清单也要带走。

    评测里 dialog_6（"尾款被扣了两次"）的真实回复就是这个形状：解释完直接跟
    三条 markdown 清单，最后一句"请先提供订单号"。第一版过滤只认
    "……欢迎告诉我："这种冒号引导的清单和"请提供"的原型说法，两处都漏了。

    同时守住反向的边界：行首的 `**加粗**` 不是列表项，那一行的内容要保留。
    """
    stub_agents(orchestrator, reply=(
        "这种情况我需要先核对系统账目，不能凭描述直接判断。\n"
        "\n"
        "- **订单号或合同号**（必须，用来定位账目）\n"
        "- 两次扣款分别的时间、金额、支付方式\n"
        "\n"
        "**如果核实确实是重复扣款**，属于实际退款，必须转人工财务审核处理。\n"
        "\n"
        "请先提供订单号，我帮您先做初步核对。"
    ))
    req = make_request("尾款被扣了两次", IntentCategory.PAYMENT_ISSUE, confidence=0.3)

    result = await orchestrator.run(req)
    body = result.response.split(_HANDOFF_NOTICE)[0]

    assert "必须转人工财务审核处理" in body        # 加粗开头的整句不能被当成列表项
    assert "核对系统账目" in body
    for ask in ("订单号或合同号", "支付方式", "请先提供"):
        assert ask not in body, f"转人工后仍在向用户索取信息: {ask}"


async def test_handoff_keeps_an_explanatory_list(orchestrator):
    """
    反向边界：不是所有清单都是在要资料。

    "清单一律带走"这条规则把破损判定的三条可能原因也吃了，后面那句
    "这三种情况责任方不同"就成了没有指代的悬空句——评测分数从 0.86 掉到 0.60
    就是这么来的。判据是条目里有没有资料名词，不是有没有清单。
    """
    stub_agents(orchestrator, reply=(
        "先按破损/开裂登记处理。\n"
        "\n"
        "**可能原因**\n"
        "- 出厂前已存在裂纹（工厂生产环节问题）\n"
        "- 运输途中磕碰导致开裂（运输损）\n"
        "\n"
        "这两种情况责任方不同，需要工厂质检判定。\n"
        "\n"
        "**后续流程**\n"
        "\n"
        "烦请先补一下订单号和照片。"
    ))
    req = make_request("门板送来就是裂的", IntentCategory.DAMAGE_DEFECT, confidence=0.3)

    result = await orchestrator.run(req)
    body = result.response.split(_HANDOFF_NOTICE)[0]

    assert "出厂前已存在裂纹" in body                 # 说明性清单是回答主体，必须留
    assert "运输途中磕碰" in body
    assert "这两种情况责任方不同" in body
    assert "烦请先补一下" not in body                 # 索取句照样摘掉
    assert "后续流程" not in body                     # 下面空了的小标题跟着走


async def test_handoff_does_not_leave_a_heading_with_nothing_under_it(orchestrator):
    """摘掉追问后，"**下一步说明：**"这种冒号标题下面就空了，标题也得跟着走。"""
    stub_agents(orchestrator, reply=(
        "这个问题涉及账目核对，我不能直接给结论。\n"
        "\n"
        "**下一步说明：**\n"
        "\n"
        "请您提供订单号和两笔扣款截图。"
    ))
    req = make_request("尾款被扣了两次", IntentCategory.PAYMENT_ISSUE, confidence=0.3)

    result = await orchestrator.run(req)
    body = result.response.split(_HANDOFF_NOTICE)[0]

    assert "我不能直接给结论" in body
    assert "下一步说明" not in body
    assert body.rstrip().endswith("。")


async def test_handoff_sends_only_the_notice_when_the_draft_was_all_questions(orchestrator):
    """整段草稿都是追问时，转人工文案是唯一该发出去的内容。"""
    stub_agents(orchestrator, reply="请问您具体想咨询什么内容呢？")
    req = make_request("报价的事", IntentCategory.QUOTE_PRICING, confidence=0.3)

    result = await orchestrator.run(req)

    assert result.response == _HANDOFF_NOTICE


async def test_handoff_keeps_the_answer_already_produced(orchestrator):
    stub_agents(orchestrator, reply="这里是已经生成的回答")
    req = make_request("报价的事", IntentCategory.QUOTE_PRICING, confidence=0.3)

    result = await orchestrator.run(req)

    assert "这里是已经生成的回答" in result.response
    assert "人工客服" in result.response


# ── 兜底降级 ──────────────────────────────────────────────────────────────────

async def test_specialist_failure_falls_back_to_general(orchestrator):
    stub_agents(orchestrator, fail_types=(AgentType.BILLING,))
    req = make_request("这套报价多少", IntentCategory.QUOTE_PRICING)

    result = await orchestrator.run(req)

    assert result.agent_type == AgentType.GENERAL
    assert result.primary_agent == AgentType.BILLING  # 路由决策本身没变
    assert "抱歉" not in result.response


async def test_total_failure_hands_off_to_human(orchestrator):
    stub_agents(orchestrator, fail_types=(AgentType.BILLING, AgentType.GENERAL))
    req = make_request("这套报价多少", IntentCategory.QUOTE_PRICING)

    result = await orchestrator.run(req)

    assert result.handoff is True
    assert "均执行失败" in result.handoff_reason


# ── 答案策略接入 ──────────────────────────────────────────────────────────────

async def test_policy_violation_is_blocked_and_surfaced(orchestrator):
    stub_agents(orchestrator, reply="补件会在 3 个工作日内送到。")
    req = make_request("补件多久能到", IntentCategory.MISSING_PART)

    result = await orchestrator.run(req)

    assert result.policy_violations
    assert "3 个工作日内送到" not in result.response
    assert orchestrator.policy.blocked_total == 1


async def test_sourced_commitment_passes_through(orchestrator):
    stub_agents(orchestrator, reply="补件确认后 7 个工作日内从工厂发出。")
    req = make_request(
        "补件多久能到",
        IntentCategory.MISSING_PART,
        grounding_sources=["[知识库] 补件确认后 7 个工作日内从工厂发出。"],
    )

    result = await orchestrator.run(req)

    assert not result.policy_violations
    assert "7 个工作日内从工厂发出" in result.response


async def test_policy_violation_drags_confidence_toward_handoff(orchestrator):
    stub_agents(orchestrator, reply="补件 3 个工作日送到，运费 50 元马上退给你。")
    req = make_request("补件多久能到", IntentCategory.MISSING_PART, confidence=0.8)

    result = await orchestrator.run(req)

    assert result.answer_confidence < 0.8


# ── 监控反馈 ──────────────────────────────────────────────────────────────────

async def test_in_flight_request_does_not_dilute_success_rate(orchestrator):
    """
    total 必须和 success 同时记，不能在请求开始时先 +1。

    真模型一轮 8~20s，Monitor 每 10s 采一次：首个请求在途时 success_rate 曾是
    0/1 = 0.000，被当成 ERROR 告警并记 0.5 路由惩罚——一个从没失败过的实例。
    """
    import asyncio
    agent = orchestrator._pool[AgentType.BILLING][0]
    gate = asyncio.Event()

    async def _slow(req, skill_prompt=""):
        await gate.wait()
        return "[billing] 好的"

    agent._call_llm = _slow
    task = asyncio.create_task(agent.handle(make_request("报价", IntentCategory.QUOTE_PRICING)))
    await asyncio.sleep(0)            # 让 handle 跑到 await _call_llm

    assert agent.stats.total == 0     # 在途：什么都没记
    assert agent.stats.success_rate == 1.0
    assert agent.stats.routing_score() == pytest.approx(1.0)

    gate.set()
    await task
    assert (agent.stats.total, agent.stats.success) == (1, 1)


async def test_failed_request_counts_once_as_failure(orchestrator):
    stub_agents(orchestrator, fail_types=(AgentType.BILLING,))
    agent = orchestrator._pool[AgentType.BILLING][0]
    resp = await agent.handle(make_request("报价", IntentCategory.QUOTE_PRICING))
    assert resp.success is False
    assert (agent.stats.total, agent.stats.success) == (1, 0)
    assert agent.stats.success_rate == 0.0


async def test_stream_records_stats_only_at_completion(orchestrator):
    import asyncio
    agent = orchestrator._pool[AgentType.ORDER][0]
    gate = asyncio.Event()

    async def _slow_stream(req, skill_prompt=""):
        yield "第一句。"
        await gate.wait()
        yield "第二句。"

    agent._stream_llm = _slow_stream
    events = []

    async def _consume():
        async for evt in agent.stream(make_request("进度", IntentCategory.ORDER_STATUS)):
            events.append(evt)

    task = asyncio.create_task(_consume())
    for _ in range(5):
        await asyncio.sleep(0)
    assert agent.stats.total == 0     # 首句已经吐出去了，统计仍未动

    gate.set()
    await task
    assert events[-1]["type"] == "end"
    assert (agent.stats.total, agent.stats.success) == (1, 1)


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
        reply="补件 3 个工作日送到。运费一共 50 元，马上退给你。",
    )
    req = make_request("补件多久能到", IntentCategory.MISSING_PART, confidence=0.9)

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
