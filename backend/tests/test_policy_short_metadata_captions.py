"""Short metadata captions keep exact timestamp roles without authorizing actions."""
import copy
from unittest.mock import AsyncMock

import pytest

from app.agents.agent_orchestrator import Request
from app.core.answer_policy import AnswerPolicy, PolicyStreamGuard
from app.core.intent_recognizer import IntentCategory, UrgencyLevel
from app.core.policy_scope import policy_reference_times, policy_source_metadata

SCOPE = {"product": "reference cabinets", "region": "CA", "audience": "employee", "as_of": "2037-08-21T19:59:59Z"}
SOURCE = {
    "doc_id": "metadata-reference", "policy_revision": "metadata-reference-revision",
    "title": "reference cabinets policy", "content": "处理时限为47个工作日。",
    "policy": {"product": "reference cabinets", "region": "CA", "audience": "employee", "status": "published",
               "version": "reference-v1", "effective_at": "2037-01-01T00:00:00Z", "expires_at": "2037-08-21T20:00:00Z"},
    "policy_scope": SCOPE, "policy_as_of": SCOPE["as_of"],
}


def check(text, *, blocked=False, source=None, times=None):
    source = SOURCE if source is None else source
    times = policy_reference_times(SCOPE, [source]) if times is None else times
    sources = [source["content"]]
    result = AnswerPolicy().enforce(text, sources, reference_times=times)
    assert result.blocked is blocked, result.violations
    for size in (1, 7):
        stream = PolicyStreamGuard(AnswerPolicy(), sources, reference_times=times)
        output = "".join(stream.feed(text[i:i + size]) for i in range(0, len(text), size)) + stream.flush()
        assert output == result.text
        assert [v.to_dict() for v in stream.violations] == [v.to_dict() for v in result.violations]
    if not blocked:
        assert result.text == text
    return result


@pytest.mark.parametrize("label,value", [("生效", "2026-01-01 00:00:00 UTC"), ("到期", "2026-11-05 20:00:00 UTC")])
def test_captured_metadata_captions(label, value):
    source = copy.deepcopy(SOURCE)
    source["policy"].update(effective_at="2026-01-01T00:00:00Z", expires_at="2026-11-05T20:00:00Z")
    source["policy_scope"]["as_of"] = source["policy_as_of"] = "2026-11-05T19:59:59Z"
    times = policy_reference_times(source["policy_scope"], [source])
    # This long caption already accepts the same UTC display on the old code.
    check(f"- {label}日期：{value}", source=source, times=times)
    check(f"- {label}：{value}", source=source, times=times)


@pytest.mark.parametrize("text", [
    "- 生效：2037-01-01 00:00:00 UTC",
    "- 到期：2037-08-21 20:00:00 UTC",
    "生效：2037-01-01T00:00:00Z",
    "到期：2037-08-21T20:00:00+00:00",
    "生效：2036-12-31T16:00:00-08:00",
    "到期：2037-08-22T04:00:00+08:00",
    "生效：2037年1月1日 00:00:00（UTC）",
    "**到期**：`2037-08-21T20:00:00Z`",
    "| 生效 | 2037-01-01 00:00 UTC |",
    "所选时点：2037-08-21 19:59:59 UTC",
])
def test_complete_short_metadata_fields_delegate_to_existing_instant_rules(text):
    check(text)


@pytest.mark.parametrize("label,value", [
    ("生效", "2037-01-01"),
    ("到期", "2037-08-21"),
    ("生效", "2037-01-01 00:00:00"),
    ("到期", "2037-08-21 20:00:00"),
    ("生效", "2037-01-01 UTC"),
    ("到期", "2037-08-21+00:00"),
    ("生效", "2037-01-01 00:00:00 UTC"),
    ("到期", "2037-08-22T04:00:00+08:00"),
])
def test_independent_short_and_long_captions_share_original_date_and_clock_semantics(label, value):
    check(f"{label}日期：{value}")
    check(f"{label}：{value}")


@pytest.mark.parametrize("text", [
    "生效：2037-01-02 00:00:00 UTC",
    "到期：2037-08-21 19:00:00 UTC",
    "到期：2037-08-21 20:01:00 UTC",
    "到期：2037-08-21 20:00:01 UTC",
    "生效：2037-01-01T00:00:00+01:00",
    "到期：2037-08-21T20:00:00-01:00",
    "生效：2037-08-21T19:59:59Z",
    "到期：2037-01-01T00:00:00Z",
    "到期：2037-08-21T20:00:00UTC+01:00",
    "生效：2037-01-01T00:00:00+24:00",
    "到期：2037-08-21T20:00:00Zjunk",
    "到期：2037-08-21T20:00:00.500Z",
    "付款到期：2037-08-21T20:00:00Z",
    "保证生效：2037-01-01T00:00:00Z",
])
def test_wrong_instants_roles_malformed_fields_and_action_labels_stay_blocked(text):
    check(text, blocked=True)


@pytest.mark.parametrize("tail", [
    "；费用998美元。", "，费率17%。", "；交期为2天。", "；处理时限为48个工作日。",
    "；届时付款。", "，保证当天交付。", "；请安排退款。", "；2037-08-22送达。",
])
def test_short_caption_cannot_hide_mixed_business_claims(tail):
    check("生效：2037-01-01 00:00:00 UTC" + tail, blocked=True)


@pytest.mark.parametrize("change", ["revision", "region", "audience", "product", "draft", "expired", "as_of"])
def test_short_caption_requires_eligible_revision_and_matching_scope(change):
    source = copy.deepcopy(SOURCE)
    if change == "revision":
        source["policy_revision"] = ""
    elif change in {"region", "audience", "product"}:
        source["policy"][change] = "other-scope"
    elif change == "draft":
        source["policy"]["status"] = "draft"
    elif change == "expired":
        source["policy"]["expires_at"] = source["policy_as_of"]
    else:
        source["policy_as_of"] = "2037-08-21T19:59:58Z"
    assert policy_source_metadata(source) is None
    check("生效：2037-01-01 00:00:00 UTC", source=source, blocked=True)


@pytest.mark.parametrize("times", [{}, {"business_as_of": [SCOPE["as_of"]]}, {"policy_effective_at": [None]},
                                  {"policy_effective_at": ["2037-01-01T00:00:00"]}])
def test_untrusted_or_naive_reference_times_do_not_authorize_short_metadata(times):
    check("生效：2037-01-01 00:00:00 UTC", times=times, blocked=True)


@pytest.mark.parametrize("human", [False, True])
async def test_short_metadata_does_not_clear_explicit_handoff_or_change_confidence_rules(orchestrator, human):
    answer = ("生效：2037-01-01 00:00:00 UTC\n到期：2037-08-21 20:00:00 UTC\n"
              + ("[[handoff:yes]]" if human else "[[handoff:no]]"))
    for agents in orchestrator._pool.values():
        for agent in agents:
            agent._call_llm = AsyncMock(return_value=answer)
    req = Request(message="查询所选政策的生效和到期元数据。", user_id="u", conv_id="c",
                  intent=IntentCategory("order_status"), intent_confidence=.95, urgency=UrgencyLevel.LOW,
                  grounding_sources=[SOURCE["content"]], reference_times=policy_reference_times(SCOPE, [SOURCE]))
    result = await orchestrator.run(req)
    assert not result.policy_violations
    assert result.answer_handoff_declared is human and result.handoff is human
    assert result.answer_confidence == round(min(req.intent_confidence, result.routing_confidence, .65 if human else 1.), 3)
