"""HTTP API: SnapEX invoice → 3x-ui client when paid."""

from __future__ import annotations

import os
import re
import secrets
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import plans, snapex, store, xui

app = FastAPI(title="3x-ui crypto payment (SnapEX Pay)", version="1.1.0")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-\.]{3,32}$")


class CheckoutIn(BaseModel):
    plan: str
    username: str | None = Field(default=None, max_length=32)
    telegram_id: int | None = None
    email: str | None = None
    success_url: str | None = None
    cancel_url: str | None = None


def require_api_token(authorization: str | None = Header(default=None)) -> None:
    """Checkout and order lookups return subscription links, so only your bot/site may call them."""
    expected = os.environ.get("API_TOKEN", "").strip()
    if not expected:
        raise HTTPException(503, "API_TOKEN is not set")
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not secrets.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(401, "invalid API token")


@app.get("/health")
def health() -> dict[str, str]:
    return {"ok": "yes"}


@app.get("/v1/plans")
def list_plans() -> dict[str, Any]:
    return {"plans": plans.parse_plans()}


@app.post("/v1/checkout", dependencies=[Depends(require_api_token)])
def checkout(body: CheckoutIn) -> dict[str, Any]:
    try:
        plan = plans.get_plan(body.plan)
    except KeyError as exc:
        raise HTTPException(404, f"unknown plan: {body.plan}") from exc
    username = (body.username or f"u{secrets.token_hex(4)}").strip()
    if not USERNAME_RE.match(username):
        raise HTTPException(422, "username must be 3–32 letters, digits, _-.")
    external_id = f"xui-{username}-{plan['id']}-{secrets.token_hex(3)}"
    try:
        created = snapex.create_invoice(
            amount_usd=plan["amount_usd"],
            external_id=external_id,
            description=f"3x-ui {plan['id']} for {username}",
            success_url=body.success_url,
            cancel_url=body.cancel_url,
            idempotency_key=external_id,
        )
    except snapex.SnapexError as exc:
        raise HTTPException(502, str(exc)) from exc
    store.save_order(
        {
            "invoice_id": created["invoice_id"],
            "external_id": external_id,
            "plan_id": plan["id"],
            "username": username,
            "telegram_id": body.telegram_id,
            "email": body.email,
            "amount_usd": plan["amount_usd"],
            "status": created.get("status") or "open",
        }
    )
    return {"invoice_id": created["invoice_id"], "pay_url": created["pay_url"], "username": username, "amount_usd": plan["amount_usd"]}


@app.post("/webhook")
async def webhook(
    request: Request,
    webhook_timestamp: str | None = Header(default=None, alias="Webhook-Timestamp"),
    webhook_signature: str | None = Header(default=None, alias="Webhook-Signature"),
) -> PlainTextResponse:
    raw = await request.body()
    try:
        event = snapex.verify_webhook(
            {"Webhook-Timestamp": webhook_timestamp or "", "Webhook-Signature": webhook_signature or ""},
            raw,
        )
    except snapex.SnapexError as exc:
        raise HTTPException(401, str(exc)) from exc
    invoice_id = str((event.get("data") or {}).get("invoice_id") or "")
    event_type = str(event.get("type") or "")
    if not invoice_id or not event_type.startswith("invoice."):
        return PlainTextResponse("ignored")
    return await run_in_threadpool(settle, invoice_id)


def settle(invoice_id: str) -> PlainTextResponse:
    """Provision once per paid invoice, however many times (or how concurrently) the webhook arrives."""
    row = store.get_order(invoice_id)
    if not row:
        return PlainTextResponse("unknown invoice")
    if row["status"] == "paid":
        return PlainTextResponse("already paid")
    try:
        remote = snapex.get_invoice(invoice_id)
    except snapex.SnapexError as exc:
        raise HTTPException(502, str(exc)) from exc
    status = str(remote.get("status") or "")
    if status != "paid":
        if status:
            store.set_status(invoice_id, status)
        return PlainTextResponse("not paid yet")
    previous = store.claim(invoice_id)
    if previous is None:
        # Another delivery is provisioning right now (or just finished); let SnapEX retry later.
        if (store.get_order(invoice_id) or {}).get("status") == "paid":
            return PlainTextResponse("already paid")
        raise HTTPException(409, "provisioning in progress")
    try:
        plan = plans.get_plan(row["plan_id"])
        sub = xui.provision(row["username"], plan["days"], plan["traffic_bytes"])
    except (xui.XuiError, KeyError, httpx.HTTPError) as exc:
        store.release(invoice_id, previous)
        raise HTTPException(502, str(exc) or exc.__class__.__name__) from exc
    except Exception:
        store.release(invoice_id, previous)
        raise
    store.mark_paid(invoice_id, sub)
    return PlainTextResponse("ok")


@app.get("/v1/orders/{invoice_id}", dependencies=[Depends(require_api_token)])
def order_status(invoice_id: str) -> dict[str, Any]:
    row = store.get_order(invoice_id)
    if not row:
        raise HTTPException(404, "not found")
    return {
        "invoice_id": row["invoice_id"],
        "status": row["status"],
        "username": row["username"],
        "subscription_url": row["subscription_url"] if row["status"] == "paid" else None,
    }
