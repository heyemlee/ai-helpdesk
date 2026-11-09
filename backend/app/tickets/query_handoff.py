"""A staff lookup with no action intent cannot authorize a model's Case action.

This is an action boundary, not a fact capability or a confidence classifier.
A message counts as a lookup when it names a business target (the selected
Case's saved order, an order or material named in the message) and asks for
information, and it carries no request to act: escalate or hand over, cancel,
refund, compensate, arrange, approve, change a record, contact the customer,
investigate, expedite or otherwise handle something. Any such request keeps the
existing handoff workflow, as do service failures and direct routes. A lookup
whose evidence could not be verified still answers "unverified" in the draft; the
model's low confidence about it is recorded, not executed as a Case action. Query
evidence, image/policy adoption, explicit staff actions and non-Case routing
remain independently gated.
"""
from __future__ import annotations

import re

from app.agents.agent_orchestrator import AgentType
from app.core.fact_completion import _PHRASES
from app.core.request_plan import _MATERIAL, make_request_plan

_ANSWER_AGENTS = {agent.value for agent in AgentType if agent not in {AgentType.HANDOFF, AgentType.SYSTEM}}

# Field and record words name information, so an action word inside one of them
# ("处理进度", "退款状态") is part of the question, not a request to act.
_FIELD_PHRASES = sorted(
    {p for phrases in _PHRASES.values() for p in phrases}
    | {"交付日期", "送货日期", "生产记录状态值", "状态字段", "状态值",
       "登记联系电话", "登记电话", "配送联系电话", "送货联系电话", "配送电话", "送货电话", "联系电话",
       "手机号码", "手机号", "registered phone", "delivery phone", "phone number"},
    key=len, reverse=True,
)
_FIELDS = re.compile("|".join(re.escape(p) for p in _FIELD_PHRASES), re.I)
_INFO = re.compile(
    r"状态|记录|信息|情况|进展|进度|结果|详情|资料|数量|件数|日期|时间|名称|型号|金额|地址|电话|手机|库存|现存|占用|工序|"
    r"什么|多少|几[个件天号]|哪|怎么样|如何|是否|有没有|有无|吗|[？?]|"
    r"查询|查一下|查看|查清|查|检索|汇报|告诉我|提供|列出|说明|显示|"
    r"\b(?:status|state|record|records|details?|information|progress|quantity|quantities|date|name|model|amount|"
    r"address|phone|inventory|stock|what|which|when|where|how|whether|show|check|list|tell|lookup|look up)\b",
    re.I,
)
# An action word directly followed by one of these nouns asks about that record.
_ABOUT = r"(?!\s*(?:状态|结果|进度|进展|记录|情况|时限|时间|日期|单号|编号|金额|数量|件数|名称|信息|申请单|原因))"
_ACTIONS = re.compile(
    "|".join(word + _ABOUT for word in (
        r"转人工", r"人工客服", r"人工处理", r"转给", r"转交", r"转接", r"升级处理", r"上报", r"找主管", r"找经理", r"投诉",
        r"取消(?=订单|这|该|此|它|掉)", r"撤销", r"撤回", r"退款", r"退货", r"退钱", r"赔偿", r"补偿", r"赔付",
        r"安排", r"办理", r"发起", r"执行", r"审批", r"批准", r"驳回", r"争议", r"纠纷", r"调查",
        r"修改", r"更改", r"改成", r"改为", r"改到", r"改一下", r"更正为", r"更新为", r"更新成", r"调整", r"变更",
        r"延期", r"加急", r"催", r"通知", r"联系客户", r"联系一下", r"致电", r"打电话", r"回电", r"发短信", r"发邮件", r"发送",
        r"下单", r"创建", r"删除", r"关闭(?:这个|该|此)?(?:工单|案件)", r"结案", r"提交", r"登记一下", r"(?<![已未])解决",
        r"承诺", r"保证", r"补发(?!配件)", r"(?:给|为|帮)[^，,。；;？?]{0,12}补件",
        r"处理(?!时限)",
    ))
    + r"|\b(?:cancel|refund|return|compensate|compensation|escalate|escalation|human|transfer|supervisor|manager|"
      r"complain|complaint|arrange|approve|reject|change|update|modify|edit|correct|expedite|notify|contact|call|"
      r"email|send|create|delete|close|submit|resolve|fix|dispute|investigate|handle)\b",
    re.I,
)
_NEGATION = re.compile(r"(?:不要|不用|无需|不需要|不必|别|先别|暂不|暂时不要|do not|don't|no need to)\s*$", re.I)
# Case navigation, selection and material updates already happened upstream
# under the input receipt; repeating their words does not request them again.
_CASE_OPERATIONS = re.compile(
    r"另开一案|新建工单|新开工单|切回第[一二三四五六七八九十0-9]+个工单|"
    r"(?:重新打开|重开|再次打开)(?:这个|该|此|之前的|刚才的)?(?:工单|案件)?|"
    r"(?:的)?(?:现场|工单)?材料(?:有|已)?更新|(?:后)?(?:出|生成|提供)审核稿|"
    r"(?:刚才|之前|上面)?(?:单号|订单号|编号)?(?:说错了?|写错了?|搞错了?|有误)|更正(?![为成])|纠正(?![为成])",
    re.I,
)


