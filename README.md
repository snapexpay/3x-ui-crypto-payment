# 3x-ui Crypto Payment Gateway — accept USDT (TRC20) for 3x-ui VPN clients with SnapEX Pay

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)

A small self-hosted service that lets your [3x-ui](https://github.com/MHSanaei/3x-ui) panel take crypto payments through [SnapEX Pay](https://snapex.pro). Your Telegram bot or website asks it for a checkout link. The customer pays in USDT on the SnapEX Pay page. When the invoice is paid, the service adds a client to your inbound, or extends the existing one, and gives you back the subscription link. 3x-ui itself has no billing, so this is the payment layer that sits between your shop and the panel.

[Русская версия](README.ru.md)

> **Connecting to SnapEX Pay?** Create a merchant account, check the pricing and see every ready-made module on **[pay.snapex.pro](https://pay.snapex.pro)**.

## Features

- **Hosted crypto checkout.** Each order gets its own SnapEX Pay invoice and `pay_url`. Customers pay in USDT on TRC20, ERC20 or BEP20 at any amount. BTC, ETH, LTC and TRX can also be offered on smaller invoices when the network fee allows. USDT on Solana and other networks and 60+ other coins are offered on invoices from $8 (the current minimum).
- **Automatic provisioning in 3x-ui.** A new username becomes a new client on `XUI_INBOUND_ID`. If the username already exists, the same client is renewed: expiry is pushed forward (or counted from now if it already ran out), the client is re-enabled, and traffic is reset for plans with a quota. The UUID/password and `subId` stay the same, so the customer's existing config and subscription keep working.
- **Works with 3x-ui 2.x and 3.x.** The new `/panel/api/clients` API (with CSRF or API token) and the older `inbounds/addClient` API are both supported. Tested against 3x-ui 2.6.0, 2.9.4 and 3.9.0 with VLESS and Trojan inbounds.
- **Safe webhooks.** HMAC-SHA256 signatures are checked in constant time with a 5-minute timestamp window. The invoice status is always re-read from the SnapEX API, and each invoice is provisioned only once, even if the webhook arrives twice or two copies arrive at the same time.
- **Simple plans.** Days, USD price and traffic quota, all in one env variable.
- **One container.** FastAPI + SQLite. Run it with `docker compose up -d`.

## How it works

```
bot / site ──POST /v1/checkout──▶ this service ──POST /invoices──▶ SnapEX Pay
     ◀────────── pay_url ─────────────┘
customer pays on pay_url
SnapEX Pay ──webhook invoice.paid──▶ /webhook ──GET /invoices/{id} (status == paid?)
                                          └──▶ 3x-ui: add or extend client
bot / site ──GET /v1/orders/{invoice_id}──▶ { status: "paid", subscription_url }
```

## Quick start

1. In the [SnapEX Pay cabinet](https://snapex.pro), create a merchant API key with the `invoices.read` and `invoices.write` scopes.
2. Clone and configure:

   ```bash
   curl -fsSLO https://pay.snapex.pro/downloads/3x-ui/snapexpay-3x-ui-1.1.0.zip
   unzip snapexpay-3x-ui-1.1.0.zip
   cd snapexpay-3x-ui-1.1.0
   cp .env.example .env
   # fill in SNAPEX_API_KEY, API_TOKEN, XUI_URL, XUI_USERNAME/XUI_PASSWORD, XUI_INBOUND_ID, XUI_SUB_URL
   ```

3. Start it:

   ```bash
   docker compose up -d
   curl http://localhost:8080/health   # {"ok":"yes"}
   ```

4. Add the webhook (see [Webhook setup](#webhook-setup)), put the signing secret into `SNAPEX_WEBHOOK_SECRET`, and run `docker compose up -d` again.

Put the service behind HTTPS (nginx, Caddy, Traefik…). SnapEX Pay has to be able to reach `/webhook` from the internet.

## API

All calls except `/health`, `/v1/plans` and `/webhook` need `Authorization: Bearer $API_TOKEN`. Call them from your bot or backend, not from the browser.

Create a payment:

```bash
curl -sS -X POST https://pay.example.com/v1/checkout \
  -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"plan":"month","username":"alice","telegram_id":123456789,"success_url":"https://t.me/your_bot"}'
```

```json
{"invoice_id":"INV-AB12CD34","pay_url":"https://pay.snapex.pro/…","username":"alice","amount_usd":"5.00"}
```

| Field | Required | Meaning |
|---|---|---|
| `plan` | yes | Plan id from `PLANS` |
| `username` | no | Becomes the 3x-ui client **email**, 3–32 characters `A-Z a-z 0-9 _ - .`. Random if left out. Send the same username again to renew. |
| `telegram_id`, `email` | no | Saved with the order for your own records |
| `success_url`, `cancel_url` | no | Where the payment page sends the customer afterwards (http/https) |

Check an order (poll it, or check it once your bot gets the payment notification):

```bash
curl -sS https://pay.example.com/v1/orders/INV-AB12CD34 -H "Authorization: Bearer $API_TOKEN"
```

```json
{"invoice_id":"INV-AB12CD34","status":"paid","username":"alice","subscription_url":"https://xui.example.com:2096/sub/3f9c…"}
```

`status` is `open`, `awaiting`, `underpaid`, `provisioning`, `paid`, `partially_paid` or `expired`. `subscription_url` is only filled in once the order is `paid`.

`GET /v1/plans` lists the configured plans.

## Configuration

Everything goes in `.env` (see [`.env.example`](.env.example)).

| Variable | Default | Description |
|---|---|---|
| `SNAPEX_API_KEY` | — | SnapEX Pay merchant API key (scopes `invoices.read`, `invoices.write`). Required. |
| `SNAPEX_WEBHOOK_SECRET` | — | Signing secret of your webhook endpoint (`whsec_…`). Required. |
| `SNAPEX_API_URL` | `https://snapex.pro/api/v1/merchant` | Merchant API base URL |
| `SNAPEX_ENVIRONMENT` | `live` | `live` or `test` invoices |
| `SNAPEX_FEE_ON` | `merchant` | Who pays the service fee: `merchant` or `customer` |
| `SNAPEX_PARTNER_ID` | — | Integrator's Partner ID (SnapEX Pay referral program). Leave empty if you set this up yourself. |
| `SNAPEX_EXPIRES_MINUTES` | `60` | Invoice lifetime, 1–1440 minutes |
| `SNAPEX_USER_AGENT` | `SnapEX-Pay-Integration/1.1.0` | User-Agent sent to SnapEX Pay |
| `API_TOKEN` | — | Bearer token for `/v1/checkout` and `/v1/orders`. Required; those endpoints return 503 without it. Generate one with `openssl rand -hex 32`. |
| `XUI_URL` | — | Panel URL **including the web base path**, e.g. `https://xui.example.com:2053/secretpath`. Required. |
| `XUI_USERNAME` | `admin` | Panel login |
| `XUI_PASSWORD` | — | Panel password |
| `XUI_API_TOKEN` | — | 3x-ui 3.x only: a panel API token (admin scope) used instead of username/password. Use it if the panel has two-factor login. |
| `XUI_INBOUND_ID` | `1` | Inbound that new clients are added to (VLESS, VMess, Trojan, Shadowsocks or Hysteria) |
| `XUI_SUB_URL` | — | Subscription prefix, e.g. `https://xui.example.com:2096/sub`. The service returns `XUI_SUB_URL/<subId>`. If it is empty, you get just the `subId`. |
| `PLANS` | `month:30:5.00:0,quarter:90:12.00:0` | Comma-separated `id:days:usd:traffic_bytes`. `0` bytes means unlimited, e.g. `mini:30:3.00:53687091200` is 50 GiB. |
| `DATA_DIR` | `./data` (`/data` in Docker) | Where `orders.sqlite` is kept |

## Webhook setup

In the SnapEX Pay cabinet, add a webhook endpoint:

- **URL:** `https://pay.example.com/webhook`
- **Events:** `invoice.paid`. You can also add `invoice.expired`, `invoice.underpaid` and `invoice.partially_paid`, which just update the order status.

Copy the secret it shows you into `SNAPEX_WEBHOOK_SECRET`.

SnapEX Pay signs `"{Webhook-Timestamp}.{raw body}"` with HMAC-SHA256 and sends `Webhook-Signature: v1=<hex>`. The service rejects bad signatures and timestamps more than 5 minutes off with `401`, so keep the server clock in sync (NTP). It also ignores the status in the webhook body and asks the API for the invoice before it provisions anything. Failed deliveries are retried by SnapEX Pay. A `502` (panel down) or `409` (the same invoice is already being processed) from this service also causes a retry.

## Testing

```bash
python3 -m venv /tmp/venv && /tmp/venv/bin/pip install -r requirements.txt pytest
/tmp/venv/bin/python -m pytest -q
```

The unit tests mock the SnapEX API. They cover signature checks, invoice request shape, API-token auth, duplicate and concurrent webhooks, and retry after a panel error.

There is also an end-to-end test against a real 3x-ui panel. The SnapEX side is still mocked; no real invoices are created.

```bash
docker run -d --name xui-test -p 127.0.0.1:2053:2053 ghcr.io/mhsanaei/3x-ui:latest
# create an inbound (e.g. VLESS) in the panel at http://127.0.0.1:2053 (admin/admin), then:
XUI_E2E_URL=http://127.0.0.1:2053 XUI_E2E_INBOUND_ID=1 /tmp/venv/bin/python -m pytest -q tests/test_e2e_xui.py
docker rm -f xui-test
```

To try the full payment flow, set `SNAPEX_ENVIRONMENT=test` to create test invoices.

## FAQ and troubleshooting

**`3x-ui login failed (HTTP 404)`.** `XUI_URL` is missing the panel's web base path, or the port is wrong. Use the exact address you open in the browser, without `/panel/…`.

**`3x-ui login failed (HTTP 200)`.** Wrong username/password, or two-factor login is on. On 3x-ui 3.x, create an API token in the panel and set `XUI_API_TOKEN`.

**`Duplicate email` / `email already in use`.** On 3x-ui 2.x a client email has to be unique across all inbounds. This service only looks for the username on `XUI_INBOUND_ID`, so a username that already exists on another inbound can't be added. Use a different username, or point `XUI_INBOUND_ID` at that inbound.

**The customer paid less than the invoice amount.** SnapEX Pay keeps the invoice open for a top-up. If the top-up doesn't arrive in time, the invoice closes as `partially_paid`. The service doesn't provision partial payments; the order shows `partially_paid` and you decide what to do with it.

**The client never expires.** Clients with `expiryTime = 0` (unlimited) stay unlimited when renewed. Only traffic and the enabled flag are updated.

**Does it need a Telegram bot?** No. Any bot or website that can make two HTTP calls can use it. Shop bots for 3x-ui can call `POST /v1/checkout` and then read `GET /v1/orders/{id}`. For a ready-made bot, see [telegram-vpn-bot-crypto-payment](https://pay.snapex.pro/integrations/telegram-vpn-bot).

**Where do the payments go?** Into your SnapEX Pay merchant balance. See [snapex.pro](https://snapex.pro) for withdrawals and fees.

## Other SnapEX Pay integrations

All modules, pricing and sign-up: **[pay.snapex.pro](https://pay.snapex.pro)**.

- [Marzban](https://pay.snapex.pro/integrations/marzban)
- [Xboard / V2Board](https://pay.snapex.pro/integrations/xboard)
- [Remnawave](https://pay.snapex.pro/integrations/remnawave)
- [Telegram VPN bot](https://pay.snapex.pro/integrations/telegram-vpn-bot)
- [FOSSBilling](https://pay.snapex.pro/integrations/fossbilling)
- [Paymenter](https://pay.snapex.pro/integrations/paymenter)
- [n8n community node](https://pay.snapex.pro/integrations/n8n)

## License

[MIT](LICENSE). Homepage and support: [snapex.pro](https://snapex.pro).
