"""Install Ollama into the app's own folder: no installer, no administrator rights, no PATH changes.

Downloads the official release archive from github.com/ollama/ollama (checked
against the checksum file published with it) and unpacks it under
<data>/bin/ollama. The app then finds and starts it like any other install.
"""
from __future__ import annotations

import platform
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path

import httpx

from app.config import get_settings
from app.library.download import fetch_resumable
from app.local import ollama

BASE = "https://github.com/ollama/ollama/releases/latest/download"


class InstallError(RuntimeError):
    pass


def asset_name() -> str:
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        if sys.platform == "win32":
            return "ollama-windows-amd64.zip"
        if sys.platform.startswith("linux"):
            return "ollama-linux-amd64.tar.zst"
    if sys.platform == "win32" and machine in ("arm64", "aarch64"):
        return "ollama-windows-arm64.zip"
    if sys.platform == "darwin":
        raise InstallError("On a Mac, install the Ollama app from https://ollama.com/download/mac, then come back here to choose a model")
    raise InstallError("There is no automatic Ollama download for this system; install it from https://ollama.com/download")


def _inside(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def extract(archive: Path, dest: Path) -> None:
    """Unpack a .zip or .tar.zst/.tgz, refusing any entry that would land outside `dest`."""
    dest.mkdir(parents=True, exist_ok=True)
    name = archive.name
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                if not _inside(dest, dest / member.filename):
                    raise InstallError(f"Unsafe path in archive: {member.filename}")
            zf.extractall(dest)
    elif name.endswith((".tar.zst", ".tgz", ".tar.gz")):
        import zstandard

        if name.endswith(".tar.zst"):
            with open(archive, "rb") as raw, zstandard.ZstdDecompressor().stream_reader(raw) as stream, tarfile.open(fileobj=stream, mode="r|") as tar:
                _safe_untar(tar, dest)
        else:
            with tarfile.open(archive, "r:gz") as tar:
                _safe_untar(tar, dest)
    else:
        raise InstallError(f"Unknown archive type: {name}")


def _safe_untar(tar: tarfile.TarFile, dest: Path) -> None:
    for member in tar:
        if not _inside(dest, dest / member.name):
            raise InstallError(f"Unsafe path in archive: {member.name}")
        if member.issym() or member.islnk():
            link = (dest / member.name).parent / member.linkname if member.issym() else dest / member.linkname
            if not _inside(dest, link):
                raise InstallError(f"Unsafe link in archive: {member.name}")
        if member.isdev():
            continue
        try:
            tar.extract(member, dest, filter="data")
        except TypeError:  # Python without extraction filters
            tar.extract(member, dest)


async def expected_sha256(asset: str) -> str | None:
    """The checksum GitHub publishes next to the release, if it can be read."""
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(f"{BASE}/sha256sum.txt")
        if resp.status_code == 200:
            for line in resp.text.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[-1].lstrip("*./") == asset:
                    return parts[0]
    except httpx.HTTPError:
        pass
    return None


def install_job():
    """Background job: download, verify, unpack, then start Ollama."""

    async def job(ctx) -> dict:
        import asyncio

        if ollama.find_exe():
            await ollama.start()
            return {"already_installed": True, "exe": ollama.find_exe()}
        asset = asset_name()
        work = get_settings().data_dir / "bin" / "_download"
        archive = work / asset
        await ctx.progress(0.0, "Finding the official Ollama release")
        sha = await expected_sha256(asset)
        await fetch_resumable(f"{BASE}/{asset}", archive, ctx, sha)
        await ctx.progress(0.97, "Unpacking")
        dest = ollama.portable_dir()
        shutil.rmtree(dest, ignore_errors=True)
        await asyncio.to_thread(extract, archive, dest)
        exe = ollama.portable_exe()
        if not exe.exists():
            raise InstallError("The download didn't contain the Ollama program")
        if sys.platform != "win32":
            exe.chmod(0o755)
        shutil.rmtree(work, ignore_errors=True)
        await ctx.progress(0.99, "Starting Ollama")
        await ollama.start()
        return {"installed": str(exe), "verified": bool(sha)}

    return job
