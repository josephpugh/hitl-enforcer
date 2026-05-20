"""Agent backend: FastAPI + WebSocket bridge + elicitation callback."""
from __future__ import annotations

import asyncio
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
from .mcp_server.elicitation import BROKER, ElicitRequest

log = logging.getLogger("backend")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


app = FastAPI(title="Trade Agent Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

agent_loop = AgentLoop()


@app.on_event("startup")
async def _startup() -> None:
    ensure_all_keys()
    log.info("backend starting on port %d", CONFIG.agent_port)
    log.info("confirmer expected at %s", CONFIG.confirmer_base_url)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True}


@app.post("/elicitation/resolve/{approval_id}")
async def resolve_elicitation(approval_id: str, request: Request) -> dict[str, Any]:
    """Called by the confirmer after it writes the receipt."""
    body = await request.json()
    action = body.get("action")
    if action not in ("accept", "decline", "cancel"):
        raise HTTPException(status_code=400, detail="invalid action")
    ok = await BROKER.resolve(approval_id, action)
    return {"resolved": ok, "approval_id": approval_id, "action": action}


# ---- Read-only inspection endpoints for the Chat UI artifact viewer ----


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


async def _drain_elicitations(
    ws: WebSocket, elicit_queue: asyncio.Queue[ElicitRequest]
) -> None:
    """Forward elicitation requests from the broker to the WS as approval_required frames."""
    while True:
        req = await elicit_queue.get()
        await _send_json(
            ws,
            {
                "type": "approval_required",
                "approval_id": req.approval_id,
                "url": req.url,
                "summary": req.summary,
                "expires_at": req.expires_at,
            },
        )


async def _run_turn(
    ws: WebSocket, session, text: str
) -> None:
    """Drive one user message through the agent loop, forwarding events to the WS."""
    async for event in agent_loop.handle_user_message(session, text):
        if event.type == "assistant_text":
            await _send_json(
                ws, {"type": "assistant_text", "delta": event.data.get("delta", "")}
            )
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
        elif event.type == "tool_result":
            payload: dict[str, Any] = {
                "type": "tool_result",
                "name": event.data.get("name"),
                "result": event.data.get("result"),
                "call_id": event.data.get("call_id"),
            }
            # If the tool was place_trade, also attach the approval_id we can grep from result.
            await _send_json(ws, payload)
        elif event.type == "assistant_done":
            await _send_json(ws, {"type": "assistant_done"})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    session = make_session()
    log.info("ws session %s connected", session.session_id)

    elicit_queue: asyncio.Queue[ElicitRequest] = asyncio.Queue()
    BROKER.attach_listener(elicit_queue)
    drain_task = asyncio.create_task(_drain_elicitations(ws, elicit_queue))

    try:
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
                    try:
                        await _run_turn(ws, session, text)
                    except Exception as exc:  # noqa: BLE001
                        log.exception("turn error")
                        await _send_json(
                            ws, {"type": "error", "message": f"Agent error: {exc!r}"}
                        )
            elif mtype == "approval_status_check":
                approval_id = msg.get("approval_id")
                if approval_id:
                    intent = store.read_intent(approval_id)
                    receipt = store.read_receipt(approval_id)
                    execution = store.read_execution(approval_id)
                    await _send_json(
                        ws,
                        {
                            "type": "approval_status",
                            "approval_id": approval_id,
                            "intent_present": intent is not None,
                            "receipt_present": receipt is not None,
                            "decision": (receipt or {}).get("payload", {}).get("decision"),
                            "execution_present": execution is not None,
                        },
                    )
            elif mtype == "ping":
                await _send_json(ws, {"type": "pong"})
    except WebSocketDisconnect:
        log.info("ws session %s disconnected", session.session_id)
    finally:
        drain_task.cancel()
        BROKER.detach_listener(elicit_queue)
        try:
            await drain_task
        except (asyncio.CancelledError, Exception):
            pass
