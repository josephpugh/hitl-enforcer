"""Live check: dwell-time round-trip through real backend + confirmer."""
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import websockets


async def run() -> int:
    async with websockets.connect("ws://127.0.0.1:8787/ws", open_timeout=5) as ws:
        await ws.send(json.dumps({"type": "user_message", "text": "buy 3 AAPL"}))
        approval_id = None
        approval_url = None
        while True:
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
            t = frame.get("type")
            if t == "approval_required":
                approval_id = frame["approval_id"]
                approval_url = frame["url"]

                async def drive():
                    async with httpx.AsyncClient(timeout=5.0) as c:
                        await c.get(approval_url)
                        await asyncio.sleep(0.6)  # simulated dwell
                        await c.post(
                            f"http://127.0.0.1:8788/approve/{approval_id}/decision",
                            data={"decision": "approve"},
                        )

                asyncio.create_task(drive())
            if t == "assistant_done":
                break

    receipt_path = Path(f"evidence/receipts/{approval_id}.json")
    receipt = json.loads(receipt_path.read_text())["payload"]
    print("approval_id:", approval_id)
    print("viewed_at:  ", receipt.get("viewed_at"))
    print("approved_at:", receipt.get("approved_at"))
    print("dwell_ms:   ", receipt.get("dwell_ms"))

    dwell = receipt.get("dwell_ms")
    assert dwell is not None and dwell >= 500, f"dwell unexpectedly small: {dwell}"
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
