"""Explicit published policies and execution cells for pre-policy regression tests.

No production default is patched: lifecycle/negative tests use the real defaults.
"""
import copy
import hashlib
import json
from datetime import UTC, datetime

from app.core.policy_scope import PolicyMetadata, policy_digest
from app.tools.knowledge_base import KnowledgeBase as RealKnowledgeBase

PUBLISHED = PolicyMetadata(status="published", version="fixture-v1", effective_at="2020-01-01T00:00:00Z").model_dump(mode="json")


class PublishedKnowledgeBase(RealKnowledgeBase):
    async def import_documents(self, documents, *args, **kwargs):
        return await super().import_documents([{**d, "policy": d.get("policy") or PUBLISHED} for d in documents], *args, **kwargs)


def policy_hits(rows):
    return [{**r, "policy": PUBLISHED, "policy_revision": "fixture-v1", "policy_scope": {"product":"","region":"","audience":"","as_of":None},
             "policy_as_of":"2026-11-06T00:00:00Z"} for r in rows]


def with_evidence(payload, rows=None):
    data = copy.deepcopy(payload)
    now = datetime.now(UTC).isoformat()
    data.setdefault("execution_status", "success")
    data.setdefault("route", "database_query")
    data["observed_at"] = now
    data["evidence"] = []
    for si, step in enumerate(data.get("steps", [])):
        step.setdefault("rows", copy.deepcopy(rows or [{"order_ref":"SO-1", "lead_time":"3个工作日", "status":"scheduled"}]))
        step.setdefault("row_count", len(step["rows"]))
        step.setdefault("truncated", False)
        step.setdefault("final_sql", "SELECT order_ref,lead_time,status FROM orders")
        step.setdefault("as_of", now)
        step.setdefault("observed_at", now)
        step.setdefault("as_of_source", "query_time")
        step.setdefault("column_units", {})
        step.setdefault("source_records", [])
        if not step.get("success"):
            continue
        qh=hashlib.sha256(step["final_sql"].encode()).hexdigest()
        for ri,row in enumerate(step["rows"]):
            for field,value in row.items():
                identity=[step["database"],qh,step["as_of"],si,ri,field,value]
                data["evidence"].append(dict(evidence_id="ev_"+hashlib.sha256(json.dumps(identity,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),
                    entity=dict(kind="query_row",query_hash=qh,row_index=ri),field=field,value=value,unit=step["column_units"].get(field),
                    as_of=step["as_of"],as_of_source=step["as_of_source"],database=step["database"],step_index=si,row_index=ri,query_hash=qh,source_records=step["source_records"],issues=[]))
    return data


async def publish_chunks(store, chunks):
    for chunk in chunks:
        chunk["doc_id"] = "test_doc_"+chunk["id"]
        await store.kb_doc_add(dict(id=chunk["doc_id"],title=chunk["title"],content=chunk["content"],content_hash=policy_digest(chunk["id"]),
            created_at=0,policy=PUBLISHED,policy_revision="fixture-v1"))
