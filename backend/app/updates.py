"""Is there a newer KeiroLake? Read from the project's GitHub Releases; there is no update server.

The desktop app installs updates itself (signed, through the shell). This check is for every copy, including the
server and Docker versions, which can't replace themselves: it says what's new and where to get it.
"""
from __future__ import annotations

import re
import time

from app import http
from app.support import APP_VERSION

REPO = "KumarRoushan9234/Keirolake"
RELEASES = f"https://github.com/{REPO}/releases"
_cache: dict = {"at": 0.0, "data": None}
TTL = 6 * 3600


def _parts(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def newer(latest: str, current: str = APP_VERSION) -> bool:
    return _parts(latest) > _parts(current)


async def latest(force: bool = False) -> dict:
    """The newest published release, cached for six hours. Never raises: offline just means "couldn't check"."""
    now = time.time()
    if not force and _cache["data"] and now - _cache["at"] < TTL:
        return _cache["data"]
    out = {"current": APP_VERSION, "available": False, "version": None, "notes": "", "url": RELEASES, "assets": [], "error": "",
           "package": package_kind()}
    try:
        r = await http.client("github").get(f"https://api.github.com/repos/{REPO}/releases/latest",
                                            headers={"Accept": "application/vnd.github+json"}, timeout=15)
        if r.status_code == 404:
            out["error"] = "No release has been published yet"
        else:
            r.raise_for_status()
            rel = r.json()
            version = str(rel.get("tag_name", "")).lstrip("v")
            out.update(version=version, available=newer(version), notes=(rel.get("body") or "")[:4000], url=rel.get("html_url") or RELEASES,
                       assets=[{"name": a["name"], "url": a["browser_download_url"], "size": a.get("size", 0)} for a in rel.get("assets", [])
                               if not a["name"].endswith((".sig", ".json"))])
    except Exception as exc:  # noqa: BLE001 - offline or rate limited
        out["error"] = f"Couldn't reach GitHub: {exc.__class__.__name__}"
    _cache.update(at=now, data=out)
    return out


# ---------- .deb / .rpm installs: the Tauri updater can't replace these, so the engine does it ----------
def package_kind() -> str | None:
    """'deb' or 'rpm' when this copy was installed by the system package manager, else None."""
    import os
    import shutil
    import subprocess
    import sys

    if not sys.platform.startswith("linux") or os.getenv("APPIMAGE"):
        return None
    exe = "/usr/bin/keirolake-desktop"
    for kind, cmd in (("deb", ["dpkg", "-S", exe]), ("rpm", ["rpm", "-qf", exe])):
        if shutil.which(cmd[0]) and subprocess.run(cmd, capture_output=True).returncode == 0:
            return kind
    return None


def package_asset(kind: str, version: str) -> str:
    return f"KeiroLake_{version}_amd64.deb" if kind == "deb" else f"KeiroLake-{version}-1.x86_64.rpm"


def install_command(kind: str, path: str) -> list[str]:
    """Installed with the system's own password prompt (pkexec), like any other package update."""
    import shutil

    if kind == "deb":
        return ["pkexec", "apt-get", "install", "-y", "--allow-downgrades", path]
    for tool in ("dnf", "zypper", "yum"):
        if shutil.which(tool):
            return ["pkexec", tool, "install", "-y", path] if tool != "zypper" else ["pkexec", "zypper", "--non-interactive", "install", "--allow-unsigned-rpm", path]
    return ["pkexec", "rpm", "-U", "--replacepkgs", path]


MAX_PACKAGE = 800 * 2**20


def install_package_job():
    """Download the new .deb or .rpm, check it against the project's signature, and install it."""
    import asyncio
    import tempfile
    from pathlib import Path

    from app import minisign

    kind = package_kind()
    if kind is None:
        raise RuntimeError("This copy wasn't installed from a .deb or .rpm package")

    async def job(ctx) -> dict:
        info = await latest(force=True)
        if not info["available"]:
            return {"installed": None, "note": f"KeiroLake {info['current']} is already the newest version"}
        name = package_asset(kind, info["version"])
        base = f"https://github.com/{REPO}/releases/download/v{info['version']}/{name}"
        await ctx.progress(0.05, f"Downloading {name}")
        c = http.client("github-download", follow_redirects=True)
        sig_resp = await c.get(base + ".sig", timeout=60)
        if sig_resp.status_code != 200:
            raise RuntimeError("This release has no signature for the package, so it wasn't installed")
        sig = sig_resp.text
        data = bytearray()
        async with c.stream("GET", base, timeout=600) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            async for chunk in r.aiter_bytes():
                data += chunk
                if len(data) > MAX_PACKAGE:
                    raise RuntimeError("The download is larger than expected")
                if total:
                    await ctx.progress(0.05 + 0.8 * len(data) / total, f"Downloading {name}")
        await ctx.progress(0.88, "Checking the signature")
        minisign.verify(bytes(data), sig)  # refuses anything not signed with the project's key
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / name
            path.write_bytes(bytes(data))
            await ctx.progress(0.9, "Installing (your system asks for your password)")
            proc = await asyncio.create_subprocess_exec(*install_command(kind, str(path)), stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.STDOUT)
            out, _ = await proc.communicate()
        if proc.returncode != 0:
            tail = out.decode(errors="replace").strip().splitlines()[-3:]
            raise RuntimeError("The install didn't finish" + (": " + " ".join(tail) if tail else " (the password prompt was closed?)"))
        return {"installed": info["version"], "restart": True}

    return job
