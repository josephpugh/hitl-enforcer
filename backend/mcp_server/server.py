"""Real FastMCP server exposing place_trade over Streamable HTTP.

Mounted as an ASGI sub-app at /mcp on the agent backend. The agent loop
connects to this endpoint as an MCP client (over loopback HTTP), exactly
as any external client would.
"""
from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from .tools.place_trade import TOOL_DESCRIPTION, TOOL_NAME, place_trade

log = logging.getLogger("mcp.server")


def build_mcp() -> FastMCP:
    """Construct the FastMCP server. Called at agent-backend startup."""
    mcp = FastMCP(
        name="hitl-enforcer-trade",
        instructions=(
            "Server for regulated stock trades. Every place_trade call is "
            "gated by a human-in-the-loop approval at a separate, signed "
            "confirmation surface. The client receives the approval URL "
            "via MCP elicitation."
        ),
        stateless_http=False,
    )
    mcp.add_tool(
        place_trade,
        name=TOOL_NAME,
        description=TOOL_DESCRIPTION,
    )
    return mcp


MCP = build_mcp()
