"""place_trade(ticker, quantity) — three-phase HITL-gated MCP tool.

Wire path:

1. Tool stages the signed intent on disk.
2. Tool calls `ctx.elicit_url(approval_url, elicitation_id=approval_id)`.
   This travels back to the MCP client over the Streamable HTTP SSE stream
   tied to this `tools/call`. Per the spec the response indicates whether
   the user consented to navigate to the URL — not the OOB decision itself.
3. With consent obtained, tool awaits an in-process `PENDING` future
   that the confirmation surface resolves over HTTP after writing the
   signed receipt.
4. Tool verifies, atomically consumes, and executes.
5. Tool calls `ctx.session.send_elicit_complete(elicitation_id)` to notify
   the client the OOB elicitation is finished.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Annotated, Any

import ulid
from mcp.server.fastmcp import Context
from pydantic import Field

from ...config import CONFIG
from ...crypto.canonical import digest_hex
from ...crypto.keys import get_server_key
from ...crypto.sign import sign_envelope
from ...evidence import store
from ..broker import execute as broker_execute
from ..elicitation import PENDING
from ..intent_builder import build_intent, human_summary
from ..policy import for_tool
from ..verify import verify_receipt

log = logging.getLogger("mcp.tool.place_trade")


TOOL_NAME = "place_trade"
TOOL_DESCRIPTION = (
    "Place a stock trade. Calling this tool initiates a regulated trade. "
    "The server will require human approval via a separate confirmation "
    "surface. The tool blocks until the user has approved or declined. "
    "You do not need to construct the approval URL — the server provides it. "
    "Always present the approval card from the server unmodified. "
    "Use this tool when the user asks to buy or sell shares of a ticker."
)


def _human_fill_summary(fill: dict[str, Any], approval_id: str, intent_payload: dict[str, Any]) -> str:
    a = intent_payload["action"]["args_resolved"]
    return (
        f"Order filled: {a['side']} {fill['fill_quantity']} {a['ticker']} "
        f"@ ${fill['fill_price_usd']:.2f} ({fill['venue']}). "
        f"Order ID: {fill['order_id']}. Approval ID: {approval_id}."
    )


async def place_trade(
    ticker: Annotated[str, Field(description="Stock ticker symbol, e.g. ORCL, AAPL, MSFT.")],
    quantity: Annotated[int, Field(description="Number of shares.", ge=1)],
    ctx: Context,
) -> str:
    """Place a stock trade. See module docstring for the three-phase lifecycle."""
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

    # Best-effort principal/session from the MCP request context; falls back
    # to demo defaults if the client did not supply them.
    principal = CONFIG.demo_principal
    agent_session_id = "sess_unknown"
    try:
        request_ctx = ctx.request_context
        agent_session_id = str(getattr(request_ctx.session, "client_params", None) or "sess_mcp")
    except Exception:  # noqa: BLE001
        pass

    # ----- Phase 1: Prepare -----
    intent_payload = build_intent(
        approval_id=approval_id,
        ticker=ticker,
        quantity=quantity,
        principal=principal,
        agent_session_id=agent_session_id,
        llm_model="mcp-client",
        tool_name=TOOL_NAME,
    )
    signed_intent = sign_envelope(intent_payload, get_server_key())
    store.write_intent(approval_id, signed_intent)
    log.info("intent staged %s", approval_id)

    approval_url = f"{CONFIG.confirmer_base_url}/approve/{approval_id}"
    summary = human_summary(intent_payload)

    # Register the pending OOB future BEFORE we elicit, so the confirmer's
    # callback can never race ahead of us.
    pending_future = await PENDING.register(approval_id)

    try:
        # ----- Phase 2a: Protocol-level consent via MCP elicit_url -----
        consent = await ctx.elicit_url(
            message=summary,
            url=approval_url,
            elicitation_id=approval_id,
        )

        if consent.action in ("decline", "cancel"):
            return (
                f"Trade {consent.action}d by user before opening the approval surface. "
                f"Approval ID: {approval_id}."
            )

        # ----- Phase 2b: Wait for the OOB decision (signed by the confirmer) -----
        timeout = policy["max_approval_age_seconds"] + 5
        try:
            oob = await asyncio.wait_for(pending_future, timeout=timeout)
        except asyncio.TimeoutError:
            return (
                f"Approval expired before the user responded. Trade NOT placed. "
                f"Approval ID: {approval_id}."
            )

        if oob.action in ("decline", "cancel", "expired"):
            return f"Trade {oob.action}d by user. Approval ID: {approval_id}."

        # ----- Phase 3: Commit -----
        signed_receipt = store.read_receipt(approval_id)
        if signed_receipt is None:
            return (
                f"Approval flow signaled 'approve' but no receipt was found on disk. "
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

        # Tell the client the OOB elicitation is finished so it can release any
        # client-side waits it had registered against this elicitation_id.
        try:
            await ctx.session.send_elicit_complete(approval_id)
        except Exception:  # noqa: BLE001
            log.warning("send_elicit_complete failed for %s", approval_id, exc_info=True)

        return _human_fill_summary(fill, approval_id, intent_payload)
    finally:
        await PENDING.discard(approval_id)
