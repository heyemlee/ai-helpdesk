"""Closed, source-bound policy excerpts prepared before the answer model."""
from __future__ import annotations

import copy
import hashlib
import hmac
import math
import os
import re
from datetime import UTC, datetime

from app.core.business_facts import fresh, language_of, render_draft
from app.core.lookup_scope import digest
from app.core.policy_scope import PolicyScope, policy_digest, policy_source_metadata

ORIGIN = "verified_policy_projection"
KIND = "policy_excerpt_v1"
_ROLES = {
    "政策原文": "text", "条款原文": "text", "原文": "text", "办理步骤": "procedure", "操作步骤": "procedure",
    "处理时限": "processing_duration", "办理时限": "processing_duration", "processing time": "processing_duration",
    "政策处理时限": "processing_duration", "政策办理时限": "processing_duration",
    "办理流程": "procedure", "有效版本": "version", "适用范围": "scope", "所选日期": "as_of",
    "所选时点": "as_of", "查询时点": "as_of", "引用": "citation",
    "policy text": "text", "procedure steps": "procedure", "procedure": "procedure", "applicable version": "version",
    "政策出处": "citation", "出处": "citation", "citation": "citation", "cite the policy": "citation",
    "cite the applicable published policy": "citation",
    "selected date": "as_of", "selected time": "as_of", "scope": "scope", "citations": "citation",
}
_GLUE = re.compile(r"说明|介绍|提供|附|是多少|多少|是什么|请|和|及|与|以及|并|内|的|在|[\s，,、。.;；:：!?！？]+|"
                   r"\b(?:please|show|explain|provide|and|the|a|within|for|of)\b", re.I)


def utc_now():
    return datetime.now(UTC)


def blocked_evidence(value):
    if not isinstance(value, dict):
        return False
    if value.get("evidence_ok") is not None and value["evidence_ok"] is not True:
        return True
    for key in ("has_risk", "needs_clarification", "degraded", "intent_degraded"):
        flag = value.get(key)
        if flag is not None and (type(flag) is not bool or flag):
            return True
    return any(value.get(key) is not None and (not isinstance(value[key], (list, tuple)) or value[key])
               for key in ("evidence_issues", "conflicts"))


def trace_state(knowledge):
    return {key: copy.deepcopy(knowledge.get(key)) for key in
            ("attempted", "policy_lookup", "lookup_status", "evidence_ok", "degraded", "has_risk", "evidence_issues", "conflicts")}


