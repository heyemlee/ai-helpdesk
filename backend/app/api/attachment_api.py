"""Every metadata, preview, original and retry operation resolves the current employee."""
from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import Response

from app.attachments.images import MAX_BYTES
from app.attachments.service import failure
from app.core.actor import get_current_actor

router = APIRouter(tags=["Attachments"])


def get_attachments(request: Request):
    service = getattr(request.app.state, "attachments", None)
    if service is None:
        raise HTTPException(503, {"code": "attachments_not_connected", "message": "附件服务尚未接入"})
    return service


@router.post("/attachments", status_code=201)
async def upload(file: UploadFile = File(...), upload_key: str = Header(alias="Idempotency-Key"),
                 actor=Depends(get_current_actor), service=Depends(get_attachments)):
    await service.actor(actor)
    try:
        chunks, size = [], 0
        while chunk := await file.read(64 * 1024):
            size += len(chunk)
            if size > MAX_BYTES:
                raise failure(413, "image_size")
            chunks.append(chunk)
        return await service.upload(actor, b"".join(chunks), file.filename, file.content_type, upload_key)
    finally:
        await file.close()


@router.get("/attachments/{attachment_id}")
async def metadata(attachment_id: str, actor=Depends(get_current_actor), service=Depends(get_attachments)):
    return service.public(await service.get(actor, attachment_id, ready=False))


@router.get("/attachments/{attachment_id}/content")
async def content(attachment_id: str, preview: bool = False,
                  actor=Depends(get_current_actor), service=Depends(get_attachments)):
    data, row = await service.content(actor, attachment_id, preview=preview)
    return Response(data, media_type="image/jpeg" if preview else row["media_type"], headers={
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        "Content-Disposition": "inline" if preview else f'attachment; filename="{row["id"]}.{row["media_type"].split("/")[-1]}"',
    })


@router.delete("/attachments/{attachment_id}", status_code=204)
async def discard(attachment_id: str, actor=Depends(get_current_actor), service=Depends(get_attachments)):
    await service.discard(actor, attachment_id)


@router.post("/attachments/maintenance/cleanup")
async def cleanup(actor=Depends(get_current_actor), service=Depends(get_attachments)):
    actor = await service.actor(actor)
    if "admin" not in actor.roles:
        raise HTTPException(403, "Administrator required")
    return await service.cleanup()
