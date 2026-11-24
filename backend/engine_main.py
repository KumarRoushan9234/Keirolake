"""Entry point for the frozen engine the desktop app runs as a sidecar.

The shell passes everything through the environment: KEIROLAKE_PORT,
KEIROLAKE_TOKEN, DATA_DIR and QDRANT_URL / QDRANT_PATH.
"""
import multiprocessing
import os

import uvicorn


def main() -> None:
    multiprocessing.freeze_support()
    from app.main import app

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("KEIROLAKE_PORT", "8765")), log_level="info",
                access_log=False)


if __name__ == "__main__":
    main()