def make_task(message, context, run_id, attempt):
    try:
        snap, receipt = context["snapshot"], context["snapshot"]["input_receipt"]
        scope = PolicyScope.model_validate(snap["policy_scope"])
        if (not run_id or type(attempt) is not int or attempt < 1 or not context["actor_id"]
                or not context.get("instance") or type(context.get("auth_version", 0)) is not int
                or type(context["expected_case_version"]) is not int
                or type(context["expected_control_version"]) is not int
                or snap["control_mode"] != "assistant" or snap.get("pending_fields") or snap.get("materials")
                or snap.get("review_materials") or snap.get("facts") or snap.get("order_refs")
                or context.get("materials_reviewed") or snap.get("issue_type") != "general"
                or receipt["case_id"] != context["case_id"] or receipt["message_id"] != context["message_id"]
                or receipt["operation"] not in {"created", "continued"}
                or not all((scope.product, scope.region, scope.audience, scope.as_of))
                or re.search(r"\b(?:SO|PO|WO)[-\d]", message, re.I)):
            return None
        text = message.strip()
        product = re.escape(scope.product)
        match = re.match(rf"(?:请)?(?:查询|查阅|查看|查|说明|介绍)\s*{product}\s*的?\s*"
                         r"(?P<subject>[^，,。；;！？\n]{1,100}?)政策(?=[的，,。；;\s]|$)", text)
        if match is None:
            patterns = (
                rf"(?:please\s+)?(?:look up|read|show|explain)\s+(?:the\s+)?"
                rf"(?P<subject>[^,;.\n]{{1,100}}?)\s+policy\s+for\s+{product}(?=[,;.\s]|$)",
                rf"(?:please\s+)?(?:look up|read|show|explain)\s+(?:the\s+)?"
                rf"(?P<role>applicable version|procedure|policy text)\s+of\s+(?:the\s+)?"
                rf"{product}\s+(?P<subject>[^,;.\n]{{1,100}}?)\s+policy(?=[,;.\s]|$)",
                rf"(?:please\s+)?(?:look up|read|show|explain)\s+(?:the\s+)?"
                rf"{product}\s+(?P<subject>[^,;.\n]{{1,100}}?)\s+(?P<role>procedure)"
                rf"(?=[,;.]|\s+(?:and|with|for|within)\b|\s*$)",
                rf"(?:please\s+)?(?:look up|read|show|explain)\s+(?:the\s+)?"
                rf"{product}\s+(?P<subject>[^,;.\n]{{1,100}}?)\s+policy(?=[,;.\s]|$)",
            )
            match = next((m for pattern in patterns if (m := re.match(pattern, text, re.I))), None)
        if match is None or re.match(r"^(?:和|与|及|以及|and\b)", match["subject"], re.I):
            return None
        remaining, fields = text[match.end():].lower(), {"text", "citation"}
        if match.groupdict().get("role"):
            fields.add(_ROLES[match["role"].lower()])
        if re.search(r"(?:the\s+)?selected\s+scope\s+and\s+time\b", remaining):
            fields.update(("scope", "as_of"))
            remaining = re.sub(r"(?:the\s+)?selected\s+scope\s+and\s+time\b", " ", remaining)
        for phrase, role in sorted(_ROLES.items(), key=lambda item: len(item[0]), reverse=True):
            if phrase in remaining:
                remaining = remaining.replace(phrase, " ")
                fields.add(role)
        if _GLUE.sub("", remaining):
            return None
        task = {"kind": KIND, "version": 1, "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
                "subject": match["subject"].strip(), "fields": sorted(fields), "scope": scope.model_dump(mode="json"),
                "run_id": run_id, "execution_attempt": attempt, "case_id": context["case_id"],
                "actor_id": context["actor_id"], "instance": context["instance"], "auth_version": context.get("auth_version", 0),
                "case_version": context["expected_case_version"], "control_version": context["expected_control_version"],
                "input_receipt": copy.deepcopy(receipt), "business_effects": [],
                "language": snap.get("language") or language_of(message)}
        return {**task, "id": digest(task)}
    except (KeyError, TypeError, AttributeError, ValueError):
        return None


def valid_task(task):
    try:
        scope = PolicyScope.model_validate(task["scope"])
        fields = task["fields"]
        return (task["kind"] == KIND and task["version"] == 1
                and task["id"] == digest({k:v for k,v in task.items() if k != "id"})
                and isinstance(fields, list) and fields == sorted(set(fields))
                and set(fields) <= set(_ROLES.values()) and {"text", "citation"} <= set(fields)
                and task["business_effects"] == [] and all((scope.product, scope.region, scope.audience, scope.as_of))
                and scope.model_dump(mode="json") == task["scope"]
                and all(isinstance(task[k], str) and task[k] for k in
                        ("run_id", "case_id", "actor_id", "instance", "subject", "message_sha256"))
                and all(type(task[k]) is int and task[k] >= minimum for k, minimum in
                        (("execution_attempt", 1), ("case_version", 1), ("control_version", 0), ("auth_version", 0))))
    except (KeyError, TypeError, AttributeError, ValueError):
        return False


def subject_name(value):
    """Normalize surface syntax only; unknown/bilingual topics need authoritative aliases."""
    value = value.strip().casefold()
    return re.sub(r"\s+", " ", re.sub(r"\s+policy$|政策$", "", value).strip())


def document_subject(task, document):
    title = document["title"].strip()
    product, version = task["scope"]["product"], document["policy"]["version"]
    if title[:len(product)].casefold() == product.casefold():
        title = title[len(product):].strip().removeprefix("的").strip()
    if title.endswith(version):
        title = title[:-len(version)].strip()
    return subject_name(title)


def subject_matches(task, document):
    return subject_name(task["subject"]) == document_subject(task, document)


