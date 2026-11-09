"""SnapEX Pay merchant invoices + webhook signatures."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any
from urllib.parse import quote

import httpx

DEFAULT_API_URL = "https://snapex.pro/api/v1/merchant"
WEBHOOK_TOLERANCE = 300


class SnapexError(RuntimeError):
    pass


def api_url() -> str:
    return os.environ.get("SNAPEX_API_URL", DEFAULT_API_URL).rstrip("/")


def api_key() -> str:
    key = os.environ.get("SNAPEX_API_KEY", "").strip()
    if not key:
        raise SnapexError("SNAPEX_API_KEY is not set")
    return key


def webhook_secret() -> str:
    secret = os.environ.get("SNAPEX_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise SnapexError("SNAPEX_WEBHOOK_SECRET is not set")
    return secret


def money(amount: float | str) -> str:
    return f"{round(float(amount), 2):.2f}"


def create_invoice(
    *,
    amount_usd: str,
    external_id: str,
    description: str,
    success_url: str | None = None,
    cancel_url: str | None = None,
    idempotency_key: str,
    expires_in: int | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "amount_usd": amount_usd,
        "fee_on": os.environ.get("SNAPEX_FEE_ON", "merchant"),
        "environment": os.environ.get("SNAPEX_ENVIRONMENT", "live"),
        "external_id": external_id,
        "description": description[:255],
    }
    partner = os.environ.get("SNAPEX_PARTNER_ID", "").strip()
    if partner:
        body["partner_ref"] = partner
    if success_url:
        body["success_url"] = success_url
    if cancel_url:
        body["cancel_url"] = cancel_url
    minutes = int(os.environ.get("SNAPEX_EXPIRES_MINUTES", "60") or 60)
    body["expires_in"] = expires_in if expires_in else max(60, min(86400, minutes * 60))
    return request("POST", "/invoices", body, {"Idempotency-Key": idempotency_key})


def get_invoice(invoice_id: str) -> dict[str, Any]:
    return request("GET", f"/invoices/{quote(invoice_id, safe='')}")


def request(method: str, path: str, body: dict[str, Any] | None = None, extra: dict[str, str] | None = None) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {api_key()}",
        "Accept": "application/json",
        "User-Agent": os.environ.get("SNAPEX_USER_AGENT", "SnapEX-Pay-Integration/1.1.0"),
    }
    if extra:
        headers.update(extra)
    payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body is not None else None
    if payload is not None:
        headers["Content-Type"] = "application/json"
    try:
        with httpx.Client(timeout=30.0, follow_redirects=False) as client:
            response = client.request(method, api_url() + path, headers=headers, content=payload)
    except httpx.HTTPError as exc:
        raise SnapexError(f"SnapEX Pay is unreachable: {exc.__class__.__name__}") from exc
    try:
        decoded = response.json()
    except ValueError as exc:
        raise SnapexError(f"SnapEX Pay returned HTTP {response.status_code}") from exc
    if response.status_code >= 300 or not isinstance(decoded, dict) or not isinstance(decoded.get("data"), dict):
        message = ""
        error = decoded.get("error") if isinstance(decoded, dict) else None
        if isinstance(error, dict):
            message = str(error.get("message") or "")
        raise SnapexError(message or f"SnapEX Pay returned HTTP {response.status_code}")
    return decoded["data"]


def verify_webhook(headers: dict[str, str], body: bytes | str) -> dict[str, Any]:
    raw_bytes = bytes(body) if isinstance(body, (bytes, bytearray)) else body.encode("utf-8")
    lowered = {str(name).lower(): str(value) for name, value in headers.items()}
    timestamp = lowered.get("webhook-timestamp", "")
    signature = lowered.get("webhook-signature", "")
    if not timestamp.isdigit() or abs(time.time() - int(timestamp)) > WEBHOOK_TOLERANCE:
        raise SnapexError("stale or missing webhook timestamp")
    expected = "v1=" + hmac.new(webhook_secret().encode(), timestamp.encode() + b"." + raw_bytes, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode(), signature.strip().encode()):
        raise SnapexError("bad webhook signature")
    try:
        event = json.loads(raw_bytes)
    except ValueError as exc:
        raise SnapexError("invalid webhook body") from exc
    if not isinstance(event, dict):
        raise SnapexError("invalid webhook body")
    return event
