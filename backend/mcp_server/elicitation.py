"""Per-process map of pending approval futures.

When the `place_trade` tool stages an intent and consents the user via
`ctx.elicit_url`, it then awaits the out-of-band decision. That decision
arrives at the agent backend via HTTP from the confirmation surface, and
gets routed into this broker to complete the awaiting future.

This is the in-process channel between the confirmer's HTTP callback and
the MCP tool's coroutine. The MCP protocol itself only carries the
*consent* to navigate to the URL (per the spec's elicit_url semantics);
the OOB outcome travels through this side channel.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

OOBAction = Literal["approve", "decline", "cancel", "expired"]


@dataclass
class OOBResult:
    action: OOBAction


class PendingDecisions:
    """Maps approval_id → asyncio.Future, resolved by the confirmer callback."""

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[OOBResult]] = {}
        self._lock = asyncio.Lock()

    async def register(self, approval_id: str) -> asyncio.Future[OOBResult]:
        loop = asyncio.get_event_loop()
        async with self._lock:
            if approval_id in self._pending:
                raise RuntimeError(f"Pending decision already exists for {approval_id}")
            fut: asyncio.Future[OOBResult] = loop.create_future()
            self._pending[approval_id] = fut
            return fut

    async def discard(self, approval_id: str) -> None:
        async with self._lock:
            self._pending.pop(approval_id, None)

    async def resolve(self, approval_id: str, action: OOBAction) -> bool:
        async with self._lock:
            fut = self._pending.get(approval_id)
        if fut is None or fut.done():
            return False
        fut.set_result(OOBResult(action=action))
        return True


PENDING = PendingDecisions()