def canonical_source(ref, document):
    """Only verified display fields survive the trusted RAG boundary."""
    from app.tools.knowledge_base import document_excerpt, document_heading
    result = {key: copy.deepcopy(ref[key]) for key in
              ("doc_id", "chunk_id", "title", "content", "policy", "policy_revision", "policy_scope", "policy_as_of")}
    chunk = next(c for c in document["chunks"] if c["id"] == ref["chunk_id"])
    result.update(passage=chunk["chunk_index"], excerpt=document_excerpt(ref["content"], 240),
                  doc_title=document_heading(document["content"]) or document["title"])
    for key in ("score", "retrieval"):
        if key in ref:
            result[key] = copy.deepcopy(ref[key])
    return result


def source_payload(ref):
    return copy.deepcopy(ref)


def document_payload(record):
    row = record["document"]
    from app.core.policy_scope import metadata
    return {"id": row["id"], "title": row["title"], "content": row["content"],
            "policy": metadata(row["policy"]).model_dump(mode="json"), "revision": row["policy_revision"],
            "content_hash": row["content_hash"], "chunk_count": row["chunk_count"],
            "chunks": [{k: chunk[k] for k in ("id", "doc_id", "title", "content", "chunk_index", "total_chunks")}
                       for chunk in record["chunks"]]}


def document_valid(document, chunk_text):
    try:
        chunks = document["chunks"]
        expected = chunk_text(document["content"])
        return (bool(expected) and isinstance(document["revision"], str) and bool(document["revision"].strip())
                and document["content_hash"] == policy_digest(
                    [document["title"].strip(), document["content"].strip(), document["policy"]])
                and len(chunks) == document["chunk_count"] == len(expected)
                and [c["chunk_index"] for c in chunks] == list(range(len(expected)))
                and len({c["id"] for c in chunks}) == len(chunks)
                and all(c["doc_id"] == document["id"] and c["title"] == document["title"]
                        and c["total_chunks"] == len(expected) and c["content"] == text
                        for c, text in zip(chunks, expected)))
    except (KeyError, TypeError, AttributeError):
        return False


def source_valid(task, ref, document, *, require_subject=True):
    try:
        from app.tools.knowledge_base import document_excerpt, document_heading
        checked = policy_source_metadata(ref)
        alias_ok = (ref.get("excerpt", document_excerpt(ref["content"], 240)) == document_excerpt(ref["content"], 240)
                    and ref.get("doc_title", document_heading(document["content"]) or document["title"])
                    == (document_heading(document["content"]) or document["title"]))
        return (not ref.get("fallback") and not blocked_evidence(ref) and alias_ok and valid_task(task) and checked is not None and checked["query_scope"] == task["scope"]
                and checked["as_of"] == PolicyScope.model_validate(task["scope"]).as_of.isoformat()
                and ref["policy_revision"] == document["revision"] and checked["policy"] == document["policy"]
                and ref["doc_id"] == document["id"] and ref["title"] == document["title"]
                and any(c["id"] == ref["chunk_id"] and c["content"] == ref["content"]
                        and all(ref.get(k, c["chunk_index"]) == c["chunk_index"] for k in ("passage", "chunk"))
                        for c in document["chunks"])
                and (not require_subject or subject_matches(task, document)))
    except (KeyError, TypeError, AttributeError, ValueError):
        return False


def _key(key=None):
    return key if key is not None else os.getenv("SQL_AGENT_API_KEY", "").strip()


def _signature(proof, key):
    return hmac.new(key.encode(), ("helpdesk-policy-projection-v1:"+digest(proof)).encode(), hashlib.sha256).hexdigest()


def candidate_snapshot(task, documents):
    """A complete authority lookup attests to uniqueness, never a top-K result."""
    return {"scope": copy.deepcopy(task["scope"]), "subject": subject_name(task["subject"]),
            "documents": [{"id": document["id"], "revision": document["revision"],
                           "sha256": digest(document)} for document in sorted(documents, key=lambda d: d["id"])]}


