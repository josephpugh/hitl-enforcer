"""Confirmation surface — the trusted display component.

Endpoints:
    GET  /approve/{approval_id}            — render the deterministic template
    POST /approve/{approval_id}/decision   — record signed receipt, notify backend
    GET  /static/...                       — stylesheet
"""
from __future__ import annotations

import datetime
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

# Make `backend.*` importable when this file is run from the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.config import CONFIG  # noqa: E402
from backend.crypto.canonical import digest_hex  # noqa: E402
from backend.crypto.keys import ensure_all_keys, get_confirmer_key  # noqa: E402
from backend.crypto.sign import sign_envelope, verify_envelope  # noqa: E402
from backend.evidence import store  # noqa: E402

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"
TEMPLATE_ID = "trade-confirm"
TEMPLATE_VERSION = "1.0.0"
TEMPLATE_FILENAME = "trade-confirm-v1.html"

env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    undefined=StrictUndefined,
    autoescape=True,
    keep_trailing_newline=True,
)


app = FastAPI(title="Confirmation Surface")


class FrameAncestorsCSP(BaseHTTPMiddleware):
    """Restrict who can iframe the confirmation surface.

    Only the chat UI origin is allowed. This prevents an arbitrary page from
    framing the trusted confirmation pages and harvesting clicks (clickjacking)
    or impersonating the surface. Same-origin embeds (e.g. a redirect target
    inside the confirmer itself) are also permitted.
    """

    def __init__(self, app_: ASGIApp, chat_ui_origin: str) -> None:
        super().__init__(app_)
        self.policy = f"frame-ancestors 'self' {chat_ui_origin}"

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = self.policy
        return response


_CHAT_UI_ORIGIN = f"http://localhost:{CONFIG.chat_ui_port}"
app.add_middleware(FrameAncestorsCSP, chat_ui_origin=_CHAT_UI_ORIGIN)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# Tracks when each approval page was most recently rendered to the user.
# Used to compute dwell time between display and decision. Process-local;
# survives the confirmer process's lifetime only. The signed receipt on
# disk is the durable record.
_VIEWED_AT: dict[str, datetime.datetime] = {}


@app.on_event("startup")
async def _startup() -> None:
    ensure_all_keys()


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat().replace("+00:00", "Z")


