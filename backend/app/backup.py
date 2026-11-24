"""Backups: a consistent SQLite copy plus one Qdrant snapshot per knowledge base.

Each backup is a folder under data/backups/<UTC timestamp>/ containing
  keirolake.db            SQLite online backup (catalog, chunks, warehouse tables)
  <collection>.snapshot.gz   gzip-compressed Qdrant snapshot (vectors for every embedding model, with payloads)
  manifest.json           embedding models + dimensions, knowledge bases, file sizes

Raw/clean/curated lake files already live on disk in data/lake, so copy that
folder with the backup if you want a complete off-machine copy.
"""
from __future__ import annotations

import gzip
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy import select

from app.catalog.models import KnowledgeBase
from app.config import get_settings
from app.db import session_scope, writer_connection
from app.vector.qdrant_store import get_store


def backups_dir() -> Path:
    path = get_settings().data_dir / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


async def run_backup(progress=None) -> dict:
    settings = get_settings()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = backups_dir() / stamp
    dest.mkdir(parents=True)

    src = writer_connection()
    dst = sqlite3.connect(dest / "keirolake.db")
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    if progress:
        await progress(0.1, "SQLite database copied")

    with session_scope() as s:
        kbs = [{"name": kb.name, "collection": kb.collection_name, "vectors": kb.vector_config}
               for kb in s.scalars(select(KnowledgeBase))]

    store = get_store()
    if settings.qdrant_path:
        snapshots = await _export_points(store, kbs, dest, progress)
        return _finish(settings, stamp, dest, kbs, snapshots, ".points.jsonl")
    headers = {"api-key": settings.qdrant_api_key} if settings.qdrant_api_key else {}
    snapshots: dict[str, int] = {}
    async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10), headers=headers) as http:
        for i, kb in enumerate(kbs):
            collection = kb["collection"]
            snap = await store.client.create_snapshot(collection_name=collection)
            url = f"{settings.qdrant_url}/collections/{collection}/snapshots/{snap.name}"
            target = dest / f"{collection}.snapshot.gz"
            try:
                async with http.stream("GET", url) as resp:
                    resp.raise_for_status()
                    with gzip.open(target, "wb", compresslevel=6) as fh:
                        async for part in resp.aiter_bytes():
                            fh.write(part)
            finally:
                # The copy now lives in data/backups; free the space inside the Qdrant container.
                await store.client.delete_snapshot(collection_name=collection, snapshot_name=snap.name)
            snapshots[collection] = target.stat().st_size
            if progress:
                await progress(0.1 + 0.85 * (i + 1) / len(kbs), f"Snapshot of {kb['name']} saved")

    return _finish(settings, stamp, dest, kbs, snapshots, ".snapshot.gz")


async def _export_points(store, kbs: list[dict], dest: Path, progress) -> dict[str, int]:
    """Embedded Qdrant has no snapshots: write every point (vectors + payload) as JSONL instead."""
    sizes: dict[str, int] = {}
    for i, kb in enumerate(kbs):
        collection, target, offset = kb["collection"], dest / f"{kb['collection']}.points.jsonl", None
        with target.open("w", encoding="utf-8") as fh:
            while True:
                points, offset = await store.client.scroll(collection, limit=256, offset=offset,
                                                           with_vectors=True, with_payload=True)
                for p in points:
                    fh.write(json.dumps({"id": str(p.id), "vector": p.vector, "payload": p.payload}) + "\n")
                if offset is None:
                    break
        sizes[collection] = target.stat().st_size
        if progress:
            await progress(0.1 + 0.85 * (i + 1) / max(len(kbs), 1), f"Vectors of {kb['name']} saved")
    return sizes


def _finish(settings, stamp: str, dest: Path, kbs: list[dict], snapshots: dict[str, int], suffix: str) -> dict:
    manifest = {
        "created_at": stamp,
        "embedding_chain": [{"model": s.label, "vector_name": s.vector_name, "dim": s.dim} for s in settings.embed_models],
        "knowledge_bases": kbs,
        "files": {"keirolake.db": (dest / "keirolake.db").stat().st_size, **{f"{c}{suffix}": n for c, n in snapshots.items()}},
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"path": str(dest), "name": stamp, "knowledge_bases": len(kbs), "bytes": sum(manifest["files"].values())}


def list_backups() -> list[dict]:
    out = []
    for folder in sorted(backups_dir().iterdir(), reverse=True):
        manifest = folder / "manifest.json"
        if manifest.is_file():
            data = json.loads(manifest.read_text(encoding="utf-8"))
            out.append({"name": folder.name, "created_at": data["created_at"],
                        "knowledge_bases": len(data["knowledge_bases"]), "bytes": sum(data["files"].values())})
    return out


def backup_due(last_utc: datetime | None, every_hours: float, now: datetime | None = None) -> bool:
    """Is an automatic backup due? `every_hours` of 0 means automatic backups are off."""
    if every_hours <= 0:
        return False
    now = now or datetime.now(timezone.utc)
    return last_utc is None or now - last_utc >= timedelta(hours=every_hours)


def last_backup_time() -> datetime | None:
    names = [b["created_at"] for b in list_backups()]
    if not names:
        return None
    return datetime.strptime(names[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def prune_backups(keep: int) -> list[str]:
    """Delete the oldest backups beyond `keep` (only folders that hold a manifest). Returns removed names."""
    import shutil

    folders = sorted((f for f in backups_dir().iterdir() if (f / "manifest.json").is_file()), reverse=True)
    removed = []
    for folder in folders[max(1, keep):]:
        shutil.rmtree(folder, ignore_errors=True)
        removed.append(folder.name)
    return removed


async def backup_loop() -> None:
    """Hourly check; takes a backup when the schedule says so, then prunes old ones."""
    import asyncio
    import logging
    import os

    from app.jobs.runner import runner

    log = logging.getLogger(__name__)
    while True:
        await asyncio.sleep(3600 if os.getenv("KEIROLAKE_BACKUP_TICK") is None else float(os.environ["KEIROLAKE_BACKUP_TICK"]))
        try:
            hours = float(os.getenv("AUTO_BACKUP_HOURS") or 0)
            if backup_due(last_backup_time(), hours):
                async def job(ctx) -> dict:
                    result = await run_backup(ctx.progress)
                    result["pruned"] = prune_backups(int(os.getenv("KEEP_BACKUPS") or 7))
                    target = os.getenv("BACKUP_TARGET")
                    if target:
                        from app import backup_copy

                        await ctx.progress(0.9, f"Copying to {target}")
                        try:
                            result["copy"] = await asyncio.to_thread(
                                backup_copy.copy_backup, result["name"], target, os.getenv("BACKUP_PASSPHRASE", ""),
                                os.getenv("BACKUP_INCLUDE_LAKE", "1") != "0")
                        except Exception as exc:  # noqa: BLE001 - the local backup still exists
                            result["copy_error"] = str(exc)
                    return result

                runner.submit("backup", "Automatic backup", job, lane="background")
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("Automatic backup check failed")