def bind_sources(task, refs, document, *, candidates, key=None, state=None):
    key = _key(key)
    if (not key or not refs or blocked_evidence(state) or candidates != candidate_snapshot(task, [document])
            or not all(source_valid(task, ref, document) for ref in refs)):
        return None
    proof = {"kind": KIND, "task": copy.deepcopy(task), "state": copy.deepcopy(state), "sources": [source_payload(r) for r in refs],
             "document": copy.deepcopy(document), "candidate_set": copy.deepcopy(candidates), "retrieved_at": utc_now().isoformat()}
    return {**proof, "signature": _signature(proof, key)}


def field_witnesses(task, document):
    """Recomputable source paths and exact spans, never inferred missing fields."""
    body = document["content"]
    witnesses = {
        "text": {"path": "document.content", "sha256": hashlib.sha256(body.encode()).hexdigest()},
        "citation": {"doc_id": document["id"], "title": document["title"], "revision": document["revision"]},
        "version": {"path": "document.policy.version", "value": document["policy"]["version"]},
        "scope": {"path": "task.scope", "value": task["scope"]},
        "as_of": {"path": "task.scope.as_of", "value": task["scope"]["as_of"]},
    }
    if "processing_duration" in task["fields"]:
        number = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十]+)"
        unit = r"(?:个)?(?:工作日|自然日|天|日|小时|分钟|周|个月)|business days?|working days?|days?|hours?|minutes?|weeks?"
        prefix = rf"(?:^|[。；;\n])\s*(?:-\s*)?(?:(?:本政策的?|政策规定|政策的?)\s*)?(?:{re.escape(task['subject'])}的?\s*)?"
        pattern = (prefix + rf"(?P<label>处理时限|办理时限|processing time)\s*(?:为|是|[:：])\s*"
                   rf"(?P<value>{number}(?:\s*[-~至到]\s*{number})?)\s*(?P<unit>{unit})(?![\w])")
        # Chinese units can adjoin prose; ASCII words need a lexical boundary.
        pattern = pattern.replace(r"(?![\w])", r"(?![a-zA-Z0-9])")
        matches = list(re.finditer(pattern, body, re.I))
        if len(matches) != 1:
            return None
        value = matches[0]
        from app.core.answer_policy import _num
        values = [float(v) if re.fullmatch(r"\d+(?:\.\d+)?", v) else _num(v.replace("两", "二"))
                  for v in re.split(r"\s*[-~至到]\s*", value["value"])]
        if not values or any(not math.isfinite(v) or v < 0 for v in values) or values[0] > values[-1]:
            return None
        witnesses["processing_duration"] = {"path": "document.content", "start": value.start("label"), "end": value.end("unit"),
            "text": body[value.start("label"):value.end("unit")], "label": value["label"],
            "value": value["value"], "unit": value["unit"],
            "label_span": list(value.span("label")), "value_span": list(value.span("value")), "unit_span": list(value.span("unit"))}
    if "procedure" in task["fields"]:
        head = re.search(r"(?:操作说明|办理步骤|操作步骤|办理流程|procedure|steps)\s*[:：]", body, re.I)
        base = head.end() if head else 0
        section = body[base:]
        markers = list(re.finditer(r"第([一二三四五六七八九十\d]+)步|(?:^|\n)\s*(\d+)[.、)]", section))
        ordered, imperative = False, False
        if not markers:
            markers = list(re.finditer(r"(?:^|[，,；;\n])\s*(首先|先|接着|再|然后|最后)", section))
            ordered = True
        if not markers and head and re.match(
                r"\s*(?:登记|核对|填写|提交|交由|收集|联系|检查|确认|查阅|保存|记录|审核|审批|通知|准备|上传|提供)"
                r"[^\s。.!?！？；;，,：:\n]{2,}", section):
            markers = [re.match(r"\s*", section)]
            imperative, ordered = True, False
        if not markers:
            verbs = r"register|verify|submit|review|record|contact|check|collect|attach|provide"
            if not re.match(rf"\s*(?:{verbs})\b", section, re.I):
                return None
            markers = list(re.finditer(rf"(?:^|[;,]\s*(?:and\s+)?)(?=(?:{verbs})\b)", section, re.I))
            imperative, ordered = True, False
        from app.core.answer_policy import _num
        spans = []
        for index, marker in enumerate(markers):
            if ordered:
                if (index == 0 and marker[1] not in {"先", "首先"}
                        or index > 0 and marker[1] in {"先", "首先"}):
                    return None
            elif not imperative and _num(marker[1] or marker[2]) != index + 1:
                return None
            start = base + marker.end()
            end = base + markers[index + 1].start() if index + 1 < len(markers) else len(body)
            terminal = re.search(r"[。.!?！？\n]", body[start:end])
            if terminal:
                end = start + terminal.end()
            text = body[start:end].strip(" ：:，,。；;\n")
            if (not text or re.search(r"未提供|待补|未知|待定|未记录|未说明|暂无|\b(?:tbd|unknown|not provided)\b", text, re.I)
                    or not re.search(r"登记|核对|填写|提交|交由|收集|联系|检查|确认|查阅|保存|记录|审核|审批|通知|准备|上传|提供|"
                                     r"\b(?:register|verify|submit|review|record|contact|check|collect|attach|provide)\b", text, re.I)):
                return None
            spans.append({"start": start, "end": end, "text": body[start:end]})
        if not spans:
            return None
        witnesses["procedure"] = {"path": "document.content", "steps": spans}
    return {role: witnesses[role] for role in task["fields"]}


