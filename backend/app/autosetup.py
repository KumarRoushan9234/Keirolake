"""Optional downloads: local AI runtime and models, offline libraries. Nothing is picked by default.

The plan is computed first so the app can show exactly what will be downloaded
and how big it is. The job then runs each stage, skipping anything already done,
and large optional downloads (Wikipedia) continue in the background after the
essentials are ready.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from sqlalchemy import select

from app import prefs
from app.catalog.models import Job
from app.db import session_scope
from app.jobs.runner import runner
from app.library import catalog as lib_catalog
from app.library import service as library
from app.local import catalog as models
from app.local import hardware, install, ollama

log = logging.getLogger(__name__)
GB = 2**30
RUNTIME_GB = 1.6
WIKI_FLAVOURS = {"mini": "wikipedia_en_all_mini", "nopic": "wikipedia_en_all_nopic", "maxi": "wikipedia_en_all_maxi"}


@dataclass
class Plan:
    local_ai: bool = True
    runtime: bool = False
    llm: str | None = None
    embedder: str | None = None
    libraries: list[dict] = field(default_factory=list)  # catalog entries still to download
    wikipedia: dict | None = None
    total_gb: float = 0.0
    skipped: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"local_ai": self.local_ai, "runtime": self.runtime, "llm": self.llm, "embedder": self.embedder,
                "libraries": [{"name": e["name"], "title": e["title"], "gb": round(e["size_bytes"] / GB, 2)} for e in self.libraries],
                "wikipedia": ({"name": self.wikipedia["name"], "title": self.wikipedia["title"], "gb": round(self.wikipedia["size_bytes"] / GB, 1)}
                              if self.wikipedia else None),
                "total_gb": round(self.total_gb, 1), "skipped": self.skipped}


async def make_plan(*, wikipedia: str | None = None, essentials: bool = False, local_ai: bool = False) -> Plan:
    """What would be downloaded on this machine. Nothing is chosen unless asked for.

    local_ai: the Ollama runtime plus the recommended chat and search models.
    essentials: the practical offline libraries. wikipedia: mini, nopic, maxi or None.
    """
    hw = await asyncio.to_thread(hardware.probe)
    rec = models.recommend(hw)
    status = await ollama.status()
    installed = {m["name"].removesuffix(":latest") for m in status["models"] if m["local"]}
    plan = Plan(local_ai=local_ai)
    free_gb = hw["disk_free_gb"]

    if local_ai:
        plan.runtime = not status["installed"]
        plan.llm = None if rec["llm"]["id"] in installed else rec["llm"]["id"]
        plan.embedder = None if rec["embedder"]["id"] in installed else rec["embedder"]["id"]
    need = (RUNTIME_GB if plan.runtime else 0) + (rec["llm"]["size_gb"] if plan.llm else 0) + (rec["embedder"]["size_gb"] if plan.embedder else 0)

    entries, _ = await lib_catalog.load() if (essentials or wikipedia) else ([], {})  # the catalog is a download
    by_name = {e["name"]: e for e in entries}
    have = {lib["name"] for lib in library.list_libraries()}
    if essentials:
        for name in models.ESSENTIALS:
            entry = by_name.get(name)
            if entry and name not in have:
                if need + entry["size_bytes"] / GB + 5 <= free_gb:
                    plan.libraries.append(entry)
                    need += entry["size_bytes"] / GB
                else:
                    plan.skipped.append(f"{entry['title']}: not enough free space")
    if wikipedia:
        entry = by_name.get(WIKI_FLAVOURS.get(wikipedia, ""))
        if entry and entry["name"] not in have:
            if need + entry["size_bytes"] / GB + 10 <= free_gb:
                plan.wikipedia = entry
                need += entry["size_bytes"] / GB
            else:
                plan.skipped.append(f"{entry['title']}: needs {entry['size_bytes'] / GB:.0f} GB but only {max(free_gb - need, 0):.0f} GB is free")
    plan.total_gb = need
    return plan


def _status_of(job_id: str) -> tuple[str, str]:
    with session_scope() as s:
        j = s.get(Job, job_id)
        return (j.status, j.message) if j else ("failed", "job vanished")


async def run_sub(kind: str, title: str, job, ctx, label: str, lane: str = "background", resume: dict | None = None) -> str:
    """Queue a job and wait for it, mirroring its progress in the parent job's message."""
    job_id = runner.submit(kind, title, job, lane=lane, resume=resume)
    while True:
        status, message = _status_of(job_id)
        if status in ("done", "failed"):
            return status if status == "done" else f"failed: {message}"
        await ctx.progress(-1, f"{label}: {message}" if message else label)
        await asyncio.sleep(2)


