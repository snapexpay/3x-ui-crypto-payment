"""3x-ui panel API: add or extend a client on a chosen inbound.

Two API generations are in the wild:
- 3x-ui 3.x: clients live panel-wide under /panel/api/clients/* and are attached to inbounds;
  POST requests from a cookie session need the X-CSRF-Token header.
- 3x-ui 2.x: clients sit inside the inbound settings JSON and are changed through
  /panel/api/inbounds/addClient and /panel/api/inbounds/updateClient/{clientId}.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import time
import uuid
from typing import Any
from urllib.parse import quote

import httpx

DAY_MS = 86400 * 1000
# ClientRecord keys (3.x) that must not be echoed back into a client update as-is.
RECORD_SKIP = {"id", "uuid", "reverse", "allowedIPs", "createdAt", "updatedAt", "limitHwid"}


class XuiError(RuntimeError):
    pass


def base_url() -> str:
    url = os.environ.get("XUI_URL", "").rstrip("/")
    if not url:
        raise XuiError("XUI_URL is not set")
    return url


def inbound_id() -> int:
    return int(os.environ.get("XUI_INBOUND_ID", "1"))


def next_expiry(current: int, now_ms: int, extra_ms: int) -> int:
    """New expiryTime (ms). 0 = never expires; negative = "N ms after first use" in 3x-ui."""
    if current == 0:
        return 0
    if current < 0:
        return current - extra_ms
    return max(current, now_ms) + extra_ms


class Panel:
    def __init__(self) -> None:
        self.base = base_url()
        self.http = httpx.Client(timeout=20.0, follow_redirects=False)
        self.csrf = ""
        token = os.environ.get("XUI_API_TOKEN", "").strip()
        if token:
            self.http.headers["Authorization"] = f"Bearer {token}"
        else:
            self.login()

    def __enter__(self) -> "Panel":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.http.close()

    def login(self) -> None:
        csrf = self.csrf_token()
        response = self.http.post(
            f"{self.base}/login",
            data={
                "username": os.environ.get("XUI_USERNAME", "admin"),
                "password": os.environ.get("XUI_PASSWORD", ""),
            },
            headers={"X-CSRF-Token": csrf} if csrf else {},
        )
        if self.decode(response) is None or not response.json().get("success"):
            raise XuiError(
                f"3x-ui login failed (HTTP {response.status_code}); check XUI_URL (with the web base path), XUI_USERNAME, XUI_PASSWORD"
            )
        self.csrf = self.csrf_token()

    def csrf_token(self) -> str:
        """3.x hands out a session CSRF token; 2.x has no such route."""
        data = self.decode(self.http.get(f"{self.base}/csrf-token"))
        return str(data.get("obj") or "") if data and data.get("success") else ""

    @staticmethod
    def decode(response: httpx.Response) -> dict[str, Any] | None:
        if response.status_code >= 300:
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) and "success" in data else None

    def call(self, method: str, path: str, what: str, body: dict[str, Any] | None = None) -> Any:
        headers = {"X-CSRF-Token": self.csrf} if self.csrf and method != "GET" else {}
        response = self.http.request(method, f"{self.base}{path}", json=body, headers=headers)
        data = self.decode(response)
        if data is None:
            raise XuiError(f"3x-ui {what} failed (HTTP {response.status_code})")
        if not data.get("success"):
            raise XuiError(f"3x-ui {what} failed: {str(data.get('msg') or '')[:300]}")
        return data.get("obj")


def provision(email: str, days: int, traffic_bytes: int) -> str:
    """Create the client or extend it by `days`; returns the subscription URL (or subId)."""
    now_ms = int(time.time() * 1000)
    extra_ms = days * DAY_MS
    total = max(0, int(traffic_bytes))
    with Panel() as panel:
        found = panel.decode(panel.http.get(f"{panel.base}/panel/api/clients/get/{quote(email, safe='')}"))
        if found is not None:
            sub_id = provision_v3(panel, found, email, now_ms, extra_ms, total)
        else:
            sub_id = provision_v2(panel, email, now_ms, extra_ms, total)
    prefix = os.environ.get("XUI_SUB_URL", "").rstrip("/")
    return f"{prefix}/{sub_id}" if prefix else sub_id


def provision_v3(panel: Panel, found: dict[str, Any], email: str, now_ms: int, extra_ms: int, total: int) -> str:
    record = (found.get("obj") or {}).get("client") if found.get("success") else None
    path = quote(email, safe="")
    if not record:
        sub_id = uuid.uuid4().hex[:16]
        client = {"email": email, "enable": True, "expiryTime": now_ms + extra_ms, "totalGB": total, "limitIp": 0, "subId": sub_id}
        panel.call("POST", "/panel/api/clients/add", "client add", {"client": client, "inboundIds": [inbound_id()]})
        return sub_id
    # Send the stored client back with new limits; omitted credentials are kept by the panel.
    client = {k: v for k, v in record.items() if k not in RECORD_SKIP}
    if isinstance(record.get("reverse"), str) and record["reverse"].strip():
        try:
            client["reverse"] = json.loads(record["reverse"])
        except ValueError:
            pass
    client["expiryTime"] = next_expiry(int(record.get("expiryTime") or 0), now_ms, extra_ms)
    client["enable"] = True
    client["totalGB"] = total
    sub_id = str(record.get("subId") or "") or uuid.uuid4().hex[:16]
    client["subId"] = sub_id
    body = {**client, "limitHwid": int(record.get("limitHwid") or 0)}
    panel.call("POST", f"/panel/api/clients/update/{path}", "client update", body)
    if total:
        panel.call("POST", f"/panel/api/clients/resetTraffic/{path}", "client traffic reset")
    return sub_id


def provision_v2(panel: Panel, email: str, now_ms: int, extra_ms: int, total: int) -> str:
    inbound = panel.call("GET", f"/panel/api/inbounds/get/{inbound_id()}", "get inbound") or {}
    protocol = str(inbound.get("protocol") or "")
    settings = inbound.get("settings")
    if isinstance(settings, str):
        settings = json.loads(settings or "{}")
    settings = settings or {}
    existing = next((c for c in settings.get("clients") or [] if c.get("email") == email), None)
    if existing:
        existing["expiryTime"] = next_expiry(int(existing.get("expiryTime") or 0), now_ms, extra_ms)
        existing["enable"] = True
        existing["totalGB"] = total
        sub_id = str(existing.get("subId") or "") or uuid.uuid4().hex[:16]
        existing["subId"] = sub_id
        payload = {"id": inbound_id(), "settings": json.dumps({"clients": [existing]})}
        key = quote(client_key(protocol, existing), safe="")
        panel.call("POST", f"/panel/api/inbounds/updateClient/{key}", "updateClient", payload)
        if total:
            panel.call("POST", f"/panel/api/inbounds/{inbound_id()}/resetClientTraffic/{quote(email, safe='')}", "resetClientTraffic")
        return sub_id
    sub_id = uuid.uuid4().hex[:16]
    new_client = {
        "email": email,
        "enable": True,
        "expiryTime": now_ms + extra_ms,
        "totalGB": total,
        "limitIp": 0,
        "subId": sub_id,
        "reset": 0,
        **credentials(protocol, settings),
    }
    payload = {"id": inbound_id(), "settings": json.dumps({"clients": [new_client]})}
    panel.call("POST", "/panel/api/inbounds/addClient", "addClient", payload)
    return sub_id


def client_key(protocol: str, client: dict[str, Any]) -> str:
    """The {clientId} that 2.x updateClient matches against, per protocol."""
    if protocol == "trojan":
        return str(client.get("password") or "")
    if protocol == "shadowsocks":
        return str(client.get("email") or "")
    if protocol in ("hysteria", "hysteria2"):
        return str(client.get("auth") or "")
    return str(client.get("id") or "")


def credentials(protocol: str, settings: dict[str, Any]) -> dict[str, Any]:
    if protocol in ("vless", "vmess"):
        return {"id": str(uuid.uuid4()), **({"security": "auto"} if protocol == "vmess" else {"flow": ""})}
    if protocol == "trojan":
        return {"password": secrets.token_urlsafe(16)}
    if protocol == "shadowsocks":
        method = str(settings.get("method") or "")
        size = 16 if "aes-128" in method else 32
        return {"password": base64.b64encode(secrets.token_bytes(size)).decode(), "method": ""}
    if protocol in ("hysteria", "hysteria2"):
        return {"auth": secrets.token_urlsafe(16)}
    raise XuiError(f"inbound {inbound_id()} uses protocol {protocol!r}, which has no per-client accounts")
