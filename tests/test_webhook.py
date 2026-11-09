import hashlib
import hmac
import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main, snapex, store, xui
from app.snapex import SnapexError, verify_webhook

SECRET = "whsec_test"
REAL_CLIENT = httpx.Client


def sign(body: str, ts: str | None = None, secret: str = SECRET) -> dict[str, str]:
    ts = ts or str(int(time.time()))
    sig = "v1=" + hmac.new(secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    return {"Webhook-Timestamp": ts, "Webhook-Signature": sig}


def event(invoice_id: str = "INV-TEST0001", kind: str = "invoice.paid") -> str:
    # Same shape and encoding as DeliverMerchantWebhook (unescaped slashes/unicode).
    return json.dumps(
        {"id": "evt-1", "type": kind, "created_at": "2026-10-09T10:00:00+00:00", "data": {"invoice_id": invoice_id, "status": "paid"}},
        separators=(",", ":"),
        ensure_ascii=False,
    )


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SNAPEX_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("SNAPEX_API_KEY", "sk_test_dummy")
    monkeypatch.setenv("SNAPEX_API_URL", "https://snapex.invalid/api/v1/merchant")
    monkeypatch.setenv("API_TOKEN", "tok_test")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PLANS", "month:30:5.00:0,mini:7:1.5:1073741824")
    return monkeypatch


@pytest.fixture
def snapex_api(env):
    """Mocked SnapEX merchant API; records every request."""
    state = {"requests": [], "status": "paid"}
    passthrough = httpx.HTTPTransport()  # anything else (e.g. a real 3x-ui in the e2e test) goes out for real

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "snapex.invalid":
            return passthrough.handle_request(request)
        state["requests"].append(request)
        if request.method == "POST" and request.url.path == "/api/v1/merchant/invoices":
            body = json.loads(request.content)
            return httpx.Response(201, json={"data": {"invoice_id": "INV-TEST0001", "status": "open", "pay_url": "https://pay.snapex.pro/abc", "amount_usd": body["amount_usd"]}})
        if request.method == "GET" and request.url.path.startswith("/api/v1/merchant/invoices/"):
            return httpx.Response(200, json={"data": {"invoice_id": request.url.path.rsplit("/", 1)[1], "status": state["status"]}})
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "The requested resource was not found."}})

    env.setattr(snapex.httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    return state


@pytest.fixture
def provisioned(env):
    calls = []

    def fake(email, days, traffic_bytes):
        calls.append((email, days, traffic_bytes))
        return f"https://sub.example/sub/{email}"

    env.setattr(xui, "provision", fake)
    return calls


def checkout(client: TestClient, **extra) -> dict:
    response = client.post("/v1/checkout", json={"plan": "month", "username": "alice", **extra}, headers={"Authorization": "Bearer tok_test"})
    assert response.status_code == 200, response.text
    return response.json()


# --- signature ---------------------------------------------------------------


def test_webhook_signature(env):
    body = event()
    assert verify_webhook(sign(body), body)["type"] == "invoice.paid"
    assert verify_webhook(sign(body), body.encode())["type"] == "invoice.paid"


def test_webhook_signature_unicode_body(env):
    body = json.dumps({"type": "invoice.paid", "data": {"invoice_id": "INV-1", "description": "Оплата/тест"}}, ensure_ascii=False, separators=(",", ":"))
    assert verify_webhook(sign(body), body.encode("utf-8"))["data"]["description"] == "Оплата/тест"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda h, b: ({**h, "Webhook-Signature": h["Webhook-Signature"][:-1] + "0"}, b),
        lambda h, b: (h, b.replace("INV-TEST0001", "INV-OTHER001")),
        lambda h, b: ({**h, "Webhook-Signature": h["Webhook-Signature"][3:]}, b),
        lambda h, b: ({**h, "Webhook-Signature": "v1=ünïcode"}, b),
        lambda h, b: ({**h, "Webhook-Timestamp": ""}, b),
        lambda h, b: (sign(b, secret="whsec_other"), b),
    ],
)
def test_webhook_signature_rejected(env, mutate):
    headers, body = mutate(sign(event()), event())
    with pytest.raises(SnapexError):
        verify_webhook(headers, body)


def test_webhook_stale_timestamp_rejected(env):
    body = event()
    with pytest.raises(SnapexError, match="stale"):
        verify_webhook(sign(body, ts=str(int(time.time()) - 301)), body)


def test_webhook_endpoint_rejects_bad_signature(snapex_api, provisioned):
    client = TestClient(main.app)
    response = client.post("/webhook", content=event(), headers={**sign(event()), "Webhook-Signature": "v1=00"})
    assert response.status_code == 401
    assert provisioned == []
    assert snapex_api["requests"] == []


# --- invoice creation request shape ----------------------------------------------


def test_checkout_request_shape(snapex_api, env):
    env.setenv("SNAPEX_EXPIRES_MINUTES", "30")
    client = TestClient(main.app)
    out = checkout(client, success_url="https://shop.example/ok")
    assert out == {"invoice_id": "INV-TEST0001", "pay_url": "https://pay.snapex.pro/abc", "username": "alice", "amount_usd": "5.00"}
    (request,) = snapex_api["requests"]
    assert request.method == "POST"
    assert str(request.url) == "https://snapex.invalid/api/v1/merchant/invoices"
    assert request.headers["Authorization"] == "Bearer sk_test_dummy"
    assert request.headers["Content-Type"] == "application/json"
    body = json.loads(request.content)
    assert request.headers["Idempotency-Key"] == body["external_id"]
    assert body["external_id"].startswith("xui-alice-month-")
    assert body == {
        "amount_usd": "5.00",
        "fee_on": "merchant",
        "environment": "live",
        "external_id": body["external_id"],
        "description": "3x-ui month for alice",
        "success_url": "https://shop.example/ok",
        "expires_in": 1800,
    }
    assert store.get_order("INV-TEST0001")["status"] == "open"


