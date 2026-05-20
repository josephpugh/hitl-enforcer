"""Live end-to-end test against running backend + confirmer.

Drives the entire WS -> tool-call -> elicit -> HTTP-POST -> resolve -> commit
flow exactly as a real browser would (minus the React UI). Verifies on-disk
artifacts via the verifier afterwards.

Prereqs: `python -m backend` and `python -m confirmer` must be running.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import httpx
import websockets


BACKEND_WS = "ws://127.0.0.1:8787/ws"
CONFIRMER = "http://127.0.0.1:8788"

OFFLINE_MARKER = "[OPENAI_API_KEY not set"


async def run() -> int:
    async with websockets.connect(BACKEND_WS) as ws:
        await ws.send(json.dumps({"type": "user_message", "text": "buy 7 ORCL"}))
        approval_id: str | None = None
        approval_url: str | None = None
        result_text: str | None = None
        offline = False
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=120)
            frame = json.loads(raw)
            t = frame.get("type")
            print(f"WS<- {t}: {str(frame)[:200]}")
            if t == "assistant_text" and OFFLINE_MARKER in frame.get("delta", ""):
                offline = True
            if t == "approval_required":
                approval_id = frame["approval_id"]
                approval_url = frame["url"]

                # Drive an APPROVE through the confirmer HTTP API in the background.
                async def approve():
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        get = await client.get(approval_url)
                        assert get.status_code == 200, f"GET {approval_url}: {get.status_code}"
                        decide_url = f"{CONFIRMER}/approve/{approval_id}/decision"
                        post = await client.post(decide_url, data={"decision": "approve"})
                        assert post.status_code == 200, f"POST {decide_url}: {post.status_code}"

                asyncio.create_task(approve())
            if t == "tool_result":
                result_text = frame.get("result", "")
            if t == "assistant_done":
                break

    if not approval_id:
        print("FAIL: never received approval_required")
        return 1
    if not result_text or "Order filled" not in result_text:
        # If we're in offline mode, the tool result still must indicate fill.
        print(f"FAIL: unexpected result_text: {result_text!r}")
        return 1
    print(f"\nGot approval_id: {approval_id}")
    print(f"Tool result: {result_text}")

    # Now check on-disk artifacts.
    art = httpx.get(f"http://127.0.0.1:8787/artifacts/{approval_id}").json()
    assert art["intent"] is not None, "intent missing"
    assert art["receipt"] is not None, "receipt missing"
    assert art["execution"] is not None, "execution missing"
    assert art["consumed"] is True, "not marked consumed"
    print("✓ On-disk artifacts: intent, receipt, execution, consumed sentinel — all present.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
