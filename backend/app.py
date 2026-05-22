"""Agent backend.

All transports are HTTP:

- POST /chat                          — open an SSE stream for one turn
- POST /chat/session                  — create a fresh chat session id
- DELETE /chat/session/{session_id}   — release a chat session
- /mcp                                — Streamable HTTP MCP transport (FastMCP)
- POST /elicitation/resolve/{id}      — confirmer's OOB decision callback
- GET  /artifacts/{approval_id}       — read-only artifact snapshot
- GET  /healthz
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

from .agent.agent_loop import AgentEvent, AgentLoop, ChatSession, make_session
from .config import CONFIG
from .crypto.keys import ensure_all_keys
from .evidence import store
from .mcp_server.elicitation import PENDING
from .mcp_server.server import MCP

log = logging.getLogger("backend")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


# Process-wide chat session registry. Keyed by session_id sent by the client.
SESSIONS: dict[str, ChatSession] = {}
SESSIONS_LOCK = asyncio.Lock()


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    """Run the MCP session manager alongside our own startup tasks."""
    ensure_all_keys()
    log.info("backend starting on port %d", CONFIG.agent_port)
    log.info("confirmer expected at %s", CONFIG.confirmer_base_url)
    async with MCP.session_manager.run():
        try:
            yield
        finally:
            # Close any chat sessions that are still hanging around.
            for sess in list(SESSIONS.values()):
                await agent_loop.disconnect_mcp(sess)
            SESSIONS.clear()


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


# --------------------------- Chat-session lifecycle ---------------------------


async def _get_or_create_session(session_id: str | None) -> ChatSession:
    async with SESSIONS_LOCK:
        if session_id and session_id in SESSIONS:
            return SESSIONS[session_id]
        sess = make_session(session_id=session_id)
        SESSIONS[sess.session_id] = sess
    # connect_mcp is async; do it outside the lock to avoid serializing setup.
    await agent_loop.connect_mcp(sess)
    return sess


@app.post("/chat/session")
async def create_chat_session() -> dict[str, str]:
    sid = f"sess_{uuid.uuid4().hex[:12]}"
    sess = make_session(session_id=sid)
    SESSIONS[sid] = sess
    await agent_loop.connect_mcp(sess)
    return {"session_id": sid}


@app.delete("/chat/session/{session_id}")
async def delete_chat_session(session_id: str) -> dict[str, Any]:
    async with SESSIONS_LOCK:
        sess = SESSIONS.pop(session_id, None)
    if sess is None:
        raise HTTPException(status_code=404, detail="unknown session")
    await agent_loop.disconnect_mcp(sess)
    return {"closed": True}


# -------------------------------- Chat turn -----------------------------------


def _sse_payload(event: AgentEvent) -> dict[str, Any]:
    return {
        "event": "message",
        "data": json.dumps({"type": event.type, **event.data}),
    }


@app.post("/chat")
async def chat(request: Request) -> EventSourceResponse:
    body = await request.json()
    text = (body.get("text") or "").strip()
    sid = body.get("session_id")
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    sess = await _get_or_create_session(sid)

    async def stream():
        # Make sure no leftover events from a previous turn leak through.
        while not sess.event_queue.empty():
            try:
                sess.event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        turn_task = asyncio.create_task(agent_loop.run_turn(sess, text))
        try:
            while True:
                # Race the queue against the turn completing so we exit
                # cleanly if the agent never emits assistant_done.
                get_task = asyncio.create_task(sess.event_queue.get())
                done, _ = await asyncio.wait(
                    {get_task, turn_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if get_task in done:
                    event = get_task.result()
                    yield _sse_payload(event)
                    if event.type == "assistant_done":
                        break
                elif turn_task in done:
                    # Turn ended without an assistant_done sentinel: emit one
                    # so the client side closes its turn state cleanly.
                    if get_task and not get_task.done():
                        get_task.cancel()
                    yield _sse_payload(AgentEvent("assistant_done"))
                    break
        finally:
            if not turn_task.done():
                turn_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await turn_task

    return EventSourceResponse(
        stream(),
        headers={"X-Session-Id": sess.session_id, "X-Accel-Buffering": "no"},
    )


# ---------------------------- Elicitation callback ----------------------------


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


# ----------------------------- Artifact snapshot ------------------------------


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


# Mount the FastMCP Streamable HTTP ASGI app LAST so its catch-all does not
# shadow the explicit routes declared above.
app.mount("/", MCP.streamable_http_app())