def test_checkout_requires_api_token(snapex_api):
    client = TestClient(main.app)
    assert client.post("/v1/checkout", json={"plan": "month"}).status_code == 401
    assert client.post("/v1/checkout", json={"plan": "month"}, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/v1/orders/INV-TEST0001").status_code == 401
    assert snapex_api["requests"] == []


def test_checkout_snapex_error_is_502(env):
    def handler(request):
        return httpx.Response(422, json={"error": {"code": "validation_failed", "message": "The amount_usd field format is invalid."}})

    env.setattr(snapex.httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    response = TestClient(main.app).post("/v1/checkout", json={"plan": "month"}, headers={"Authorization": "Bearer tok_test"})
    assert response.status_code == 502
    assert "amount_usd" in response.json()["detail"]


# --- idempotency -----------------------------------------------------------------


def test_webhook_delivered_twice_provisions_once(snapex_api, provisioned):
    client = TestClient(main.app)
    checkout(client)
    body = event()
    first = client.post("/webhook", content=body, headers=sign(body))
    second = client.post("/webhook", content=body, headers=sign(body))
    assert (first.status_code, first.text) == (200, "ok")
    assert (second.status_code, second.text) == (200, "already paid")
    assert provisioned == [("alice", 30, 0)]
    order = client.get("/v1/orders/INV-TEST0001", headers={"Authorization": "Bearer tok_test"}).json()
    assert order == {"invoice_id": "INV-TEST0001", "status": "paid", "username": "alice", "subscription_url": "https://sub.example/sub/alice"}


def test_concurrent_deliveries_provision_once(snapex_api, env):
    client = TestClient(main.app)
    checkout(client)
    calls, gate = [], threading.Event()

    def slow(email, days, traffic_bytes):
        calls.append(email)
        gate.wait(5)
        return "sub"

    env.setattr(xui, "provision", slow)
    body = event()
    results = []
    threads = [threading.Thread(target=lambda: results.append(client.post("/webhook", content=body, headers=sign(body)).status_code)) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.5)
    gate.set()
    for t in threads:
        t.join()
    assert calls == ["alice"]
    assert sorted(results) == [200, 409, 409]


def test_failed_provision_is_retried(snapex_api, env):
    client = TestClient(main.app)
    checkout(client)
    attempts = []

    def flaky(email, days, traffic_bytes):
        attempts.append(email)
        if len(attempts) == 1:
            raise xui.XuiError("3x-ui login failed")
        return "sub"

    env.setattr(xui, "provision", flaky)
    body = event()
    assert client.post("/webhook", content=body, headers=sign(body)).status_code == 502
    assert store.get_order("INV-TEST0001")["status"] == "open"
    assert client.post("/webhook", content=body, headers=sign(body)).text == "ok"
    assert len(attempts) == 2


def test_unpaid_invoice_is_not_provisioned(snapex_api, provisioned):
    client = TestClient(main.app)
    checkout(client)
    snapex_api["status"] = "partially_paid"
    body = event(kind="invoice.partially_paid")
    assert client.post("/webhook", content=body, headers=sign(body)).text == "not paid yet"
    assert provisioned == []
    assert store.get_order("INV-TEST0001")["status"] == "partially_paid"
    # The status is always re-read from the API, never trusted from the webhook body.
    get = snapex_api["requests"][-1]
    assert get.method == "GET" and get.url.path == "/api/v1/merchant/invoices/INV-TEST0001"


def test_unknown_invoice_and_other_events_ignored(snapex_api, provisioned):
    client = TestClient(main.app)
    body = event("INV-NOTOURS1")
    assert client.post("/webhook", content=body, headers=sign(body)).text == "unknown invoice"
    body = json.dumps({"type": "payment.credited", "data": {"payment_id": "p"}})
    assert client.post("/webhook", content=body, headers=sign(body)).text == "ignored"
    assert provisioned == []


def test_stale_claim_can_be_taken_again(env):
    store.save_order({"invoice_id": "INV-X", "external_id": "x", "plan_id": "month", "username": "bob", "telegram_id": None, "email": None, "amount_usd": "5.00", "status": "open"})
    assert store.claim("INV-X") == "open"
    assert store.claim("INV-X") is None
    assert store.claim("INV-X", stale_after=-1) == "provisioning"


# --- 3x-ui helpers -----------------------------------------------------------------


def test_next_expiry():
    now, day = 1_000_000_000_000, 86_400_000
    assert xui.next_expiry(0, now, 30 * day) == 0  # never-expiring stays so
    assert xui.next_expiry(now - day, now, 30 * day) == now + 30 * day  # expired: from now
    assert xui.next_expiry(now + day, now, 30 * day) == now + 31 * day  # active: on top
    assert xui.next_expiry(-7 * day, now, 30 * day) == -37 * day  # not started yet
