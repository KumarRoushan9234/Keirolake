"""Watched folders: keep a knowledge base in sync with a folder on disk.

Each scan compares file modification times and sizes with the last scan, then
re-indexes only what was added or changed and removes what was deleted. Files are
copied into the lake's raw zone before indexing, like any other ingestion.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from app.catalog.models import Document, Watch
from app.db import session_scope
from app.ingest.parsers import SUPPORTED_EXTS, UnsupportedFile, parse_bytes
from app.ingest.pipeline import IncomingDoc, delete_document, start_run
from app.ingest.web import _run
from app.jobs.runner import runner
from app.lake.zones import raw_run_dir, write_raw_bytes

log = logging.getLogger(__name__)
MAX_FILES = 5000
MAX_FILE_BYTES = 50 * 2**20
SKIP_DIRS = {".git", ".obsidian", ".trash", "node_modules", "__pycache__", ".venv", ".idea", ".vscode"}


class WatchError(ValueError):
    pass


def check_folder(path: str) -> Path:
    folder = Path(path).expanduser()
    if not folder.is_absolute():
        raise WatchError("Give the full path of the folder")
    if not folder.is_dir():
        raise WatchError("That folder doesn't exist")
    return folder.resolve()


def scan_folder(root: Path) -> dict[str, list[int]]:
    """relative path -> [mtime_ns, size] for every supported file. Does not follow links out of the folder."""
    found: dict[str, list[int]] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if Path(name).suffix.lower() not in SUPPORTED_EXTS or name.startswith("."):
                continue
            file = Path(dirpath) / name
            try:
                if file.is_symlink():
                    continue
                stat = file.stat()
            except OSError:
                continue
            if stat.st_size > MAX_FILE_BYTES:
                continue
            found[file.relative_to(root).as_posix()] = [stat.st_mtime_ns, stat.st_size]
            if len(found) >= MAX_FILES:
                return found
    return found


def diff_state(old: dict, new: dict) -> tuple[list[str], list[str], list[str]]:
    """(added, changed, removed) relative paths."""
    added = sorted(p for p in new if p not in old)
    changed = sorted(p for p in new if p in old and list(old[p]) != list(new[p]))
    removed = sorted(p for p in old if p not in new)
    return added, changed, removed


def file_url(root: Path, rel: str) -> str:
    return (root / rel).as_uri()


def list_watches(kb_id: str | None = None) -> list[dict]:
    with session_scope() as s:
        query = select(Watch).order_by(Watch.created_at)
        if kb_id:
            query = query.where(Watch.kb_id == kb_id)
        return [{"id": w.id, "kb_id": w.kb_id, "path": w.path, "interval_minutes": w.interval_minutes,
                 "enabled": bool(w.enabled), "last_scan_at": w.last_scan_at.isoformat() if w.last_scan_at else None,
                 "last_result": w.last_result, "files": len(w.state)} for w in s.scalars(query)]


def add_watch(kb_id: str, path: str, interval_minutes: int = 15) -> str:
    folder = check_folder(path)
    with session_scope() as s:
        if s.scalar(select(Watch).where(Watch.kb_id == kb_id, Watch.path == str(folder))):
            raise WatchError("This folder is already watched for this knowledge base")
        watch = Watch(kb_id=kb_id, path=str(folder), interval_minutes=max(1, min(interval_minutes, 24 * 60)))
        s.add(watch)
        s.flush()
        return watch.id


def delete_watch(watch_id: str) -> None:
    with session_scope() as s:
        watch = s.get(Watch, watch_id)
        if watch:
            s.delete(watch)


def scan_job(watch_id: str):
    async def job(ctx) -> dict:
        with session_scope() as s:
            watch = s.get(Watch, watch_id)
            if watch is None:
                raise WatchError("This watch was removed")
            kb_id, root_text, old_state = watch.kb_id, watch.path, dict(watch.state)
        root = check_folder(root_text)
        await ctx.progress(0.02, "Looking for changes")
        current = await asyncio.to_thread(scan_folder, root)
        added, changed, removed = diff_state(old_state, current)
        stats = {"added": len(added), "changed": len(changed), "removed": len(removed), "failed": []}

        stale = [file_url(root, rel) for rel in changed + removed]
        if stale:
            with session_scope() as s:
                doc_ids = list(s.scalars(select(Document.id).where(Document.kb_id == kb_id, Document.url.in_(stale))))
            for doc_id in doc_ids:
                await delete_document(doc_id)

        todo = added + changed
        source_id = run_id = None
        if todo:
            source_id, run_id = start_run("watch", root.name, kb_id, {"path": str(root)})
            raw_dir = raw_run_dir("watch", run_id)
            docs, new_state = [], dict(current)
            for rel in todo:
                try:
                    data = await asyncio.to_thread((root / rel).read_bytes)
                    parsed = parse_bytes(Path(rel).name, data)
                    raw = write_raw_bytes(raw_dir, rel.replace("/", "__"), data)
                    docs.append(IncomingDoc(parsed.title, parsed.text, raw, parsed.mime, file_url(root, rel),
                                            {**parsed.meta, "folder": str(root), "relative_path": rel}))
                except (OSError, UnsupportedFile, ValueError) as exc:
                    stats["failed"].append(f"{rel}: {exc}")
                    new_state.pop(rel, None)  # try again next scan
            result = await _run(kb_id, source_id, run_id, raw_dir, docs, ctx) if docs else {}
            stats["indexed"] = result.get("documents", 0)
            current = new_state
        with session_scope() as s:
            watch = s.get(Watch, watch_id)
            if watch is not None:
                watch.state, watch.last_scan_at, watch.last_result = current, datetime.now(timezone.utc), stats
        return stats

    return job


async def watch_loop() -> None:
    """Every minute, queue a scan for watches that are due."""
    while True:
        await asyncio.sleep(60)
        try:
            now = datetime.now(timezone.utc)
            with session_scope() as s:
                due = []
                for watch in s.scalars(select(Watch).where(Watch.enabled == 1)):
                    last = watch.last_scan_at.replace(tzinfo=timezone.utc) if watch.last_scan_at else None
                    if last is None or now - last >= timedelta(minutes=watch.interval_minutes):
                        watch.last_scan_at = now  # claim it so a slow scan isn't queued twice
                        due.append((watch.id, Path(watch.path).name))
            for watch_id, name in due:
                runner.submit("watch", f"Scan folder: {name}", scan_job(watch_id))
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("Folder watch tick failed")
