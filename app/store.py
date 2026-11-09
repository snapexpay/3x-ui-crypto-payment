"""SQLite mapping from SnapEX invoices to panel checkouts."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any


def db_path() -> Path:
    path = Path(os.environ.get("DATA_DIR", "./data"))
    path.mkdir(parents=True, exist_ok=True)
    return path / "orders.sqlite"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            invoice_id TEXT PRIMARY KEY,
            external_id TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            username TEXT,
            telegram_id INTEGER,
            email TEXT,
            amount_usd TEXT NOT NULL,
            status TEXT NOT NULL,
            subscription_url TEXT,
            created_at INTEGER NOT NULL,
            paid_at INTEGER,
            claimed_at INTEGER
        )
        """
    )
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(orders)")}
    if "claimed_at" not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN claimed_at INTEGER")
    conn.commit()
    return conn


def save_order(row: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO orders
            (invoice_id, external_id, plan_id, username, telegram_id, email, amount_usd, status, subscription_url, created_at, paid_at)
            VALUES (:invoice_id, :external_id, :plan_id, :username, :telegram_id, :email, :amount_usd, :status, :subscription_url, :created_at, :paid_at)
            """,
            {
                "subscription_url": None,
                "paid_at": None,
                "created_at": int(time.time()),
                **row,
            },
        )


def get_order(invoice_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        found = conn.execute("SELECT * FROM orders WHERE invoice_id = ?", (invoice_id,)).fetchone()
    return dict(found) if found else None


def claim(invoice_id: str, stale_after: int = 600) -> str | None:
    """Atomically take the order for provisioning; returns the previous status, or None if busy/done.

    A claim older than `stale_after` seconds (crashed worker) can be taken again.
    """
    now = int(time.time())
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        found = conn.execute("SELECT status, claimed_at FROM orders WHERE invoice_id = ?", (invoice_id,)).fetchone()
        if not found or found["status"] == "paid":
            return None
        if found["status"] == "provisioning" and (found["claimed_at"] or 0) > now - stale_after:
            return None
        conn.execute("UPDATE orders SET status = 'provisioning', claimed_at = ? WHERE invoice_id = ?", (now, invoice_id))
    return found["status"]


def release(invoice_id: str, status: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE orders SET status = ?, claimed_at = NULL WHERE invoice_id = ? AND status = 'provisioning'",
            (status, invoice_id),
        )


def set_status(invoice_id: str, status: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE orders SET status = ? WHERE invoice_id = ? AND status NOT IN ('paid', 'provisioning')",
            (status, invoice_id),
        )


def mark_paid(invoice_id: str, subscription_url: str | None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE orders SET status = 'paid', subscription_url = ?, paid_at = ?, claimed_at = NULL WHERE invoice_id = ?",
            (subscription_url, int(time.time()), invoice_id),
        )
