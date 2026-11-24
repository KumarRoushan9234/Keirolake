"""Start the engine for UI development: port 8000 (the Vite proxy's target), embedded Qdrant in data/dev.

    backend/.venv/Scripts/python qa/dev_engine.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "dev"
os.environ.setdefault("DATA_DIR", str(DATA))
os.environ.setdefault("QDRANT_PATH", str(DATA / "qdrant"))
os.environ.setdefault("KEIROLAKE_KEYRING", "0")
os.environ.setdefault("LOCAL_AUTOSTART", "0")
sys.path.insert(0, str(ROOT / "backend"))

import uvicorn  # noqa: E402

uvicorn.run("app.main:app", host="127.0.0.1", port=int(os.getenv("DEV_PORT", "8000")), log_level="warning")
