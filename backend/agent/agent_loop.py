"""Agent loop: OpenAI chat-completions driving an MCP client over Streamable HTTP.

The chat transport between UI and backend is per-turn SSE-over-POST. Each
chat session keeps a long-lived MCP `ClientSession` open against the local
FastMCP endpoint at `/mcp`. The session's `elicitation_callback` writes
straight into the same event queue the turn is draining, so URL elicitations
from the MCP server reach the SSE response without any extra plumbing.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

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
    """Event written into a session's queue and emitted as one SSE frame."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatSession:
    session_id: str
    principal: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    # The queue both the agent loop and the MCP client's elicitation
    # callback push events into. Drained by the SSE response generator.
    event_queue: asyncio.Queue[AgentEvent] = field(default_factory=asyncio.Queue)
    _mcp_stack: contextlib.AsyncExitStack | None = None
    _mcp_session: ClientSession | None = None
    _mcp_tools: list[mcp_types.Tool] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.messages:
            self.messages.append({"role": "system", "content": SYSTEM_PROMPT})


def make_session(session_id: str | None = None, principal: str | None = None) -> ChatSession:
    return ChatSession(
        session_id=session_id or f"sess_{uuid.uuid4().hex[:12]}",
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


class AgentLoop:
    def __init__(self) -> None:
        self.client: AsyncOpenAI | None = (
            AsyncOpenAI(api_key=CONFIG.openai_api_key) if CONFIG.openai_api_key else None
        )

    # ------------------------------ MCP lifecycle ------------------------------

    async def connect_mcp(self, session: ChatSession) -> None:
        stack = contextlib.AsyncExitStack()
        try:
            read, write, _ = await stack.enter_async_context(streamablehttp_client(MCP_URL))

            queue = session.event_queue

            async def elicitation_cb(
                context: RequestContext["ClientSession", Any],
                params: mcp_types.ElicitRequestParams,
            ) -> mcp_types.ElicitResult | mcp_types.ErrorData:
                # Forward URL-mode elicitations to the SSE stream. Return
                # `accept` immediately — per spec the protocol response is
                # the consent to navigate, not the OOB decision.
                if isinstance(params, mcp_types.ElicitRequestURLParams):
                    await queue.put(
                        AgentEvent(
                            "approval_required",
                            {
                                "approval_id": params.elicitationId,
                                "url": params.url,
                                "summary": params.message,
                                "expires_at": "",
                            },
                        )
                    )
                    return mcp_types.ElicitResult(action="accept")
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

    async def disconnect_mcp(self, session: ChatSession) -> None:
        stack = session._mcp_stack
        if stack is not None:
            with contextlib.suppress(Exception):
                await stack.aclose()
        session._mcp_stack = None
        session._mcp_session = None

    # -------------------------------- Turn loop --------------------------------

    async def run_turn(self, session: ChatSession, user_text: str) -> None:
        """Drive one user message through the model + tools, writing events into the queue."""
        q = session.event_queue
        session.messages.append({"role": "user", "content": user_text})

        if self.client is None:
            await q.put(
                AgentEvent(
                    "assistant_text",
                    {"delta": "[OPENAI_API_KEY not set — running offline]"},
                )
            )
            await q.put(AgentEvent("assistant_done"))
            return

        if session._mcp_session is None:
            await q.put(AgentEvent("error", {"message": "MCP client not connected"}))
            await q.put(AgentEvent("assistant_done"))
            return

        tools = [_mcp_tool_to_openai(t) for t in session._mcp_tools]
        mcp_session = session._mcp_session

        try:
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
                    await q.put(AgentEvent("assistant_text", {"delta": msg.content}))

                tool_calls = msg.tool_calls or []
                assistant_msg: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
                if tool_calls:
                    assistant_msg["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in tool_calls
                    ]
                session.messages.append(assistant_msg)

                if not tool_calls:
                    await q.put(AgentEvent("assistant_done"))
                    return

                for tc in tool_calls:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    await q.put(
                        AgentEvent(
                            "tool_call",
                            {"name": tc.function.name, "args": args, "call_id": tc.id},
                        )
                    )
                    result_text = await self._call_mcp_tool(mcp_session, tc.function.name, args)
                    await q.put(
                        AgentEvent(
                            "tool_result",
                            {
                                "name": tc.function.name,
                                "result": result_text,
                                "call_id": tc.id,
                            },
                        )
                    )
                    session.messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": result_text}
                    )
        except Exception as exc:  # noqa: BLE001
            log.exception("turn error")
            await q.put(AgentEvent("error", {"message": f"Agent error: {exc!r}"}))
            await q.put(AgentEvent("assistant_done"))

    async def _call_mcp_tool(
        self, mcp_session: ClientSession, name: str, args: dict[str, Any]
    ) -> str:
        try:
            result = await mcp_session.call_tool(name, args)
        except Exception as exc:  # noqa: BLE001
            log.exception("MCP tool error")
            return f"Tool error: {exc!r}"
        parts: list[str] = []
        for block in result.content:
            if isinstance(block, mcp_types.TextContent):
                parts.append(block.text)
        text = "\n".join(parts) if parts else ""
        if result.isError:
            return text or "MCP tool reported an error."
        return text
