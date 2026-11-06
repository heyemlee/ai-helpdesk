"""Transparent request tracing in disposable acceptance processes only.

This does not replace models, orchestrators, prompts, SQL, auth, or stores.
ContextVars preserve attribution across concurrent tasks and worker threads.
"""
from __future__ import annotations

import contextvars
import importlib
import os
import sys

parent_run = contextvars.ContextVar("acceptance_parent_run", default=None)


def request_ids():
    module = sys.modules.get("app.runs.memo") or sys.modules.get("ai_sql_agent.core.run_context")
    run = module.current_run() if module else None
    return getattr(run, "run_id", None), parent_run.get()


def instrument(gateway_origin, sql_origin):
    import httpx
    original_sync, original_async = httpx.Client.send, httpx.AsyncClient.send
    def decorate(request):
        origin = str(request.url).split("/", 3)[:3]
        origin = "/".join(origin)
        run, parent = request_ids()
        if origin == gateway_origin:
            if run:
                request.headers["x-acceptance-run"] = run
            if parent:
                request.headers["x-acceptance-parent-run"] = parent
        elif origin == sql_origin and run:
            request.headers["x-acceptance-parent-run"] = run
    def sync(client, request, *args, **kwargs):
        decorate(request)
        return original_sync(client, request, *args, **kwargs)
    async def asynchronous(client, request, *args, **kwargs):
        decorate(request)
        return await original_async(client, request, *args, **kwargs)
    httpx.Client.send, httpx.AsyncClient.send = sync, asynchronous


class Correlate:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        value = dict(scope.get("headers", [])).get(b"x-acceptance-parent-run")
        token = parent_run.set(value.decode() if value else None)
        try:
            await self.app(scope, receive, send)
        finally:
            parent_run.reset(token)


def main():
    import uvicorn
    if sys.argv[1].startswith("app."):
        import httpx2
        httpx2.alias_httpx()
    instrument(os.environ["ACCEPTANCE_GATEWAY_ORIGIN"], os.environ["ACCEPTANCE_SQL_ORIGIN"])
    module, name = sys.argv[1].split(":")
    app = getattr(importlib.import_module(module), name)
    uvicorn.run(Correlate(app), host=os.getenv("ACCEPTANCE_BIND_HOST", "127.0.0.1"), port=int(sys.argv[2]), access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