def _without(text, value):
    # A sentence-ending period is punctuation, while a period followed by
    # another identifier character still belongs to a different value.
    return re.sub(r"(?<![A-Za-z0-9_.-])" + re.escape(value) + r"(?![A-Za-z0-9_-]|\.[A-Za-z0-9_.-])", " ", text)


def _requests_action(text):
    for match in _ACTIONS.finditer(text):
        if not _NEGATION.search(text[max(0, match.start() - 8):match.start()]):
            return True
    return False


def read_only_staff_query(message, context):
    """A lookup about a business target with no request to act, using the saved selected Case.

    Case selection/material updates already happened upstream. Recognizing their
    context does not execute them again or resolve a different Case from prose.
    This function grants neither field coverage nor business evidence authority.
    """
    if not isinstance(context, dict) or not isinstance(snapshot := context.get("snapshot"), dict):
        return False
    receipt = snapshot.get("input_receipt")
    if not isinstance(receipt, dict):
        return False
    if (not isinstance(message, str) or not message.strip()
            or receipt.get("case_id") != context.get("case_id")
            or receipt.get("message_id") != context.get("message_id")
            or receipt.get("operation") not in {"created", "continued"}):
        return False
    refs = snapshot.get("order_refs") or []
    if not isinstance(refs, list) or not all(isinstance(ref, str) and ref for ref in refs):
        return False
    plan = make_request_plan(message, case_refs=refs)
    if plan["business_effects"]:
        return False
    text = message
    for ref in sorted(refs, key=len, reverse=True):
        # A correction that points at the saved reference only fixes the input.
        text = re.sub(r"(?:应该是|应为|改成|改为|更正为|更正成)\s*(?=" + re.escape(ref) + r")", " ", text)
        text = _without(text, ref)
    for target in plan["targets"]:
        if target["kind"] == "material_name":
            text = text.replace(target["value"], " ")
    # Only the planner's own order reading names a target; product or policy
    # names such as "Juniper-47 cabinets" are not orders.
    named = bool(plan["lookup_entities"].get("order_id"))
    text = _CASE_OPERATIONS.sub(" ", text)
    text, fields = _FIELDS.subn(" ", text)
    if not (refs or named or _MATERIAL.search(message)):
        return False
    return bool(fields or _INFO.search(text)) and not _requests_action(text)


def restrict_query_handoff(result, *, message, context):
    """Apply at the Case transaction's side-effect boundary, before _handoff.

    A typed declaration or an executed answer-agent role identifies an answer
    stage. A serialized None, a review object, or the HANDOFF role does not:
    direct server routing never invokes an answer agent. Confidence values and
    review classifications do not authorize actions. Existing untyped internal
    service commands remain outside the model-proposal path.
    """
    model_proposal = (
        type(result.get("answer_handoff_declared")) is bool
        or result.get("agent_type") in _ANSWER_AGENTS
    )
    if (not result.get("handoff") or result.get("service_unavailable") is True
            or not model_proposal or not read_only_staff_query(message, context)):
        return result
    review = result.get("handoff_review") or {}
    return {
        **result, "handoff": False, "escalated": False, "handoff_reason": "",
        "handoff_review": {
            **review, "original_handoff_reason": review.get("original_handoff_reason", result.get("handoff_reason", "")),
            "original_answer_confidence": review.get("original_answer_confidence", result.get("answer_confidence")),
            "outcome": "not_authorized_for_read_only_case",
        },
    }
