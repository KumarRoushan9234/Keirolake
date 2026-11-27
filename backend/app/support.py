"""Support: diagnostics with fixes, a shareable support bundle, cache clearing and an engine restart."""
from __future__ import annotations

import asyncio
import io
import json
import os
import platform
import re
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psutil
from sqlalchemy import select

APP_VERSION = os.getenv("KEIROLAKE_APP_VERSION", "0.9.0")
KEIROLABS_SUPPORT = "https://platform.keirolabs.cloud"
OLLAMA_HELP = "https://ollama.com/download"
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), re.compile(r"keiro(?:labs)?_[A-Za-z0-9]{12,}"),
    re.compile(r"gsk_[A-Za-z0-9]{16,}"), re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}"), re.compile(r"(?i)(api[_-]?key[\"'=:\s]+)[A-Za-z0-9._\-]{12,}"),
    re.compile(r"(?i)(x-keirolake-token[\"'=:\s]+)[A-Za-z0-9]{12,}"), re.compile(r"/api/zim/[0-9a-f]{32}/"),
    re.compile(r"(klk_)[A-Za-z0-9_\-]{8,}"), re.compile(r"(/mcp/k/)[^\s/\"']+"),
]


def redact(text: str) -> str:
    """Remove anything that looks like a key or token before it leaves this computer."""
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "[redacted]", text)
    return text


def about() -> dict:
    from app.config import get_settings

    s = get_settings()
    return {
        "version": APP_VERSION, "python": platform.python_version(), "frozen": bool(getattr(sys, "frozen", False)),
        "os": f"{platform.system()} {platform.release()} ({platform.machine()})", "data_dir": str(s.data_dir),
        "vector_store": "embedded" if s.qdrant_path else s.qdrant_url, "mode": s.llm_mode,
        "supervised": os.getenv("KEIROLAKE_SUPERVISED") == "1",
        "support": {"keirolabs": KEIROLABS_SUPPORT, "ollama": OLLAMA_HELP},
    }


def _check(id_: str, label: str, status: str, detail: str, fix: str = "") -> dict:
    return {"id": id_, "label": label, "status": status, "detail": detail, "fix": fix}


async def diagnostics(probe: bool = False) -> list[dict]:
    from app.backup import last_backup_time
    from app.catalog.models import Job
    from app.config import get_settings, provider_config
    from app.db import get_engine, session_scope
    from app.keiro.client import get_keiro
    from app.library import service as library
    from app.local import ollama
    from app.net import connectivity
    from app.vector.qdrant_store import get_store

    s = get_settings()
    out: list[dict] = []

    try:
        with get_engine().connect() as conn:
            result = conn.exec_driver_sql("PRAGMA quick_check").scalar()
        out.append(_check("database", "Database", "ok" if result == "ok" else "fail", f"Integrity check: {result}",
                          "" if result == "ok" else "Restore the latest backup from Status → Backups"))
    except Exception as exc:  # noqa: BLE001
        out.append(_check("database", "Database", "fail", str(exc)[:200], "Restart the app; if it persists, restore a backup"))

    health = await get_store().healthy()
    out.append(_check("vectors", "Vector store", "ok" if health.get("ok") else "fail",
                      f"{health.get('collections', 0)} collections ({'embedded' if s.qdrant_path else 'server'})" if health.get("ok") else str(health.get("error"))[:200],
                      "" if health.get("ok") else "Restart the engine from this page"))

    disk = psutil.disk_usage(str(s.data_dir))
    free_gb = disk.free / 2**30
    out.append(_check("disk", "Free disk space", "ok" if free_gb > 20 else "warn" if free_gb > 5 else "fail", f"{free_gb:.0f} GB free on the data drive",
                      "" if free_gb > 20 else "Remove unused libraries or models, or move the library folder to another drive"))

    online = connectivity.is_online
    out.append(_check("internet", "Internet", "ok" if online else "warn", "Online" if online else "Offline: local models and libraries only",
                      "" if online else "Nothing to fix for offline use; web search and cloud models wait for the connection"))

    st = await ollama.status()
    local_model = os.getenv("LOCAL_LLM_MODEL", "")
    installed = {m["name"].removesuffix(":latest") for m in st["models"] if m["local"]}
    if not st["installed"]:
        out.append(_check("runtime", "Local AI runtime", "warn", "Not installed", "Use Set up (Ctrl K → Open setup) to install it without admin rights"))
    elif not st["running"]:
        out.append(_check("runtime", "Local AI runtime", "warn", "Installed but stopped", "Models page → Start Ollama"))
    else:
        out.append(_check("runtime", "Local AI runtime", "ok", f"Ollama {st['version']} running"))
    if local_model and local_model.removesuffix(":latest") not in installed and st["running"]:
        out.append(_check("local_model", "Local model", "fail", f"{local_model} is chosen but not installed", "Models page → Install it, or pick another"))
    elif not local_model:
        out.append(_check("local_model", "Local model", "warn", "None chosen: offline answers aren't possible", "Models page → Install and Use a model"))
    else:
        out.append(_check("local_model", "Local model", "ok", local_model))

    cloud = [p for p in s.llm_fallback_order if provider_config(p).configured]
    out.append(_check("cloud", "Cloud models", "ok" if cloud else "warn", ", ".join(cloud) if cloud else "No cloud provider key",
                      "" if cloud else "Models page → add an OpenRouter or Groq key (optional)"))

    keiro = get_keiro()
    if not keiro.configured:
        out.append(_check("keirolabs", "KeiroLabs", "warn", "No key: web search, research and pipelines are off", "Models page → KeiroLabs key"))
    elif probe and online:
        result = await keiro.probe()
        out.append(_check("keirolabs", "KeiroLabs", "ok" if result.get("ok") else "fail",
                          f"{result.get('credits_remaining') or '?'} credits" if result.get("ok") else str(result.get("error"))[:200],
                          "" if result.get("ok") else f"Check the key and credits at {KEIROLABS_SUPPORT}"))
    else:
        out.append(_check("keirolabs", "KeiroLabs", "ok", "Key set" + (f" · {keiro.state.credits_remaining:,} credits" if keiro.state.credits_remaining else "")))

    libs = library.list_libraries()
    ready = [lib for lib in libs if lib["status"] == "ready"]
    failed = [lib for lib in libs if lib["status"] == "failed"]
    out.append(_check("libraries", "Offline libraries", "fail" if failed else "ok",
                      f"{len(ready)} ready" + (f", {len(failed)} failed" if failed else ""),
                      "Lake → Libraries → retry the failed downloads" if failed else ""))

    last = last_backup_time()
    age = (datetime.now(timezone.utc) - last) if last else None
    auto = float(os.getenv("AUTO_BACKUP_HOURS") or 0)
    out.append(_check("backups", "Backups", "ok" if age and age < timedelta(days=8) else "warn",
                      (f"Last backup {age.days} days ago" if age else "No backup yet") + (" · automatic" if auto else " · automatic backups off"),
                      "" if age and age < timedelta(days=8) else "Status → Back up now, and turn on automatic backups"))

    since = datetime.now(timezone.utc) - timedelta(days=1)
    with session_scope() as db:
        bad = list(db.scalars(select(Job).where(Job.status == "failed", Job.created_at >= since)))
    out.append(_check("jobs", "Background jobs (24 h)", "ok" if not bad else "warn",
                      f"{len(bad)} failed" + (f": {bad[-1].title}: {bad[-1].message[:120]}" if bad else ""),
                      "" if not bad else "Status → Recent jobs shows each error"))
    return out


