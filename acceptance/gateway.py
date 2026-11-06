"""No-retry provider gateway; the only process holding a real supplier key.

Application processes receive a random local token, never the supplier key.
Every outbound attempt is durably reserved before I/O, including failures and
uncorrelated background work. A closed budget returns an explicit 429.
"""
from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from acceptance.budget import Exhausted, Ledger
from acceptance.common import ROOT, dataset, read

CALIBRATION_LIMITS = {"authorization": "stage-6-development-calibration-v1", "requests": 40,
                      "scenarios": 12, "max_output_tokens": 2048, "model": "gpt-6.1-sol"}


def calibration_ledger():
    # Fixed outside every temporary service stack, shared across reruns. There
    # is deliberately no CLI flag for resetting/replacing this real ledger.
    path = ROOT / ".stage6" / "calibration.sqlite"
    from acceptance.common import DATA, sha
    return Ledger(path, {**CALIBRATION_LIMITS, "corpus_sha256": sha((DATA / "manifest.json").read_bytes())})


def release_ledger(approval):
    """Load a separately approved bulk budget; never reuse calibration consent."""
    from acceptance.common import DATA, sha
    import re
    if (approval.get("scope") != "release_and_baseline" or approval.get("confirmed") is not True
            or not approval.get("user_approval_reference") or not re.fullmatch(r"[a-z0-9-]{8,64}", approval.get("id", ""))
            or approval.get("model") != "gpt-6.1-sol" or approval.get("max_output_tokens") != 2048
            or type(approval.get("requests")) is not int or approval["requests"] <= 0
            or type(approval.get("scenarios")) is not int or approval["scenarios"] <= 0
            or approval.get("corpus_sha256") != sha((DATA / "manifest.json").read_bytes())):
        raise ValueError("A distinct, explicit release-and-baseline budget is required")
    return Ledger(ROOT / ".stage6" / (approval["id"] + ".sqlite"), approval)


def sizes(body):
    chars, image_bytes = 0, 0
    def walk(value):
        nonlocal chars, image_bytes
        if isinstance(value, str):
            if value.startswith("data:image/") and ";base64," in value:
                image_bytes += len(base64.b64decode(value.split(",", 1)[1]))
            else:
                chars += len(value)
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
    walk(body)
    return chars, image_bytes


def create_app(ledger, token, upstream=None, transport=None):
    app = FastAPI()
    @app.get("/health")
    async def health():
        return {"healthy": True, "provider_enabled": upstream is not None,
                "remaining_requests": ledger.report()["remaining_requests"]}

    @app.post("/{service}/v1/{purpose}")
    async def forward(service: str, purpose: str, request: Request):
        if not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
            raise HTTPException(401, "Local gateway authentication required")
        if service not in {"helpdesk", "sql"} or purpose not in {"responses", "embeddings", "rerank"}:
            raise HTTPException(400, "Unexpected outbound endpoint")
        if upstream is None:
            raise HTTPException(503, "Real provider disabled for deterministic verification")
        body = await request.json()
        if purpose == "responses" and body.get("model") != ledger.limits["model"]:
            raise HTTPException(400, "Model differs from approved calibration configuration")
        # Calibration deliberately uses explicit local retrieval. A future
        # remote-retrieval authorization must define its provider routes first.
        if purpose != "responses":
            raise HTTPException(400, "Remote retrieval is not enabled in this configuration")
        try:
            chars, image_bytes = sizes(body)
            attempt = ledger.reserve(service, purpose, body, input_chars=chars, image_bytes=image_bytes,
                                     run_id=request.headers.get("x-acceptance-run"),
                                     parent_run_id=request.headers.get("x-acceptance-parent-run"))
        except Exhausted as exc:
            raise HTTPException(429, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        started = time.monotonic()
        client = httpx.AsyncClient(timeout=httpx.Timeout(150, connect=20), follow_redirects=False,
                                   transport=transport, trust_env=False)
        try:
            upstream_request = client.build_request("POST", upstream["base_url"].rstrip("/") + "/responses", json=body,
                headers={"Authorization": "Bearer " + upstream["api_key"], "Content-Type": "application/json"})
            response = await client.send(upstream_request, stream=True)
        except Exception as exc:
            ledger.finish(attempt, error=type(exc).__name__)
            await client.aclose()
            raise HTTPException(502, "Supplier request failed; attempt remains charged to budget") from None
        if "text/event-stream" not in response.headers.get("content-type", ""):
            raw = await response.aread()
            try:
                data = json.loads(raw)
            except ValueError:
                data = {}
            ledger.finish(attempt, status=response.status_code, usage=data.get("usage"), model=data.get("model"),
                          first_byte_ms=(time.monotonic() - started) * 1000)
            await response.aclose()
            await client.aclose()
            return Response(raw, status_code=response.status_code, media_type="application/json")
        async def stream():
            usage, model, error, first_byte, pending = None, None, None, None, b""
            try:
                async for chunk in response.aiter_bytes():
                    if first_byte is None:
                        first_byte = (time.monotonic() - started) * 1000
                    pending += chunk
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        if line.startswith(b"data:"):
                            try:
                                event = json.loads(line[5:].strip())
                                value = event.get("response", event)
                                usage = value.get("usage") or usage
                                model = value.get("model") or model
                            except (ValueError, AttributeError):
                                pass
                    yield chunk
            except BaseException as exc:
                error = type(exc).__name__
                raise
            finally:
                ledger.finish(attempt, status=response.status_code, usage=usage, model=model, error=error, first_byte_ms=first_byte)
                await response.aclose()
                await client.aclose()
        return StreamingResponse(stream(), status_code=response.status_code, media_type="text/event-stream")
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--real-calibration", action="store_true")
    parser.add_argument("--release-budget", type=Path)
    args = parser.parse_args()
    cfg = read(args.config)
    upstream = None
    if args.real_calibration and args.release_budget:
        raise ValueError("Calibration and bulk budgets cannot be combined")
    if args.real_calibration or args.release_budget:
        manifest, _, _ = dataset()
        ledger = release_ledger(read(args.release_budget)) if args.release_budget else calibration_ledger()
        # Read the existing supplier address/key together; never rewrite it or
        # copy its contents into a temporary stack, report, or environment.
        saved = read(ROOT / "backend/data/runtime/model_settings.json")["chat"]["values"]
        if saved["provider"] != "openai" or urlsplit(saved["base_url"]).scheme != "https":
            raise ValueError("An existing HTTPS OpenAI-compatible supplier connection is required")
        upstream = {"base_url": saved["base_url"], "api_key": saved["api_key"]}
        expected = cfg["manifest_sha256"]
        from acceptance.common import DATA, sha
        if expected != sha((DATA / "manifest.json").read_bytes()) or len(manifest["calibration_scenarios"]) != 12:
            raise ValueError("Calibration corpus changed")
    else:
        ledger = Ledger(args.config.parent / "deterministic-budget.sqlite", CALIBRATION_LIMITS)
    import uvicorn
    uvicorn.run(create_app(ledger, cfg["gateway_token"], upstream), host=os.getenv("ACCEPTANCE_BIND_HOST", "127.0.0.1"), port=cfg["gateway_port"], log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
