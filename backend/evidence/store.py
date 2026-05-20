"""File-system evidence store: intents, receipts, executions, and consume sentinel."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from ..config import CONFIG
from ..crypto.canonical import digest_hex
from . import journal


def _evidence_root() -> Path:
    return CONFIG.evidence_dir


def _ensure_dirs() -> None:
    root = _evidence_root()
    for sub in ("intents", "receipts", "executions", "consumed"):
        (root / sub).mkdir(parents=True, exist_ok=True)


def intent_path(approval_id: str) -> Path:
    return _evidence_root() / "intents" / f"{approval_id}.json"


def receipt_path(approval_id: str) -> Path:
    return _evidence_root() / "receipts" / f"{approval_id}.json"


def execution_path(approval_id: str) -> Path:
    return _evidence_root() / "executions" / f"{approval_id}.json"


def consumed_path(approval_id: str) -> Path:
    return _evidence_root() / "consumed" / approval_id


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(tmp, path)


def _sha256_hex(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def write_intent(approval_id: str, signed_intent: dict[str, Any]) -> str:
    _ensure_dirs()
    path = intent_path(approval_id)
    content = json.dumps(signed_intent, indent=2, sort_keys=True)
    _write_atomic(path, content)
    sha = _sha256_hex(content)
    journal.append(
        "intent_staged",
        approval_id=approval_id,
        artifact=f"intents/{approval_id}.json",
        sha256=sha,
        intent_digest="sha256:" + digest_hex(signed_intent["payload"]),
    )
    return sha


def write_receipt(approval_id: str, signed_receipt: dict[str, Any]) -> str:
    _ensure_dirs()
    path = receipt_path(approval_id)
    # Receipts are write-once: if it already exists, the caller already approved.
    if path.exists():
        raise FileExistsError(f"Receipt already exists for {approval_id}")
    content = json.dumps(signed_receipt, indent=2, sort_keys=True)
    _write_atomic(path, content)
    sha = _sha256_hex(content)
    decision = signed_receipt["payload"].get("decision")
    journal.append(
        "receipt_recorded",
        approval_id=approval_id,
        artifact=f"receipts/{approval_id}.json",
        sha256=sha,
        decision=decision,
        receipt_digest="sha256:" + digest_hex(signed_receipt["payload"]),
    )
    return sha


def write_execution(approval_id: str, signed_execution: dict[str, Any]) -> str:
    _ensure_dirs()
    path = execution_path(approval_id)
    content = json.dumps(signed_execution, indent=2, sort_keys=True)
    _write_atomic(path, content)
    sha = _sha256_hex(content)
    journal.append(
        "execution_committed",
        approval_id=approval_id,
        artifact=f"executions/{approval_id}.json",
        sha256=sha,
    )
    return sha


def read_intent(approval_id: str) -> dict[str, Any] | None:
    path = intent_path(approval_id)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def read_receipt(approval_id: str) -> dict[str, Any] | None:
    path = receipt_path(approval_id)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def read_execution(approval_id: str) -> dict[str, Any] | None:
    path = execution_path(approval_id)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def try_consume_receipt(approval_id: str) -> bool:
    """Atomically create the consume sentinel. Returns True on first call, False on replay."""
    _ensure_dirs()
    path = consumed_path(approval_id)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    try:
        os.write(fd, b"")
    finally:
        os.close(fd)
    journal.append("receipt_consumed", approval_id=approval_id)
    return True


def is_consumed(approval_id: str) -> bool:
    return consumed_path(approval_id).exists()
