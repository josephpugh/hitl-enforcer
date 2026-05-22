"""Failure-scenario tests mirroring DESIGN_SPEC §9.

Run with:  .venv/bin/python -m pytest tests/ -v

These exercise the place_trade tool directly (bypassing the MCP transport
layer) with a minimal mock Context. The transport itself is exercised by
`tests/check_dwell_live.py`.
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import mcp.types as mcp_types

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend.config import CONFIG  # noqa: E402
from backend.crypto.canonical import digest_hex  # noqa: E402
from backend.crypto.keys import ensure_all_keys, get_confirmer_key  # noqa: E402
from backend.crypto.sign import sign_envelope, verify_envelope  # noqa: E402
from backend.evidence import journal, store  # noqa: E402
from backend.mcp_server.elicitation import PENDING  # noqa: E402
from backend.mcp_server.tools.place_trade import place_trade  # noqa: E402
from backend.mcp_server.verify import verify_receipt  # noqa: E402

from jinja2 import Environment, FileSystemLoader, StrictUndefined  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def _ensure_keys():
    ensure_all_keys()


# ---------------------------- Mock MCP Context ----------------------------
# place_trade only uses three things on its Context:
#   ctx.elicit_url(...)              — returns ElicitResult(action="accept")
#   ctx.request_context.session       — best-effort, may be None
#   ctx.session.send_elicit_complete  — informational, may fail silently


@dataclass
class _MockSession:
    async def send_elicit_complete(self, elicitation_id: str) -> None:
        return None


@dataclass
class _MockRequestContext:
    session: Any = None


class MockContext:
    def __init__(self) -> None:
        self.session = _MockSession()
        self.request_context = _MockRequestContext(session=None)

    async def elicit_url(self, message: str, url: str, elicitation_id: str) -> Any:
        # Match the spec semantics: consent to navigate.
        return mcp_types.ElicitResult(action="accept")


@pytest.fixture
def ctx() -> MockContext:
    return MockContext()


# --------------------------- Fake confirmer helpers ---------------------------


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


async def _drive_decision(ticker: str, qty: int, ctx: MockContext, *, decision: str) -> str:
    """Run place_trade and concurrently simulate the confirmer's OOB callback."""

    async def fake_confirmer() -> None:
        # Wait until place_trade has registered a pending decision.
        for _ in range(500):
            if PENDING._pending:  # type: ignore[attr-defined]
                break
            await asyncio.sleep(0.005)
        else:
            raise RuntimeError("no pending decision registered")
        aid = next(iter(PENDING._pending.keys()))  # type: ignore[attr-defined]
        intent = store.read_intent(aid)
        p = intent["payload"]
        material = {f: p["action"]["args_resolved"][f] for f in p["policy"]["required_material_fields"]}
        html = _render_template(aid, p["expires_at"], material)
        receipt = _build_receipt(p, material, html, decision=decision)
        store.write_receipt(aid, receipt)
        await PENDING.resolve(aid, decision)

    task = asyncio.create_task(fake_confirmer())
    result = await place_trade(ticker, qty, ctx)
    await task
    return result


# ------------------------------- Tests -------------------------------


@pytest.mark.asyncio
async def test_happy_path(ctx):
    result = await _drive_decision("AAPL", 10, ctx, decision="approve")
    assert "Order filled" in result
    assert "AAPL" in result


@pytest.mark.asyncio
async def test_decline(ctx):
    result = await _drive_decision("MSFT", 5, ctx, decision="decline")
    assert "decline" in result.lower()
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
    result = await _drive_decision("NVDA", 3, ctx, decision="approve")
    assert "Order filled" in result
    aid = result.split("Approval ID: ")[1].rstrip(".")
    assert store.try_consume_receipt(aid) is False


@pytest.mark.asyncio
async def test_tampered_receipt_fails_verification(ctx):
    """If someone edits the receipt material fields after signing, verification fails."""
    result = await _drive_decision("AMZN", 7, ctx, decision="approve")
    aid = result.split("Approval ID: ")[1].rstrip(".")
    receipt_path = store.receipt_path(aid)
    raw = json.loads(receipt_path.read_text())
    raw["payload"]["display_manifest"]["material_fields_shown"]["quantity"] = 99999
    receipt_path.write_text(json.dumps(raw, indent=2, sort_keys=True))

    intent = store.read_intent(aid)
    receipt = store.read_receipt(aid)
    v = verify_receipt(intent, receipt)
    assert not v.all_passed()
    assert v.intent_signature_valid is True
    assert v.receipt_signature_valid is False or v.payload_match is False


@pytest.mark.asyncio
async def test_expired_intent(ctx):
    """If the OOB decision never arrives, the tool returns 'expired'."""
    from backend.mcp_server import policy as pol

    original = dict(pol.TOOL_POLICY["place_trade"])
    pol.TOOL_POLICY["place_trade"] = {**original, "max_approval_age_seconds": 1}
    try:
        # No confirmer task — let asyncio.wait_for time out.
        result = await place_trade("TSLA", 2, ctx)
        assert "expired" in result.lower()
    finally:
        pol.TOOL_POLICY["place_trade"] = original


def test_journal_chain_intact():
    ok, err = journal.verify_chain()
    assert ok, f"journal broken: {err}"


def test_dwell_time_captured_via_confirmer():
    """End-to-end with the real confirmer ASGI app: GET, wait, POST, assert dwell_ms."""
    import time

    from starlette.testclient import TestClient

    from backend.crypto.keys import get_server_key
    from backend.mcp_server.intent_builder import build_intent
    from confirmer import app as confirmer_app

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
        get = client.get(f"/approve/{approval_id}")
        assert get.status_code == 200
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
    assert verify_envelope(receipt)