def projection(task, knowledge, *, key=None, at=None):
    try:
        proof = copy.deepcopy(knowledge["_policy_dispatch"])
        signature = proof.pop("signature")
        if (blocked_evidence(knowledge) or not valid_task(task) or not _key(key) or proof["kind"] != KIND or proof["task"] != task
                or not hmac.compare_digest(signature, _signature(proof, _key(key)))
                or proof["state"] != trace_state(knowledge)
                or not fresh(proof["retrieved_at"], at or utc_now())
                or knowledge.get("attempted") is not True or knowledge.get("lookup_status") != "found"
                or knowledge.get("policy_lookup") is not True or (knowledge.get("evidence_ok") is not None and knowledge.get("evidence_ok") is not True)
                or [source_payload(r) for r in knowledge["sources"]] != proof["sources"]):
            return None
        document = proof["document"]
        from app.tools.knowledge_base import KnowledgeBase
        if (not document_valid(document, KnowledgeBase._chunk_text)
                or proof["candidate_set"] != candidate_snapshot(task, [document])):
            return None
        if not all(source_valid(task, r, document) for r in proof["sources"]):
            return None
        if field_witnesses(task, document) is None:
            return None
        return proof
    except (KeyError, TypeError, AttributeError, ValueError):
        return None


def proposal(task, knowledge, *, key=None, at=None):
    proof = projection(task, knowledge, key=key, at=at)
    if proof is None:
        return None
    ref = {**proof["sources"][0], "content": proof["document"]["content"]}
    response = render_draft([ref], {}, language=task["language"])
    receipt = {"kind": KIND, "version": 1, "task": task, "proof": proof | {"signature": knowledge["_policy_dispatch"]["signature"]},
               "body_sha256": hashlib.sha256(response.encode()).hexdigest(),
               "coverage": field_witnesses(task, proof["document"])}
    receipt["id"] = digest(receipt)
    return {"response": response, "answer_origin": ORIGIN, "answer_model_invoked": False,
            "fact_receipt": receipt, "agent_type": "system", "agent_types": [], "handoff": False, "escalated": False,
            "knowledge_used": True, "knowledge_sources": knowledge["sources"],
            "knowledge": {k:v for k,v in knowledge.items() if k != "sources"}, "business_data": {}, "policy_violations": []}


def receipt_valid(receipt, refs, *, message, context, run_id, attempt, body, at=None):
    try:
        task = receipt["task"]
        if receipt["kind"] != KIND or task != make_task(message, context, run_id, task["execution_attempt"]) or task["execution_attempt"] > attempt:
            return False
        knowledge = {**receipt["proof"]["state"], "sources": refs, "_policy_dispatch": receipt["proof"]}
        expected = proposal(task, knowledge, at=at)
        return bool(expected and expected["response"] == body and expected["fact_receipt"] == receipt)
    except (KeyError, TypeError, AttributeError, ValueError):
        return False
