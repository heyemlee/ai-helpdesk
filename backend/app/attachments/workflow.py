"""Bounded image work; proposals are adopted only inside the Case commit fence."""
from __future__ import annotations

import asyncio
import json

from fastapi import HTTPException

from app.attachments.review_failure import extraction_failure
from app.attachments.verification import Extraction, order_context, verification_query, verify
from app.runs.store import _NOW


class MaterialWorkflow:
    def __init__(self, attachments, extractor, query, *, query_timeout_s=90):
        self.attachments, self.extractor, self.query = attachments, extractor, query
        self.query_timeout_s = query_timeout_s

    async def review(self, run):
        ctx = run.case_context
        actor = await self.attachments.actors.from_snapshot(ctx)
        reviews, queries = [], {}
        for material in ctx["snapshot"]["review_materials"]:
            attachment_id, sha = material["attachment_id"], material["sha256"]
            review = {"attachment_id": attachment_id, "sha256": sha, "fields": [], "query_outcome": "unavailable"}
            stage = "material"
            try:
                # Even cached extraction must reauthorize AND check original and
                # preview bytes. An object URL is never stored in a Run.
                _, row = await self.attachments.content(actor, attachment_id)
                if row["sha256"] != sha:
                    raise HTTPException(409, {"code": "attachment_changed"})
                preview, _ = await self.attachments.content(actor, attachment_id, preview=True)
                version = self.extractor.version
                stage = "extract"
                cached = await self.attachments.db.fetchrow(
                    "SELECT result FROM attachment_extractions WHERE attachment_id=$1 AND sha256=$2 AND extractor_version=$3",
                    attachment_id, sha, version)
                if cached:
                    raw = cached["result"]
                    extraction = Extraction.model_validate(json.loads(raw) if isinstance(raw, str) else raw)
                else:
                    extraction = await self.extractor.extract(preview)
                    await self.attachments.actor(actor)
                    await self.attachments.db.execute(
                        f"INSERT INTO attachment_extractions VALUES($1,$2,$3,$4::jsonb,{_NOW}) ON CONFLICT DO NOTHING",
                        attachment_id, sha, version, extraction.model_dump_json())
                stage = "verify"
                ref, error = order_context(ctx["snapshot"], extraction)
                data = {}
                if ref and not error:
                    await self.attachments.actor(actor)
                    question = verification_query(ref, extraction)
                    key = (ref, question)
                    if key not in queries:
                        try:
                            queries[key] = await asyncio.wait_for(self.query(question, run), self.query_timeout_s)
                        except TimeoutError:
                            queries[key] = {"unavailable": True, "query_error": "timeout"}
                        except Exception:
                            queries[key] = {"unavailable": True, "query_error": "query_failed"}
                    data = queries[key]
                if data.get("query_error"):
                    review["query_error"] = data["query_error"]
                review.update(verify(extraction, data, ref, error, ctx["snapshot"]["issue_type"]))
                review["extractor_version"] = version
            except HTTPException as exc:
                if exc.status_code in {401, 403, 404}:
                    raise
                review["query_outcome"] = (exc.detail.get("code", "unavailable")
                                           if isinstance(exc.detail, dict) else "unavailable")
            except Exception as exc:
                review["query_outcome"] = "extraction_failed"
                review["failure_reason"] = extraction_failure(exc) if stage == "extract" else "incomplete"
            reviews.append(review)
        await self.attachments.actor(actor)
        return reviews