def _iso(ts: datetime.datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


def _render_error(message: str, approval_id: str | None = None, status: int = 400) -> HTMLResponse:
    html = env.get_template("error.html").render(message=message, approval_id=approval_id)
    return HTMLResponse(content=html, status_code=status)


def _render_completed(approval_id: str, decision: str) -> HTMLResponse:
    headline = "Trade approved." if decision == "approve" else "Trade declined."
    html = env.get_template("completed.html").render(
        approval_id=approval_id, headline=headline, decision=decision
    )
    return HTMLResponse(content=html)


def _render_trade_template(material_fields: dict[str, Any], approval_id: str, expires_at: str) -> str:
    template = env.get_template(TEMPLATE_FILENAME)
    return template.render(
        approval_id=approval_id,
        expires_at=expires_at,
        side=material_fields["side"],
        ticker=material_fields["ticker"],
        quantity=material_fields["quantity"],
        order_type=material_fields["order_type"],
        time_in_force=material_fields["time_in_force"],
        account_id=material_fields["account_id"],
    )


def _load_and_validate_intent(approval_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """Return (signed_intent, error_message)."""
    signed_intent = store.read_intent(approval_id)
    if signed_intent is None:
        return None, "Unknown approval."
    if not verify_envelope(signed_intent):
        return None, "Intent signature failed verification."
    payload = signed_intent["payload"]
    expires_at = payload.get("expires_at")
    if expires_at:
        ts = expires_at
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        try:
            expires_dt = datetime.datetime.fromisoformat(ts)
        except ValueError:
            return None, "Intent expires_at malformed."
        if datetime.datetime.now(datetime.timezone.utc) >= expires_dt:
            return None, "Approval has expired."
    if store.is_consumed(approval_id):
        return None, "Approval already consumed."
    return signed_intent, None


@app.get("/approve/{approval_id}", response_class=HTMLResponse)
async def get_approval(approval_id: str, request: Request) -> HTMLResponse:
    signed_intent, err = _load_and_validate_intent(approval_id)
    if err:
        return _render_error(err, approval_id=approval_id, status=404 if "Unknown" in err else 410)

    if store.read_receipt(approval_id) is not None:
        return _render_error("This approval has already been decided.", approval_id=approval_id, status=409)

    payload = signed_intent["payload"]
    material_fields = {
        f: payload["action"]["args_resolved"][f]
        for f in payload["policy"]["required_material_fields"]
    }
    html = _render_trade_template(material_fields, approval_id, payload["expires_at"])
    # Record the most recent page render so we can report dwell time on POST.
    # Reloads overwrite — we want the time since the user most recently saw the page.
    _VIEWED_AT[approval_id] = _now_utc()
    return HTMLResponse(content=html)


@app.post("/approve/{approval_id}/decision", response_class=HTMLResponse)
async def post_decision(
    approval_id: str,
    request: Request,
    decision: str = Form(...),
) -> HTMLResponse:
    if decision not in ("approve", "decline"):
        return _render_error("Invalid decision.", approval_id=approval_id, status=400)

    signed_intent, err = _load_and_validate_intent(approval_id)
    if err:
        return _render_error(err, approval_id=approval_id, status=410)

    if store.read_receipt(approval_id) is not None:
        return _render_error("This approval has already been decided.", approval_id=approval_id, status=409)

    payload = signed_intent["payload"]
    required = payload["policy"]["required_material_fields"]
    resolved = payload["action"]["args_resolved"]
    material_fields_shown = {f: resolved[f] for f in required}

    # Re-render the exact same HTML the user saw on GET (deterministic) and hash it.
    rendered_html = _render_trade_template(material_fields_shown, approval_id, payload["expires_at"])
    rendered_digest = "sha256:" + hashlib.sha256(rendered_html.encode("utf-8")).hexdigest()

    auth_assertion = f"demo:{CONFIG.demo_principal}"
    auth_assertion_digest = "sha256:" + hashlib.sha256(auth_assertion.encode("utf-8")).hexdigest()

    decided_at = _now_utc()
    viewed_at = _VIEWED_AT.pop(approval_id, None)
    dwell_ms = int((decided_at - viewed_at).total_seconds() * 1000) if viewed_at else None

    receipt_payload: dict[str, Any] = {
        "artifact_type": "approval_receipt",
        "schema_version": "1.0.0",
        "approval_id": approval_id,
        "intent_digest": "sha256:" + digest_hex(payload),
        "decision": decision,
        "approver": {
            "subject": CONFIG.demo_principal,
            "auth_method": "demo_session_header",
            "auth_assertion_digest": auth_assertion_digest,
        },
        "approved_at": _iso(decided_at),
        "viewed_at": _iso(viewed_at) if viewed_at else None,
        "dwell_ms": dwell_ms,
        "client_meta": {
            "user_agent": request.headers.get("user-agent", ""),
            "ip": request.client.host if request.client else "127.0.0.1",
        },
    }
    if decision == "approve":
        receipt_payload["display_manifest"] = {
            "template_id": TEMPLATE_ID,
            "template_version": TEMPLATE_VERSION,
            "rendered_digest": rendered_digest,
            "material_fields_shown": material_fields_shown,
            "locale": "en-US",
            "timezone": "America/New_York",
        }

    signed_receipt = sign_envelope(receipt_payload, get_confirmer_key())
    try:
        store.write_receipt(approval_id, signed_receipt)
    except FileExistsError:
        return _render_error("This approval was just decided in another window.", approval_id=approval_id, status=409)

    # Notify the agent backend so the MCP elicitation can resolve.
    action = "accept" if decision == "approve" else "decline"
    notify_url = f"http://localhost:{CONFIG.agent_port}/elicitation/resolve/{approval_id}"
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.post(notify_url, json={"action": action})
    except Exception:
        # Backend not running, or transient — receipt is on disk regardless.
        pass

    return _render_completed(approval_id, decision)
