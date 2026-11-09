"""Bound raw upload bodies BEFORE multipart parsing / temp-file spooling."""
import asyncio

from starlette.responses import JSONResponse

from app.attachments.images import MAX_BYTES

MAX_BODY = MAX_BYTES + 512 * 1024  # multipart headers/boundaries, one file per request


class UploadBodyLimit:
    def __init__(self, app):
        self.app = app
        self.slots = asyncio.Semaphore(8)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"].rstrip("/") != "/attachments":
            return await self.app(scope, receive, send)

        async def reject(status):
            await JSONResponse({"detail": "Upload body exceeded its resource limit"}, status)(scope, receive, send)

        headers = dict(scope.get("headers", []))
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await reject(400)
        if length < 0 or length > MAX_BODY:
            return await reject(413)
        try:
            await asyncio.wait_for(self.slots.acquire(), 2)
        except TimeoutError:
            return await reject(429)
        try:
            messages, size = [], 0
            try:
                async with asyncio.timeout(30):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        size += len(message.get("body", b""))
                        if size > MAX_BODY:
                            return await reject(413)
                        messages.append(message)
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                return await reject(408)
            index = 0

            async def replay():
                nonlocal index
                if index < len(messages):
                    index += 1
                    return messages[index - 1]
                return await receive()
            await self.app(scope, replay, send)
        finally:
            self.slots.release()
