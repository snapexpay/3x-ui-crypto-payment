"""PLAN env: id:days:usd:bytes,id:days:usd:bytes  (bytes 0 = unlimited)."""

from __future__ import annotations

import os
from typing import Any


def parse_plans() -> list[dict[str, Any]]:
    raw = os.environ.get("PLANS", "month:30:5.00:0,quarter:90:12.00:0")
    plans = []
    for chunk in raw.split(","):
        parts = [p.strip() for p in chunk.split(":")]
        if len(parts) < 3:
            continue
        plan_id, days, usd = parts[0], int(parts[1]), parts[2]
        quota = int(parts[3]) if len(parts) > 3 else 0
        plans.append({"id": plan_id, "days": days, "amount_usd": f"{float(usd):.2f}", "traffic_bytes": quota})
    if not plans:
        raise RuntimeError("PLANS is empty")
    return plans


def get_plan(plan_id: str) -> dict[str, Any]:
    for plan in parse_plans():
        if plan["id"] == plan_id:
            return plan
    raise KeyError(plan_id)
