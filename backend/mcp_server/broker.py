"""Simulated broker. Produces a stubbed fill record."""
from __future__ import annotations

import hashlib
import random
import time
from typing import Any

# Deterministic-ish mid prices for the demo tickers.
_MID_PRICES = {
    "ORCL": 142.31,
    "AAPL": 213.05,
    "MSFT": 421.10,
    "NVDA": 138.42,
    "AMZN": 195.55,
    "TSLA": 247.88,
}


def _fallback_price(ticker: str) -> float:
    # Hash the ticker into a stable pseudo-price in [50, 500].
    h = int(hashlib.sha256(ticker.encode("utf-8")).hexdigest()[:8], 16)
    return round(50 + (h % 45000) / 100.0, 2)


def execute(args_resolved: dict[str, Any]) -> dict[str, Any]:
    """Pretend to send the order to a broker. Returns a fill record."""
    ticker = args_resolved["ticker"]
    qty = int(args_resolved["quantity"])
    mid = _MID_PRICES.get(ticker, _fallback_price(ticker))
    # Tiny jitter so repeat demos don't look frozen.
    jitter = random.uniform(-0.05, 0.05)
    fill_price = round(mid + jitter, 2)
    order_id = f"DB-{int(time.time() * 1000):013d}-{random.randint(1000, 9999)}"
    return {
        "name": "DemoBroker",
        "order_id": order_id,
        "fill_price_usd": fill_price,
        "fill_quantity": qty,
        "venue": "SIMULATED",
    }
