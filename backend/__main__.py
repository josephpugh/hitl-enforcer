from __future__ import annotations

import uvicorn

from .config import CONFIG


def main() -> None:
    uvicorn.run(
        "backend.app:app",
        host="127.0.0.1",
        port=CONFIG.agent_port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
