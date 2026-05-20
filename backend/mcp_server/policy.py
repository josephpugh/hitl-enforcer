"""Policy registry. Server-side, not LLM-controlled."""
from __future__ import annotations

from ..config import CONFIG

TOOL_POLICY: dict[str, dict] = {
    "place_trade": {
        "rule_id": "TRADE_REQUIRES_HITL",
        "rule_version": "1.0.0",
        "required_material_fields": [
            "side",
            "ticker",
            "quantity",
            "order_type",
            "time_in_force",
            "account_id",
        ],
        "max_approval_age_seconds": CONFIG.max_approval_age_seconds,
        "single_use": True,
        "default_resolutions": {
            "side": "BUY",
            "order_type": "MARKET",
            "time_in_force": "DAY",
            "account_id": "DEMO-0001",
        },
        # Hard policy limit before staging an intent at all.
        "max_quantity": CONFIG.max_trade_quantity,
    },
}


def for_tool(tool_name: str) -> dict:
    return TOOL_POLICY[tool_name]
