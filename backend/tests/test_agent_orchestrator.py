"""多 Agent 编排：领域打分路由、主+辅协作、澄清、置信度转人工、兜底降级。"""
import pytest
from conftest import make_request, stub_agents

from app.agents.agent_orchestrator import (
    _HANDOFF_NOTICE,
    SPECIALIST_AGENTS,
    AgentOrchestrator,
    AgentType,
)
from app.core.intent_recognizer import IntentCategory

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
    from app.core.intent_recognizer import UrgencyLevel

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


# ── 双语作答 ──────────────────────────────────────────────────────────────────

def test_system_prompt_pins_reply_language_to_the_customer(orchestrator):
    """
    在此之前系统提示词里一条语言指令都没有，中文回复只是模型顺着中文 SOP 和
    中文脚手架走出来的结果——是巧合不是契约。
    """
    for agents in orchestrator._pool.values():
        prompt = agents[0]._build_system_prompt()
        assert "[回复语言]" in prompt
        assert "客户提问所用的语言" in prompt


def test_language_rule_survives_dynamic_skills(orchestrator):
    """Skill SOP 是拼在语言规则之后的，别把规则挤掉。"""
    agent = orchestrator._pool[AgentType.BILLING][0]
    prompt = agent._build_system_prompt("【报价SOP】不得口头承诺单价")
    assert "[回复语言]" in prompt and "【报价SOP】" in prompt


# ── 提示词缓存 ────────────────────────────────────────────────────────────────

def test_system_prompt_is_marked_cacheable(orchestrator):
    """
    缓存标记必须落在 system 上。

    system = 角色提示词 + 语言规则 + SKILL.md，同一个 Agent 在同一份 SOP 下
    每次都一样；messages 里装的是记忆、检索出处和用户这句话，每次都不一样。
    标记挪到 messages 那侧（顶层 cache_control 的默认行为就是这样）等于每次
    写一份再也命中不了的新缓存，还要按 1.25 倍写入价付钱。
    """
    agent = orchestrator._pool[AgentType.BILLING][0]
    blocks = agent._system_blocks("【报价SOP】不得口头承诺单价")

    assert [b["type"] for b in blocks] == ["text"]
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    # 缓存的就是那份完整 system prompt，不是被截过的片段
    assert blocks[0]["text"] == agent._build_system_prompt("【报价SOP】不得口头承诺单价")


