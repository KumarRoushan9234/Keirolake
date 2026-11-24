"""Resumable, checksum-verified downloads for library files and models."""
from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)
CHUNK = 1 << 20


class DownloadError(RuntimeError):
    pass


async def fetch_resumable(url: str, target: Path, ctx=None, expected_sha256: str | None = None) -> Path:
    """Download `url` to `target`, resuming from `<target>.part` if it exists."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    started, last_report = time.monotonic(), 0.0
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, read=120), follow_redirects=True) as client:
        async with client.stream("GET", url, headers=headers) as resp:
            if resp.status_code == 416:  # already complete
                pass
            elif resp.status_code not in (200, 206):
                raise DownloadError(f"HTTP {resp.status_code} for {url}")
            else:
                if resp.status_code == 200 and have:
                    have = 0  # server ignored Range: start over
                total = have + int(resp.headers.get("content-length") or 0)
                with open(part, "ab" if have else "wb") as out:
                    got = have
                    async for block in resp.aiter_bytes(CHUNK):
                        out.write(block)
                        got += len(block)
                        now = time.monotonic()
                        if ctx and now - last_report > 1.0:
                            last_report = now
                            speed = (got - have) / max(now - started, 1e-6)
                            eta = (total - got) / speed if speed and total else 0
                            await ctx.progress(got / total if total else 0,
                                               f"{got / 2**30:.2f} / {total / 2**30:.2f} GB · {speed / 2**20:.1f} MB/s"
                                               + (f" · {eta / 60:.0f} min left" if eta else ""))
    if expected_sha256:
        if ctx:
            await ctx.progress(1.0, "Verifying checksum")
        actual = sha256_file(part)
        if actual.lower() != expected_sha256.lower():
            part.unlink(missing_ok=True)
            raise DownloadError("Checksum mismatch; the file was deleted, try again")
    os.replace(part, target)
    return target


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(CHUNK * 8):
            digest.update(block)
    return digest.hexdigest()


async def published_sha256(url: str) -> str | None:
    """Kiwix publishes `<file>.sha256` next to each ZIM."""
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url + ".sha256")
        if resp.status_code == 200 and resp.text.strip():
            return resp.text.split()[0]
    except httpx.HTTPError:
        pass
    return None
