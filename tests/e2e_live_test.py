"""Live end-to-end test against running backend + confirmer.

Drives the entire SSE -> tool-call -> elicit -> HTTP-POST -> resolve -> commit
flow exactly as the chat UI would (minus the React rendering). Verifies
on-disk artifacts via the verifier afterwards.

Prereqs: `python -m backend` and `python -m confirmer` must be running.
"""
from __future__ import annotations

import asyncio
import json
import sys

import httpx


BACKEND = "http://127.0.0.1:8787"
CONFIRMER = "http://127.0.0.1:8788"


def _parse_sse_block(block: str) -> dict | None:
    data = ""
    for line in block.split("\n"):
        if line.startswith(":") or not line:
            continue
        if line.startswith("data:"):
            data += line[5:].lstrip()
    if not data:
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


async def run() -> int:
    approval_id: str | None = None
    approval_url: str | None = None
    result_text: str | None = None

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST",
            f"{BACKEND}/chat",
            json={"text": "buy 7 ORCL"},
            headers={"Accept": "text/event-stream"},
        ) as resp:
            assert resp.status_code == 200, f"unexpected status {resp.status_code}"
            buffer = ""
            async for chunk in resp.aiter_text():
                # Normalize CRLF; the HTML5 SSE spec allows CR, LF, or CRLF.
                buffer += chunk.replace("\r\n", "\n").replace("\r", "\n")
                while "\n\n" in buffer:
                    block, buffer = buffer.split("\n\n", 1)
                    frame = _parse_sse_block(block)
                    if frame is None:
                        continue
                    t = frame.get("type")
                    print(f"SSE<- {t}: {str(frame)[:200]}")
                    if t == "approval_required":
                        approval_id = frame["approval_id"]
                        approval_url = frame["url"]

                        async def approve() -> None:
                            async with httpx.AsyncClient(timeout=5.0) as c:
                                g = await c.get(approval_url)
                                assert g.status_code == 200, f"GET {approval_url}: {g.status_code}"
                                p = await c.post(
                                    f"{CONFIRMER}/approve/{approval_id}/decision",
                                    data={"decision": "approve"},
                                )
                                assert p.status_code == 200, f"POST decision: {p.status_code}"

                        asyncio.create_task(approve())
                    if t == "tool_result":
                        result_text = frame.get("result", "")
                    if t == "assistant_done":
                        # Drain remainder of the stream so the server closes cleanly.
                        break

    if not approval_id:
        print("FAIL: never received approval_required")
        return 1
    if not result_text or "Order filled" not in result_text:
        print(f"FAIL: unexpected result_text: {result_text!r}")
        return 1
    print(f"\nGot approval_id: {approval_id}")
    print(f"Tool result: {result_text}")

    art = httpx.get(f"{BACKEND}/artifacts/{approval_id}").json()
    assert art["intent"] is not None, "intent missing"
    assert art["receipt"] is not None, "receipt missing"
    assert art["execution"] is not None, "execution missing"
    assert art["consumed"] is True, "not marked consumed"
    print("✓ On-disk artifacts: intent, receipt, execution, consumed sentinel — all present.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