def _tail(path: Path, lines: int = 2000) -> str:
    try:
        data = path.read_bytes()[-400_000:].decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(data.splitlines()[-lines:])


async def bundle() -> bytes:
    """A zip to send with a support request. Keys and tokens are removed; your documents are not included."""
    from app import prefs
    from app.catalog.models import Job
    from app.config import get_settings
    from app.db import session_scope

    s = get_settings()
    diag = await diagnostics(probe=False)
    with session_scope() as db:
        jobs = [{"title": j.title, "kind": j.kind, "status": j.status, "message": j.message[:500],
                 "created_at": j.created_at.isoformat() if j.created_at else None}
                for j in db.scalars(select(Job).order_by(Job.created_at.desc()).limit(60))]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", "KeiroLake support bundle\n\nContains diagnostics, versions, settings (keys are shown only as set/unset), "
                                  "recent job messages and log tails with keys removed. It contains none of your documents, notes or vault.\n")
        zf.writestr("about.json", json.dumps(about(), indent=2))
        zf.writestr("diagnostics.json", json.dumps(diag, indent=2))
        zf.writestr("settings.json", json.dumps(prefs.describe(), indent=2))
        zf.writestr("jobs.json", redact(json.dumps(jobs, indent=2)))
        hw = await asyncio.to_thread(__import__("app.local.hardware", fromlist=["probe"]).probe)
        zf.writestr("hardware.json", json.dumps(hw, indent=2))
        for log in sorted((s.data_dir / "logs").glob("*.log")):
            zf.writestr(f"logs/{log.name}", redact(_tail(log)))
    return buf.getvalue()


def clear_caches() -> dict:
    from app.keiro.client import get_keiro
    from app.library import zim
    from app.local import ollama

    web = get_keiro().clear_cache()
    vectors = len(ollama._VECTORS.data)
    ollama._VECTORS.data.clear()
    zim.read_article.cache_clear()
    zim.open_archive.cache_clear()
    return {"web_results": web, "vectors": vectors}


def schedule_restart(delay: float = 0.5) -> bool:
    """Exit so the desktop shell starts a fresh engine. Only when a supervisor is there to restart it."""
    if os.getenv("KEIROLAKE_SUPERVISED") != "1":
        return False

    def _exit() -> None:
        time.sleep(delay)
        os._exit(75)  # EX_TEMPFAIL: the shell restarts the engine

    import threading

    threading.Thread(target=_exit, daemon=True).start()
    return True
