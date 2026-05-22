"""Agent backend.

Hosts:
- /mcp (Streamable HTTP MCP transport for the place_trade tool)
- /ws  (WebSocket between chat UI and agent loop; carries the elicitation
       events forwarded by the MCP client's elicitation_callback)
- /elicitation/resolve/{approval_id} (HTTP callback the confirmer hits
       after writing the receipt; resolves the in-process PENDING future)
- /artifacts/{approval_id} (read-only artifact snapshot for the chat UI)
- /healthz
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .agent.agent_loop import AgentLoop, make_session
from .config import CONFIG
from .crypto.keys import ensure_all_keys
from .evidence import store
from .mcp_server.elicitation import PENDING
from .mcp_server.server import MCP

log = logging.getLogger("backend")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    """Run MCP session manager alongside our own startup tasks."""
    ensure_all_keys()
    log.info("backend starting on port %d", CONFIG.agent_port)
    log.info("confirmer expected at %s", CONFIG.confirmer_base_url)
    async with MCP.session_manager.run():
        yield


app = FastAPI(title="Trade Agent Backend", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

agent_loop = AgentLoop()


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True}


@app.post("/elicitation/resolve/{approval_id}")
async def resolve_elicitation(approval_id: str, request: Request) -> dict[str, Any]:
    """Called by the confirmer after it writes the receipt."""
    body = await request.json()
    action = body.get("action")
    if action == "accept":
        action = "approve"
    if action not in ("approve", "decline", "cancel"):
        raise HTTPException(status_code=400, detail="invalid action")
    ok = await PENDING.resolve(approval_id, action)
    return {"resolved": ok, "approval_id": approval_id, "action": action}


def _sha256_of_file(p: Path) -> str | None:
    if not p.exists():
        return None
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()


@app.get("/artifacts/{approval_id}")
async def get_artifacts(approval_id: str) -> dict[str, Any]:
    intent = store.read_intent(approval_id)
    receipt = store.read_receipt(approval_id)
    execution = store.read_execution(approval_id)
    return {
        "approval_id": approval_id,
        "intent": intent,
        "intent_sha256": _sha256_of_file(store.intent_path(approval_id)),
        "receipt": receipt,
        "receipt_sha256": _sha256_of_file(store.receipt_path(approval_id)),
        "execution": execution,
        "execution_sha256": _sha256_of_file(store.execution_path(approval_id)),
        "consumed": store.is_consumed(approval_id),
    }


# ------------------------- WebSocket bridge -------------------------


async def _send_json(ws: WebSocket, payload: dict[str, Any]) -> None:
    await ws.send_text(json.dumps(payload))


async def _run_turn(ws: WebSocket, session, text: str) -> None:
    """Drive one user message through the agent loop, forwarding events to the WS."""
    async for event in agent_loop.handle_user_message(session, text):
        if event.type == "assistant_text":
            await _send_json(ws, {"type": "assistant_text", "delta": event.data.get("delta", "")})
        elif event.type == "tool_call":
            await _send_json(
                ws,
                {
                    "type": "tool_call",
                    "name": event.data.get("name"),
                    "args": event.data.get("args"),
                    "call_id": event.data.get("call_id"),
                },
            )
        elif event.type == "approval_required":
            await _send_json(
                ws,
                {
                    "type": "approval_required",
                    "approval_id": event.data.get("approval_id"),
                    "url": event.data.get("url"),
                    "summary": event.data.get("summary"),
                    "expires_at": event.data.get("expires_at"),
                },
            )
        elif event.type == "tool_result":
            await _send_json(
                ws,
                {
                    "type": "tool_result",
                    "name": event.data.get("name"),
                    "result": event.data.get("result"),
                    "call_id": event.data.get("call_id"),
                },
            )
        elif event.type == "assistant_done":
            await _send_json(ws, {"type": "assistant_done"})
        elif event.type == "error":
            await _send_json(ws, {"type": "error", "message": event.data.get("message", "")})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    session = make_session()
    log.info("ws session %s connected", session.session_id)
    try:
        await agent_loop.connect_mcp(session)
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            mtype = msg.get("type")
            if mtype == "user_message":
                text = msg.get("text", "")
                if text.strip():
                    session.elicit_sink = ws  # type: ignore[attr-defined]
                    try:
                        await _run_turn(ws, session, text)
                    except Exception as exc:  # noqa: BLE001
                        log.exception("turn error")
                        await _send_json(ws, {"type": "error", "message": f"Agent error: {exc!r}"})
            elif mtype == "ping":
                await _send_json(ws, {"type": "pong"})
    except WebSocketDisconnect:
        log.info("ws session %s disconnected", session.session_id)
    finally:
        await agent_loop.disconnect_mcp(session)


# Mount the FastMCP Streamable HTTP ASGI app LAST so its catch-all does not
# shadow the explicit routes declared above. Its internal route is /mcp, so
# on this FastAPI app the endpoint is at /mcp.
app.mount("/", MCP.streamable_http_app())
