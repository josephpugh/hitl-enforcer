"""Agent loop: drives OpenAI chat-completions with embedded MCP tools.

Each WS session creates one `AgentSession`. Messages persist across the
session so the model keeps context across turns.

This implementation uses chat.completions (not Responses API) because the
tool-call loop is simpler and more battle-tested. The semantics required by
the design spec — tool description, HITL block, structured tool result —
are identical.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Optional

from openai import AsyncOpenAI

from ..config import CONFIG
from ..mcp_server import server as mcp_server
from ..mcp_server.tools.place_trade import CallContext
from .openai_tools import mcp_to_openai

log = logging.getLogger("agent")


SYSTEM_PROMPT = (
    "You are a helpful trading assistant. You have one tool: place_trade, "
    "which initiates a regulated stock trade. When a user asks to buy or "
    "sell a number of shares of a ticker, call place_trade with that "
    "ticker and quantity. The server will require human approval before "
    "the trade executes; the tool will block until the user approves or "
    "declines, then return a status string. After the tool returns, "
    "convey its result to the user plainly. Do not invent approval URLs "
    "or describe approval mechanics — the system handles the approval UI "
    "separately. Refuse trade-adjacent requests unrelated to placing "
    "trades by saying you can only place trades in this demo."
)


@dataclass
class AgentEvent:
    """Event yielded from the agent loop to the WS handler."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentSession:
    session_id: str
    principal: str
    messages: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.messages:
            self.messages.append({"role": "system", "content": SYSTEM_PROMPT})


def make_session(principal: str | None = None) -> AgentSession:
    return AgentSession(
        session_id=f"sess_{uuid.uuid4().hex[:12]}",
        principal=principal or CONFIG.demo_principal,
    )


class AgentLoop:
    def __init__(self) -> None:
        if not CONFIG.openai_api_key:
            self.client: AsyncOpenAI | None = None
        else:
            self.client = AsyncOpenAI(api_key=CONFIG.openai_api_key)

    async def handle_user_message(
        self,
        session: AgentSession,
        user_text: str,
    ) -> AsyncIterator[AgentEvent]:
        """Yield events as the model thinks, calls tools, and replies."""
        session.messages.append({"role": "user", "content": user_text})

        if self.client is None:
            # No API key: yield a canned response for offline tests.
            yield AgentEvent(
                "assistant_text",
                {"delta": "[OPENAI_API_KEY not set — running offline. " "Calling place_trade as a demo.]"},
            )
            # Best-effort: parse "<verb> <qty> <ticker>" from the message.
            parsed = _parse_offline(user_text)
            if parsed is not None:
                ticker, qty = parsed
                call_id = f"call_offline_{uuid.uuid4().hex[:8]}"
                async for ev in self._call_tool(
                    session,
                    name="place_trade",
                    arguments={"ticker": ticker, "quantity": qty},
                    call_id=call_id,
                ):
                    yield ev
                yield AgentEvent("assistant_done", {})
            else:
                yield AgentEvent("assistant_done", {})
            return

        tools = mcp_to_openai(mcp_server.list_tools())

        # Iterate the tool-call loop until the model stops requesting tools.
        while True:
            log.info("calling model with %d messages", len(session.messages))
            response = await self.client.chat.completions.create(
                model=CONFIG.openai_model,
                messages=session.messages,
                tools=tools,
                tool_choice="auto",
            )
            choice = response.choices[0]
            msg = choice.message

            # Echo any assistant text to the client.
            if msg.content:
                yield AgentEvent("assistant_text", {"delta": msg.content})

            tool_calls = msg.tool_calls or []
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in tool_calls
                ]
            session.messages.append(assistant_msg)

            if not tool_calls:
                yield AgentEvent("assistant_done", {})
                return

            # Execute each tool call. place_trade will block during HITL.
            for tc in tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                async for ev in self._call_tool(
                    session, name=tc.function.name, arguments=args, call_id=tc.id
                ):
                    yield ev

            # Loop: feed tool results back in.

    async def _call_tool(
        self,
        session: AgentSession,
        name: str,
        arguments: dict[str, Any],
        call_id: str,
    ) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(
            "tool_call",
            {"name": name, "args": arguments, "call_id": call_id},
        )
        ctx = CallContext(
            principal=session.principal,
            agent_session_id=session.session_id,
            llm_model=CONFIG.openai_model,
        )
        try:
            result = await mcp_server.call_tool(name, arguments, ctx)
        except Exception as exc:  # noqa: BLE001
            log.exception("tool error")
            result = f"Tool error: {exc!r}"
        yield AgentEvent(
            "tool_result",
            {"name": name, "result": result, "call_id": call_id},
        )
        session.messages.append(
            {"role": "tool", "tool_call_id": call_id, "content": result}
        )


def _parse_offline(text: str) -> tuple[str, int] | None:
    """Tiny no-LLM parser: 'buy 100 ORCL' / 'sell 50 AAPL'."""
    import re

    m = re.search(r"\b(\d+)\s+([A-Za-z]{1,6})\b", text)
    if m:
        return m.group(2).upper(), int(m.group(1))
    return None
