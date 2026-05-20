#!/usr/bin/env python3
"""Standalone audit verifier — runs against `./evidence/` and `./keys/trust_bundle.json`.

Usage:
    python tools/verify.py --approval-id 01JABC...
    python tools/verify.py --all
    python tools/verify.py --journal   # just walk the hash chain

This script intentionally has no dependency on the backend's runtime state:
everything it needs is on disk.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.config import CONFIG  # noqa: E402
from backend.crypto.canonical import digest_hex  # noqa: E402
from backend.crypto.keys import load_trust_bundle  # noqa: E402
from backend.crypto.sign import verify_envelope  # noqa: E402
from backend.evidence import journal as journal_mod  # noqa: E402


GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"


def _ok(msg: str) -> None:
    print(f"{GREEN}[OK] {RESET}  {msg}")


def _fail(msg: str) -> None:
    print(f"{RED}[FAIL]{RESET}  {msg}")


def _warn(msg: str) -> None:
    print(f"{YELLOW}[WARN]{RESET}  {msg}")


def _load(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _seconds_between(a: str, b: str) -> float:
    import datetime as dt

    def parse(s: str) -> dt.datetime:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return dt.datetime.fromisoformat(s)

    return (parse(b) - parse(a)).total_seconds()


def verify_one(approval_id: str, evidence_dir: Path, keys_dir: Path) -> bool:
    trust = load_trust_bundle(keys_dir)
    if not trust:
        _fail(f"trust bundle not found at {keys_dir / 'trust_bundle.json'}")
        return False

    intent_path = evidence_dir / "intents" / f"{approval_id}.json"
    receipt_path = evidence_dir / "receipts" / f"{approval_id}.json"
    execution_path = evidence_dir / "executions" / f"{approval_id}.json"

    signed_intent = _load(intent_path)
    signed_receipt = _load(receipt_path)
    signed_execution = _load(execution_path)

    if signed_intent is None:
        _fail(f"intent not found: {intent_path}")
        return False

    all_ok = True

    intent_payload = signed_intent["payload"]
    intent_kid = signed_intent["signature"]["kid"]
    if verify_envelope(signed_intent, trust):
        _ok(f"Intent signature valid       (kid={intent_kid})")
    else:
        _fail(f"Intent signature INVALID    (kid={intent_kid})")
        all_ok = False

    if signed_receipt is None:
        _warn("No receipt on disk — approval was never decided.")
        return all_ok

    receipt_payload = signed_receipt["payload"]
    receipt_kid = signed_receipt["signature"]["kid"]
    if verify_envelope(signed_receipt, trust):
        _ok(f"Receipt signature valid      (kid={receipt_kid})")
    else:
        _fail(f"Receipt signature INVALID   (kid={receipt_kid})")
        all_ok = False

    expected_digest = "sha256:" + digest_hex(intent_payload)
    if receipt_payload.get("intent_digest") == expected_digest:
        _ok("Receipt.intent_digest matches sha256(JCS(intent.payload))")
    else:
        _fail(
            "Receipt.intent_digest MISMATCH "
            f"(expected {expected_digest}, got {receipt_payload.get('intent_digest')})"
        )
        all_ok = False

    decision = receipt_payload.get("decision")

    if signed_execution is not None:
        execution_payload = signed_execution["payload"]
        execution_kid = signed_execution["signature"]["kid"]
        if verify_envelope(signed_execution, trust):
            _ok(f"Execution signature valid    (kid={execution_kid})")
        else:
            _fail(f"Execution signature INVALID (kid={execution_kid})")
            all_ok = False

        if execution_payload.get("intent_digest") == expected_digest:
            _ok("Execution.intent_digest matches")
        else:
            _fail("Execution.intent_digest MISMATCH")
            all_ok = False
        expected_receipt_digest = "sha256:" + digest_hex(receipt_payload)
        if execution_payload.get("receipt_digest") == expected_receipt_digest:
            _ok("Execution.receipt_digest matches")
        else:
            _fail("Execution.receipt_digest MISMATCH")
            all_ok = False

    # Material fields check (skipped for declines).
    if decision == "approve":
        required = intent_payload["policy"]["required_material_fields"]
        shown = receipt_payload.get("display_manifest", {}).get("material_fields_shown", {})
        resolved = intent_payload["action"]["args_resolved"]
        missing = [f for f in required if f not in shown]
        mismatched = [f for f in required if f in shown and shown[f] != resolved.get(f)]
        if not missing and not mismatched:
            _ok(f"All material fields present and match: {', '.join(required)}")
        else:
            if missing:
                _fail(f"Material fields missing: {missing}")
            if mismatched:
                _fail(f"Material fields mismatched: {mismatched}")
            all_ok = False

    # Age check
    try:
        max_age = intent_payload["policy"]["max_approval_age_seconds"]
        age = _seconds_between(intent_payload["created_at"], receipt_payload["approved_at"])
        if 0 <= age <= max_age:
            _ok(f"Receipt within max age ({age:.1f}s < {max_age}s)")
        else:
            _fail(f"Receipt OUTSIDE max age (age={age:.1f}s, max={max_age}s)")
            all_ok = False
    except (KeyError, ValueError) as exc:
        _fail(f"Age check failed: {exc}")
        all_ok = False

    # Dwell time (informational): how long the approval page was visible
    # before the user decided. Absent on receipts written by older confirmers
    # or by callers that didn't first GET the approval page.
    dwell_ms = receipt_payload.get("dwell_ms")
    viewed_at = receipt_payload.get("viewed_at")
    if dwell_ms is not None and viewed_at is not None:
        _ok(f"Dwell time before decision: {dwell_ms} ms (viewed_at={viewed_at})")
    else:
        _warn("No dwell time recorded — receipt may predate the viewed_at field, "
              "or the decider POSTed without ever GETting the approval page.")

    print()
    if all_ok and decision == "approve" and signed_execution:
        a = intent_payload["action"]["args_resolved"]
        broker = signed_execution["payload"]["broker"]
        approver = receipt_payload["approver"]["subject"]
        print(
            f"{GREEN}VERIFIED:{RESET} {a['side']} {a['quantity']} {a['ticker']} "
            f"@ {a['order_type']} {a['time_in_force']} for {a['account_id']}"
        )
        print(f"  Approved by {approver} at {receipt_payload['approved_at']}")
        print(
            f"  Executed at {signed_execution['payload']['executed_at']}, "
            f"fill {broker['fill_price_usd']}"
        )
    elif decision == "decline":
        print(f"{YELLOW}DECLINED{RESET}: approver {receipt_payload['approver']['subject']} declined.")
    elif not all_ok:
        print(f"{RED}VERIFICATION FAILED{RESET}")
    return all_ok


def verify_journal(evidence_dir: Path) -> bool:
    # Use the module's own verifier, but point at the right path.
    original = journal_mod._journal_path  # type: ignore[attr-defined]
    journal_mod._journal_path = lambda: evidence_dir / "journal.ndjson"  # type: ignore[assignment]
    try:
        ok, err = journal_mod.verify_chain()
    finally:
        journal_mod._journal_path = original  # type: ignore[assignment]
    if ok:
        _ok("Journal hash chain intact")
        return True
    _fail(f"Journal hash chain BROKEN: {err}")
    return False


def main() -> int:
    p = argparse.ArgumentParser(description="Verify HITL-enforcer evidence chain.")
    p.add_argument("--approval-id", help="Verify one approval_id end-to-end.")
    p.add_argument("--all", action="store_true", help="Verify every intent on disk.")
    p.add_argument("--journal", action="store_true", help="Verify only the journal hash chain.")
    p.add_argument("--evidence-dir", default=str(CONFIG.evidence_dir))
    p.add_argument("--keys-dir", default=str(CONFIG.keys_dir))
    args = p.parse_args()

    evidence_dir = Path(args.evidence_dir).resolve()
    keys_dir = Path(args.keys_dir).resolve()

    if args.journal:
        return 0 if verify_journal(evidence_dir) else 1

    if args.approval_id:
        ok = verify_one(args.approval_id, evidence_dir, keys_dir)
        print()
        verify_journal(evidence_dir)
        return 0 if ok else 1

    if args.all:
        intents_dir = evidence_dir / "intents"
        if not intents_dir.exists():
            _fail(f"no intents dir at {intents_dir}")
            return 1
        all_ok = True
        for path in sorted(intents_dir.glob("*.json")):
            print(f"\n--- {path.stem} ---")
            if not verify_one(path.stem, evidence_dir, keys_dir):
                all_ok = False
        print()
        verify_journal(evidence_dir)
        return 0 if all_ok else 1

    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
