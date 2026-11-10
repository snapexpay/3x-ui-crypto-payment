# Приём криптовалюты в 3x-ui: оплата VPN в USDT (TRC20) через SnapEX Pay

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)

Небольшой сервис, который ставится рядом с панелью [3x-ui](https://github.com/MHSanaei/3x-ui) и принимает за неё оплату криптовалютой через [SnapEX Pay](https://snapex.pro). Ваш Telegram-бот или сайт запрашивает у него ссылку на оплату, клиент платит USDT на странице SnapEX Pay. Когда счёт оплачен, сервис добавляет клиента на inbound или продлевает уже существующего и возвращает ссылку подписки. Своего биллинга в 3x-ui нет, этот сервис его заменяет.

[English version](README.md)

> **Подключаетесь к SnapEX Pay?** Мерчант-аккаунт, тарифы и все готовые модули — на **[pay.snapex.pro](https://pay.snapex.pro)**.

## Возможности

- **Страница оплаты SnapEX Pay.** На каждый заказ создаётся отдельный счёт со своим `pay_url`. Платить можно в USDT в сетях TRC20, ERC20 и BEP20 на любую сумму. BTC, ETH, LTC и TRX — и на меньшие суммы, если позволяет комиссия сети. USDT в Solana и других сетях и ещё 60+ монет — в счетах от $8 (текущий минимум).
- **Автоматическая выдача доступа в 3x-ui.** Для нового username на `XUI_INBOUND_ID` создаётся клиент. Если клиент с таким username уже есть, он продлевается: срок сдвигается вперёд (а если уже истёк, то отсчитывается от текущего момента), клиент включается, а на тарифах с лимитом обнуляется трафик. UUID/пароль и `subId` не меняются, так что старый конфиг и подписка продолжают работать.
- **Поддерживаются 3x-ui 2.x и 3.x.** Работает и с новым API `/panel/api/clients` (CSRF или API-токен), и со старым `inbounds/addClient`. Проверено на 3x-ui 2.6.0, 2.9.4 и 3.9.0 с inbound'ами VLESS и Trojan.
- **Защищённые вебхуки.** Подпись HMAC-SHA256 сверяется в постоянное время, отметка времени должна укладываться в окно 5 минут. Статус счёта сервис всегда перепроверяет через API. Каждый счёт выдаётся ровно один раз, даже если вебхук пришёл дважды или две копии пришли одновременно.
- **По запросу: рубли по QR и Alipay.** Кроме криптовалюты, по запросу к аккаунту SnapEX Pay подключаем ещё два способа оплаты: рубли по QR-коду (Россия) и Alipay. Напишите нам в чат на [pay.snapex.pro](https://pay.snapex.pro).

## Как это работает

1. Бот вызывает `POST /v1/checkout`, сервис создаёт счёт в SnapEX Pay и отдаёт `pay_url`.
2. Клиент оплачивает.
3. SnapEX Pay присылает на `/webhook` событие `invoice.paid`. Сервис проверяет подпись, запрашивает счёт через API и, если статус `paid`, добавляет или продлевает клиента в 3x-ui.
4. Бот вызывает `GET /v1/orders/{invoice_id}` и получает `subscription_url`.

## Быстрый старт

```bash
curl -fsSLO https://pay.snapex.pro/downloads/3x-ui/snapexpay-3x-ui-1.1.0.zip
unzip snapexpay-3x-ui-1.1.0.zip
cd snapexpay-3x-ui-1.1.0
cp .env.example .env   # заполните SNAPEX_API_KEY, API_TOKEN, XUI_URL, XUI_USERNAME/XUI_PASSWORD, XUI_INBOUND_ID, XUI_SUB_URL
docker compose up -d
```

API-ключ создаётся в [кабинете SnapEX Pay](https://snapex.pro) со scope `invoices.read` и `invoices.write`. Сервис нужно поставить за HTTPS (nginx, Caddy и т. п.), а `/webhook` должен быть доступен из интернета.

```bash
curl -sS -X POST https://pay.example.com/v1/checkout \
  -H "Authorization: Bearer $API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"plan":"month","username":"alice"}'
curl -sS https://pay.example.com/v1/orders/INV-AB12CD34 -H "Authorization: Bearer $API_TOKEN"
```

`username` становится **email** клиента в 3x-ui. Если отправить тот же username ещё раз, клиент продлится. Все переменные окружения, поля запросов и статусы заказа перечислены в [английском README](README.md#configuration). Они совпадают с [`.env.example`](.env.example).

## Вебхук

В кабинете SnapEX Pay добавьте endpoint `https://pay.example.com/webhook` с событием `invoice.paid`. Можно также подписаться на `invoice.expired`, `invoice.underpaid` и `invoice.partially_paid`, они только обновляют статус заказа. Секрет endpoint'а (`whsec_…`) запишите в `SNAPEX_WEBHOOK_SECRET`. Часы сервера должны быть синхронизированы (NTP).

## Частые проблемы

- **`3x-ui login failed (HTTP 404)`.** В `XUI_URL` не указан web base path панели или указан не тот порт.
- **`3x-ui login failed (HTTP 200)`.** Неверный логин или пароль, либо включена двухфакторная авторизация. В 3x-ui 3.x в этом случае задайте `XUI_API_TOKEN`.
- **`Duplicate email`.** В 3x-ui 2.x email клиента должен быть уникальным среди всех inbound'ов.
- **Оплачено не полностью (`partially_paid`).** Такие заказы автоматически не выдаются, решение остаётся за вами.

## Другие интеграции SnapEX Pay

Все модули, тарифы и регистрация: **[pay.snapex.pro](https://pay.snapex.pro)**.

- [Marzban](https://pay.snapex.pro/integrations/marzban)
- [Xboard / V2Board](https://pay.snapex.pro/integrations/xboard)
- [Remnawave](https://pay.snapex.pro/integrations/remnawave)
- [Telegram VPN bot](https://pay.snapex.pro/integrations/telegram-vpn-bot)
- [FOSSBilling](https://pay.snapex.pro/integrations/fossbilling)
- [Paymenter](https://pay.snapex.pro/integrations/paymenter)
- [n8n community node](https://pay.snapex.pro/integrations/n8n)

Лицензия [MIT](LICENSE). Сайт и поддержка: [snapex.pro](https://snapex.pro).
