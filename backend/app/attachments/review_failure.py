"""Closed execution-failure reasons; never infer unreadability from an exception."""
import ssl

import httpx
from openai import APIConnectionError, APIStatusError
from pydantic import ValidationError

from app.core.llm_guard import LLMUnavailableError
from app.core.openai_compat import GatewayResponseError

REASONS = {"service_unavailable", "invalid_output", "incomplete"}


def extraction_failure(exc):
    if isinstance(exc, (TimeoutError, ssl.SSLError, httpx.TransportError, APIConnectionError, LLMUnavailableError)):
        return "service_unavailable"
    if isinstance(exc, APIStatusError) and (exc.status_code >= 500 or exc.status_code == 429):
        return "service_unavailable"
    if isinstance(exc, (GatewayResponseError, ValidationError)):
        return "invalid_output"
    return "incomplete"


def review_failures(reviews):
    """Only empty reviews carry execution-failure reasons into the server receipt."""
    return [{"attachment_id": r["attachment_id"], "sha256": r["sha256"],
             "reason": r.get("failure_reason") if isinstance(r.get("failure_reason"), str)
             and r["failure_reason"] in REASONS else "incomplete"}
            for r in reviews if not r.get("fields")]


def incomplete_image_message(snapshot):
    """Legacy/invalid receipts remain neutral; only bound closed reasons name a service."""
    receipt = snapshot.get("material_review_receipt") or {}
    original_input = snapshot.get("input_receipt")
    original_message = original_input.get("message_id") if isinstance(original_input, dict) else None
    reasons = set()
    raw_materials = snapshot.get("materials")
    materials = {(m.get("attachment_id"), m.get("sha256")) for m in raw_materials
                 if isinstance(m, dict) and isinstance(m.get("attachment_id"), str) and m["attachment_id"]
                 and isinstance(m.get("sha256"), str) and m["sha256"]} if isinstance(raw_materials, list) else set()
    if (isinstance(receipt, dict) and isinstance(receipt.get("case_id"), str) and receipt["case_id"]
            and receipt["case_id"] == snapshot.get("case_id")
            and isinstance(original_message, str) and original_message.strip()
            and isinstance(receipt.get("request_message_id"), str) and receipt["request_message_id"].strip()
            and receipt["request_message_id"] == original_message
            and receipt.get("complete") is False and isinstance(receipt.get("failures"), list)):
        for entry in receipt["failures"]:
            if (isinstance(entry, dict) and isinstance(entry.get("attachment_id"), str)
                    and isinstance(entry.get("sha256"), str) and (entry["attachment_id"], entry["sha256"]) in materials
                    and isinstance(entry.get("reason"), str) and entry["reason"] in REASONS):
                reasons.add(entry["reason"])
    en = snapshot.get("language") == "en"
    if "service_unavailable" in reasons:
        return ("Image verification could not finish because its service was unavailable. The original image is retained."
                if en else "核验服务暂时不可用，图片核验未完成；原图已保留。")
    if "invalid_output" in reasons:
        return ("Image verification did not return a usable result. The original image is retained."
                if en else "图片核验未得到可用结果，核验未完成；原图已保留。")
    return ("Image verification is incomplete. The original image is retained."
            if en else "图片核验尚未完成；原图已保留。")
