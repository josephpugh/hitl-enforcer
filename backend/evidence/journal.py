"""Hash-chained NDJSON journal.

Each line is a JSON object with at least: ts, event, prev_hash.
prev_hash is sha256 of the previous full line (raw bytes, including the trailing newline).
This makes truncation, reordering, or insertion detectable.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

from ..config import CONFIG

_LOCK = threading.Lock()
_GENESIS = "sha256:" + "0" * 64


def _journal_path() -> Path:
    return CONFIG.evidence_dir / "journal.ndjson"


def _last_line_hash(path: Path) -> str:
    if not path.exists() or path.stat().st_size == 0:
        return _GENESIS
    # Walk backwards to find the final newline; small journals so just read the tail.
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        chunk = min(size, 64 * 1024)
        f.seek(size - chunk, os.SEEK_SET)
        tail = f.read(chunk)
    lines = [ln for ln in tail.split(b"\n") if ln]
    if not lines:
        return _GENESIS
    last = lines[-1]
    return "sha256:" + hashlib.sha256(last + b"\n").hexdigest()


def append(event: str, **fields: Any) -> None:
    """Append one entry to the journal. Thread-safe within this process."""
    path = _journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        prev_hash = _last_line_hash(path)
        entry: dict[str, Any] = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            "event": event,
            "prev_hash": prev_hash,
        }
        entry.update(fields)
        line = json.dumps(entry, sort_keys=True, separators=(",", ":"))
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)


def read_all() -> list[dict[str, Any]]:
    path = _journal_path()
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def verify_chain() -> tuple[bool, str | None]:
    """Walk the journal, verifying every line's prev_hash matches the previous line."""
    path = _journal_path()
    if not path.exists():
        return True, None
    raw_lines = [ln for ln in path.read_bytes().split(b"\n") if ln]
    expected = _GENESIS
    for i, raw in enumerate(raw_lines):
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError as exc:
            return False, f"line {i}: invalid JSON ({exc})"
        if entry.get("prev_hash") != expected:
            return False, f"line {i}: prev_hash mismatch (expected {expected}, got {entry.get('prev_hash')})"
        expected = "sha256:" + hashlib.sha256(raw + b"\n").hexdigest()
    return True, None
