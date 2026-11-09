"""Trusted policy timestamps retain their role without grounding business promises."""
import copy
from unittest.mock import AsyncMock

import pytest

from app.agents.agent_orchestrator import BaseAgent, Request
from app.core.answer_policy import AnswerPolicy, PolicyStreamGuard
from app.core.intent_recognizer import IntentCategory, UrgencyLevel
from app.core.policy_scope import policy_reference_times


def check(text, times, *, blocked):
    result = AnswerPolicy().enforce(text, [], reference_times=times)
    assert result.blocked is blocked, result.violations
    for size in (1, 7):
        stream = PolicyStreamGuard(AnswerPolicy(), [], reference_times=times)
        output = "".join(stream.feed(text[i:i + size]) for i in range(0, len(text), size)) + stream.flush()
        assert output == result.text
        assert [v.to_dict() for v in stream.violations] == [v.to_dict() for v in result.violations]
    if not blocked:
        assert result.text == text


SELECTED = "2033-04-17T08:09:10Z"
EFFECTIVE = "2033-04-01T00:00:00Z"
TIMES = {"policy_as_of": [SELECTED], "policy_effective_at": [EFFECTIVE], "policy_open_ended_at": [EFFECTIVE]}


@pytest.mark.parametrize("text", [
    "| 截至时点 | 2033-04-17 08:09:10 UTC |",
    "- 有效性判定时点：2033-04-17T08:09:10Z",
    "该版本自 **2033-04-01 00:00:00 UTC** 生效，未设置失效时间。",
    "本政策自2033-04-01T00:00:00Z起生效。",
    "> 截至 2033-04-17 08:09:10 UTC，本次针对 OR 地区、Solis lockers、员工适用范围的检索，"
    "未找到可引用的已发布押金退回审批流程条款，故暂无可列示的有效版本及条款引用。",
    "截至2033-04-17T08:09:10Z，本次检索，未找到可引用的已发布派单办理政策。",
])
def test_metadata_role_variants_remain_metadata(text):
    check(text, TIMES, blocked=False)


@pytest.mark.parametrize("text", [
    "| 截至时点 | 2033-04-17 08:09:11 UTC |",
    "- 有效性判定时点：2033-04-18T08:09:10Z",
    "该版本自2033-04-17T08:09:10Z生效。",
    "该版本自2033-04-01T00:00:01Z生效。",
    "该版本自2033-04-01T00:00:00+01:00生效。",
    "截至2033-04-17T08:09:11Z，本次检索，未找到可引用的已发布派单办理政策。",
    "截至2033-04-17T08:09:10+01:00，本次检索，未找到可引用的已发布派单办理政策。",
    "截至2033-04-17T08:09:10Z，保证当天派单。",
    "> 截至2033-04-17T08:09:10Z，本次检索，未找到可引用的已发布派单办理政策，届时付款。",
    "该版本自2033-04-01T00:00:00Z生效，届时发货。",
    "有效性判定时点：2033-04-17T08:09:10Z，保证当天完成。",
    "有效性判定时点：2033-04-17T08:09:10Z，费用998美元。",
    "| 截至时点 | 2033-04-17 08:09:10 UTC | 保证当天送达。",
    "该版本自2033-04-01T00:00:00Z生效，并保证于该时点退款。",
    "截至2033-04-17T08:09:10Z，本次检索，未找到可引用的已发布派单办理政策；2033-04-18送达。",
])
def test_wrong_roles_instants_and_business_commitments_stay_guarded(text):
    check(text, TIMES, blocked=True)


@pytest.mark.parametrize("times", [{}, {"business_as_of": [SELECTED]}, {"policy_as_of": [None]},
                                  {"policy_as_of": [SELECTED.removesuffix("Z")]},
                                  {"policy_as_of": SELECTED}])
def test_bounded_lookup_clock_requires_trusted_typed_selected_time(times):
    check("截至2033-04-17T08:09:10Z，本次检索，未找到可引用的已发布派单办理政策。", times, blocked=True)


def test_open_ended_claim_requires_open_ended_source():
    check("该版本自2033-04-01T00:00:00Z生效，未设置失效时间。",
          {"policy_effective_at": [EFFECTIVE]}, blocked=True)


@pytest.mark.parametrize("change", ["revision", "region", "audience", "draft", "expired"])
def test_ineligible_reference_does_not_supply_effective_time(change):
    source = copy.deepcopy(RECORDED[1]["sources"][0])
    if change == "revision":
        source["policy_revision"] = ""
    elif change in {"region", "audience"}:
        source["policy"][change] = "other-scope"
    elif change == "draft":
        source["policy"]["status"] = "draft"
    else:
        source["policy"]["expires_at"] = source["policy_as_of"]
    times = policy_reference_times(RECORDED[1]["scope"], [source])
    check("该版本自2026-11-05T20:00:00Z生效，未设置失效时间。", times, blocked=True)


