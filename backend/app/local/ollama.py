"""Find, start and manage a local Ollama runtime."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx

from app import http

from app.config import get_settings

log = logging.getLogger(__name__)
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def base_url() -> str:
    url = (os.getenv("OLLAMA_BASE_URL") or "http://127.0.0.1:11434").rstrip("/")
    return url[:-3] if url.endswith("/v1") else url


def portable_dir() -> Path:
    """Where the app installs its own copy of Ollama (no administrator rights needed)."""
    return get_settings().data_dir / "bin" / "ollama"


def portable_exe() -> Path:
    return portable_dir() / ("ollama.exe" if sys.platform == "win32" else "bin/ollama")


def is_portable(exe: str | None) -> bool:
    return bool(exe) and portable_exe().exists() and Path(exe).resolve() == portable_exe().resolve()


def find_exe() -> str | None:
    found = shutil.which("ollama")
    if found:
        return found
    if portable_exe().exists():
        return str(portable_exe())
    candidates = []
    if sys.platform == "win32":
        local = os.getenv("LOCALAPPDATA", "")
        candidates += [Path(local) / "Programs" / "Ollama" / "ollama.exe", Path(r"C:\Program Files\Ollama\ollama.exe")]
    else:
        candidates += [Path("/usr/local/bin/ollama"), Path("/usr/bin/ollama"), Path.home() / ".local/bin/ollama"]
    return next((str(p) for p in candidates if p.exists()), None)


async def _get(path: str, timeout: float = 3.0) -> dict | None:
    try:
        resp = await http.client("ollama").get(base_url() + path, timeout=timeout)
        return resp.json() if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


async def status() -> dict:
    version = await _get("/api/version")
    running = version is not None
    tags = (await _get("/api/tags") or {}).get("models", []) if running else []
    loaded = (await _get("/api/ps") or {}).get("models", []) if running else []
    return {
        "installed": find_exe() is not None or running,
        "exe": find_exe(),
        "running": running,
        "version": (version or {}).get("version"),
        "base_url": base_url(),
        "models": [{"name": m["name"], "size_gb": round(m.get("size", 0) / 1e9, 2),
                    "local": not m.get("remote_host") and not m["name"].endswith(("-cloud", ":cloud")) and m.get("size", 0) > 2**20}
                   for m in tags],
        "loaded": [m.get("name") for m in loaded],
        "install_url": "https://ollama.com/download",
    }


_proc: subprocess.Popen | None = None


async def start(wait_seconds: float = 20.0) -> dict:
    """Start `ollama serve` if it isn't running. Returns the new status."""
    global _proc
    if await _get("/api/version"):
        return await status()
    exe = find_exe()
    if not exe:
        raise RuntimeError("Ollama is not installed. Get it from https://ollama.com/download")
    log_path = get_settings().data_dir / "logs" / "ollama.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if is_portable(exe):
        env.setdefault("OLLAMA_MODELS", str(get_settings().data_dir / "models" / "ollama"))
    with open(log_path, "ab") as out:
        _proc = subprocess.Popen([exe, "serve"], stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                                 creationflags=_NO_WINDOW, start_new_session=sys.platform != "win32")
    for _ in range(int(wait_seconds * 2)):
        await asyncio.sleep(0.5)
        if await _get("/api/version"):
            log.info("Started Ollama (%s)", exe)
            return await status()
    raise RuntimeError(f"Ollama did not start within {wait_seconds:.0f}s; see {log_path}")


async def restart(force: bool = False) -> dict:
    """Restart Ollama so changed runtime settings (context length, cache type...) take effect. Loaded models unload.

    An Ollama this app started is stopped directly. One started elsewhere (its own tray app) is only stopped with
    `force`, so the user decides.
    """
    global _proc
    import psutil

    managed = _proc is not None and _proc.poll() is None
    running = await _get("/api/version") is not None
    if managed:
        _proc.terminate()
        try:
            _proc.wait(10)
        except subprocess.TimeoutExpired:
            _proc.kill()
        _proc = None
    elif running:
        if not force:
            raise RuntimeError("Ollama was started outside KeiroLake. Confirm to restart it here, or quit it from its tray icon and start it again.")
        for p in psutil.process_iter(["name"]):
            if (p.info["name"] or "").lower().startswith("ollama"):
                try:
                    p.terminate()
                except psutil.Error:
                    pass
    for _ in range(40):  # wait for the port to free up
        if await _get("/api/version") is None:
            break
        await asyncio.sleep(0.5)
    return await start()


