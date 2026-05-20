"""Failure-scenario tests mirroring DESIGN_SPEC §9.

Run with:  .venv/bin/python -m pytest tests/ -v
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend.config import CONFIG  # noqa: E402
from backend.crypto.canonical import digest_hex  # noqa: E402
from backend.crypto.keys import ensure_all_keys, get_confirmer_key  # noqa: E402
from backend.crypto.sign import sign_envelope, verify_envelope  # noqa: E402
from backend.evidence import journal, store  # noqa: E402
from backend.mcp_server.elicitation import BROKER  # noqa: E402
from backend.mcp_server.tools.place_trade import (  # noqa: E402
    CallContext,
    place_trade,
)
from backend.mcp_server.verify import verify_receipt  # noqa: E402

from jinja2 import Environment, FileSystemLoader, StrictUndefined  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def _ensure_keys():
    ensure_all_keys()


@pytest.fixture
def ctx() -> CallContext:
    return CallContext(
        principal=CONFIG.demo_principal,
        agent_session_id="sess_test",
        llm_model="test-model",
    )


def _render_template(approval_id: str, expires_at: str, material: dict) -> str:
    env = Environment(
        loader=FileSystemLoader(str(REPO_ROOT / "confirmer" / "templates")),
        undefined=StrictUndefined,
        autoescape=True,
        keep_trailing_newline=True,
    )
    tmpl = env.get_template("trade-confirm-v1.html")
    return tmpl.render(approval_id=approval_id, expires_at=expires_at, **material)


def _build_receipt(intent_payload: dict, material: dict, rendered_html: str, decision: str = "approve") -> dict:
    rendered_digest = "sha256:" + hashlib.sha256(rendered_html.encode("utf-8")).hexdigest()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    auth = f"demo:{CONFIG.demo_principal}"
    payload: dict = {
        "artifact_type": "approval_receipt",
        "schema_version": "1.0.0",
        "approval_id": intent_payload["approval_id"],
        "intent_digest": "sha256:" + digest_hex(intent_payload),
        "decision": decision,
        "approver": {
            "subject": CONFIG.demo_principal,
            "auth_method": "demo_session_header",
            "auth_assertion_digest": "sha256:" + hashlib.sha256(auth.encode()).hexdigest(),
        },
        "approved_at": now,
        "client_meta": {"user_agent": "tests", "ip": "127.0.0.1"},
    }
    if decision == "approve":
        payload["display_manifest"] = {
            "template_id": "trade-confirm",
            "template_version": "1.0.0",
            "rendered_digest": rendered_digest,
            "material_fields_shown": material,
            "locale": "en-US",
            "timezone": "America/New_York",
        }
    return sign_envelope(payload, get_confirmer_key())


async def _drive_happy_path(ticker: str, qty: int, ctx: CallContext) -> str:
    async def fake_approver() -> None:
        for _ in range(200):
            if BROKER._pending:
                break
            await asyncio.sleep(0.02)
        aid = next(iter(BROKER._pending.keys()))
        intent = store.read_intent(aid)
        p = intent["payload"]
        material = {f: p["action"]["args_resolved"][f] for f in p["policy"]["required_material_fields"]}
        html = _render_template(aid, p["expires_at"], material)
        receipt = _build_receipt(p, material, html, decision="approve")
        store.write_receipt(aid, receipt)
        await BROKER.resolve(aid, "accept")

    t = asyncio.create_task(fake_approver())
    result = await place_trade(ticker, qty, ctx)
    await t
    return result


@pytest.mark.asyncio
async def test_happy_path(ctx):
    result = await _drive_happy_path("AAPL", 10, ctx)
    assert "Order filled" in result
    assert "AAPL" in result


@pytest.mark.asyncio
async def test_decline(ctx):
    async def fake_decliner():
        for _ in range(200):
            if BROKER._pending:
                break
            await asyncio.sleep(0.02)
        aid = next(iter(BROKER._pending.keys()))
        intent = store.read_intent(aid)
        p = intent["payload"]
        material = {f: p["action"]["args_resolved"][f] for f in p["policy"]["required_material_fields"]}
        html = _render_template(aid, p["expires_at"], material)
        receipt = _build_receipt(p, material, html, decision="decline")
        store.write_receipt(aid, receipt)
        await BROKER.resolve(aid, "decline")

    t = asyncio.create_task(fake_decliner())
    result = await place_trade("MSFT", 5, ctx)
    await t
    assert "declined" in result.lower()
    aid = result.split("Approval ID: ")[1].rstrip(".")
    assert store.read_execution(aid) is None


@pytest.mark.asyncio
async def test_over_quantity_policy(ctx):
    result = await place_trade("ORCL", CONFIG.max_trade_quantity + 1, ctx)
    assert "rejected by policy" in result.lower()


@pytest.mark.asyncio
async def test_zero_quantity(ctx):
    result = await place_trade("ORCL", 0, ctx)
    assert "rejected by policy" in result.lower()


@pytest.mark.asyncio
async def test_replay_detection(ctx):
    """After a successful execution, the same approval_id cannot be re-consumed."""
    result = await _drive_happy_path("NVDA", 3, ctx)
    assert "Order filled" in result
    aid = result.split("Approval ID: ")[1].rstrip(".")

    # Try to consume again -> must return False (sentinel exists)
    assert store.try_consume_receipt(aid) is False


@pytest.mark.asyncio
async def test_tampered_receipt_fails_verification(ctx):
    """If someone edits the receipt material fields after signing, verification fails."""
    result = await _drive_happy_path("AMZN", 7, ctx)
    aid = result.split("Approval ID: ")[1].rstrip(".")
    receipt_path = store.receipt_path(aid)
    raw = json.loads(receipt_path.read_text())
    # Tamper.
    raw["payload"]["display_manifest"]["material_fields_shown"]["quantity"] = 99999
    receipt_path.write_text(json.dumps(raw, indent=2, sort_keys=True))

    # Re-verify against the now-tampered file.
    intent = store.read_intent(aid)
    receipt = store.read_receipt(aid)
    v = verify_receipt(intent, receipt)
    assert not v.all_passed()
    # Should fail at signature OR payload-match.
    assert v.intent_signature_valid is True
    assert v.receipt_signature_valid is False or v.payload_match is False


@pytest.mark.asyncio
async def test_expired_intent(ctx, monkeypatch):
    """If the intent has already expired, the elicitation broker times out and the tool returns 'expired'."""
    # Patch the policy max_approval_age_seconds to a tiny window for this test.
    from backend.mcp_server import policy as pol

    original = dict(pol.TOOL_POLICY["place_trade"])
    pol.TOOL_POLICY["place_trade"] = {**original, "max_approval_age_seconds": 1}
    try:
        # Don't resolve — let it timeout.
        result = await place_trade("TSLA", 2, ctx)
        assert "expired" in result.lower()
    finally:
        pol.TOOL_POLICY["place_trade"] = original


def test_journal_chain_intact():
    ok, err = journal.verify_chain()
    assert ok, f"journal broken: {err}"


def test_dwell_time_captured_via_confirmer():
    """End-to-end with the real confirmer ASGI app: GET, wait, POST, assert dwell_ms."""
    import asyncio as _asyncio
    import time

    from starlette.testclient import TestClient

    from backend.crypto.keys import get_server_key
    from backend.mcp_server.intent_builder import build_intent
    from confirmer import app as confirmer_app

    # Stage an intent directly.
    import ulid as _ulid

    approval_id = str(_ulid.ULID())
    intent_payload = build_intent(
        approval_id=approval_id,
        ticker="ORCL",
        quantity=4,
        principal=CONFIG.demo_principal,
        agent_session_id="sess_dwell",
        llm_model="test-model",
    )
    signed_intent = sign_envelope(intent_payload, get_server_key())
    store.write_intent(approval_id, signed_intent)

    with TestClient(confirmer_app.app) as client:
        # GET — establishes viewed_at.
        get = client.get(f"/approve/{approval_id}")
        assert get.status_code == 200

        # Simulate the user reading.
        time.sleep(0.25)

        post = client.post(
            f"/approve/{approval_id}/decision",
            data={"decision": "approve"},
        )
        assert post.status_code == 200

    receipt = store.read_receipt(approval_id)
    assert receipt is not None
    rp = receipt["payload"]
    assert rp.get("viewed_at") is not None, "viewed_at missing"
    dwell = rp.get("dwell_ms")
    assert isinstance(dwell, int), f"dwell_ms not int: {dwell!r}"
    assert dwell >= 200, f"dwell_ms unexpectedly small: {dwell}"
    # Receipt signature must still verify after the new fields are added.
    assert verify_envelope(receipt)
