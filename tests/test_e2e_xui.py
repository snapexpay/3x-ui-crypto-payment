"""End-to-end against a real 3x-ui panel (SnapEX API is mocked).

    docker run -d --name xui-test -p 127.0.0.1:2053:2053 ghcr.io/mhsanaei/3x-ui:latest
    XUI_E2E_URL=http://127.0.0.1:2053 python -m pytest tests/test_e2e_xui.py

The panel must have an inbound with per-client accounts (VLESS/VMess/Trojan/...);
XUI_E2E_INBOUND_ID picks it (default 1). Login uses admin/admin unless overridden.
"""

import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from app import main, store, xui

from .test_webhook import checkout, env, event, sign, snapex_api  # noqa: F401

URL = os.environ.get("XUI_E2E_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="XUI_E2E_URL is not set")


def client_on_panel(email):
    with xui.Panel() as panel:
        inbound = panel.call("GET", f"/panel/api/inbounds/get/{xui.inbound_id()}", "get inbound")
    settings = inbound["settings"]
    if isinstance(settings, str):
        settings = json.loads(settings)
    return next(c for c in settings["clients"] if c["email"] == email)


def test_pay_then_renew(env, snapex_api):  # noqa: F811
    env.setenv("XUI_URL", URL)
    env.setenv("XUI_USERNAME", os.environ.get("XUI_E2E_USERNAME", "admin"))
    env.setenv("XUI_PASSWORD", os.environ.get("XUI_E2E_PASSWORD", "admin"))
    env.setenv("XUI_INBOUND_ID", os.environ.get("XUI_E2E_INBOUND_ID", "1"))
    env.setenv("XUI_SUB_URL", "https://sub.example/sub")
    username = f"e2e{int(time.time() * 1000) % 10**9}"
    client = TestClient(main.app)
    now = time.time() * 1000

    checkout(client, username=username)
    body = event()
    assert client.post("/webhook", content=body, headers=sign(body)).text == "ok"
    assert client.post("/webhook", content=body, headers=sign(body)).text == "already paid"
    first = client_on_panel(username)
    assert 29.9 < (first["expiryTime"] - now) / 86_400_000 < 30.1
    assert first["enable"] is True

    # Renewal: a second paid invoice for the same username extends the same client.
    store.save_order({**store.get_order("INV-TEST0001"), "invoice_id": "INV-TEST0002", "status": "open", "subscription_url": None})
    body = event("INV-TEST0002")
    assert client.post("/webhook", content=body, headers=sign(body)).text == "ok"
    second = client_on_panel(username)
    assert 59.9 < (second["expiryTime"] - now) / 86_400_000 < 60.1
    assert second["subId"] == first["subId"]
    assert second.get("id") == first.get("id") and second.get("password") == first.get("password")
    assert store.get_order("INV-TEST0002")["subscription_url"] == f"https://sub.example/sub/{first['subId']}"
