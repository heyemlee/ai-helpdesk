"""Real pixels, object stores and Case commit fences; no external model calls."""

import asyncio
import copy
import io
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from PIL import Image
from test_cases import ACTOR, action, claim, delivery, follow, new, scalar, worker_for
from test_cases import pair as pair  # noqa: F401

from app.api.attachment_api import router
from app.attachments.body_limit import MAX_BODY, UploadBodyLimit
from app.attachments.images import MAX_BYTES, InvalidImage, validate_image
from app.attachments.objects import LocalObjects, ObjectFailure, S3Objects
from app.attachments.service import AttachmentService
from app.attachments.verification import Candidate, Extraction, verify
from app.attachments.workflow import MaterialWorkflow
from app.core.actor import ActorDirectory, get_current_actor
from app.core.admission import estimate_tokens
from app.events import EventLog
from app.tickets.case_execution import CaseExecutor


def pixels(fmt="PNG", size=(80, 60), **kwargs):
    out = io.BytesIO()
    Image.new("RGB", size, "white").save(out, format=fmt, **kwargs)
    return out.getvalue()


def extraction(**values):
    return Extraction(
        document_type="parts",
        fields=[
            Candidate(
                field=k,
                value=v,
                unit="pcs" if k == "quantity" else None,
                kind="missing" if k == "quantity" else "unknown",
                readability="clear",
                location="test",
            )
            for k, v in (values or {"order_ref": "SO-1", "model": "A", "quantity": "2"}).items()
        ],
    )


def evidence(rows=None):
    return {
        "observed_at": datetime.now(UTC).isoformat(),
        "execution_status": "success",
        "queried_database": True,
        "has_risk": False,
        "degraded": False,
        "selected_databases": ["erp_db"],
        "trace_id": "sql-trace",
        "run_id": "sql-run",
        "result_truncated": False,
        "steps": [
            {
                "database": "erp_db",
                "final_sql": "SELECT " + ", ".join((rows or [{"order_ref": "SO-1", "model": "A", "missing_qty": 2}])[0]) + " FROM image_records",
                "success": True,
                "truncated": False,
                "rows": rows or [{"order_ref": "SO-1", "model": "A", "missing_qty": 2}],
                "row_count": len(rows) if rows is not None else 1,
                "column_units": {"missing_qty": "pcs"},
                "as_of": datetime.now(UTC).isoformat(),
                "as_of_source": "query_time",
                "source_records": [{"id": "line-1"}],
            }
        ],
    }


