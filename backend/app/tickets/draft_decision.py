"""Bind answer-quality handoff signals to the draft actually being published."""
from __future__ import annotations

import hashlib

from app.agents.agent_orchestrator import AgentOrchestrator, AgentType
from app.core.business_facts import render_draft, rendered_images, verified_cells
from app.core.intent_recognizer import IntentCategory, UrgencyLevel


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
        basis = "answer_quality"
    else:
        basis = "uncertain_routing"
    return {"basis": basis, "model_answer_sha256": digest(result.response), "draft_sha256": digest(draft),
            "original_handoff_reason": result.handoff_reason, "original_answer_confidence": result.answer_confidence,
            "outcome": "retained"}


def reconcile_handoff(result, context):
    """Called only after the transaction revalidates actor and evidence.

An obsolete answer-quality signal cannot authorize a side effect on behalf of
a different, entirely evidence-rendered draft. Other handoff reasons remain.
"""
    snapshot = context["snapshot"]
    review = result.get("handoff_review") or {}
    if (not context.get("materials_reviewed")
            or not result.get("handoff") or review.get("basis") != "answer_quality"
            or result.get("needs_clarification") or result.get("service_unavailable")
            or snapshot.get("pending_fields")):
        return result
    response = result.get("response", "")
    if (review.get("draft_sha256") != digest(response)
            or review.get("model_answer_sha256") == review.get("draft_sha256")):
        return result
    knowledge, business, facts = result.get("knowledge_sources") or [], result.get("business_data") or {}, snapshot.get("facts") or {}
    if (result.get("knowledge") or {}).get("degraded") or business.get("evidence_issues"):
        return result
    cells = []
    if business.get("evidence"):
        cells, issues = verified_cells(business.get("evidence_payload") or {})
        if issues or cells != business["evidence"]:
            return result
    elif business and business.get("outcome") not in {"not_applicable", "disabled"}:
        return result
    if any(not r.get("policy_revision") or not (r.get("content") or r.get("excerpt")) for r in knowledge):
        return result
    images = rendered_images(facts)
    # Only the image-review continuation has established complete field coverage.
    # An ordinary request may still need human action despite having some records.
    if not images or len(images) != sum(f.get("source") == "verified_image" for f in facts.values()):
        return result
    if response != render_draft(knowledge, business, facts, language=snapshot.get("language", "zh")):
        return result
    return {**result, "handoff": False, "escalated": False, "handoff_reason": "",
            "handoff_review": {**review, "outcome": "replaced_by_verified_draft"}}