@pytest.mark.parametrize("index", [0, 1])
def test_original_complete_model_answers_have_no_business_date_violation(index):
    record = RECORDED[index]
    content, declared = BaseAgent._split_handoff_trailer(record["answer"])
    assert declared is False
    result = AnswerPolicy().enforce(content, [s["content"] for s in record["sources"]],
                                    reference_times=policy_reference_times(record["scope"], record["sources"]))
    assert not result.violations
    assert result.text == content


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("human", [False, True])
async def test_real_orchestrator_preserves_original_declaration_without_quality_handoff(orchestrator, index, human):
    record = RECORDED[index]
    answer = record["answer"].replace("[[handoff:no]]", "[[handoff:yes]]") if human else record["answer"]
    for agents in orchestrator._pool.values():
        for agent in agents:
            agent._call_llm = AsyncMock(return_value=answer)
    req = Request(message=record["message"], user_id="u", conv_id="c",
                  intent=IntentCategory(record["intent"]), intent_confidence=record["confidence"],
                  urgency=UrgencyLevel.LOW, grounding_sources=[s["content"] for s in record["sources"]],
                  reference_times=policy_reference_times(record["scope"], record["sources"]))
    result = await orchestrator.run(req)
    assert result.handoff is human and result.answer_handoff_declared is human
    assert not result.policy_violations
    for agents in orchestrator._pool.values():
        for agent in agents:
            assert agent._call_llm.await_count <= 1

RECORDED = [{'answer': '按本次已完成的限定范围政策检索，**未找到符合以下范围和时点的已发布政策**，因此目前无法提供可引用的加急排产处理时限，也无法列出适用的有效版本或条款引用。\n'
            '\n'
            '| 检索条件 | 所选范围 |\n'
            '|---|---|\n'
            '| 地区 | CA |\n'
            '| 产品 | Juniper-29 cabinets |\n'
            '| 适用对象 | 员工 |\n'
            '| 截至时点 | 2026-11-05 20:00:01 UTC |\n'
            '\n'
            '建议客服答复保留以下表述：\n'
            '\n'
            '> 截至 2026-11-05 20:00:01 UTC，本次针对 CA 地区、Juniper-29 '
            'cabinets、员工适用范围的检索，未找到可引用的已发布加急排产处理时限条款，故暂无可列示的有效版本及条款引用。此结果仅限上述检索范围与时点，不代表加急排产已获批准、免费或不存在相关规定。\n'
            '\n'
            '**引用说明：**依据为本次限定范围政策检索结果；未返回可引用的已发布政策原文、版本号或条款编号。不能以草稿、已失效版本或其他适用范围的条款替代。\n'
            '\n'
            '[[handoff:no]]',
  'scope': {'as_of': '2026-11-05T20:00:01Z',
            'audience': 'employee',
            'product': 'Juniper-29 cabinets',
            'region': 'CA'},
  'sources': [],
  'message': '我在整理 CA 地区 Juniper-29 cabinets 客服答复。按所选日期和适用范围，加急排产的处理时限有什么规定？请说明有效版本并保留引用。',
  'intent': 'order_status',
  'confidence': 0.672},
 {'answer': '按所选查询时点 **2026-11-05 20:00:01 UTC**，适用于 **CA 地区、Juniper-50 cabinets、员工受众**的已发布有效版本为 '
            '**2026-B**。该版本自 **2026-11-05 20:00:00 UTC** 生效，未设置失效时间。[1]\n'
            '\n'
            '### 付款手续费办理步骤\n'
            '1. **先登记订单号**。\n'
            '2. **再核对现场记录**。\n'
            '3. **最后由运营审核**。\n'
            '\n'
            '**未完成审核，不能承诺赔偿。**[1]\n'
            '\n'
            '本次检索条款规定了以上办理步骤及赔偿承诺限制，未提供手续费金额、费率或减免条件，不能据此认定免费或已获减免。\n'
            '\n'
            '**引用 [1]**：《Juniper-50 cabinets 付款手续费 2026-B》  \n'
            '- 文档 ID：`doc_237897717d1ca8ba1036`\n'
            '- 修订标识：`237897717d1ca8ba1036bb7cc2be125abeea6742d115aa216fd32591b06a4007`\n'
            '- 适用范围：CA / Juniper-50 cabinets / employee\n'
            '- 状态：published；版本：2026-B\n'
            '- 有效性判定时点：2026-11-05T20:00:01Z\n'
            '\n'
            '[[handoff:no]]',
  'scope': {'as_of': '2026-11-05T20:00:01Z',
            'audience': 'employee',
            'product': 'Juniper-50 cabinets',
            'region': 'CA'},
  'sources': [{'chunk_id': 'fc64b43bbf8f48ae4de07ef4f5037ca6',
               'content': 'Juniper-50 cabinets 的付款手续费：先登记订单号，再核对现场记录，最后由运营审核。未完成审核不能承诺赔偿。',
               'doc_id': 'doc_237897717d1ca8ba1036',
               'doc_title': 'Juniper-50 cabinets 付款手续费 2026-B',
               'excerpt': 'Juniper-50 cabinets 的付款手续费：先登记订单号，再核对现场记录，最后由运营审核。未完成审核不能承诺赔偿。',
               'passage': 0,
               'policy': {'audience': 'employee',
                          'effective_at': '2026-11-05T20:00:00Z',
                          'expires_at': None,
                          'product': 'Juniper-50 cabinets',
                          'region': 'CA',
                          'status': 'published',
                          'version': '2026-B'},
               'policy_as_of': '2026-11-05T20:00:01+00:00',
               'policy_revision': '237897717d1ca8ba1036bb7cc2be125abeea6742d115aa216fd32591b06a4007',
               'policy_scope': {'as_of': '2026-11-05T20:00:01Z',
                                'audience': 'employee',
                                'product': 'Juniper-50 cabinets',
                                'region': 'CA'},
               'retrieval': 'hybrid',
               'score': 0.3111,
               'title': 'Juniper-50 cabinets 付款手续费 2026-B'}],
  'message': '我在整理 CA 地区 Juniper-50 cabinets 客服答复。按所选日期和适用范围，付款手续费的办理步骤有什么规定？请说明有效版本并保留引用。',
  'intent': 'billing',
  'confidence': 0.658}]
