"""Offline libraries: plain-language find, full-text search across libraries, and source setup."""
from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import assist, prefs
from app.jobs.runner import runner
from app.library import service as library
from app.library import zim
from app.local import catalog as model_catalog
from app.local import hardware, ollama

router = APIRouter(prefix="/api")


class FindBody(BaseModel):
    q: str = Field(min_length=2, max_length=500)


@router.post("/library/find")
async def library_find(body: FindBody) -> dict:
    return await assist.find_in_libraries(body.q)


# ---------- search across all libraries ----------
@router.get("/search/libraries")
async def search_libraries(q: str, limit: int = 20) -> dict:
    libs = library.ready_libraries()
    if not libs:
        return {"results": []}

    def run() -> list[dict]:
        per = max(3, min(limit, 40) // max(len(libs), 1) + 2)
        lists = []
        for lib in libs:
            out = []
            archive = zim.open_archive(lib.path)
            for path in zim.search(lib.path, q, per):
                try:
                    out.append({"library_id": lib.id, "library": lib.title or lib.name, "path": path,
                                "title": archive.get_entry_by_path(path).title or path})
                except KeyError:
                    continue
            lists.append(out)
        merged = []
        for rank in range(per):  # interleave so one big library doesn't bury the others
            merged += [lst[rank] for lst in lists if rank < len(lst)]
        return merged[:limit]

    return {"results": await asyncio.to_thread(run)}


# ---------- first-run setup ----------
@router.get("/setup")
async def setup_status() -> dict:
    hw = await asyncio.to_thread(hardware.probe)
    rec = model_catalog.recommend(hw)
    oll = await ollama.status()
    installed = {m["name"].removesuffix(":latest") for m in oll["models"] if m["local"]}
    libs = library.list_libraries()
    cloud = bool(os.getenv("OPENROUTER_API_KEY") or os.getenv("GROQCLOUD_API_KEY") or os.getenv("GEMINI_API_KEY"))
    steps = [
        {"id": "runtime", "title": "Local AI runtime", "done": oll["running"],
         "detail": f"Ollama {oll['version']} is running" if oll["running"] else
                   "Ollama is installed but stopped" if oll["installed"] else "Install Ollama to run models on this computer"},
        {"id": "llm", "title": "Local model", "done": any(m["id"] in installed for m in model_catalog.LLMS) or bool(os.getenv("LOCAL_LLM_MODEL")),
         "detail": f"Recommended for this PC: {rec['llm']['label']} ({rec['llm']['size_gb']} GB)", "model": rec["llm"]["id"]},
        {"id": "embedder", "title": "Offline search model", "done": rec["embedder"]["id"] in installed or any(m["id"] in installed for m in model_catalog.EMBEDDERS),
         "detail": f"{rec['embedder']['label']} ({rec['embedder']['size_gb']} GB)", "model": rec["embedder"]["id"]},
        {"id": "library", "title": "Offline library", "done": any(lib["status"] in ("ready", "downloading") for lib in libs),
         "detail": (f"{rec['wikipedia']['label']}, {rec['wikipedia']['size_gb']} GB, fits your drive" if rec["wikipedia"]
                    else "Not enough free space for Wikipedia; pick a smaller library"), "library": (rec["wikipedia"] or {}).get("name")},
        {"id": "cloud", "title": "Cloud models (optional)", "done": cloud, "optional": True,
         "detail": "Add an OpenRouter key for stronger answers when online" if not cloud else "A cloud key is set"},
    ]
    return {"done": os.getenv("SETUP_DONE") == "1", "steps": steps, "hardware": hw, "recommendation": rec}


@router.post("/setup/done")
async def setup_done() -> dict:
    prefs.update({"SETUP_DONE": "1"})
    return {"done": True}


class AutoBody(BaseModel):
    wikipedia: str | None = Field(None, pattern="^(mini|nopic|maxi)$")
    essentials: bool = False
    local_ai: bool = False


@router.get("/setup/plan")
async def setup_plan(wikipedia: str | None = None, essentials: bool = False, local_ai: bool = False) -> dict:
    from app import autosetup

    if wikipedia not in (None, "", "mini", "nopic", "maxi"):
        raise HTTPException(400, "wikipedia must be mini, nopic or maxi")
    return (await autosetup.make_plan(wikipedia=wikipedia or None, essentials=essentials, local_ai=local_ai)).to_dict()


@router.post("/setup/auto", status_code=202)
async def setup_auto(body: AutoBody) -> dict:
    from app import autosetup

    plan = await autosetup.make_plan(wikipedia=body.wikipedia, essentials=body.essentials, local_ai=body.local_ai)
    return {"job_id": runner.submit("setup", "Download what you picked", autosetup.auto_job(plan), lane="background"), "plan": plan.to_dict()}
