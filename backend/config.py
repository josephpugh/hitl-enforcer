"""Centralized config loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    openai_api_key: str | None
    openai_model: str
    agent_port: int
    confirmer_port: int
    chat_ui_port: int
    evidence_dir: Path
    keys_dir: Path
    demo_principal: str
    max_approval_age_seconds: int
    max_trade_quantity: int

    @property
    def confirmer_base_url(self) -> str:
        return f"http://localhost:{self.confirmer_port}"


def _abs(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else (REPO_ROOT / path).resolve()


def load_config() -> Config:
    return Config(
        openai_api_key=os.environ.get("OPENAI_API_KEY"),
        openai_model=os.environ.get("OPENAI_MODEL", "gpt-4.1"),
        agent_port=int(os.environ.get("AGENT_PORT", "8787")),
        confirmer_port=int(os.environ.get("CONFIRMER_PORT", "8788")),
        chat_ui_port=int(os.environ.get("CHAT_UI_PORT", "5173")),
        evidence_dir=_abs(os.environ.get("EVIDENCE_DIR", "./evidence")),
        keys_dir=_abs(os.environ.get("KEYS_DIR", "./keys")),
        demo_principal=os.environ.get("DEMO_PRINCIPAL", "user@example.com"),
        max_approval_age_seconds=int(os.environ.get("MAX_APPROVAL_AGE_SECONDS", "60")),
        max_trade_quantity=int(os.environ.get("MAX_TRADE_QUANTITY", "10000")),
    )


CONFIG = load_config()
