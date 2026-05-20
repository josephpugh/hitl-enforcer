from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import uvicorn  # noqa: E402

from backend.config import CONFIG  # noqa: E402


def main() -> None:
    uvicorn.run(
        "confirmer.app:app",
        host="127.0.0.1",
        port=CONFIG.confirmer_port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
