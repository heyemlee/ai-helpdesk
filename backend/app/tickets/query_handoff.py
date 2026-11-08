"""A closed read-only staff query cannot authorize a model's Case action.

This is an action boundary, not a fact capability or a confidence classifier.
Unknown requests keep the existing workflow. Query evidence, image/policy
adoption, explicit staff actions and non-Case routing remain independently gated.
"""
from __future__ import annotations

import re

from app.core.fact_completion import _PHRASES

_FIELDS = re.compile(
    "|".join(re.escape(p) for p in sorted(
        {p for phrases in _PHRASES.values() for p in phrases}
        | {"交付日期", "送货日期", "生产记录状态值", "状态字段", "状态值", "状态"},
        key=len, reverse=True,
    )), re.I,
)
_GLUE = re.compile(
    r"请帮我|查一下|查询|查看|核对|核验|检索|汇报|告诉我|提供|请|只|重新|"
    r"订单编号|订单号|订单|编号确定为|编号|业务|系统中|对应记录|对应|记录|生产|"
    r"沿用上文编号|这笔业务|这个订单|该订单|这笔|这个|该|此|其|它|"
    r"的|在|及|与|和|分别|是什么|是多少|什么|多少|目前|现在|"
    r"\b(?:please|check|show|tell me|what|is|are|the|for|of|order|and|current)\b|"
    r"[\s，,、。.;；:：?？]+", re.I,
)


def read_only_staff_query(message, context):
    """Accept only complete query/draft grammar, using the saved selected Case.

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
    text = message
    if receipt["operation"] == "created":
        text = re.sub(r"^\s*(?:另开一案|新建工单|新开工单)[，,；;。\s]*", "", text)
    else:
        text = re.sub(r"^\s*切回第[一二三四五六七八九十0-9]+个工单[，,；;。\s]*", "", text)
    # A material update is already committed as Case input; producing a review
    # draft is not authority to notify/transfer or mutate a business record.
    text = re.sub(r"(?:的)?(?:现场|工单)?材料(?:有|已)?更新[，,；;。\s]*", "", text)
    text = re.sub(r"(?:后)?(?:出|生成|提供)审核稿", "", text)
    text = re.sub(r"不要转人工|无需转人工|do not (?:transfer|escalate)(?: me)?", "", text, flags=re.I)
    refs = snapshot.get("order_refs") or []
    if not isinstance(refs, list) or not refs or not all(isinstance(ref, str) and ref for ref in refs):
        return False
    for ref in sorted(refs, key=len, reverse=True):
        text = re.sub(r"(?<![A-Za-z0-9_.-])" + re.escape(ref) + r"(?![A-Za-z0-9_.-])", " ", text)
    text, fields = _FIELDS.subn(" ", text)
    return bool(fields and not _GLUE.sub("", text))


def restrict_query_handoff(result, *, message, context):
    """Apply at the Case transaction's side-effect boundary, before _handoff.

    Typed answer fields identify model proposals; confidence values and review
    classifications never authorize actions. Existing untyped internal service
    commands are not model proposals. A model answer remains identifiable even
    if its optional handoff_review was absent in an older checkpoint.
    """
    model_proposal = (
        "answer_handoff_declared" in result or "handoff_review" in result
        or result.get("agent_type") not in {None, "system"}
    )
    if not result.get("handoff") or not model_proposal or not read_only_staff_query(message, context):
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
