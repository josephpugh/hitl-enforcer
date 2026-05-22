"""Agent loop: OpenAI chat-completions driving an MCP client over Streamable HTTP.

For each WebSocket session we open one MCP `ClientSession` against the local
FastMCP endpoint at `/mcp`. The session's `elicitation_callback` is bound to
that WS — when the MCP server's `place_trade` tool calls `ctx.elicit_url`,
the callback forwards an `approval_required` event back to the WS and
returns `accept` (consent to navigate; the OOB decision is signaled over
HTTP from the confirmer).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.context import RequestContext
import mcp.types as mcp_types
from openai import AsyncOpenAI

from ..config import CONFIG

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


MCP_URL = f"http://127.0.0.1:{CONFIG.agent_port}/mcp"


@dataclass
class AgentEvent:
    type: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentSession:
    session_id: str
    principal: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    # Sink for elicitation events (set by the WS handler before each turn).
    elicit_sink: Any | None = None
    # MCP client plumbing (filled in by `connect_mcp`).
    _mcp_stack: contextlib.AsyncExitStack | None = None
    _mcp_session: ClientSession | None = None
    _mcp_tools: list[mcp_types.Tool] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.messages:
            self.messages.append({"role": "system", "content": SYSTEM_PROMPT})


def make_session(principal: str | None = None) -> AgentSession:
    return AgentSession(
        session_id=f"sess_{uuid.uuid4().hex[:12]}",
        principal=principal or CONFIG.demo_principal,
    )


def _mcp_tool_to_openai(tool: mcp_types.Tool) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.inputSchema,
        },
    }


async def _send_ws(ws: Any, payload: dict[str, Any]) -> None:
    if ws is None:
        return
    try:
        await ws.send_text(json.dumps(payload))
    except Exception:  # noqa: BLE001
        log.warning("ws send failed", exc_info=True)


class AgentLoop:
    def __init__(self) -> None:
        self.client: AsyncOpenAI | None = (
            AsyncOpenAI(api_key=CONFIG.openai_api_key) if CONFIG.openai_api_key else None
        )

    # ------------------------------ MCP lifecycle ------------------------------

    async def connect_mcp(self, session: AgentSession) -> None:
        stack = contextlib.AsyncExitStack()
        try:
            read, write, _ = await stack.enter_async_context(streamablehttp_client(MCP_URL))

            async def elicitation_cb(
                context: RequestContext["ClientSession", Any],
                params: mcp_types.ElicitRequestParams,
            ) -> mcp_types.ElicitResult | mcp_types.ErrorData:
                # The MCP server's place_trade tool calls ctx.elicit_url. The
                # URL travels through the protocol — not LLM prose. We forward
                # it to the chat UI and immediately return `accept`, which is
                # the spec-defined "consent to navigate." The actual OOB
                # decision is signaled by the confirmer over HTTP.
                if isinstance(params, mcp_types.ElicitRequestURLParams):
                    await _send_ws(
                        session.elicit_sink,
                        {
                            "type": "approval_required",
                            "approval_id": params.elicitationId,
                            "url": params.url,
                            "summary": params.message,
                            "expires_at": "",
                        },
                    )
                    return mcp_types.ElicitResult(action="accept")
                # Form elicitation: not used in this demo. Decline politely.
                return mcp_types.ElicitResult(action="decline")

            client_session = await stack.enter_async_context(
                ClientSession(read, write, elicitation_callback=elicitation_cb)
            )
            await client_session.initialize()
            tools = await client_session.list_tools()
            session._mcp_stack = stack
            session._mcp_session = client_session
            session._mcp_tools = list(tools.tools)
            log.info(
                "MCP client ready for %s with tools: %s",
                session.session_id,
                [t.name for t in session._mcp_tools],
            )
        except Exception:
            await stack.aclose()
            raise

    async def disconnect_mcp(self, session: AgentSession) -> None:
        stack = session._mcp_stack
        if stack is not None:
            with contextlib.suppress(Exception):
                await stack.aclose()
        session._mcp_stack = None
        session._mcp_session = None

    # -------------------------------- Turn loop --------------------------------

    async def handle_user_message(
        self, session: AgentSession, user_text: str
    ) -> AsyncIterator[AgentEvent]:
        session.messages.append({"role": "user", "content": user_text})

        if self.client is None:
            yield AgentEvent(
                "assistant_text",
                {"delta": "[OPENAI_API_KEY not set — running offline]"},
            )
            yield AgentEvent("assistant_done", {})
            return

        if session._mcp_session is None:
            yield AgentEvent(
                "error", {"message": "MCP client not connected for this session"}
            )
            yield AgentEvent("assistant_done", {})
            return

        tools = [_mcp_tool_to_openai(t) for t in session._mcp_tools]
        mcp_session = session._mcp_session

        while True:
            response = await self.client.chat.completions.create(
                model=CONFIG.openai_model,
                messages=session.messages,
                tools=tools,
                tool_choice="auto",
            )
            choice = response.choices[0]
            msg = choice.message

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

            for tc in tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                yield AgentEvent(
                    "tool_call",
                    {"name": tc.function.name, "args": args, "call_id": tc.id},
                )
                # This blocks while the MCP server's tool awaits the OOB
                # decision. During that wait, the elicitation_callback fires
                # and surfaces the approval card to the chat UI.
                result_text = await self._call_mcp_tool(mcp_session, tc.function.name, args)
                yield AgentEvent(
                    "tool_result",
                    {"name": tc.function.name, "result": result_text, "call_id": tc.id},
                )
                session.messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": result_text}
                )

    async def _call_mcp_tool(
        self, mcp_session: ClientSession, name: str, args: dict[str, Any]
    ) -> str:
        try:
            result = await mcp_session.call_tool(name, args)
        except Exception as exc:  # noqa: BLE001
            log.exception("MCP tool error")
            return f"Tool error: {exc!r}"
        # Concatenate text content blocks; ignore non-text content for this demo.
        parts: list[str] = []
        for block in result.content:
            if isinstance(block, mcp_types.TextContent):
                parts.append(block.text)
        text = "\n".join(parts) if parts else ""
        if result.isError:
            return text or "MCP tool reported an error."
        return text
