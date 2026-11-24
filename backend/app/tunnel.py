"""Reach the lake from any network with one switch: a Cloudflare quick tunnel to the app's own listener.

How it works: the engine already serves the UI and API on a private listener that needs a signed-in session. The
tunnel program (cloudflared, downloaded once from Cloudflare's GitHub releases and checked against the published
SHA-256) makes an outbound connection and hands back a public https address that reaches only that listener. Nothing
is opened on the router. The address changes every time the tunnel starts, it only works while this computer and the
app are running, and it is closed when the app quits.

Safety: a password must be set first; the sign-in lockout and session rules apply to every visitor; anyone with the
address can see the sign-in page, so use a long password. For a permanent private address, Tailscale is the better fit.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx

from app import auth, network, prefs
from app.config import get_settings

log = logging.getLogger(__name__)
RELEASES = "https://api.github.com/repos/cloudflare/cloudflared/releases/latest"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ASSETS = {"win32": "cloudflared-windows-amd64.exe", "linux": "cloudflared-linux-amd64", "darwin": "cloudflared-darwin-amd64.tgz"}


def asset_name() -> str:
    """Cloudflare's file for this computer (Macs get a .tgz holding the program; Apple Silicon has its own build)."""
    if sys.platform == "darwin":
        import platform

        return "cloudflared-darwin-arm64.tgz" if platform.machine() == "arm64" else "cloudflared-darwin-amd64.tgz"
    return ASSETS[sys.platform]

_proc: asyncio.subprocess.Process | None = None
_url = ""
_error = ""
_drain: asyncio.Task | None = None


class TunnelError(RuntimeError):
    pass


def binary() -> Path:
    return get_settings().data_dir / "bin" / ("cloudflared.exe" if sys.platform == "win32" else "cloudflared")


def supported() -> bool:
    return sys.platform in ASSETS


def running() -> bool:
    return _proc is not None and _proc.returncode is None


def status() -> dict:
    return {"supported": supported(), "installed": binary().exists(), "running": running(), "url": _url if running() else "", "error": _error,
            "password_set": auth.has_password(), "provider": "Cloudflare named tunnel" if named() else "Cloudflare quick tunnel",
            "named": named(), "hostname": hostname(), "port": tunnel_port(), "resume": os.getenv("TUNNEL_ON") == "1"}


async def install() -> None:
    """Download cloudflared once and verify it against the SHA-256 Cloudflare publishes for the release."""
    if not supported():
        raise TunnelError("The one-switch tunnel is available on Windows, macOS and Linux. Use Tailscale on this system.")
    name = asset_name()
    async with httpx.AsyncClient(timeout=60, follow_redirects=True, headers={"User-Agent": "KeiroLake"}) as client:
        meta = (await client.get(RELEASES)).json()
        asset = next((a for a in meta.get("assets", []) if a.get("name") == name), None)
        if asset is None:
            raise TunnelError("Couldn't find the tunnel program for this system in Cloudflare's latest release")
        digest = str(asset.get("digest", "")).removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise TunnelError("The release doesn't publish a checksum, so it was not downloaded")
        data = bytearray()
        async with client.stream("GET", asset["browser_download_url"]) as resp:
            if resp.status_code != 200:
                raise TunnelError(f"Download failed (HTTP {resp.status_code})")
            async for chunk in resp.aiter_bytes():
                data += chunk
                if len(data) > 200 * 2**20:
                    raise TunnelError("The download is larger than expected")
    if hashlib.sha256(data).hexdigest() != digest:
        raise TunnelError("The downloaded file didn't match its checksum and was discarded")
    if name.endswith(".tgz"):  # macOS: the program is the one file inside the checked archive
        import io
        import tarfile

        with tarfile.open(fileobj=io.BytesIO(bytes(data)), mode="r:gz") as tar:
            member = next((m for m in tar.getmembers() if m.isfile() and m.name.rsplit("/", 1)[-1] == "cloudflared"), None)
            if member is None:
                raise TunnelError("The downloaded archive didn't contain the tunnel program")
            data = bytearray(tar.extractfile(member).read())
    target = binary()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".part")
    tmp.write_bytes(bytes(data))
    if sys.platform != "win32":
        tmp.chmod(0o755)
    tmp.replace(target)


DEFAULT_TUNNEL_PORT = 8442
_listener = None
_listener_task: asyncio.Task | None = None


def tunnel_port() -> int:
    try:
        return int(os.getenv("TUNNEL_PORT") or DEFAULT_TUNNEL_PORT)
    except ValueError:
        return DEFAULT_TUNNEL_PORT


def named() -> bool:
    """A named tunnel (your own Cloudflare account and hostname) instead of a random trycloudflare.com address."""
    return bool(os.getenv("CLOUDFLARE_TUNNEL_TOKEN", "").strip())


