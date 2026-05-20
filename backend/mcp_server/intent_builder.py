"""Builds order intent payloads from raw tool args and policy."""
from __future__ import annotations

import base64
import datetime
import secrets
from typing import Any

from ..config import CONFIG
from .policy import for_tool


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _plus_seconds_iso(seconds: int) -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
    ).isoformat().replace("+00:00", "Z")


def build_intent(
    *,
    approval_id: str,
    ticker: str,
    quantity: int,
    principal: str,
    agent_session_id: str,
    llm_model: str = "unknown",
    tool_name: str = "place_trade",
) -> dict[str, Any]:
    """Return the unsigned intent payload (the inner `payload` of the envelope)."""
    policy = for_tool(tool_name)
    defaults = policy["default_resolutions"]
    args_resolved = {
        "ticker": ticker.upper(),
        "quantity": int(quantity),
        "side": defaults["side"],
        "order_type": defaults["order_type"],
        "time_in_force": defaults["time_in_force"],
        "account_id": defaults["account_id"],
    }
    max_age = policy["max_approval_age_seconds"]
    return {
        "artifact_type": "order_intent",
        "schema_version": "1.0.0",
        "approval_id": approval_id,
        "nonce": _b64u(secrets.token_bytes(16)),
        "created_at": _now_iso(),
        "expires_at": _plus_seconds_iso(max_age),
        "caller": {
            "agent_session_id": agent_session_id,
            "tool_name": tool_name,
            "llm_model": llm_model,
            "principal": principal,
        },
        "action": {
            "tool": tool_name,
            "args_raw": {"ticker": ticker, "quantity": int(quantity)},
            "args_resolved": args_resolved,
        },
        "policy": {
            "rule_id": policy["rule_id"],
            "rule_version": policy["rule_version"],
            "required_material_fields": policy["required_material_fields"],
            "max_approval_age_seconds": max_age,
            "single_use": policy["single_use"],
        },
        "risk": {
            "destructive": True,
            "reversible": False,
            "estimated_notional_usd": None,
        },
    }


def human_summary(intent_payload: dict[str, Any]) -> str:
    a = intent_payload["action"]["args_resolved"]
    return (
        f"{a['side']} {a['quantity']} {a['ticker']} — {a['order_type']}, "
        f"{a['time_in_force']}, account {a['account_id']}"
    )
