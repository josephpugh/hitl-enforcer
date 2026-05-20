"""In-process elicitation broker.

The MCP `place_trade` tool stages an intent and then "elicits" approval via
a URL. In this demo we bridge that elicitation directly to the WebSocket bridge:

1. The tool calls `elicit_url(approval_id, url, summary, expires_at)`.
2. The broker pushes an `approval_required` event onto an asyncio queue that
   the WebSocket handler is draining.
3. The broker awaits a future tied to `approval_id`.
4. The confirmation surface writes the receipt and then calls
   `resolve(approval_id, action)`; the broker completes the future and the
   tool resumes.

This is the conceptual analogue of MCP's elicitation-URL mode: the URL
travels through the protocol (here, the WS bridge), not through LLM prose.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal

ElicitAction = Literal["accept", "decline", "cancel", "expired"]


@dataclass
class ElicitRequest:
    approval_id: str
    url: str
    summary: str
    expires_at: str


@dataclass
class ElicitResult:
    action: ElicitAction


class ElicitationBroker:
    """Process-wide singleton wiring tool -> WS bridge -> surface."""

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[ElicitResult]] = {}
        self._listener: asyncio.Queue[ElicitRequest] | None = None
        self._lock = asyncio.Lock()

    def attach_listener(self, queue: asyncio.Queue[ElicitRequest]) -> None:
        self._listener = queue

    def detach_listener(self, queue: asyncio.Queue[ElicitRequest]) -> None:
        if self._listener is queue:
            self._listener = None

    async def elicit_url(
        self,
        approval_id: str,
        url: str,
        summary: str,
        expires_at: str,
        timeout_seconds: float,
    ) -> ElicitResult:
        loop = asyncio.get_event_loop()
        async with self._lock:
            if approval_id in self._pending:
                raise RuntimeError(f"Elicitation already in progress for {approval_id}")
            fut: asyncio.Future[ElicitResult] = loop.create_future()
            self._pending[approval_id] = fut
        request = ElicitRequest(
            approval_id=approval_id, url=url, summary=summary, expires_at=expires_at
        )
        if self._listener is not None:
            await self._listener.put(request)
        try:
            return await asyncio.wait_for(fut, timeout=timeout_seconds)
        except asyncio.TimeoutError:
            return ElicitResult(action="expired")
        finally:
            async with self._lock:
                self._pending.pop(approval_id, None)

    async def resolve(self, approval_id: str, action: ElicitAction) -> bool:
        async with self._lock:
            fut = self._pending.get(approval_id)
        if fut is None or fut.done():
            return False
        fut.set_result(ElicitResult(action=action))
        return True

    def has_pending(self, approval_id: str) -> bool:
        return approval_id in self._pending


BROKER = ElicitationBroker()
