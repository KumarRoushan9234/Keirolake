"""llama.cpp fallback for machines without Ollama.

Runs `llama-server` (OpenAI-compatible) with a GGUF file on 127.0.0.1:8081.
The router talks to it as the `llamacpp` provider. Paths come from settings
(LLAMACPP_SERVER, LLAMACPP_MODEL_PATH); GGUF files can be downloaded with a job.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.config import get_settings

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
PORT = 8081
_proc: subprocess.Popen | None = None


def server_path() -> str | None:
    configured = os.getenv("LLAMACPP_SERVER")
    if configured and Path(configured).exists():
        return configured
    bundled = get_settings().data_dir / "bin" / ("llama-server.exe" if sys.platform == "win32" else "llama-server")
    return str(bundled) if bundled.exists() else shutil.which("llama-server")


def models_dir() -> Path:
    path = get_settings().data_dir / "models"
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _healthy() -> bool:
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            return (await client.get(f"http://127.0.0.1:{PORT}/health")).status_code == 200
    except httpx.HTTPError:
        return False


async def status() -> dict:
    return {"server": server_path(), "model_path": os.getenv("LLAMACPP_MODEL_PATH") or None,
            # A refused connection takes ~2 s on Windows, so only probe when llama.cpp is actually set up.
            "running": bool(server_path() and os.getenv("LLAMACPP_MODEL_PATH")) and await _healthy(), "port": PORT,
            "ggufs": sorted(p.name for p in models_dir().glob("*.gguf"))}


async def start(gpu_layers: int = 99, context: int = 8192) -> dict:
    global _proc
    if await _healthy():
        return await status()
    exe, model = server_path(), os.getenv("LLAMACPP_MODEL_PATH")
    if not exe:
        raise RuntimeError("llama-server not found; set its path on the Models page")
    if not model or not Path(model).exists():
        raise RuntimeError("No GGUF model chosen; download one or set LLAMACPP_MODEL_PATH")
    log_path = get_settings().data_dir / "logs" / "llamacpp.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as out:
        _proc = subprocess.Popen([exe, "-m", model, "--host", "127.0.0.1", "--port", str(PORT), "-ngl", str(gpu_layers),
                                  "-c", str(context)], stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 creationflags=_NO_WINDOW)
    for _ in range(120):
        await asyncio.sleep(0.5)
        if await _healthy():
            return await status()
        if _proc.poll() is not None:
            break
    raise RuntimeError(f"llama-server did not start; see {log_path}")


def stop() -> None:
    if _proc and _proc.poll() is None:
        _proc.terminate()


def download_gguf_job(url: str):
    """Download a GGUF (e.g. from Hugging Face) into data/models with resume."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.path.endswith(".gguf"):
        raise ValueError("Give an https:// link to a .gguf file")
    target = models_dir() / Path(parsed.path).name

    async def job(ctx) -> dict:
        from app.library.download import fetch_resumable

        await fetch_resumable(url, target, ctx)
        return {"path": str(target)}

    return job