def auto_job(plan: Plan):
    async def job(ctx) -> dict:
        stages: dict[str, str] = {}
        total = 4

        async def stage(n: int, name: str):
            await ctx.progress(n / total, name)

        # 1. runtime
        await stage(0, "Local AI runtime")
        if not plan.local_ai:
            stages["runtime"] = "skipped"
        elif plan.runtime:
            stages["runtime"] = await run_sub("setup", "Install Ollama", install.install_job(), ctx, "Installing the local AI runtime")
        else:
            try:
                await ollama.start()
                stages["runtime"] = "done"
            except Exception as exc:  # noqa: BLE001 - reported in the summary
                stages["runtime"] = f"failed: {exc}"
        runtime_ok = stages["runtime"] == "done"
        if not plan.local_ai:
            status = await ollama.status()
            runtime_ok = status["running"]

        # 2. models (in parallel)
        await stage(1, "Models")
        pulls = []
        if runtime_ok and plan.local_ai:
            for model, label in ((plan.llm, "language model"), (plan.embedder, "search model")):
                if model:
                    pulls.append(asyncio.create_task(run_sub("model", f"Download {model}", ollama.pull_job(model), ctx, f"Downloading the {label}",
                                                       resume={"type": "model", "model": model})))
            results = await asyncio.gather(*pulls) if pulls else []
            stages["models"] = ("skipped: already installed" if not pulls else
                                "done" if all(r == "done" for r in results) else "; ".join(r for r in results if r != "done"))
            if plan.llm and results and results[0] == "done":
                prefs.update({"LOCAL_LLM_MODEL": plan.llm})
        else:
            stages["models"] = "skipped" if not plan.local_ai else "skipped: no runtime"

        # 3. essential libraries
        await stage(2, "Offline libraries")
        waits = []
        for entry in plan.libraries:
            lib_id, dl = library.start_download(entry)
            waits.append(asyncio.create_task(run_sub("library", f"Download {entry['title']}", dl, ctx, f"Downloading {entry['title']}",
                                                       resume={"type": "library", "entry": entry})))
        results = await asyncio.gather(*waits) if waits else []
        stages["libraries"] = ("skipped: nothing to download" if not waits else
                               "done" if all(r == "done" for r in results) else "; ".join(r for r in results if r != "done"))

        # 4. Wikipedia keeps downloading on its own; the app is usable before it finishes
        if plan.wikipedia:
            _, dl = library.start_download(plan.wikipedia)
            runner.submit("library", f"Download {plan.wikipedia['title']}", dl, lane="background",
                          resume={"type": "library", "entry": plan.wikipedia})
            stages["wikipedia"] = "downloading in the background"

        prefs.update({"SETUP_DONE": "1"})
        failed = {k: v for k, v in stages.items() if v.startswith("failed") or "failed" in v}
        if failed and len(failed) == len([v for v in stages.values() if not v.startswith("skipped")]):
            raise RuntimeError("Setup couldn't complete: " + "; ".join(f"{k}: {v}" for k, v in failed.items()))
        return {"stages": stages, "plan": plan.to_dict()}

    return job
