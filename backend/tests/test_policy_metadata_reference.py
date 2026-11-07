"""Policy metadata references do not authorize business dates or actions."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_case_draft_handoff import finish
from test_cases import ACTOR, PG, scalar
from test_cases import pair as pair  # noqa: F401

from app.agents.agent_orchestrator import Request
from app.core import policy_scope as ps
from app.core.answer_policy import AnswerPolicy, PolicyStreamGuard
from app.core.embedder import Embedder
from app.core.intent_recognizer import IntentCategory, UrgencyLevel
from app.events import EventLog
from app.storage.vector_store import PgStore
from app.tickets.case_execution import CaseExecutor
from app.tickets.cases import CaseInput
from app.tools.knowledge_base import KnowledgeBase


def recording():
    return json.loads((Path(__file__).parent / "fixtures/policy_metadata_reference.json").read_text())


def check(text, *, blocked=False, sources=None, times=None):
    record = recording()
    sources = sources if sources is not None else [s["content"] for s in record["sources"]]
    times = times if times is not None else ps.policy_reference_times(record["scope"], record["sources"])
    result = AnswerPolicy().enforce(text, sources, reference_times=times)
    assert result.blocked is blocked, result.violations
    stream = PolicyStreamGuard(AnswerPolicy(), sources, reference_times=times)
    output = "".join(stream.feed(text[i:i + 2]) for i in range(0, len(text), 2)) + stream.flush()
    assert output == result.text
    assert [v.to_dict() for v in stream.violations] == [v.to_dict() for v in result.violations]
    if not blocked:
        assert output == text
    return result


@pytest.mark.parametrize("index", [0, 1])
def test_recorded_metadata_reference_is_not_a_commitment(index):
    record = recording()
    assert record["original_handoff"] and record["original_answer_confidence"] == .5
    check(record["sentences"][index])


@pytest.mark.parametrize("text", [
    "按所选日期 **2026-11-05T19:59:59Z**，付款手续费的处理时限为77个工作日。",
    "所选时点：2026-11-05T19:59:59+00:00，手续费的处理时限为77个工作日。",
    "查询时点：2026-11-05T19:59:59Z；费用处理时限为77个工作日。",
    "- 到期日期：2026-11-05T20:00:00Z",
    "- 政策到期时间：2026-11-05T20:00:00Z",
    "Expiry date: 2026-11-05T20:00:00Z",
])
def test_same_metadata_roles_and_financial_topics_keep_their_meaning(text):
    check(text)


@pytest.mark.parametrize("text", [
    "按所选时点2026-11-05T19:59:59Z，届时发货。",
    "按所选日期2026-11-05T19:59:59Z，于该时点退款。",
    "查询时点：2026-11-05T19:59:59Z，我们会于该时点支付。",
    "查询时点：2026-11-05T19:59:59Z，付款。",
    "查询时点：2026-11-05T19:59:59Z，保证当天完成。",
    "As of: 2026-11-05T19:59:59Z, we will pay at that time.",
    "到期时间：2026-11-05T20:00:00Z；届时退款。",
    "按所选日期2026-11-05T19:59:59Z，付款手续费998美元。",
    "按所选时点2026-11-05T19:59:59Z，处理时限为78个工作日。",
    "按所选日期2026-11-05T19:59:59Z；2026-11-06送达。",
    "所选日期：2026-11-05T20:00:00Z",
    "到期时间：2026-11-05T19:59:59Z",
    "生效时点：2026-11-05T20:00:00Z",
    "付款到期时间：2026-11-05T20:00:00Z",
])
def test_metadata_does_not_authorize_actions_other_values_or_other_roles(text):
    check(text, blocked=True)


@pytest.mark.parametrize("text", [
    "按所选时点2026-11-05T19:59:59Z，支付费用。",
    "按所选时点2026-11-05T19:59:59Z，付款手续费。",
    "按所选日期2026-11-05T19:59:59Z，扣款费用。",
    "请按所选日期2026-11-05T19:59:59Z支付费用。",
    "按所选时点2026-11-05T19:59:59Z，我们会支付手续费。",
    "As of: 2026-11-05T19:59:59Z, pay the fee.",
])
def test_same_time_financial_actions_are_not_fee_topics(text):
    check(text, blocked=True)


@pytest.mark.parametrize("change", ["revision", "region", "audience", "status", "expired"])
def test_unqualified_source_cannot_supply_expiry_metadata(change):
    source = copy.deepcopy(recording()["sources"][0])
    if change == "revision":
        source["policy_revision"] = ""
    elif change in {"region", "audience", "status"}:
        source["policy"][change] = {"region": "NV", "audience": "customer", "status": "retired"}[change]
    else:
        source["policy_as_of"] = source["policy_scope"]["as_of"] = source["policy"]["expires_at"]
    assert ps.policy_source_metadata(source) is None
    check("到期时间：2026-11-05T20:00:00Z", blocked=True,
          times=ps.policy_reference_times(sources=[source]))


@pytest.mark.parametrize("text", [
    "查询时点：2032-07-08T09:10:11Z，付款手续费处理时限为13个工作日。",
    "按所选日期2032年7月8日 09:10:11 UTC，费用处理时限为13个工作日。",
])
def test_metadata_reference_rule_is_not_tied_to_a_recorded_date_or_duration(text):
    check(text, sources=["费用处理时限为13个工作日。"],
          times={"policy_as_of": ["2032-07-08T09:10:11Z"]})


@pytest.mark.parametrize("explicit_handoff", [False, True])
@pytest.mark.skipif(not PG, reason="TEST_POSTGRES_DSN is required")
async def test_billing_case_keeps_policy_evidence_and_explicit_human_decision(pair, orchestrator, explicit_handoff):
    service, _ = pair
    record = recording()
    scope = ps.PolicyScope.model_validate(record["scope"])
    kb = KnowledgeBase(store=PgStore(service.db), embedder=Embedder(dim=128))
    service.knowledge_base = kb
    source = record["sources"][0]
    await kb.import_documents([{"title": source["title"], "content": source["content"], "policy": source["policy"]}])
    with ps.bind_policy_scope(scope):
        refs = await kb.search_async("付款手续费处理时限")
    assert len(refs) == 1 and ps.policy_source_metadata(refs[0])
    receipt = await service.accept_input(ACTOR, CaseInput(
        message_id="policy-reference-case", conv_id="conversation", new_case=True,
        issue_type="general", message=record["message"], policy_scope=scope))
    request = Request(message=record["message"], user_id=ACTOR.actor_id, conv_id="conversation",
                      intent=IntentCategory.BILLING, intent_confidence=1., urgency=UrgencyLevel.LOW,
                      grounding_sources=[source["content"]], reference_times=ps.policy_reference_times(record["scope"], refs))
    assert not ps.read_only_policy_query(request.message)
    answer = "\n".join(record["sentences"]) + ("\n[[handoff:yes]]" if explicit_handoff else "")
    for agents in orchestrator._pool.values():
        for agent in agents:
            agent._call_llm = AsyncMock(return_value=answer)
    prepared = SimpleNamespace(request=request, intent=SimpleNamespace(intent_group="billing"), knowledge_used=True,
                               evidence={"knowledge": {"sources": refs, "attempted": True, "evidence_ok": True,
                                          "lookup_status": "found", "policy_lookup": True, "degraded": False},
                                         "business_data": {"enabled": True, "attempted": False, "outcome": "skipped"}})
    executor = CaseExecutor(service, prepare=AsyncMock(return_value=prepared), answer=orchestrator.run,
                            event_log=EventLog.disabled())
    run = await finish(service, executor, receipt["run_id"])
    assert run.status == "succeeded" and run.result["case_applied"]
    assert not run.result["policy_violations"]
    assert run.result["handoff"] is explicit_handoff
    if not explicit_handoff:
        assert run.result["answer_confidence"] >= .7
        assert "handoff_review" not in run.result
    case = await service.get(ACTOR, run.result["case_id"])
    assert case["drafts"][0]["valid"] and case["drafts"][0]["status"] == "pending"
    assert await scalar(service.db, "SELECT count(*) FROM case_actions WHERE kind='handoff'") == int(explicit_handoff)
