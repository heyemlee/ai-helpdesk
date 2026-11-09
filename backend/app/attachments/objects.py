"""Private local / S3 objects. No public ACLs, redirects or presigned URLs."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
from pathlib import Path

from app.attachments.images import MAX_BYTES


class ObjectFailure(RuntimeError):
    pass


def safe_key(key):
    if not re.fullmatch(r"[a-z0-9_-]+/[a-z0-9_-]+/(original|preview)", key):
        raise ValueError("Invalid object key")
    return key


class LocalObjects:
    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path(self, key):
        path = self.root / safe_key(key)
        if not path.resolve().is_relative_to(self.root):
            raise ObjectFailure("Invalid object path")
        return path

    async def put(self, key, data, media_type):
        def write():
            path = self.path(key)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temp = None
            try:
                # Only publish complete bytes. Hard-link is atomic and fails if
                # another same-key uploader already won; rename would overwrite.
                with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".upload-", delete=False) as out:
                    temp = Path(out.name)
                    out.write(data)
                    out.flush()
                    os.fsync(out.fileno())
                os.link(temp, path)
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except FileExistsError:
                if path.read_bytes() != data:
                    raise ObjectFailure("Object content changed") from None
            finally:
                if temp:
                    temp.unlink(missing_ok=True)
        await asyncio.to_thread(write)

    async def get(self, key):
        def read():
            with self.path(key).open("rb") as src:
                data = src.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ObjectFailure("Object too large")
            return data
        try:
            return await asyncio.to_thread(read)
        except OSError as exc:
            raise ObjectFailure("Object unavailable") from exc

    async def delete(self, key):
        def remove():
            path = self.path(key)
            path.unlink(missing_ok=True)
            # The attachment row is exclusively marked deleting first. This
            # directory belongs only to that upload; also recover crash debris.
            for temp in path.parent.glob(".upload-*"):
                temp.unlink(missing_ok=True)
        await asyncio.to_thread(remove)


class S3Objects:
    def __init__(self, *, bucket, endpoint=None, region="us-east-1", access_key=None, secret_key=None):
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.client = boto3.client("s3", endpoint_url=endpoint, region_name=region,
                                   aws_access_key_id=access_key, aws_secret_access_key=secret_key,
                                   config=Config(connect_timeout=5, read_timeout=15,
                                                 retries={"max_attempts": 2, "mode": "standard"},
                                                 s3={"addressing_style": "path"}))

    async def put(self, key, data, media_type):
        from botocore.exceptions import ClientError
        try:
            await asyncio.to_thread(self.client.put_object, Bucket=self.bucket, Key=safe_key(key),
                                    Body=data, ContentType=media_type, IfNoneMatch="*",
                                    Metadata={"sha256": hashlib.sha256(data).hexdigest()})
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"PreconditionFailed", "412"}:
                if await self.get(key) == data:
                    return
            raise ObjectFailure("Object write failed") from None
        except Exception:
            raise ObjectFailure("Object write failed") from None

    async def get(self, key):
        def read():
            response = self.client.get_object(Bucket=self.bucket, Key=safe_key(key))
            with response["Body"] as stream:
                data = stream.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ObjectFailure("Object too large")
            return data
        try:
            return await asyncio.to_thread(read)
        except Exception:
            raise ObjectFailure("Object read failed") from None

    async def delete(self, key):
        try:
            await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=safe_key(key))
        except Exception:
            raise ObjectFailure("Object deletion failed") from None


def configured_objects(env=os.environ):
    backend = env.get("AI_HELPDESK_ATTACHMENT_STORAGE", "off")
    if backend == "off":
        return None
    if backend == "local":
        root = env.get("AI_HELPDESK_ATTACHMENT_DIR", "")
        if not root or not Path(root).is_absolute():
            raise ValueError("An absolute private AI_HELPDESK_ATTACHMENT_DIR is required")
        return LocalObjects(root)
    if backend == "s3":
        bucket = env.get("AI_HELPDESK_ATTACHMENT_BUCKET", "")
        if not bucket:
            raise ValueError("AI_HELPDESK_ATTACHMENT_BUCKET is required")
        return S3Objects(bucket=bucket, endpoint=env.get("AI_HELPDESK_ATTACHMENT_ENDPOINT") or None,
                         region=env.get("AI_HELPDESK_ATTACHMENT_REGION", "us-east-1"))
    raise ValueError("Invalid AI_HELPDESK_ATTACHMENT_STORAGE")