@pytest.mark.parametrize("fmt,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
def test_real_image_decoding_and_metadata_free_preview(fmt, mime):
    data = pixels(fmt)
    result = validate_image(data, mime)
    assert result.media_type == mime and (result.width, result.height) == (80, 60)
    assert len(result.sha256) == 64
    with Image.open(io.BytesIO(result.preview)) as image:
        assert image.format == "JPEG" and not image.getexif()


@pytest.mark.parametrize(
    "data,mime,code",
    [
        (b"<svg/onload=alert(1)>", "image/png", "image_corrupt"),
        (pixels(), "image/jpeg", "image_type"),
        (b"x" * (MAX_BYTES + 1), "image/png", "image_size"),
        (pixels(size=(8193, 1)), "image/png", "image_dimensions"),
        (pixels()[:40], "image/png", "image_corrupt"),
    ],
)
def test_reject_invalid_images(data, mime, code):
    with pytest.raises(InvalidImage, match=code):
        validate_image(data, mime)


def test_exif_rotation_and_multiframe_rejection():
    exif = Image.Exif()
    exif[274] = 6
    result = validate_image(pixels("JPEG", exif=exif), "image/jpeg")
    assert (result.width, result.height) == (60, 80)
    out = io.BytesIO()
    Image.new("RGB", (10, 10)).save(out, format="PNG", save_all=True, append_images=[Image.new("RGB", (10, 10), "red")])
    with pytest.raises(InvalidImage, match="image_frames"):
        validate_image(out.getvalue(), "image/png")


async def test_body_limit_counts_chunked_before_parsing():
    app = AsyncMock()
    middleware = UploadBodyLimit(app)
    send = AsyncMock()
    receive = AsyncMock(
        side_effect=[{"type": "http.request", "body": b"x" * (MAX_BODY // 2 + 1), "more_body": True}] * 2
    )
    await middleware({"type": "http", "method": "POST", "path": "/attachments", "headers": []}, receive, send)
    app.assert_not_awaited()
    assert send.await_args_list[0].args[0]["status"] == 413


def test_image_admission_does_not_count_base64_but_retains_tool_arguments():
    def count(data):
        return estimate_tokens({"messages": [{"content": [{"type": "image", "source": {"data": data}}]}]})

    assert count("a") == count("a" * 1000000) >= 4096
    assert estimate_tokens({"messages": [{"content": [{"type": "tool_use", "input": {"data": "x" * 50000}}]}]}) >= 25000


def test_structured_rows_match_not_natural_language_answer():
    assert all(i["outcome"] == "matched" for i in verify(extraction(), evidence(), "SO-1")["fields"])
    data = evidence()
    data["steps"][0]["rows"] = []
    data["answer"] = "SO-1 A 缺件2pcs"
    assert all(i["outcome"] != "matched" for i in verify(extraction(), data, "SO-1")["fields"])


@pytest.mark.parametrize(
    "broken", ["units", "as_of", "provenance", "failed_step", "truncated", "other_order", "unknown_time"]
)
def test_untrusted_source_is_not_confirmation(broken):
    data = evidence()
    step = data["steps"][0]
    if broken == "units":
        step["column_units"] = {}
    if broken == "as_of":
        step["as_of"] = "yesterday"
    if broken == "provenance":
        step["source_records"] = []
        data["trace_id"] = data["run_id"] = ""
    if broken == "failed_step":
        data["steps"].append({**copy.deepcopy(step), "success": False})
    if broken == "truncated":
        step["truncated"] = True
    if broken == "other_order":
        step["rows"][0]["order_ref"] = "SO-2"
    if broken == "unknown_time":
        step["as_of_source"] = "unknown"
    assert not any(i["outcome"] == "matched" for i in verify(extraction(), data, "SO-1")["fields"])


def test_multiline_order_never_borrows_other_models_quantity_or_combines_rows():
    data = evidence(
        [{"order_ref": "SO-1", "model": "A", "missing_qty": 1}, {"order_ref": "SO-1", "model": "B", "missing_qty": 2}]
    )
    assert not any(i["outcome"] == "matched" for i in verify(extraction(), data, "SO-1")["fields"])
    matching = verify(extraction(model="B", quantity="2"), data, "SO-1")["fields"]
    assert all(i["outcome"] == "matched" and i["source"]["row_index"] == 1 for i in matching)


def test_line_quantity_without_model_anchor_and_partial_rows_cannot_confirm():
    assert verify(extraction(quantity="2"), evidence(), "SO-1")["fields"][0]["outcome"] != "matched"
    data = evidence()
    data["steps"][0]["row_count"] = 2
    assert not any(f["outcome"] == "matched" for f in verify(extraction(), data, "SO-1")["fields"])


@pytest.mark.parametrize("prefix", ["reply_", "adopt_", "verified_"])
def test_internal_message_namespaces_are_reserved(prefix):
    from app.tickets.cases import CaseInput

    with pytest.raises(ValueError):
        CaseInput(message_id=prefix + "test", conv_id="c", new_case=True, message="input")


async def test_local_atomic_create_once_and_retry_after_interruption(tmp_path, monkeypatch):
    objects = LocalObjects(str(tmp_path))
    key = "staff/at_test/original"
    real_link = os.link
    monkeypatch.setattr(os, "link", lambda *_: (_ for _ in ()).throw(OSError("crash before publish")))
    with pytest.raises(OSError):
        await objects.put(key, b"complete", "image/png")
    assert not objects.path(key).exists()
    monkeypatch.setattr(os, "link", real_link)
    await asyncio.gather(objects.put(key, b"complete", "image/png"), objects.put(key, b"complete", "image/png"))
    with pytest.raises(ObjectFailure):
        await objects.put(key, b"changed", "image/png")
    assert await objects.get(key) == b"complete" and not list(tmp_path.rglob(".upload-*"))


@pytest.fixture
async def connected(pair, tmp_path):
    a, b = pair
    provider = AsyncMock(return_value=ACTOR)
    service = AttachmentService(a.db, LocalObjects(str(tmp_path)), ActorDirectory(provider), "staff")
    for case in (a, b):
        case.attachments, case.attachment_resolver = service, service.resolve
    return a, b, service, provider


async def upload(service, key="upload_one"):
    return await service.upload(ACTOR, pixels(), "order.png", "image/png", key)


async def start(connected):
    a, b, service, provider = connected
    image = await upload(service)
    receipt = await a.accept_input(ACTOR, new(issue_type="missing_parts", attachment_ids=[image["id"]]))
    run = await claim(a, receipt)
    extractor = AsyncMock()
    extractor.version = "test-sol-responses-v1"
    extractor.extract.return_value = extraction()
    query = AsyncMock(return_value=evidence())
    workflow = MaterialWorkflow(service, extractor, query)
    return a, b, service, provider, image, run, workflow, extractor, query


async def test_authenticated_upload_preview_download_idempotency_and_revocation(connected):
    a, _, service, provider = connected
    app = FastAPI()
    app.state.attachments = service
    app.add_middleware(UploadBodyLimit)
    app.include_router(router)
    app.dependency_overrides[get_current_actor] = lambda: ACTOR
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        response = await client.post(
            "/attachments", files={"file": ("a.png", pixels(), "image/png")}, headers={"Idempotency-Key": "stable"}
        )
        assert response.status_code == 201
        image = response.json()
        assert "object_key" not in image
        again = await client.post(
            "/attachments", files={"file": ("a.png", pixels(), "image/png")}, headers={"Idempotency-Key": "stable"}
        )
        assert again.json()["id"] == image["id"]
        preview = await client.get(f"/attachments/{image['id']}/content?preview=true")
        assert preview.status_code == 200 and preview.headers["cache-control"] == "private, no-store"
        assert (await client.get(f"/attachments/{image['id']}/content")).content == pixels()
        provider.return_value = None
        assert (await client.get(f"/attachments/{image['id']}/content")).status_code == 401
        app.dependency_overrides.clear()
        assert (await client.get(f"/attachments/{image['id']}", headers={"X-API-Key": "shared"})).status_code == 401
    assert await scalar(a.db, "SELECT count(*) FROM attachments") == 1


async def test_atomic_review_continuation_cache_and_no_loop(connected):
    a, _, _, _, image, run, workflow, extractor, query = await start(connected)
    reviews = await workflow.review(run)
    await workflow.review(run)
    assert extractor.extract.await_count == 1 and query.await_count == 2  # DB evidence always fresh
    result, ok = await a.complete(run, {"response": "review", "material_reviews": reviews})
    assert ok and result["next_run_id"] != run.run_id and not result["draft_id"]
    c = await a.get(ACTOR, result["case_id"])
    assert not c["pending_fields"] and c["facts"]["quantity"]["value"] == "2"
    assert any(call.kwargs.get("conn") is not None for call in a.attachments.actors.provider.await_args_list)
    assert c["messages"][0]["attachment_ids"] == [image["id"]]
    assert len(c["image_reviews"]) == 1 and len(c["runs"]) == 2 and not c["drafts"]
    again, _ = await a.complete(run, {"response": "different"})
    assert again == result
    next_run = await claim(a, {"run_id": result["next_run_id"]})
    assert next_run.case_context["materials_reviewed"] and next_run.idempotency_key.startswith("verified_")
    await a.complete(next_run, {"response": "已核对，请确认内部草稿。"})
    c = await a.get(ACTOR, c["id"])
    assert len(c["runs"]) == 2 and len(c["drafts"]) == 1


async def test_different_case_model_cannot_adopt_pictured_models_quantity(connected):
    a, _, service, _ = connected
    image = await upload(service)
    receipt = await a.accept_input(
        ACTOR, new(issue_type="missing_parts", facts={"model": "B"}, attachment_ids=[image["id"]])
    )
    run = await claim(a, receipt)
    extractor = AsyncMock()
    extractor.version = "v1"
    extractor.extract.return_value = extraction()  # picture and DB describe model A
    reviews = await MaterialWorkflow(service, extractor, AsyncMock(return_value=evidence())).review(run)
    await a.complete(run, {"response": "review", "material_reviews": reviews})
    c = await a.get(ACTOR, receipt["case_id"])
    assert c["facts"]["model"]["value"] == "B" and "quantity" not in c["facts"]
    assert "image:quantity" in c["pending_fields"]


@pytest.mark.parametrize("change", ["revoke", "hash", "takeover", "closed", "supplement", "lease"])
async def test_review_rechecks_permissions_hash_case_and_lease_at_commit(connected, change):
    a, b, service, provider, image, run, workflow, _, _ = await start(connected)
    reviews = await workflow.review(run)
    c = await a.get(ACTOR, run.case_context["case_id"])
    if change == "revoke":
        provider.return_value = None
    if change == "hash":
        row = await service.get(ACTOR, image["id"])
        service.objects.path(row["object_key"]).write_bytes(b"tampered")
    if change == "takeover":
        await b.mutate(ACTOR, c["id"], action(c, "takeover"))
    if change == "closed":
        await b.mutate(ACTOR, c["id"], action(c, "status", status="resolved"))
    if change == "supplement":
        await b.accept_input(ACTOR, follow(c, message="新材料"))
    if change == "lease":
        await a.db.execute("UPDATE agent_runs SET lease_until=0 WHERE run_id=$1", run.run_id)
    if change == "lease":
        with pytest.raises(HTTPException):
            await a.complete(run, {"response": "review", "material_reviews": reviews})
    else:
        result, ok = await a.complete(run, {"response": "review", "material_reviews": reviews})
        assert not ok and "next_run_id" not in result
    assert await scalar(a.db, "SELECT count(*) FROM case_image_reviews") == 0
    assert await scalar(a.db, "SELECT count(*) FROM case_drafts") == 0


async def test_complete_rollback_cannot_leave_continuation_or_review(connected, monkeypatch):
    a, _, _, _, _, run, workflow, _, _ = await start(connected)
    reviews = await workflow.review(run)
    real = a._event

    async def crash(conn, c, kind, *args, **kwargs):
        if kind == "images_reviewed":
            raise RuntimeError("process interrupted before terminal write")
        return await real(conn, c, kind, *args, **kwargs)

    monkeypatch.setattr(a, "_event", crash)
    with pytest.raises(RuntimeError):
        await a.complete(run, {"response": "review", "material_reviews": reviews})
    assert await scalar(a.db, "SELECT count(*) FROM agent_runs") == 1
    assert await scalar(a.db, "SELECT count(*) FROM case_image_reviews") == 0
    monkeypatch.setattr(a, "_event", real)
    result, ok = await a.complete(run, {"response": "review", "material_reviews": reviews})
    assert ok and result["next_run_id"]


async def test_upload_failure_recovers_and_bound_material_cannot_be_deleted(connected, monkeypatch):
    a, _, service, _ = connected
    put = service.objects.put
    monkeypatch.setattr(service.objects, "put", AsyncMock(side_effect=ObjectFailure("offline")))
    with pytest.raises(HTTPException):
        await upload(service)
    assert await scalar(a.db, "SELECT state FROM attachments") == "failed"
    monkeypatch.setattr(service.objects, "put", put)
    image = await upload(service)
    receipt = await a.accept_input(ACTOR, new(attachment_ids=[image["id"]]))
    with pytest.raises(HTTPException):
        await service.discard(ACTOR, image["id"])
    await a.db.execute("UPDATE attachments SET updated_at=0")
    assert (await service.cleanup())["removed"] == 0
    assert (await a.get(ACTOR, receipt["case_id"]))["materials"]


async def test_transaction_binding_rejects_discard_after_preflight(connected, monkeypatch):
    a, _, service, _ = connected
    image = await upload(service)

    async def resolve(actor, ids):
        refs = await service.resolve(actor, ids)
        await service.discard(actor, image["id"])
        return refs

    a.attachment_resolver = resolve
    with pytest.raises(HTTPException):
        await a.accept_input(ACTOR, new(attachment_ids=[image["id"]]))
    assert await scalar(a.db, "SELECT count(*) FROM tickets") == 0


async def test_case_executor_invokes_image_work_before_missing_fields(connected):
    a, _, _, _, _, run, workflow, _, query = await start(connected)
    ctx = AsyncMock()
    ctx.run = run
    ctx.degrade.shed = False
    ctx.scope = None
    ctx.commit.return_value = True
    executor = CaseExecutor(
        a, prepare=AsyncMock(), answer=AsyncMock(), event_log=EventLog.disabled(), materials=workflow
    )
    result = await executor(ctx)
    assert result.result["next_run_id"] and query.await_count == 1
    executor.answer.assert_not_awaited()


async def test_slow_object_reads_do_not_block_real_worker_heartbeat(connected, monkeypatch):
    a, _, service, _ = connected
    image = await upload(service)
    receipt = await a.accept_input(ACTOR, new(issue_type="missing_parts", attachment_ids=[image["id"]]))
    extractor = AsyncMock()
    extractor.version = "test-v1"
    extractor.extract.return_value = extraction()
    workflow = MaterialWorkflow(service, extractor, AsyncMock(return_value=evidence()))
    real_get = service.objects.get

    async def slow(key):
        await asyncio.sleep(0.25)  # each read exceeds the whole worker lease
        return await real_get(key)

    monkeypatch.setattr(service.objects, "get", slow)
    executor = CaseExecutor(
        a, prepare=AsyncMock(), answer=AsyncMock(), event_log=EventLog.disabled(), materials=workflow
    )
    worker = worker_for(a, executor, lease_s=0.15)
    heartbeat = AsyncMock(wraps=worker.store.heartbeat)
    worker.store.heartbeat = heartbeat
    await asyncio.wait_for(worker._handle(await delivery(worker, receipt["run_id"]), False), 5)
    run = await a.run(receipt["run_id"])
    assert run.status == "succeeded" and run.attempt == 1 and run.result["next_run_id"]
    assert heartbeat.await_count > 5


async def test_image_pending_is_server_owned_and_clearer_image_can_resolve_it(connected):
    a, _, service, _, _, run, workflow, extractor, _ = await start(connected)
    extractor.extract.return_value = extraction(order_ref="SO-1", model="A", quantity="9")
    result, _ = await a.complete(run, {"response": "review", "material_reviews": await workflow.review(run)})
    c = await a.get(ACTOR, result["case_id"])
    assert "image:quantity" in c["pending_fields"]
    with pytest.raises(ValueError):
        follow(c, facts={"image:quantity": "yes"})
    receipt = await a.accept_input(ACTOR, follow(c, message="cannot clear", pending_fields=[]))
    assert "image:quantity" in (await a.get(ACTOR, c["id"]))["pending_fields"]
    # Fresh image in an explicit supplement replaces the reviewed batch. Old
    # conflicting images remain in history but do not permanently block it.
    corrected = await service.upload(ACTOR, pixels(size=(100, 60)), "clear.png", "image/png", "clear")
    c = await a.get(ACTOR, c["id"])
    receipt = await a.accept_input(ACTOR, follow(c, attachment_ids=[corrected["id"]]))
    run = await claim(a, receipt)
    extractor.extract.return_value = extraction()
    await a.complete(run, {"response": "review", "material_reviews": await workflow.review(run)})
    c = await a.get(ACTOR, c["id"])
    assert not c["pending_fields"] and c["facts"]["quantity"]["value"] == "2"


@pytest.mark.parametrize("unreadable", ["failed", "empty"])
async def test_mixed_image_batch_keeps_readability_pending_until_clear_supplement(connected, unreadable):
    a, _, service, _ = connected
    images = [await upload(service, key) for key in ("mixed-one", "mixed-two")]
    receipt = await a.accept_input(ACTOR, new(issue_type="missing_parts", attachment_ids=[i["id"] for i in images]))
    run = await claim(a, receipt)
    extractor = AsyncMock()
    extractor.version = "mixed-batch-v1"
    extractor.extract.side_effect = [
        RuntimeError("model unavailable") if unreadable == "failed" else Extraction(document_type="unknown", fields=[]),
        extraction(),
    ]
    workflow = MaterialWorkflow(service, extractor, AsyncMock(return_value=evidence()))
    reviews = await workflow.review(run)
    assert not reviews[0]["fields"] and reviews[1]["fields"]
    result, applied = await a.complete(run, {"response": "review", "material_reviews": reviews})
    c = await a.get(ACTOR, receipt["case_id"])
    assert applied and c["facts"]["quantity"]["value"] == "2"
    assert c["pending_fields"] == ["image:readability"] and c["status"] == "waiting_for_info"

    # The durable continuation reports incomplete verification instead of preparing
    # a business answer from the other, successfully extracted image alone.
    ctx = AsyncMock()
    ctx.run = await claim(a, {"run_id": result["next_run_id"]})
    ctx.degrade.shed, ctx.scope, ctx.commit.return_value = False, None, True
    executor = CaseExecutor(a, prepare=AsyncMock(), answer=AsyncMock(), event_log=EventLog.disabled(), materials=workflow)
    outcome = await executor(ctx)
    assert outcome.result["needs_clarification"] and "核验" in outcome.result["response"]
    assert "清晰图片" not in outcome.result["response"]
    executor.prepare.assert_not_awaited()
    executor.answer.assert_not_awaited()

    # An explicit new, fully readable batch can resolve the earlier failure;
    # the unreadable original remains in the Case history.
    corrected = await service.upload(ACTOR, pixels(size=(100, 60)), "clear.png", "image/png", "mixed-corrected")
    c = await a.get(ACTOR, c["id"])
    receipt = await a.accept_input(ACTOR, follow(c, attachment_ids=[corrected["id"]]))
    run = await claim(a, receipt)
    extractor.extract.side_effect, extractor.extract.return_value = None, extraction()
    result, applied = await a.complete(run, {"response": "review", "material_reviews": await workflow.review(run)})
    c = await a.get(ACTOR, c["id"])
    assert applied and not c["pending_fields"] and c["status"] == "in_progress"
    assert len(c["materials"]) == len(c["image_reviews"]) == 3
    assert not (await a.run(result["next_run_id"])).case_context["snapshot"]["pending_fields"]


async def test_crashed_upload_cleanup_removes_temporary_objects(connected):
    a, _, service, _ = connected
    image = await upload(service)
    row = await service.get(ACTOR, image["id"])
    temp = service.objects.path(row["object_key"]).parent / ".upload-crash"
    temp.write_bytes(b"partial")
    await a.db.execute("UPDATE attachments SET state='staging',updated_at=0")
    assert (await service.cleanup())["removed"] == 1
    assert not temp.exists()


async def test_upload_retry_cannot_resurrect_a_gc_claim(connected):
    a, b, service, _ = connected
    image = await upload(service)
    await a.db.execute("UPDATE attachments SET state='failed',updated_at=0 WHERE id=$1", image["id"])
    async with b.db.transaction() as conn:
        await conn.execute("UPDATE attachments SET state='deleting' WHERE id=$1", image["id"])
        task = asyncio.create_task(upload(service))
        await asyncio.sleep(0.05)
        assert not task.done()
    with pytest.raises(HTTPException) as exc:
        await task
    assert exc.value.status_code == 409
    assert await scalar(a.db, "SELECT state FROM attachments WHERE id=$1", image["id"]) == "deleting"


async def test_database_preserves_image_pending_state_for_older_workers(connected):
    import asyncpg
    a, _, service, _ = connected
    image = await upload(service)
    receipt = await a.accept_input(ACTOR, new(attachment_ids=[image["id"]]))
    c = await a.get(ACTOR, receipt["case_id"])
    assert "image:review" in c["pending_fields"]
    # Same action an old CaseInput/worker would attempt, bypassing new validators.
    with pytest.raises(asyncpg.CheckViolationError):
        await a.db.execute("UPDATE ticket_case_state SET pending_fields='[]'::jsonb WHERE case_id=$1", c["id"])
    with pytest.raises(asyncpg.CheckViolationError):
        await a.db.execute("UPDATE ticket_case_state SET facts=$2::jsonb WHERE case_id=$1", c["id"], '{"image:review":{"value":"yes"}}')
    # A worker without the stage-3 extractor can only produce a clarification.
    run = await claim(a, receipt)
    ctx = AsyncMock()
    ctx.run, ctx.scope, ctx.degrade.shed = run, None, False
    executor = CaseExecutor(a, prepare=AsyncMock(), answer=AsyncMock(), event_log=EventLog.disabled())
    result = await executor(ctx)
    assert result.result["needs_clarification"]
    executor.answer.assert_not_awaited()
    assert "image:review" in (await a.get(ACTOR, c["id"]))["pending_fields"]


async def test_case_read_does_not_mix_old_history_with_new_terminal_status(pair, monkeypatch):
    a, b = pair
    receipt = await a.accept_input(ACTOR, new())
    run = await claim(a, receipt)
    transaction = a.db.transaction
    inserted = False

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        async def fetch(self, sql, *args):
            nonlocal inserted
            rows = await self.conn.fetch(sql, *args)
            if "JOIN case_messages m ON m.seq=l.seq" in sql and not inserted:
                inserted = True
                await b.complete(run, {"response": "new reply"})
            return rows

    @asynccontextmanager
    async def interleaved():
        async with transaction() as conn:
            yield Connection(conn)

    monkeypatch.setattr(a.db, "transaction", interleaved)
    old = await a.get(ACTOR, receipt["case_id"])
    assert old["runs"][0]["status"] == "running" and len(old["messages"]) == 1
    current = await a.get(ACTOR, receipt["case_id"])
    assert current["runs"][0]["status"] == "succeeded" and current["messages"][-1]["content"] == "new reply"


@pytest.mark.skipif(not os.getenv("TEST_S3_ENDPOINT"), reason="TEST_S3_ENDPOINT is required")
async def test_real_s3_private_create_once_read_delete():
    objects = S3Objects(
        bucket="attachments-test",
        endpoint=os.environ["TEST_S3_ENDPOINT"],
        access_key="attachment-test",
        secret_key="attachment-test-secret",
    )
    await asyncio.to_thread(objects.client.create_bucket, Bucket=objects.bucket)
    key = "staff/at_test/original"
    await objects.put(key, pixels(), "image/png")
    await objects.put(key, pixels(), "image/png")
    assert await objects.get(key) == pixels()
    with pytest.raises(ObjectFailure):
        await objects.put(key, b"other", "image/png")
    async with AsyncClient() as client:
        assert (await client.get(os.environ["TEST_S3_ENDPOINT"] + "/attachments-test/" + key)).status_code == 403
    await objects.delete(key)
    with pytest.raises(ObjectFailure):
        await objects.get(key)