def hostname() -> str:
    raw = os.getenv("TUNNEL_HOSTNAME", "").strip().lower().removeprefix("https://").strip("/")
    return raw if re.fullmatch(r"[a-z0-9.-]{3,253}", raw or "") else ""


async def _start_listener() -> None:
    """The tunnel's own door: plain HTTP on this computer only, separate from the Wi-Fi or open-port listener,
    so changing those settings never breaks the link. Every request is tagged, so sign-in and limits apply."""
    global _listener, _listener_task
    if _listener_task is not None and not _listener_task.done():
        return
    import uvicorn

    from app.main import app

    class _Quiet(uvicorn.Server):
        def install_signal_handlers(self) -> None:
            pass

        @contextlib.contextmanager
        def capture_signals(self):
            yield

    _listener = _Quiet(uvicorn.Config(network.tagged(app), host="127.0.0.1", port=tunnel_port(), log_level="warning", access_log=False, lifespan="off"))
    _listener_task = asyncio.create_task(_listener.serve())
    await asyncio.sleep(0.5)
    if _listener_task.done():
        exc = _listener_task.exception()
        _listener, _listener_task = None, None
        raise TunnelError(f"Port {tunnel_port()} on this computer is busy ({exc}). Pick another port for the link.")


async def _stop_listener() -> None:
    global _listener, _listener_task
    if _listener is not None:
        _listener.should_exit = True
        if _listener_task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(_listener_task, 5)
    _listener, _listener_task = None, None


def _kill_strays() -> int:
    """A tunnel left running by an earlier copy of the app (killed by an update or a crash) points at nothing: end it."""
    try:
        import psutil
    except ImportError:
        return 0
    ours, n = str(binary().resolve()).lower(), 0
    for p in psutil.process_iter(["exe", "pid"]):
        try:
            if p.info["exe"] and str(Path(p.info["exe"]).resolve()).lower() == ours and (_proc is None or p.pid != _proc.pid):
                p.kill()
                n += 1
        except (psutil.Error, OSError):
            continue
    return n


async def _read_ready(proc: asyncio.subprocess.Process) -> str:
    """The quick tunnel's address, or (named tunnel) the configured hostname once a connection registers."""
    assert proc.stderr

    async def scan() -> str:
        async for raw in proc.stderr:
            line = raw.decode("utf-8", "replace")
            if named():
                if "Registered tunnel connection" in line or "Connection registered" in line:
                    return f"https://{hostname()}" if hostname() else "connected"
            else:
                m = URL_RE.search(line)
                if m:
                    return m.group(0)
        return ""

    return await asyncio.wait_for(scan(), 60)


async def start() -> dict:
    global _proc, _url, _error, _drain
    if running():
        return status()
    _error = ""
    if not auth.has_password():
        raise TunnelError("Set a password first (Sharing). Anyone with the link can reach the sign-in page.")
    if not binary().exists():
        await install()
    _kill_strays()
    await _start_listener()
    if named():
        args = [str(binary()), "tunnel", "--no-autoupdate", "run"]
        env = {**os.environ, "NO_COLOR": "1", "TUNNEL_TOKEN": os.environ["CLOUDFLARE_TUNNEL_TOKEN"].strip()}  # never on the command line
    else:
        args = [str(binary()), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{tunnel_port()}"]
        env = {**os.environ, "NO_COLOR": "1"}
    _proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, creationflags=NO_CONSOLE, env=env)
    try:
        _url = await _read_ready(_proc)
    except asyncio.TimeoutError:
        _url = ""
    if not _url:
        await stop(remember=False)
        _error = ("Cloudflare didn't accept the tunnel token, or the tunnel couldn't connect." if named()
                  else "The tunnel didn't come up. Check your internet connection and try again.")
        raise TunnelError(_error)

    async def drain() -> None:  # keep reading so the pipe never fills
        assert _proc and _proc.stderr
        async for _ in _proc.stderr:
            pass

    _drain = asyncio.create_task(drain())
    prefs.update({"TUNNEL_ON": "1"})
    log.info("Tunnel is up")
    return status()


async def stop(remember: bool = True) -> dict:
    """Close the link. `remember` records that the user turned it off, so it doesn't come back on the next start."""
    global _proc, _url, _drain
    proc, _proc, _url = _proc, None, ""
    if proc is not None and proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 5)
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
    if _drain is not None:
        _drain.cancel()
        _drain = None
    await _stop_listener()
    if remember:
        prefs.update({"TUNNEL_ON": ""})
    return status()


async def resume() -> None:
    """If the link was on when the app last stopped (an update, a restart, a reboot), turn it back on."""
    if os.getenv("TUNNEL_ON") != "1" or running():
        return
    try:
        await start()
    except Exception as exc:  # noqa: BLE001 - shown on the Sharing page
        global _error
        _error = f"The link didn't come back after the restart: {exc}"
