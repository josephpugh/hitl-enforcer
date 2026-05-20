"""Convert MCP-shape tool defs to the OpenAI `tools=[...]` shape."""
from __future__ import annotations

from typing import Any


def mcp_to_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["inputSchema"],
            },
        }
        for t in tools
    ]