async def test_both_call_paths_send_the_same_cached_system(orchestrator):
    """
    流式和非流式必须送同一份 system。

    两条链路的上下文一旦漂移，表现是"同一个问题流式和非流式给出不同答案"；
    放到缓存上还多一层代价——两份只差一个字节的前缀会各占一份缓存，命中率减半。
    """
    agent = orchestrator._pool[AgentType.TECHNICAL][0]
    sent: list = []

    class _Stream:
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False
        @property
        def text_stream(self): return self._iter()
        async def _iter(self):
            yield "ok"
        async def get_final_message(self): return type("M", (), {"usage": None})()

    class _Messages:
        async def create(self, **kwargs):
            sent.append(kwargs["system"])
            return type("R", (), {"content": [type("B", (), {"type": "text", "text": "ok"})()],
                                  "usage": None, "stop_reason": "end_turn"})()

        def stream(self, **kwargs):
            sent.append(kwargs["system"])
            return _Stream()

    agent._client = type("C", (), {"messages": _Messages()})()
    agent._skill_manager = None

    req = make_request("柜门关不严", IntentCategory.INSTALLATION)
    await agent._call_llm(req)
    async for _ in agent._stream_llm(req):
        pass

    assert len(sent) == 2
    assert sent[0] == sent[1]
    assert sent[0][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.parametrize("content", [
    "I'll hand this over to our project team.",
    "Let me connect you with a specialist who can help.",
    "This is beyond what I can confirm here — our team will follow up.",
    "我帮你转人工客服。",
])
def test_escalation_is_detected_in_both_languages(orchestrator, content):
    """
    回复语言跟客户走之后，英文回复说"要转人工"用的是 hand this over /
    connect you with 这类说法，一个中文关键词都不含。漏检的后果是
    escalate 恒为 False：不扣自评置信度、不建工单，说了转人工却没转。
    """
    assert orchestrator._pool[AgentType.GENERAL][0]._needs_escalation(content) is True


def test_normal_answers_are_not_mistaken_for_escalation(orchestrator):
    agent = orchestrator._pool[AgentType.TECHNICAL][0]
    assert agent._needs_escalation("Here are the three hinge adjustment screws.") is False
    assert agent._needs_escalation("铰链上有三颗调节螺丝，先调左右那颗。") is False


def test_scaffolding_follows_the_customer_language(orchestrator):
    """
    _build_messages 注入的那两轮是**伪造的助手发言**。写死中文等于让模型看见
    自己刚用中文说过两句话，再用系统提示词要求它用英文回答英文客户——
    拿指令去对抗上下文。这里让脚手架跟客户走，不制造这个对抗。
    """
    req = make_request("The door arrived damaged.", IntentCategory.DAMAGE_DEFECT,
                       entities={"item_code": ["W3012"]})
    req.context = "客户上一轮提到订单 SO-2026-0912。"

    messages = orchestrator._pool[AgentType.TECHNICAL][0]._build_messages(req)
    spoken = [m["content"] for m in messages if m["role"] == "assistant"]

    assert spoken and all("好的" not in s for s in spoken), f"英文对话里出现中文助手发言: {spoken}"
    assert any("Understood" in s for s in spoken)
    # 注入的资料保持原样：那是依据的语言，不是回复的语言
    assert any("SO-2026-0912" in m["content"] for m in messages)
    assert any("客户上一轮" in m["content"] for m in messages)


def test_chinese_question_keeps_chinese_scaffolding(orchestrator):
    req = make_request("送来的门板有划痕。", IntentCategory.DAMAGE_DEFECT)
    req.context = "客户上一轮提到订单 SO-2026-0912。"

    messages = orchestrator._pool[AgentType.TECHNICAL][0]._build_messages(req)
    spoken = [m["content"] for m in messages if m["role"] == "assistant"]

    assert any("好的" in s for s in spoken)
    assert all("Understood" not in s for s in spoken)


@pytest.mark.parametrize("message,forbidden,expected", [
    ("The door arrived damaged.", "转交人工客服", "passed this to a human colleague"),
    ("送来的门板有划痕。", "human colleague", "转交人工客服"),
])
def test_handoff_notice_follows_the_customer_language(
    orchestrator, message, forbidden, expected,
):
    """
    转人工通知是客户读的正文。写死中文时，一整段英文回答的结尾会突然接上
    一段中文——实测就是这样。
    """
    from app.agents.agent_orchestrator import _customer_text

    assert forbidden not in _customer_text(message, "handoff_notice")
    assert expected in _customer_text(message, "handoff_notice")


@pytest.mark.parametrize("key", [
    "handoff_notice", "handoff_after_ask", "agent_error",
    "all_failed", "unavailable", "clarify", "role_primary", "role_supporting",
])
def test_every_customer_string_exists_in_both_languages(key):
    """漏一条就是一段中文掉进英文回复里，加新文案时这条会拦住。"""
    from app.agents.agent_orchestrator import _CUSTOMER_TEXT

    for lang in ("zh", "en"):
        assert _CUSTOMER_TEXT[lang].get(key), f"{lang} 缺少文案 {key}"


def test_english_asks_are_stripped_before_the_handoff_notice():
    """
    转人工前要摘掉"让客户补资料"的表述，否则同一条回复一边要资料、一边说
    已经转交人工。_ASK_RE 原本一条英文规则都没有，英文回复必然自相矛盾。
    """
    from app.agents.agent_orchestrator import _strip_customer_asks

    answer = (
        "A deep scratch is usually a transit issue.\n"
        "Could you send me photos of the damage?\n"
        "Please provide your order number as well.\n"
        "We will classify it once QC reviews the case."
    )
    body, asks = _strip_customer_asks(answer)

    assert len(asks) == 2, f"英文索取表述没被识别: {asks}"
    assert "photos of the damage" not in body
    assert "order number" not in body
    assert "transit issue" in body and "QC reviews" in body


# ── 升级检测覆盖率语料 ────────────────────────────────────────────────────────
#
# 关键词匹配天生脆。固定成语料至少让"英文那半又退化了"这件事会被测出来，
# 而不是等到 escalate 恒为 False、工单不建、置信度不扣，从面板上看不出来。

_ESCALATION_PHRASINGS = [
    "I'll hand this over to our project team.",
    "Let me connect you with a specialist who can help.",
    "This is beyond what I can confirm here — our team will follow up.",
    "I'm escalating this to our operations team.",
    "I can't resolve this from here, so I'm referring you to our service desk.",
    "I'll transfer you to a human agent.",
    "我帮你转人工客服。",
    "这个我处理不了，需要转人工。",
    "这超出我能确认的范围，我先转交人工客服。",
]

_NOT_ESCALATION = [
    "Here are the three hinge adjustment screws.",
    "Turnaround is typically 4-6 weeks, subject to factory scheduling.",
    "The showroom is open Monday through Friday.",
    "铰链上有三颗调节螺丝，先调左右那颗。",
    "门缝正常应为 2-3mm，且上下一致。",
]


@pytest.mark.parametrize("content", _ESCALATION_PHRASINGS)
def test_escalation_corpus_is_detected(orchestrator, content):
    agent = orchestrator._pool[AgentType.GENERAL][0]
    assert agent._needs_escalation(content) is True, f"漏检: {content}"


@pytest.mark.parametrize("content", _NOT_ESCALATION)
def test_normal_answers_stay_out_of_escalation(orchestrator, content):
    agent = orchestrator._pool[AgentType.GENERAL][0]
    assert agent._needs_escalation(content) is False, f"误检: {content}"


# ── 「提到人工」≠「需要人工」 ────────────────────────────────────────────────
#
# 实测过的误判：Agent 查到真实数据、算对了欠款，收尾加了一句「如果您有异议，需要
# 转人工财务审核」。「转人工」命中关键词 → 自评扣到 0.65 → 真的开工单转人工，还把
# Agent 的追问删了。一个好答案被系统自己改坏。

@pytest.mark.parametrize("content", [
    "如果您对这个数字有异议，需要转人工财务审核后再确认。",
    "有任何问题随时联系人工客服。",
    "If you'd like, I can connect you with a specialist.",
    "Feel free to reach our team will if anything looks off.",
])
def test_offering_a_human_is_not_escalating(orchestrator, content):
    """条件句里提到人工是把话说周全，不是宣布自己不行——有没有数据都不该算。"""
    agent = orchestrator._pool[AgentType.GENERAL][0]
    assert agent._needs_escalation(content, grounded=False) is False
    assert agent._needs_escalation(content, grounded=True) is False


def test_bare_mention_counts_only_without_grounding(orchestrator):
    """
    光秃秃一句「请联系人工客服」：没有数据支撑时它就是在推卸，该转；
    有数据支撑时它已经证明了能答，提一句人工只是收尾。
    """
    agent = orchestrator._pool[AgentType.GENERAL][0]
    bare = "这个金额以系统为准。请联系人工客服。"
    assert agent._needs_escalation(bare, grounded=False) is True
    assert agent._needs_escalation(bare, grounded=True) is False


@pytest.mark.parametrize("content", [
    "订单查到了。取消需要人工审批，我帮您转人工。",
    "I found the order. Cancellation needs approval, so I'll hand this over to our team.",
    "这超出我能确认的范围，我先转交人工客服。",
    "这个我处理不了。",
])
def test_a_real_decision_escalates_even_when_grounded(orchestrator, content):
    """
    「能答就答」不能绝对化。客户要取消订单，Agent 查到了订单（有数据），
    然后明确说「我帮您转」——这必须转。豁免只放过「提到人工」，不放过「决定转」
    和「做不到」。这条边界不能省。
    """
    agent = orchestrator._pool[AgentType.GENERAL][0]
    assert agent._needs_escalation(content, grounded=True) is True


def test_a_conditional_decision_is_still_waiting(orchestrator):
    """「如果您确认要取消，我帮您转人工」——还在等客户确认，这一轮没转。"""
    agent = orchestrator._pool[AgentType.GENERAL][0]
    assert agent._needs_escalation("如果您确认要取消，我帮您转人工。", grounded=True) is False
    assert agent._needs_escalation("If you confirm, I'll hand this over.", grounded=False) is False


# ── Agent 自己声明转人工 ─────────────────────────────────────────────────────
#
# 第二层：不再从回答的文字里猜，让 Agent 在最后一行写 [[handoff:yes|no]]。
# 关键词三档退为兜底。标记不能漏给客户，流式和非流式都不能。

def test_trailer_is_stripped_and_parsed(orchestrator):
    agent = orchestrator._pool[AgentType.GENERAL][0]
    text, decided = agent._split_handoff_trailer("已经帮您查到了。\n[[handoff:no]]")
    assert text == "已经帮您查到了。"
    assert decided is False
    text, decided = agent._split_handoff_trailer("这个需要人工审批。\n\n[[ HANDOFF : YES ]]  ")
    assert text == "这个需要人工审批。"
    assert decided is True


def test_missing_trailer_returns_none(orchestrator):
    agent = orchestrator._pool[AgentType.GENERAL][0]
    text, decided = agent._split_handoff_trailer("普通回答，没有标记。")
    assert text == "普通回答，没有标记。"
    assert decided is None


def test_declaration_beats_keywords(orchestrator):
    """
    有声明时以声明为准——这正是第二层存在的理由。
    正文里「如果您有异议需要转人工财务审核」按关键词是条件句、不算；
    但就算换成会命中关键词的写法，声明 no 也压得住。
    """
    agent = orchestrator._pool[AgentType.GENERAL][0]
    prose = "订单查到了，当前应付 49,448.16 美元。这个我处理不了。"   # 关键词会判 True
    assert agent._decide_escalation(prose, declared=False, grounded=True) is False
    assert agent._decide_escalation("已经帮您查到了。", declared=True, grounded=True) is True


def test_no_declaration_falls_back_to_keywords(orchestrator):
    agent = orchestrator._pool[AgentType.GENERAL][0]
    assert agent._decide_escalation("这个我处理不了。", declared=None, grounded=True) is True
    assert agent._decide_escalation("已经帮您查到了。", declared=None, grounded=True) is False


def test_handoff_rule_is_in_the_system_prompt(orchestrator):
    agent = orchestrator._pool[AgentType.GENERAL][0]
    prompt = agent._build_system_prompt("")
    assert "[[handoff:yes]]" in prompt and "[[handoff:no]]" in prompt


@pytest.mark.asyncio
async def test_streaming_never_leaks_the_trailer(orchestrator, monkeypatch):
    """
    流式路径：标记可能被 \\n 当成一句放出来，也可能留在 flush 的尾巴里，
    两种情况都不能出现在 delta 事件里，最终 content 里也不能有。
    """
    from app.agents.agent_orchestrator import Request
    agent = orchestrator._pool[AgentType.GENERAL][0]

    async def fake_stream(req, skill_prompt=""):
        for piece in ["订单已排产。", "预计下周出厂。\n", "[[handoff:no]]\n"]:
            yield piece
    monkeypatch.setattr(agent, "_stream_llm", fake_stream)
    monkeypatch.setattr(agent, "_skill_prompt", lambda req: "")

    req = Request(message="我的柜子呢", user_id="u", conv_id="c",
                  grounding_sources=["订单已排产。预计下周出厂。"])
    events = [e async for e in agent.stream(req)]
    deltas = "".join(e["text"] for e in events if e["type"] == "delta")
    end = next(e for e in events if e["type"] == "end")["response"]
    assert "handoff" not in deltas
    assert "handoff" not in end.content
    assert end.escalate is False
