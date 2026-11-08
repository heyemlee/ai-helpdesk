"""A closed read-only staff query cannot authorize a model's Case action.

This is an action boundary, not a fact capability or a confidence classifier.
Unknown requests keep the existing workflow. Query evidence, image/policy
adoption, explicit staff actions and non-Case routing remain independently gated.
"""
from __future__ import annotations

import re

from app.agents.agent_orchestrator import AgentType
from app.core.fact_completion import _PHRASES
from app.core.request_plan import _EFFECTS, _MATERIAL, make_request_plan

_ANSWER_AGENTS = {agent.value for agent in AgentType if agent not in {AgentType.HANDOFF, AgentType.SYSTEM}}
# Contact phones (request_role_catalog.json: registered_phone, delivery_phone) use
# the phrases make_request_plan reads as those roles.
_PHONES = {"登记联系电话", "登记电话", "配送联系电话", "送货联系电话", "配送电话", "送货电话", "联系电话",
           "手机号码", "手机号", "手机", "电话", "registered phone", "delivery phone", "phone number", "phone"}
_FIELDS = re.compile(
    "|".join(re.escape(p) for p in sorted(
        {p for phrases in _PHRASES.values() for p in phrases}
        | {"交付日期", "送货日期", "生产记录状态值", "状态字段", "状态值", "状态"} | _PHONES,
        key=len, reverse=True,
    )), re.I,
)
_GLUE = re.compile(
    r"请帮我|查一下|查询|查看|查清楚|查清|核对|核验|检索|汇报|告诉我|提供|请|只|重新|"
    r"订单编号|订单号|订单|编号确定为|编号|业务|系统中|系统里|系统内|系统|对应记录|对应|记录|生产|"
    r"沿用上文编号|这笔业务|这个订单|该订单|这笔|这个|该|此|其|它|是否有|是否存在|有没有|"
    r"的|在|及|与|和|也|分别|是什么|是多少|有多少|什么|多少|目前|现在|"
    r"\b(?:please|check|show|tell me|what|is|are|the|for|of|order|and|current)\b|"
    r"[\s，,、。.;；:：?？\"“”'「」]+", re.I,
)
# Replacement-part records (request_role_catalog.json: part_name, part_quantity,
# part_status) are named the way make_request_plan names them. Their bare role
# words count as fields only after such a record is named, so "数量" or "进度"
# never widen another record family, and a record name alone is not a query.
_PART_RECORDS = re.compile(r"补件申请|补发配件|补件|配件|\breplacement parts?\b|\bparts\b", re.I)
_PART_FIELDS = re.compile(r"件数|数量|名称|进度|\b(?:name|quantity|quantities|progress|status)\b", re.I)
# Material inventory (request_role_catalog.json: on_hand_qty, allocated_qty) is
# recognized where make_request_plan plans it (_MATERIAL), with the same scoping
# as replacement parts. The material name is the planner's own target, accepted
# only as at most two words and a spec code with no business effect, so no
# request can hide inside it.
_INVENTORY_RECORDS = re.compile(r"库存表中|库存表|物料名称|物料|\bmaterials?\b", re.I)
_INVENTORY_FIELDS = re.compile(
    r"现有库存|已占用库存|已占用量|占用量|已占用|占用|现存|现有|库存数量|库存量|库存|件数|数量|"
    r"\b(?:on.hand|allocated|inventory|stock|quantity|quantities)\b", re.I,
)
_MATERIAL_NAME = re.compile(r"(?:[A-Za-z]+ ){0,2}(?=[A-Za-z0-9./-]*\d)[A-Za-z0-9]+(?:[-./][A-Za-z0-9]+)+")


def _material_names(message, refs):
    """The planner's material names, or None when one of them is not a closed name."""
    names = []
    for target in make_request_plan(message, case_refs=refs)["targets"]:
        if target["kind"] != "material_name":
            continue
        # Text the planner kept after the name (for example "现有") stays in the
        # query and must still parse as fields or glue.
        name = re.match(r"[A-Za-z0-9 ./-]*", target["value"]).group().strip()
        if not _MATERIAL_NAME.fullmatch(name) or _EFFECTS.search(name):
            return None
        names.append(name)
    return names


def _without(text, value):
    # A sentence-ending period is punctuation, while a period followed by
    # another identifier character still belongs to a different value.
    return re.sub(r"(?<![A-Za-z0-9_.-])" + re.escape(value) + r"(?![A-Za-z0-9_-]|\.[A-Za-z0-9_.-])", " ", text)


def read_only_staff_query(message, context):
    """Accept only complete query/draft grammar, using the saved selected Case.

    Case selection/material updates already happened upstream. Recognizing their
    context does not execute them again or resolve a different Case from prose.
    This function grants neither field coverage nor business evidence authority.
    Only a material inventory query may come without a saved order reference;
    any order reference it carries then stays unrecognized.
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
    inventory = _MATERIAL.search(text) is not None
    if not isinstance(refs, list) or not all(isinstance(ref, str) and ref for ref in refs) or not (refs or inventory):
        return False
    for ref in sorted(refs, key=len, reverse=True):
        text = _without(text, ref)
    if inventory:
        if (names := _material_names(message, refs)) is None:
            return False
        for name in sorted(names, key=len, reverse=True):
            text = _without(text, name)
        text = _INVENTORY_RECORDS.sub(" ", text)
    parts = _PART_RECORDS.search(text) is not None
    if parts:
        text = _PART_RECORDS.sub(" ", text)
    text, fields = _FIELDS.subn(" ", text)
    for named, role_words in ((parts, _PART_FIELDS), (inventory, _INVENTORY_FIELDS)):
        if named:
            text, count = role_words.subn(" ", text)
            fields += count
    return bool(fields and not _GLUE.sub("", text))


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
