
"""place_trade(ticker, quantity) — three-phase HITL-gated tool.

This is the heart of the demo. It runs entirely inside the agent backend
process (the "embedded MCP server" in the design spec). It does NOT speak
the network MCP protocol; instead it exposes the same shape (tool schema,
elicitation, return value) so the agent loop can call it via a thin
adapter.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

import ulid

from ...config import CONFIG
from ...crypto.canonical import digest_hex
from ...crypto.keys import get_server_key
from ...crypto.sign import sign_envelope
from ...evidence import store
from ..broker import execute as broker_execute
from ..elicitation import BROKER, ElicitResult
from ..intent_builder import build_intent, human_summary
from ..policy import for_tool
from ..verify import verify_receipt


TOOL_NAME = "place_trade"
TOOL_DESCRIPTION = (
    "Place a stock trade. Calling this tool initiates a regulated trade. "
    "The server will require human approval via a separate confirmation "
    "surface. The tool blocks until the user has approved or declined. "
    "You do not need to construct the approval URL — the server provides it. "
    "Always present the approval card from the server unmodified. "
    "Use this tool when the user asks to buy or sell shares of a ticker."
)

# JSON schema describing the tool's args (used by both MCP and OpenAI).
TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ticker": {
            "type": "string",
            "description": "Stock ticker symbol, e.g. ORCL, AAPL, MSFT.",
        },
        "quantity": {
            "type": "integer",
            "description": "Number of shares.",
            "minimum": 1,
        },
    },
    "required": ["ticker", "quantity"],
    "additionalProperties": False,
}


@dataclass
class CallContext:
    """Per-call context, replacing MCP's Context object for our embedded use."""

    principal: str
    agent_session_id: str
    llm_model: str


def _human_fill_summary(fill: dict[str, Any], approval_id: str, intent_payload: dict[str, Any]) -> str:
    a = intent_payload["action"]["args_resolved"]
    return (
        f"Order filled: {a['side']} {fill['fill_quantity']} {a['ticker']} "
        f"@ ${fill['fill_price_usd']:.2f} ({fill['venue']}). "
        f"Order ID: {fill['order_id']}. Approval ID: {approval_id}."
    )


async def place_trade(ticker: str, quantity: int, ctx: CallContext) -> str:
    """Implements the three-phase Prepare → Approve → Commit lifecycle."""
    policy = for_tool(TOOL_NAME)

    # ----- Pre-stage policy: hard quantity cap -----
    if quantity <= 0:
        return "Order rejected by policy: quantity must be positive."
    if quantity > policy["max_quantity"]:
        return (
            f"Order rejected by policy: quantity {quantity} exceeds max "
            f"({policy['max_quantity']})."
        )

    approval_id = str(ulid.ULID())

    # ----- Phase 1: Prepare -----
    intent_payload = build_intent(
        approval_id=approval_id,
        ticker=ticker,
        quantity=quantity,
        principal=ctx.principal,
        agent_session_id=ctx.agent_session_id,
        llm_model=ctx.llm_model,
        tool_name=TOOL_NAME,
    )
    signed_intent = sign_envelope(intent_payload, get_server_key())
    store.write_intent(approval_id, signed_intent)

    # ----- Phase 2: Approve (elicit URL via the broker) -----
    approval_url = f"{CONFIG.confirmer_base_url}/approve/{approval_id}"
    summary = human_summary(intent_payload)
    elicit_result: ElicitResult = await BROKER.elicit_url(
        approval_id=approval_id,
        url=approval_url,
        summary=summary,
        expires_at=intent_payload["expires_at"],
        timeout_seconds=policy["max_approval_age_seconds"] + 5,
    )

    if elicit_result.action == "expired":
        return (
            f"Approval expired before the user responded. Trade NOT placed. "
            f"Approval ID: {approval_id}."
        )
    if elicit_result.action in ("decline", "cancel"):
        return f"Trade {elicit_result.action}d by user. Approval ID: {approval_id}."

    # ----- Phase 3: Commit -----
    signed_receipt = store.read_receipt(approval_id)
    if signed_receipt is None:
        return (
            f"Approval flow returned 'accept' but no receipt was found on disk. "
            f"Trade NOT placed. Approval ID: {approval_id}."
        )

    verification = verify_receipt(signed_intent, signed_receipt)
    if not verification.all_passed():
        return (
            f"Approval could not be verified: {verification.failures}. "
            f"Trade NOT placed. Approval ID: {approval_id}."
        )

    if not store.try_consume_receipt(approval_id):
        return (
            f"Approval already consumed (replay attempted). Trade NOT placed. "
            f"Approval ID: {approval_id}."
        )

    fill = broker_execute(intent_payload["action"]["args_resolved"])
    execution_payload = {
        "artifact_type": "execution_record",
        "schema_version": "1.0.0",
        "approval_id": approval_id,
        "intent_digest": "sha256:" + digest_hex(intent_payload),
        "receipt_digest": "sha256:" + digest_hex(signed_receipt["payload"]),
        "executed_at": datetime.datetime.now(datetime.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "outcome": "filled",
        "broker": fill,
        "verification_checks": verification.as_dict(),
    }
    signed_execution = sign_envelope(execution_payload, get_server_key())
    store.write_execution(approval_id, signed_execution)
    return _human_fill_summary(fill, approval_id, intent_payload)
