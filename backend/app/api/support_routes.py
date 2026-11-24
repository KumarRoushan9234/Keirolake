"""Support: about, diagnostics, support bundle, cache clearing, engine restart."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from app import support

router = APIRouter(prefix="/api")


@router.get("/updates")
async def updates(force: bool = False) -> dict:
    """The newest release on GitHub and whether it's newer than this copy."""
    from app import updates as _updates

    return await _updates.latest(force)


@router.post("/updates/install", status_code=202)
async def install_update(request: Request) -> dict:
    """Linux .deb/.rpm copies: download, check the signature and install the newest release (asks for the password)."""
    from app import updates as _updates
    from app.api.access_routes import _local_only
    from app.jobs.runner import runner

    _local_only(request)
    try:
        job = _updates.install_package_job()
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"job_id": runner.submit("update", "Install the KeiroLake update", job, lane="background")}


@router.get("/about")
async def about() -> dict:
    return support.about()


@router.get("/support/diagnostics")
async def diagnostics(probe: bool = False) -> list[dict]:
    return await support.diagnostics(probe)


@router.get("/support/bundle")
async def bundle() -> Response:
    data = await support.bundle()
    name = f"keirolake-support-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.zip"
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/support/clear-caches")
async def clear_caches() -> dict:
    return support.clear_caches()


@router.post("/support/restart")
async def restart() -> dict:
    if not support.schedule_restart():
        raise HTTPException(400, "Restart is only available in the desktop app")
    return {"restarting": True}


_disk_cache: dict = {"at": 0.0, "lake_bytes": 0}


def _lake_bytes() -> int:
    """Size of the data folder, cached for a minute (walking it on every poll would be slow)."""
    import time

    from app.config import get_settings

    if time.monotonic() - _disk_cache["at"] > 60:
        total = 0
        for path in get_settings().data_dir.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
        _disk_cache.update(at=time.monotonic(), lake_bytes=total)
    return _disk_cache["lake_bytes"]


@router.get("/statusbar")
async def statusbar() -> dict:
    """Everything the bottom status bar shows, in one cheap call."""
    import asyncio
    import os
    import shutil

    import psutil
    from sqlalchemy import func, select

    from app import network, usage
    from app.catalog.models import Job
    from app.config import get_settings, offline_mode
    from app.db import session_scope
    from app.keiro.client import get_keiro
    from app.local import ollama
    from app.vector.qdrant_store import get_store

    settings = get_settings()
    qdrant, local = await asyncio.gather(get_store().healthy(), ollama.status())
    with session_scope() as s:
        running = s.scalar(select(func.count()).select_from(Job).where(Job.status.in_(["queued", "running"])))
    proc = psutil.Process()
    keiro = get_keiro().status()
    return {
        "engine": {"ok": True, "ram_mb": round(proc.memory_info().rss / 2**20), "cpu": proc.cpu_percent(interval=None)},
        "vectors": {"ok": bool(qdrant.get("ok"))},
        "local_model": {"running": bool(local.get("running")), "model": os.getenv("LOCAL_LLM_MODEL", "")},
        "mode": settings.llm_mode, "offline": offline_mode(),
        "usage": usage.today(),
        "keiro": {"configured": keiro["configured"], "credits": keiro["credits_remaining"], "zdr": keiro["zdr"]},
        "network": network.status()["mode"],
        "disk": {"lake_bytes": await asyncio.to_thread(_lake_bytes), "free_bytes": shutil.disk_usage(settings.data_dir).free},
        "jobs_running": running,
    }


@router.get("/map")
async def lake_map() -> dict:
    """The lake as a graph (sources, builds, sheets, collections, embeddings, models, access) with problems and fixes."""
    from app import lakemap

    return await lakemap.build()


@router.get("/onboarding")
async def onboarding() -> dict:
    """Has the welcome tour been finished? Kept on this computer, so every window and browser agrees."""
    import os

    return {"done": os.getenv("ONBOARDED") == "1"}


@router.post("/onboarding/{state}")
async def onboarding_set(state: str) -> dict:
    from fastapi import HTTPException

    from app import prefs

    if state not in ("done", "reset"):
        raise HTTPException(404, "Unknown state")
    prefs.update({"ONBOARDED": "1" if state == "done" else ""})
    return {"done": state == "done"}


@router.get("/samples")
async def samples_status() -> dict:
    from app import samples

    return samples.status()


@router.post("/samples")
async def samples_add() -> dict:
    from app import samples
    from app.jobs.runner import runner

    return await samples.add(lambda kind, title, job: runner.submit(kind, title, job, lane="background"))


@router.delete("/samples")
async def samples_remove() -> dict:
    from app import samples

    return await samples.remove()
