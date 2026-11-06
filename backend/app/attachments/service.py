"""Authorized immutable objects and durable upload recovery."""
from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from pathlib import PurePosixPath

from fastapi import HTTPException

from app.attachments.images import MAX_IMAGES, InvalidImage, validate_image
from app.runs.store import _NOW


def failure(status, code):
    return HTTPException(status, {"code": code, "message": {
        "image_size": "图片不能为空或超过 8 MiB", "image_type": "请选择真实的 PNG、JPEG 或 WebP 图片",
        "image_dimensions": "图片尺寸过大，最长边上限 8192 像素，总像素上限 2000 万",
        "image_frames": "只支持单帧图片", "image_corrupt": "图片损坏或无法完整解码",
        "attachment_unavailable": "附件尚未保存完成，请使用同一文件重试上传",
        "attachment_changed": "附件内容已变化，已拒绝读取，请重新上传原始材料",
        "upload_conflict": "此上传回执已用于其他文件，请重新选择文件",
    }.get(code, "附件操作未完成，请检查权限或稍后重试")})


class AttachmentService:
    def __init__(self, db, objects, actors, instance):
        self.db, self.objects, self.actors, self.instance = db, objects, actors, instance

    async def actor(self, actor, *, conn=None):
        current = await self.actors.refresh(actor, conn=conn)
        if current.instance != self.instance:
            raise HTTPException(404, "Not Found")
        return current

    async def get(self, actor, attachment_id, *, conn=None, ready=True, lock=True):
        actor = await self.actor(actor, conn=conn)
        db = conn or self.db
        row = await db.fetchrow(
            "SELECT a.* FROM attachments a WHERE a.id=$1 AND a.instance=$2 AND"
            " (a.actor_id=$3 OR EXISTS (SELECT 1 FROM case_materials m JOIN ticket_case_state c"
            " ON c.case_id=m.case_id WHERE m.attachment_id=a.id AND m.sha256=a.sha256 AND c.instance=$2))"
            + (" FOR UPDATE OF a" if conn and lock else ""), attachment_id, self.instance, actor.actor_id)
        if not row:
            raise HTTPException(404, "Not Found")
        scopes = await db.fetch("SELECT c.policy_scope FROM case_materials m JOIN ticket_case_state c ON c.case_id=m.case_id"
                                " WHERE m.attachment_id=$1 AND c.instance=$2", attachment_id, self.instance)
        # A shared attachment cannot be used to bypass any linked Case audience.
        import json
        for linked in scopes:
            value = linked["policy_scope"]
            actor.require_policy(json.loads(value) if isinstance(value, str) else value)
        if ready and row["state"] != "ready":
            raise failure(409, "attachment_unavailable")
        return dict(row)

    @staticmethod
    def public(row):
        return {k: row[k] for k in ("id", "filename", "sha256", "media_type", "byte_size", "width", "height", "state", "error_code")}

    async def resolve(self, actor, ids, *, conn=None):
        if len(ids) > MAX_IMAGES:
            raise HTTPException(422, "每次最多提交 4 张图片")
        result = []
        for attachment_id in sorted(set(ids)):
            row = await self.get(actor, attachment_id, conn=conn)
            result.append({"id": row["id"], "sha256": row["sha256"]})
        return result

    async def upload(self, actor, data, filename, media_type, upload_key):
        actor = await self.actor(actor)
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", upload_key):
            raise HTTPException(422, "上传需要稳定的 Idempotency-Key")
        try:
            validated = await asyncio.to_thread(validate_image, data, media_type)
        except InvalidImage as exc:
            raise failure(413 if str(exc) == "image_size" else 422, str(exc)) from None
        filename = PurePosixPath((filename or "image").replace("\\", "/")).name
        filename = "".join(c for c in filename if c.isprintable())[:180] or "image"
        # Commit staging before any object write. Retry/cleanup can locate objects
        # even if the process dies before the ready transition.
        async with self.db.transaction() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                               f"upload:{self.instance}:{actor.actor_id}:{upload_key}")
            row = await conn.fetchrow("SELECT * FROM attachments WHERE instance=$1 AND actor_id=$2 AND upload_key=$3 FOR UPDATE",
                                      self.instance, actor.actor_id, upload_key)
            if row:
                if row["sha256"] != validated.sha256:
                    raise failure(409, "upload_conflict")
                if row["state"] == "ready":
                    return self.public(row)
                if row["state"] == "deleting":
                    raise failure(409, "attachment_unavailable")
                await conn.execute(f"UPDATE attachments SET state='staging',error_code='',updated_at={_NOW} WHERE id=$1", row["id"])
            else:
                attachment_id = "at_" + uuid.uuid4().hex
                prefix = hashlib.sha256(self.instance.encode()).hexdigest()[:24] + "/" + attachment_id
                row = await conn.fetchrow(
                    f"INSERT INTO attachments(id,instance,actor_id,upload_key,filename,sha256,preview_sha256,media_type,"
                    f"byte_size,width,height,state,object_key,preview_key,created_at,updated_at)"
                    f" VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'staging',$12,$13,{_NOW},{_NOW}) RETURNING *",
                    attachment_id, self.instance, actor.actor_id, upload_key, filename, validated.sha256,
                    validated.preview_sha256, validated.media_type, len(data), validated.width, validated.height,
                    prefix + "/original", prefix + "/preview")
        try:
            await self.objects.put(row["object_key"], data, validated.media_type)
            await self.objects.put(row["preview_key"], validated.preview, "image/jpeg")
            await self.actor(actor)  # Uploading bytes did not extend the account grant.
            await self.db.execute(f"UPDATE attachments SET state='ready',error_code='',updated_at={_NOW} WHERE id=$1 AND state IN ('staging','failed')", row["id"])
        except Exception:
            await self.db.execute(f"UPDATE attachments SET state='failed',error_code='upload_failed',updated_at={_NOW} WHERE id=$1 AND state='staging'", row["id"])
            raise failure(503, "attachment_unavailable") from None
        return self.public(await self.get(actor, row["id"]))

    async def content(self, actor, attachment_id, *, preview=False, conn=None):
        row = await self.get(actor, attachment_id, conn=conn)
        try:
            data = await self.objects.get(row["preview_key" if preview else "object_key"])
        except Exception:
            raise failure(503, "attachment_unavailable") from None
        if hashlib.sha256(data).hexdigest() != row["preview_sha256" if preview else "sha256"]:
            raise failure(409, "attachment_changed")
        await self.get(actor, attachment_id, conn=conn)  # Recheck after storage latency.
        return data, row

    async def discard(self, actor, attachment_id):
        async with self.db.transaction() as conn:
            row = await self.get(actor, attachment_id, conn=conn, ready=False)
            if row["state"] == "staging":
                raise failure(409, "attachment_unavailable")
            if row["actor_id"] != actor.actor_id or await conn.fetchval(
                    "SELECT 1 FROM case_materials WHERE attachment_id=$1 LIMIT 1", attachment_id):
                raise HTTPException(409, "已关联的材料保留在工单中，不能删除")
            await conn.execute(f"UPDATE attachments SET state='deleting',updated_at={_NOW} WHERE id=$1", attachment_id)
        await self._delete(row)

    async def _delete(self, row):
        # A failed delete remains durable in 'deleting' for the cleanup job.
        await self.objects.delete(row["object_key"])
        await self.objects.delete(row["preview_key"])
        await self.db.execute("DELETE FROM attachments WHERE id=$1 AND state='deleting'", row["id"])

    async def cleanup(self, older_than_s=86400):
        """Only old, unreferenced uploads in this instance; never Case evidence."""
        async with self.db.transaction() as conn:
            rows = await conn.fetch(
                f"SELECT a.* FROM attachments a WHERE a.instance=$1 AND a.updated_at<{_NOW}-$2"
                " AND NOT EXISTS(SELECT 1 FROM case_materials m WHERE m.attachment_id=a.id)"
                " FOR UPDATE OF a SKIP LOCKED", self.instance, max(3600, older_than_s))
            for row in rows:
                # Recheck after locking: a concurrent binder may have committed
                # while this SELECT was waiting on the attachment row.
                if await conn.fetchval("SELECT 1 FROM case_materials WHERE attachment_id=$1 LIMIT 1", row["id"]):
                    continue
                await conn.execute("UPDATE attachments SET state='deleting' WHERE id=$1", row["id"])
            rows = [row for row in rows if await conn.fetchval("SELECT state='deleting' FROM attachments WHERE id=$1", row["id"])]
        removed = 0
        for row in rows:
            try:
                await self._delete(row)
                removed += 1
            except Exception:
                pass
        return {"selected": len(rows), "removed": removed, "pending": len(rows) - removed}
