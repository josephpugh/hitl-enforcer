"""Embedded MCP server registration shim.

The design spec calls for FastMCP, but since this server lives in-process
with the agent backend AND the tool needs a per-call principal/session
context, we expose `list_tools()` and `call_tool()` directly. These return
shapes are MCP-compatible and the agent loop can plumb them to OpenAI
without needing the network protocol.
"""
from __future__ import annotations

from typing import Any

from .tools.place_trade import (
    TOOL_DESCRIPTION,
    TOOL_INPUT_SCHEMA,
    TOOL_NAME,
    CallContext,
    place_trade,
)


def list_tools() -> list[dict[str, Any]]:
    """MCP-shape tool definitions."""
    return [
        {
            "name": TOOL_NAME,
            "description": TOOL_DESCRIPTION,
            "inputSchema": TOOL_INPUT_SCHEMA,
        }
    ]


async def call_tool(name: str, arguments: dict[str, Any], ctx: CallContext) -> str:
    if name != TOOL_NAME:
        return f"Unknown tool: {name}"
    ticker = str(arguments.get("ticker", "")).strip()
    raw_qty = arguments.get("quantity")
    try:
        quantity = int(raw_qty)
    except (TypeError, ValueError):
        return f"Invalid quantity: {raw_qty!r}"
    if not ticker:
        return "Missing ticker."
    return await place_trade(ticker, quantity, ctx)