async def has_model(name: str) -> bool:
    tags = (await _get("/api/tags") or {}).get("models", [])
    wanted = name if ":" in name else f"{name}:latest"
    return any(m["name"] in (name, wanted) for m in tags)


def pull_job(model: str):
    """Job that downloads a model, reporting byte progress."""

    async def job(ctx) -> dict:
        await start()
        last = -1.0
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", base_url() + "/api/pull", json={"model": model, "stream": True}) as resp:
                if resp.status_code != 200:
                    raise RuntimeError(f"Ollama pull failed: HTTP {resp.status_code} {(await resp.aread())[:200]!r}")
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if "error" in event:
                        raise RuntimeError(f"Ollama: {event['error']}")
                    total, done = event.get("total"), event.get("completed")
                    if total and done is not None:
                        fraction = done / total
                        if fraction - last >= 0.01:
                            last = fraction
                            await ctx.progress(fraction, f"{event.get('status', 'downloading')}: "
                                                         f"{done / 1e9:.2f} / {total / 1e9:.2f} GB")
                    elif event.get("status"):
                        await ctx.progress(max(last, 0), event["status"])
        return {"model": model}

    return job


async def delete(model: str) -> None:
    resp = await http.client("ollama").request("DELETE", base_url() + "/api/delete", json={"model": model}, timeout=30)
    if resp.status_code not in (200, 404):
        raise RuntimeError(f"Ollama delete failed: HTTP {resp.status_code}")


async def embed(model: str, texts: list[str], dim: int | None = None, timeout: float = 120.0) -> list[list[float]]:
    """Embed with Ollama's native API. Matryoshka models are truncated to `dim` and re-normalised."""
    cached = [_VECTORS.get((model, dim, t)) for t in texts]
    missing = [t for t, v in zip(texts, cached) if v is None]
    if not missing:
        return [list(v) for v in cached]
    resp = await http.client("ollama").post(base_url() + "/api/embed", json={"model": model, "input": missing, "truncate": True, "keep_alive": "30m"},
                                            timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"Ollama embed failed: HTTP {resp.status_code} {resp.text[:200]}")
    vectors = resp.json().get("embeddings") or []
    if len(vectors) != len(missing):
        raise RuntimeError(f"Ollama returned {len(vectors)} vectors for {len(missing)} texts")
    if dim:
        out = []
        for v in vectors:
            v = v[:dim]
            norm = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / norm for x in v])
        vectors = out
    fresh = iter(vectors)
    result = []
    for t, v in zip(texts, cached):
        if v is None:
            v = next(fresh)
            _VECTORS.put((model, dim, t), tuple(v))
        result.append(list(v))
    return result


class _LRU:
    """Small bounded cache: repeated questions and re-read passages skip the embedder."""

    def __init__(self, size: int) -> None:
        from collections import OrderedDict

        self.size, self.data = size, OrderedDict()

    def get(self, key):
        if key in self.data:
            self.data.move_to_end(key)
            return self.data[key]
        return None

    def put(self, key, value) -> None:
        self.data[key] = value
        self.data.move_to_end(key)
        while len(self.data) > self.size:
            self.data.popitem(last=False)


_VECTORS = _LRU(8192)


async def unload_all() -> list[str]:
    """Free the GPU: ask Ollama to drop every loaded model now (they load again on the next request)."""
    st = await status()
    freed = []
    for name in st.get("loaded", []):
        try:
            await http.client("ollama").post(base_url() + "/api/generate", json={"model": name, "keep_alive": 0}, timeout=30)
            freed.append(name)
        except httpx.HTTPError:
            pass
    return freed
