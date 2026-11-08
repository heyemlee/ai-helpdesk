"""Information scope and typed metadata remain distinct from business actions.

The frozen records contain only completed individual-case observations. Original
whole-PG/model bodies are pending; these tests do not claim full-turn replay.
"""
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.agents.agent_orchestrator import Request
from app.core.answer_policy import AnswerPolicy, PolicyStreamGuard
from app.core.intent_recognizer import IntentCategory, UrgencyLevel
from app.core.policy_scope import policy_reference_times

RECORDS = json.loads((Path(__file__).parent / "fixtures/policy_scope_metadata_preliminary.json").read_text())["records"]


def checked(text, times, sources=(), *, blocked=False):
    result = AnswerPolicy().enforce(text, sources, reference_times=times)
    assert result.blocked is blocked, result.violations
    for size in (1, 7):
        stream = PolicyStreamGuard(AnswerPolicy(), sources, reference_times=times)
        output = "".join(stream.feed(text[i:i + size]) for i in range(0, len(text), size)) + stream.flush()
        assert output == result.text
        assert [v.to_dict() for v in stream.violations] == [v.to_dict() for v in result.violations]
    if not blocked:
        assert result.text == text


@pytest.mark.parametrize("record", RECORDS, ids=lambda r: r["id"])
def test_exact_recorded_violation_sentences_are_bound_metadata(record):
    times = policy_reference_times(record["scope"], record["sources"])
    for sentence in record["sentences"]:
        checked(sentence, times, [s["content"] for s in record["sources"]])


def test_title_metadata_does_not_authorize_payment_other_clocks_or_values():
    record = RECORDS[1]
    times = policy_reference_times(record["scope"], record["sources"])
    title = record["sources"][0]["title"]
    at = record["scope"]["as_of"]
    for text in [f"按所选时点{at}，支付《{title}》的费用。",
                 f"[1]《{title}》；生效时间：{at}。",
                 f"[1]《{title}》；生效时间：2026-11-05T20:00:00Z；届时付款。",
                 f"按所选时点{at}，《{title}》保证退款998美元。"]:
        checked(text, times, blocked=True)


@pytest.mark.parametrize("change", ["revision", "status", "region"])
def test_ineligible_source_title_does_not_supply_metadata_authority(change):
    record = copy.deepcopy(RECORDS[1])
    source = record["sources"][0]
    if change == "revision":
        source["policy_revision"] = ""
    else:
        source["policy"][change] = "draft" if change == "status" else "other-region"
    checked(record["sentences"][1], policy_reference_times(record["scope"], record["sources"]), blocked=True)


def test_quoted_title_cannot_borrow_a_different_policy_reference():
    record = copy.deepcopy(RECORDS[1])
    other = copy.deepcopy(record["sources"][0])
    other["doc_id"] = "other-document"
    other["policy_revision"] = "other-revision"
    other["title"] = "Other fee policy"
    times = policy_reference_times(record["scope"], record["sources"] + [other])
    assert "policy_titles" not in times
    checked(record["sentences"][1], times, blocked=True)


@pytest.mark.parametrize("human", [False, True])
async def test_partial_metadata_sentences_do_not_override_explicit_human(orchestrator, human):
    record = RECORDS[1]
    # A synthetic guard/orchestrator control, not the pending original model body.
    answer = "\n".join(record["sentences"]) + ("\n[[handoff:yes]]" if human else "\n[[handoff:no]]")
    for agents in orchestrator._pool.values():
        for agent in agents:
            agent._call_llm = AsyncMock(return_value=answer)
    req = Request(message=record["message"], user_id="synthetic-user", conv_id="synthetic-conversation",
                  intent=IntentCategory.BILLING, intent_confidence=1., urgency=UrgencyLevel.LOW,
                  grounding_sources=[s["content"] for s in record["sources"]],
                  reference_times=policy_reference_times(record["scope"], record["sources"]))
    result = await orchestrator.run(req)
    assert not result.policy_violations
    assert result.answer_handoff_declared is human and result.handoff is human
    assert (result.answer_confidence < .7) is human
