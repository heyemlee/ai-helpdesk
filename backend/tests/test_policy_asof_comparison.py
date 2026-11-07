"""Only trusted applicability comparisons may exclude their metadata clock."""
import pytest
from conftest import ScriptedModel, make_request, text_step
from test_agent_tool_loop import _orchestrator

from app.agents.agent_orchestrator import AgentType, RoutingDecision
from app.core.answer_policy import AnswerPolicy, PolicyStreamGuard
from app.core.intent_recognizer import IntentCategory
from app.core.policy_scope import policy_reference_times, policy_source_metadata

COMPARISON = "以上适用性按所选查询时点判定，并非按今天判定。"
ASOF = "2034-02-09T07:08:09Z"
SCOPE = {"product": "Panel-X", "region": "CA", "audience": "employee", "as_of": ASOF}
SOURCE = {"title": "Control policy", "content": "查阅流程：登记问题，再由运营审核。", "doc_id": "doc-control",
          "policy": {"status": "published", "version": "rev-control", "product": "Panel-X", "region": "CA",
                     "audience": "employee", "effective_at": "2034-02-01T00:00:00Z", "expires_at": None},
          "policy_scope": SCOPE, "policy_as_of": ASOF, "policy_revision": "revision-control"}


def times():
    assert policy_source_metadata(SOURCE)
    return policy_reference_times(SCOPE, [SOURCE])


def check(text, *, blocked, sources=(), reference_times=None, evidence=None):
    metadata = times() if reference_times is None else reference_times
    result = AnswerPolicy().enforce(text, sources, reference_times=metadata)
    assert result.blocked is blocked, result.violations
    if evidence:
        assert result.violations[0].evidence == evidence
    for size in (1, 7):
        stream = PolicyStreamGuard(AnswerPolicy(), sources, reference_times=metadata)
        output = "".join(stream.feed(text[i:i + size]) for i in range(0, len(text), size)) + stream.flush()
        assert output == result.text
        assert [v.to_dict() for v in stream.violations] == [v.to_dict() for v in result.violations]
    if not blocked:
        assert result.text == text
    return result


@pytest.mark.parametrize("text", [
    COMPARISON,
    "政策是否适用应按查询时点判断，而不是按今日判断。",
    "按所选日期判定政策适用性，并非按今天判定。",
    "该政策适用性不能按今天判定。",
    "不能按今天的日期判断是否适用。",
    "不应依据今日的日期判定该政策是否适用。",
    "上述规则的有效性根据所选时点确定，而非依据今日确定。",
    "以上适用性按所选查询时点判定；并非按今天判定。",
    "以上适用性按**所选查询时点**判定，并非按`今天`判定。",
    "Policy applicability is determined at the selected query time, not today.",
    "Applicability is evaluated using the selected date, rather than today's date.",
    "Do not use today's date to determine policy applicability.",
])
def test_complete_applicability_comparison_is_metadata_not_a_promise(text):
    check(text, blocked=False)


@pytest.mark.parametrize("metadata", [
    {}, {"policy_as_of": []}, {"policy_as_of": ["not-a-time"]}, {"policy_as_of": [None]},
    {"policy_as_of": [ASOF.removesuffix("Z")]}, {"policy_as_of": ASOF},
    {"policy_effective_at": [ASOF]}, {"business_as_of": [ASOF]},
])
def test_no_valid_policy_asof_role_means_no_comparison_exemption(metadata):
    check(COMPARISON, blocked=True, reference_times=metadata, evidence="今天")


@pytest.mark.parametrize("text", [
    "不能按今天的日期判断是否已经送达。",
    "不要按今天的日期安排发货。",
    "按所选发货日期判断是否送达，并非按今天判定。",
    "约定交付日期按所选查询时点判定，并非按今天判定。",
    "以上适用性按所选查询时点判定，并非按今天判定是否发货。",
    "我们不能今天发货。",
    "不要今天支付款项。",
    "政策今日生效。",
    "Do not use today's date to schedule delivery.",
    "The delivery date is determined at the selected query time, not today.",
    "以上适用性按所选查询时点判定，但配送要按今天判定。",
    "以上适用性按所选查询时点判定，中间另有业务安排，并非按今天判定。",
])
def test_negation_or_query_words_do_not_turn_business_dates_into_metadata(text):
    check(text, blocked=True)


@pytest.mark.parametrize("suffix,evidence", [
    ("，费用为998美元。", "998美元"),
    ("，7个工作日内发货。", "7个工作日"),
    ("，保证明天送到。", "明天"),
    ("，今天退款。", "今天"),
    ("，2034-02-09送到。", "2034-02-09"),
    ("，届时支付费用。", "今天"),
    ("，并保证退款。", "今天"),
])
def test_remaining_same_sentence_values_actions_and_shared_clocks_are_still_checked(suffix, evidence):
    check(COMPARISON.removesuffix("。") + suffix, blocked=True, evidence=evidence)


@pytest.mark.parametrize("suffix,source", [
    ("，费用为25元。", "费用为25元。"),
    ("，13个工作日内发货。", "13个工作日内发货。"),
    ("，2034-02-09送达。", "已核实送达日期为2034-02-09。"),
])
def test_independent_grounded_values_are_checked_normally_after_comparison(suffix, source):
    check(COMPARISON.removesuffix("。") + suffix, sources=[source], blocked=False)


@pytest.mark.parametrize("declared", [False, True])
async def test_comparison_fix_never_clears_an_existing_model_handoff(declared):
    model = ScriptedModel(text_step(COMPARISON + "\n[[handoff:" + ("yes" if declared else "no") + "]]"))
    request = make_request("请说明所选时点的政策原文。", IntentCategory.QUERY, confidence=1.,
                           grounding_sources=[SOURCE["content"]], reference_times=times())
    orch = _orchestrator((AgentType.GENERAL, model))
    orch._route_decision = lambda req: RoutingDecision(primary_agent=AgentType.GENERAL, confidence=1.)
    result = await orch.run(request)
    assert len(model.requests) == 1 and not result.policy_violations
    assert result.handoff is declared and result.answer_handoff_declared is declared
    if declared:
        assert result.answer_confidence == .65
    else:
        assert result.answer_confidence >= .7


@pytest.mark.parametrize("text", [
    "适用性依据所选查询时点，而不是今天。",
    "本次只说明历史查询时点的适用政策，不按今日判断。",
    "判断依据是所选日期，不是今天的日期。",
    "Applicability is evaluated at the selected as-of time, not today.",
    "This policy selection uses the requested historical timestamp rather than today's date.",
])
def test_closed_comparison_grammar_handles_omitted_judgment_and_inline_contrast(text):
    check(text, blocked=False)
