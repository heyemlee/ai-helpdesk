"""答案策略守卫：拦截无依据的价格承诺和时效承诺。"""
import pytest

from core.answer_policy import AnswerPolicy

KB = (
    "[知识库检索结果]\n"
    "1. 标题: 交期说明\n   内容: 标准交期 4-6 周，以工厂排产确认为准。\n"
    "2. 标题: 补件流程\n   内容: 补件确认后 7 个工作日内从工厂发出。\n"
    "3. 标题: 五金升级\n   内容: 阻尼铰链升级单价 199 元。\n"
)


@pytest.fixture
def policy() -> AnswerPolicy:
    return AnswerPolicy()


# ── 应当拦截 ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("answer,rule", [
    ("补件会在 3 个工作日内送到。", "lead_time_commitment"),
    ("我保证 24 小时内一定安排好。", "lead_time_commitment"),
    ("这单的运费是 25 元，马上就能退给你。", "price_commitment"),
    ("加一个抽屉只要 88 元。", "price_commitment"),
])
def test_blocks_unsourced_commitments(policy, answer, rule):
    result = policy.enforce(answer, sources=[KB])

    assert result.blocked
    assert result.violations[0].rule == rule
    assert answer not in result.text  # 原句必须被替换掉


def test_blocks_when_no_sources_at_all(policy):
    """没有任何检索结果时，一切具体数字都是无依据的。"""
    result = policy.enforce("补件会在 3 个工作日内送到。", sources=[])
    assert result.blocked


def test_number_borrowed_from_a_different_topic_is_still_unsourced(policy):
    """
    知识库写的是"补件 7 个工作日发出"，模型说成"4 个工作日就能送到"，
    数字被跨条目张冠李戴——这正是这类承诺出事的主要方式，必须拦下。
    """
    result = policy.enforce("补件 4 个工作日就能送到。", sources=[KB])
    assert result.blocked


def test_absolute_wording_overrides_hedging(policy):
    """加了"通常"但同时说"保证"，仍然是承诺。"""
    result = policy.enforce("通常我保证 2 小时内一定安排好。", sources=[KB])
    assert result.blocked


# ── 应当放行 ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("answer", [
    "补件确认后 7 个工作日内从工厂发出。",
    "标准交期 4-6 周。",
    "标准交期 4~6 周。",
    "阻尼铰链升级单价是 199 元。",
])
def test_allows_commitments_backed_by_sources(policy, answer):
    result = policy.enforce(answer, sources=[KB])
    assert not result.blocked
    assert result.text == answer


@pytest.mark.parametrize("answer", [
    "补件通常在一周左右发出，具体以工厂排产实际情况为准。",
    "预计 3 个工作日左右，具体需要核实订单状态后确认。",
])
def test_allows_hedged_statements(policy, answer):
    """SOP 要求的保守表述本来就不是承诺。"""
    result = policy.enforce(answer, sources=[KB])
    assert not result.blocked


@pytest.mark.parametrize("answer", [
    "您的问题我们会尽快处理，请先提供订单号。",
    "请提供订单号和支付时间，我帮您核实。",
    "订单号 12345 已收到，我来帮您查询。",
])
def test_does_not_flag_ordinary_replies(policy, answer):
    result = policy.enforce(answer, sources=[KB])
    assert not result.blocked


# ── 替换行为 ──────────────────────────────────────────────────────────────────

def test_only_the_offending_sentence_is_replaced(policy):
    answer = "好的，我来帮您处理。退款会在 3 个工作日内到账。请保留支付凭证。"

    result = policy.enforce(answer, sources=[KB])

    assert "好的，我来帮您处理。" in result.text
    assert "请保留支付凭证。" in result.text
    assert "3 个工作日内到账" not in result.text


def test_disabled_policy_passes_through():
    policy = AnswerPolicy(enabled=False)
    answer = "退款会在 3 个工作日内到账。"

    result = policy.enforce(answer, sources=[])

    assert result.text == answer
    assert not result.blocked
    assert result.checked is False


def test_stats_track_block_rate(policy):
    policy.enforce("退款会在 3 个工作日内到账。", sources=[])
    policy.enforce("请提供订单号。", sources=[])

    stats = policy.stats()
    assert stats["checked_total"] == 2
    assert stats["blocked_total"] == 1
    assert stats["block_rate"] == 0.5
    assert stats["blocked_by_rule"]["lead_time_commitment"] == 1
