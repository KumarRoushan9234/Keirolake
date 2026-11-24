"""Web pages into the lake: reader view, in-app page capture, and export to a drive."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import shutil
import socket
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
import trafilatura

from app.backup import run_backup
from app.config import get_settings
from app.ingest.pipeline import IncomingDoc, start_run
from app.ingest.web import _run
from app.keiro.client import KeiroError, get_keiro
from app.lake.zones import raw_run_dir, write_raw_bytes
from app.net import connectivity

UA = "Mozilla/5.0 (KeiroLake reader)"
MAX_PAGE_BYTES = 8 * 2**20


class CaptureError(ValueError):
    pass


def check_public_url(url: str) -> str:
    """Only http(s) URLs that resolve to public addresses (no localhost, LAN or metadata IPs)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise CaptureError("Only http:// and https:// pages can be opened")
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        raise CaptureError(f"Can't resolve {parsed.hostname}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if isinstance(ip, ipaddress.IPv6Address):
            # NAT64 (64:ff9b::/96, used on IPv6-only networks) and IPv4-mapped addresses carry a real IPv4 address: judge that one.
            if ip in ipaddress.ip_network("64:ff9b::/96"):
                ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
            elif ip.ipv4_mapped:
                ip = ip.ipv4_mapped
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise CaptureError("Local and private network addresses can't be opened here")
    return url


def extract_markdown(html: str, url: str) -> tuple[str, str]:
    """(title, markdown) of the readable part of a page."""
    meta = trafilatura.extract_metadata(html)
    text = trafilatura.extract(html, url=url, output_format="markdown", include_links=True, include_tables=True,
                               favor_recall=True) or ""
    title = (meta.title if meta and meta.title else "") or url
    return title, text


async def reader_view(url: str) -> dict:
    """Readable version of a live page. JavaScript-heavy pages go through KeiroLabs, which renders them."""
    if not connectivity.is_online:
        raise CaptureError("You're offline. Library pages still open; web pages need the internet.")
    await asyncio.to_thread(check_public_url, url)
    title, text, via = url, "", "direct"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": UA}) as client:
            resp = await client.get(url)
        if resp.status_code < 400 and "html" in resp.headers.get("content-type", "") and len(resp.content) <= MAX_PAGE_BYTES:
            await asyncio.to_thread(check_public_url, str(resp.url))  # redirects must stay public too
            title, text = await asyncio.to_thread(extract_markdown, resp.text, str(resp.url))
    except httpx.HTTPError:
        pass
    if len(text) < 400 and get_keiro().configured:
        try:
            results = await get_keiro().extract([url])
            if results and results[0].content:
                title, text, via = results[0].title or title, results[0].content, "keirolabs"
        except KeiroError:
            pass
    if not text:
        raise CaptureError("Couldn't read this page")
    return {"url": url, "title": title, "markdown": text, "via": via}


def capture_job(kb_id: str, url: str, title: str, html: str | None = None, markdown: str | None = None):
    """Save a page (as shown in the app) to a knowledge base: raw HTML in raw/, readable text indexed."""
    if not html and not markdown:
        raise CaptureError("Nothing to save")
    if len((html or markdown or "").encode()) > MAX_PAGE_BYTES:
        raise CaptureError("Page is too large to save")
    source_id, run_id = start_run("capture", title or url, kb_id, {"url": url})
    raw_dir = raw_run_dir("capture", run_id)
    raw = write_raw_bytes(raw_dir, "page.html" if html else "page.md", (html or markdown).encode())

    async def job(ctx) -> dict:
        name, text = title, markdown or ""
        if html:
            name, text = await asyncio.to_thread(extract_markdown, html, url)
            name = title or name
        if len(text.strip()) < 50:
            raise CaptureError("The page has no readable text")
        doc = IncomingDoc(name or url, text, raw, "text/markdown", url, {"captured_at": datetime.now(timezone.utc).isoformat()})
        return await _run(kb_id, source_id, run_id, raw_dir, [doc], ctx)

    return job


def export_job(target: str, include_libraries: bool = True):
    """Copy everything needed to rebuild the lake on another machine to `target`."""
    dest_root = Path(target).expanduser()
    if not dest_root.is_absolute() or not dest_root.exists() or not dest_root.is_dir():
        raise CaptureError("Choose an existing folder (for example a USB drive)")

    async def job(ctx) -> dict:
        from app.library.service import list_libraries

        settings = get_settings()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = dest_root / f"keirolake-export-{stamp}"
        dest.mkdir()
        await ctx.progress(0.02, "Backing up database and vectors")
        backup = await run_backup()
        await asyncio.to_thread(shutil.copytree, backup["path"], dest / "backup")
        await ctx.progress(0.2, "Copying the lake")
        await asyncio.to_thread(shutil.copytree, settings.lake_dir, dest / "lake", dirs_exist_ok=True)
        copied = []
        if include_libraries:
            libs = [lib for lib in list_libraries() if lib["status"] == "ready" and lib["path"]]
            (dest / "library").mkdir()
            for i, lib in enumerate(libs):
                src = Path(lib["path"])
                if src.exists():
                    await ctx.progress(0.25 + 0.7 * i / max(len(libs), 1), f"Copying {lib['title'] or lib['name']}")
                    await asyncio.to_thread(shutil.copy2, src, dest / "library" / src.name)
                    copied.append(src.name)
        vault_file = settings.data_dir / "vault.json"
        if vault_file.exists():  # already encrypted; useless without the passphrase
            await asyncio.to_thread(shutil.copy2, vault_file, dest / "vault.json")
        (dest / "README.txt").write_text(
            "KeiroLake export\n\nbackup/   SQLite database + vectors (see manifest.json)\nlake/     raw, clean and curated files\n"
            "library/  offline library ZIM files (open with KeiroLake or Kiwix)\n\nRestore: copy these into a new data folder.\n",
            encoding="utf-8")
        (dest / "export.json").write_text(json.dumps({"created_at": stamp, "libraries": copied, "backup": backup["name"]}, indent=2),
                                          encoding="utf-8")
        return {"path": str(dest), "libraries": len(copied)}

    return job
