"""Run KeiroLake without a window: on a home server, a NAS, a cloud VM or in Docker.

    keirolake serve                          # this computer only: http://127.0.0.1:8443
    keirolake serve --share wifi             # devices on your network, over HTTPS
    keirolake serve --share internet         # plus a private internet link (Cloudflare quick tunnel)

It is the same engine as the desktop app, set up the same way: the engine itself listens on a private loopback port
with a random token, and people reach it only through the sharing listener, which needs your password (the web app)
or an API key (/v1 and /mcp). The CLI finds the engine through <data>/serve.json, readable only by you.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import sys
from pathlib import Path

SHARES = {"local": "local", "wifi": "lan", "lan": "lan", "internet": "local", "open": "open"}


def default_data_dir() -> Path:
    if os.getenv("DATA_DIR"):
        return Path(os.environ["DATA_DIR"])
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "keirolake-server"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "keirolake-server"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def info_path(data: Path) -> Path:
    return data / "serve.json"


def read_info(data: Path | None = None) -> dict | None:
    try:
        return json.loads(info_path(data or default_data_dir()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def prepare(data: Path, share: str, port: int, password: str | None, keep_awake: bool) -> dict:
    """Set the environment and saved settings for this run, before the engine is imported."""
    if share not in SHARES:
        raise SystemExit(f"--share must be one of: {', '.join(SHARES)}")
    data.mkdir(parents=True, exist_ok=True)
    engine_port, token = free_port(), secrets.token_hex(24)
    os.environ.update(DATA_DIR=str(data), KEIROLAKE_TOKEN=token, KEIROLAKE_PORT=str(engine_port))
    if not os.getenv("QDRANT_URL"):
        os.environ.setdefault("QDRANT_PATH", str(data / "qdrant"))
    os.environ.setdefault("KEIROLAKE_KEYRING", "0" if not sys.stdout.isatty() else "1")  # services usually have no keychain

    from app import auth, prefs
    from app.db import init_db

    prefs.apply_saved()
    init_db()
    if password:
        auth.set_password(password)
    if not auth.has_password():
        raise SystemExit("Set a password first: keirolake serve --password-file FILE, or KEIROLAKE_PASSWORD in the environment.")
    prefs.update({"LAN_ACCESS": SHARES[share], "LAN_PORT": str(port), "TUNNEL_ON": "1" if share == "internet" else "",
                  "KEEP_AWAKE": "1" if keep_awake else ""})
    info = {"url": f"http://127.0.0.1:{engine_port}", "token": token, "share": share, "listener_port": port, "pid": os.getpid()}
    p = info_path(data)
    p.write_text(json.dumps(info), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return info


def run(data: Path, share: str = "local", port: int = 8443, password: str | None = None, keep_awake: bool = False) -> None:
    info = prepare(data, share, port, password, keep_awake)
    import asyncio

    import uvicorn

    from app.main import app

    scheme = "https" if SHARES[share] in ("lan", "open") else "http"
    print(f"KeiroLake engine starting. Data: {data}", flush=True)
    print(f"Sign in with your password at the address below ({scheme}). Apps use /v1 and /mcp with an API key.")
    if share == "internet":
        print("The internet link appears in a few seconds: keirolake link")

    async def announce() -> None:
        from app import network, tunnel

        await asyncio.sleep(4)
        st = network.status()
        for url in st.get("urls", []):
            print(f"  open: {url}")
        if st.get("error"):
            print(f"  sharing problem: {st['error']}")
        for _ in range(30):
            url = tunnel.status().get("url") if share == "internet" else None
            if url or share != "internet":
                break
            await asyncio.sleep(2)
        if share == "internet":
            print(f"  internet link: {url or 'not up yet, see keirolake link'}")

    config = uvicorn.Config(app, host="127.0.0.1", port=int(info["url"].rsplit(":", 1)[1]), log_level="warning", access_log=False)
    server = uvicorn.Server(config)

    async def main() -> None:
        task = asyncio.create_task(announce())
        try:
            await server.serve()
        finally:
            task.cancel()
            info_path(data).unlink(missing_ok=True)

    asyncio.run(main())
