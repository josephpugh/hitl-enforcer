"""All 8 verification checks (§6.2)."""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any

from ..crypto.canonical import digest_hex
from ..crypto.keys import load_trust_bundle
from ..crypto.sign import verify_envelope
from ..evidence import store


def _parse_iso(ts: str) -> datetime.datetime:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.datetime.fromisoformat(ts)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


@dataclass
class VerificationResult:
    intent_signature_valid: bool = False
    receipt_signature_valid: bool = False
    intent_not_expired: bool = False
    receipt_within_max_age: bool = False
    payload_match: bool = False
    material_fields_complete: bool = False
    single_use_not_consumed: bool = False
    approver_authorized: bool = False
    failures: list[str] = field(default_factory=list)

    def all_passed(self) -> bool:
        return (
            self.intent_signature_valid
            and self.receipt_signature_valid
            and self.intent_not_expired
            and self.receipt_within_max_age
            and self.payload_match
            and self.material_fields_complete
            and self.single_use_not_consumed
            and self.approver_authorized
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent_signature_valid": self.intent_signature_valid,
            "receipt_signature_valid": self.receipt_signature_valid,
            "intent_not_expired": self.intent_not_expired,
            "receipt_within_max_age": self.receipt_within_max_age,
            "payload_match": self.payload_match,
            "material_fields_complete": self.material_fields_complete,
            "single_use_not_consumed": self.single_use_not_consumed,
            "approver_authorized": self.approver_authorized,
        }


def verify_receipt(signed_intent: dict[str, Any], signed_receipt: dict[str, Any]) -> VerificationResult:
    """Run all 8 checks. Caller decides whether to commit based on `all_passed()`."""
    result = VerificationResult()
    trust = load_trust_bundle()

    # 1. Intent signature valid.
    if verify_envelope(signed_intent, trust):
        result.intent_signature_valid = True
    else:
        result.failures.append("intent signature invalid")

    # 2. Receipt signature valid.
    if verify_envelope(signed_receipt, trust):
        result.receipt_signature_valid = True
    else:
        result.failures.append("receipt signature invalid")

    intent_payload = signed_intent.get("payload", {})
    receipt_payload = signed_receipt.get("payload", {})
    approval_id = intent_payload.get("approval_id")

    # 3. now() < intent.expires_at.
    try:
        expires_at = _parse_iso(intent_payload["expires_at"])
        if _now() < expires_at:
            result.intent_not_expired = True
        else:
            result.failures.append("intent expired")
    except (KeyError, ValueError):
        result.failures.append("intent expires_at missing or malformed")

    # 4. receipt.approved_at - intent.created_at <= policy.max_approval_age_seconds.
    try:
        created_at = _parse_iso(intent_payload["created_at"])
        approved_at = _parse_iso(receipt_payload["approved_at"])
        max_age = intent_payload["policy"]["max_approval_age_seconds"]
        age = (approved_at - created_at).total_seconds()
        if 0 <= age <= max_age:
            result.receipt_within_max_age = True
        else:
            result.failures.append(f"receipt outside max age (age={age}s, max={max_age}s)")
    except (KeyError, ValueError):
        result.failures.append("timestamps missing or malformed")

    # 5. receipt.intent_digest == sha256(JCS(intent.payload)).
    try:
        expected_digest = "sha256:" + digest_hex(intent_payload)
        actual_digest = receipt_payload["intent_digest"]
        if expected_digest == actual_digest:
            result.payload_match = True
        else:
            result.failures.append(f"intent_digest mismatch (expected {expected_digest}, got {actual_digest})")
    except KeyError:
        result.failures.append("receipt missing intent_digest")

    # 6. material_fields_shown contains every required field with matching values.
    try:
        required = intent_payload["policy"]["required_material_fields"]
        shown = receipt_payload["display_manifest"]["material_fields_shown"]
        resolved = intent_payload["action"]["args_resolved"]
        missing = [f for f in required if f not in shown]
        mismatched = [
            f for f in required
            if f in shown and shown[f] != resolved.get(f)
        ]
        # Decline receipts skip the material-fields check by design (no execution to gate).
        if receipt_payload.get("decision") == "decline":
            result.material_fields_complete = True
        elif missing:
            result.failures.append(f"material fields missing: {missing}")
        elif mismatched:
            result.failures.append(f"material fields mismatched: {mismatched}")
        else:
            result.material_fields_complete = True
    except KeyError as exc:
        result.failures.append(f"display_manifest missing field: {exc}")

    # 7. single-use not consumed.
    if approval_id and not store.is_consumed(approval_id):
        result.single_use_not_consumed = True
    else:
        result.failures.append("approval already consumed")

    # 8. approver.subject == intent.caller.principal.
    try:
        approver_subject = receipt_payload["approver"]["subject"]
        principal = intent_payload["caller"]["principal"]
        if approver_subject == principal:
            result.approver_authorized = True
        else:
            result.failures.append(
                f"approver subject does not match principal ({approver_subject} vs {principal})"
            )
    except KeyError:
        result.failures.append("approver or principal missing")

    return result
