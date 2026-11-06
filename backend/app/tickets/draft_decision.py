"""Bind answer-quality handoff signals to the draft actually being published."""
from __future__ import annotations

import hashlib

from app.agents.agent_orchestrator import AgentOrchestrator, AgentType
from app.core.business_facts import render_draft, rendered_images, verified_cells
from app.core.intent_recognizer import IntentCategory, UrgencyLevel

ORDER_IMAGE_FIELDS = frozenset({"order_ref", "model", "quantity", "date", "status"})


def complete_order_image_review(reviews):
    """A server-side coverage receipt, computed after adoption rejects conflicts.

    Extraction may omit fields entirely. Require the full order-image record in
    every attachment, not merely no unresolved candidates or an order number.
    """
    return bool(reviews) and all(
        review.get("document_type") == "order" and review.get("query_outcome") == "checked"
        and ORDER_IMAGE_FIELDS <= {item["field"] for item in review["fields"]}
        and all(item.get("outcome") == "matched" and item.get("readability") == "clear"
                and item.get("db_value") is not None for item in review["fields"])
        for review in reviews)


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def handoff_review(request, result, draft):
    """Server-derived routing facts, never parsed from a model's reason text."""
    if (result.primary_agent == AgentType.HANDOFF
            or request.intent in {IntentCategory.HUMAN_HANDOFF, IntentCategory.ESCALATION}
            or request.urgency == UrgencyLevel.CRITICAL):
        basis = "direct"
    elif (not request.intent_degraded and not result.service_unavailable
          and request.intent_confidence >= AgentOrchestrator.INTENT_MIN_CONFIDENCE
          and result.routing_confidence >= AgentOrchestrator.ROUTING_MIN_CONFIDENCE):
        # Zero policy violations includes an explicit model handoff with .65
        # confidence. Verified facts do not replace that human-action request.
        basis = "answer_quality" if result.policy_violations else "unresolved_answer"
    else:
        basis = "uncertain_routing"
    from app.core.policy_scope import read_only_policy_query
    return {"basis": basis, "model_answer_sha256": digest(result.response), "draft_sha256": digest(draft),
            "answer_handoff_declared": result.answer_handoff_declared,
            "read_only_policy_lookup": read_only_policy_query(request.message, request.entities),
            "original_handoff_reason": result.handoff_reason, "original_answer_confidence": result.answer_confidence,
            "outcome": "retained"}


def reconcile_handoff(result, context):
    """Called only after the transaction revalidates actor and evidence.

An obsolete answer-quality signal cannot authorize a side effect on behalf of
a different, entirely evidence-rendered draft. Other handoff reasons remain.
"""
    snapshot = context["snapshot"]
    review = result.get("handoff_review") or {}
    policy_only = review.get("read_only_policy_lookup") is True and review.get("answer_handoff_declared") is False
    if (not result.get("handoff") or review.get("basis") != "answer_quality"
            or review.get("answer_handoff_declared") is True
            or (not policy_only and (not context.get("materials_reviewed") or context.get("image_review_complete") is not True))
            or not result.get("policy_violations")
            or result.get("needs_clarification") or result.get("service_unavailable")
            or snapshot.get("pending_fields")):
        return result
    response = result.get("response", "")
    if (review.get("draft_sha256") != digest(response)
            or review.get("model_answer_sha256") == review.get("draft_sha256")):
        return result
    knowledge, business, facts = result.get("knowledge_sources") or [], result.get("business_data") or {}, snapshot.get("facts") or {}
    knowledge_state = result.get("knowledge") or {}
    if knowledge_state.get("degraded") or knowledge_state.get("evidence_ok") is False or business.get("evidence_issues"):
        return result
    cells = []
    if business.get("evidence"):
        cells, issues = verified_cells(business.get("evidence_payload") or {})
        if issues or cells != business["evidence"]:
            return result
    elif business and (business.get("outcome") not in {"not_applicable", "disabled", "skipped", "not_configured"}
                       or (policy_only and business.get("attempted"))):
        return result
    if any(not r.get("policy_revision") or not (r.get("content") or r.get("excerpt")) for r in knowledge):
        return result
    if policy_only:
        if (not knowledge or knowledge_state.get("evidence_ok") is not True
                or knowledge_state.get("lookup_status") != "found"
                or response != render_draft(knowledge, business, facts, language=snapshot.get("language", "zh"), knowledge_state=knowledge_state)):
            return result
        return {**result, "handoff": False, "escalated": False, "handoff_reason": "",
                "handoff_review": {**review, "outcome": "replaced_by_verified_policy_draft"}}
    images = rendered_images(facts)
    # Only the image-review continuation has established complete field coverage.
    # An ordinary request may still need human action despite having some records.
    current_review = context.get("message_id", "").removeprefix("verified_")
    if (not ORDER_IMAGE_FIELDS <= {field for field, _ in images}
            or len(images) != sum(f.get("source") == "verified_image" for f in facts.values())
            or any(facts[field].get("evidence_id") != current_review + ":" + field for field in ORDER_IMAGE_FIELDS)):
        return result
    if response != render_draft(knowledge, business, facts, language=snapshot.get("language", "zh"), knowledge_state=knowledge_state):
        return result
    return {**result, "handoff": False, "escalated": False, "handoff_reason": "",
            "handoff_review": {**review, "outcome": "replaced_by_verified_draft"}}
