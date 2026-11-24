"""Quantized models: catalog of precisions, availability checks, downloads, re-quantizing, runtime settings."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import prefs
from app.jobs.runner import runner
from app.local import hardware, heretic, ollama, quant

router = APIRouter(prefix="/api/models")


def _guard(exc: Exception) -> HTTPException:
    return HTTPException(400, str(exc))


@router.get("/quant")
async def overview() -> dict:
    hw = await asyncio.to_thread(hardware.probe)
    return {"quants": quant.QUANTS, "hardware": {k: hw.get(k) for k in ("vram_gb", "ram_gb", "disk_free_gb", "gpu")},
            "installed": await quant.installed(), "remembered": quant.remembered(),
            "runtime": {"values": quant.runtime_values(), "options": {k: {"label": v[0], "choices": v[1]} for k, v in quant.RUNTIME_KEYS.items()}}}


class CheckBody(BaseModel):
    kind: str = Field(pattern="^(ollama|hf)$")
    ref: str = Field(min_length=3, max_length=200)
    quant: str = Field(max_length=12)
    ctx: int = Field(4096, ge=512, le=262144)
    kv: str = Field("f16", pattern="^(f16|q8_0|q4_0)$")


@router.post("/quant/check")
async def check(body: CheckBody) -> dict:
    hw = await asyncio.to_thread(hardware.probe)
    try:
        return await quant.check(body.kind, body.ref, body.quant, hw, body.ctx, body.kv)
    except quant.QuantError as exc:
        raise _guard(exc) from exc


@router.get("/quant/hf")
async def hf(repo: str) -> dict:
    try:
        return await quant.hf_files(repo)
    except quant.QuantError as exc:
        raise _guard(exc) from exc


class PullBody(BaseModel):
    kind: str = Field(pattern="^(ollama|hf)$")
    ref: str = Field(min_length=3, max_length=200)
    quant: str = Field(max_length=12)


@router.post("/quant/pull", status_code=202)
async def pull(body: PullBody) -> dict:
    try:
        name = quant.model_name(body.kind, body.ref, body.quant)
        job = quant.pull_job(body.kind, body.ref, body.quant)
    except quant.QuantError as exc:
        raise _guard(exc) from exc
    return {"name": name, "job_id": runner.submit("model", f"Download {name}", job, lane="background", resume={"type": "model", "model": name})}


class QuantizeBody(BaseModel):
    source: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:/-]+$")
    quant: str = Field(max_length=12)
    name: str | None = Field(None, max_length=120)


@router.post("/quant/quantize", status_code=202)
async def quantize(body: QuantizeBody) -> dict:
    try:
        job = quant.quantize_job(body.source, body.quant, body.name)
    except quant.QuantError as exc:
        raise _guard(exc) from exc
    return {"job_id": runner.submit("model", f"Quantize {body.source} to {body.quant}", job, lane="background")}


class RuntimeBody(BaseModel):
    values: dict[str, str]
    restart: bool = False
    force: bool = False


@router.put("/runtime")
async def set_runtime(body: RuntimeBody) -> dict:
    """Save the runtime settings. Ollama reads them when it starts, so `restart` applies them now."""
    try:
        values = quant.validate_runtime(body.values)
    except quant.QuantError as exc:
        raise _guard(exc) from exc
    prefs.update(values)
    restarted = False
    if body.restart:
        try:
            await ollama.restart(force=body.force)
            restarted = True
        except RuntimeError as exc:
            raise _guard(exc) from exc
    return {"values": quant.runtime_values(), "restarted": restarted}


# ---------- Heretic (run from its own folder and environment) ----------
@router.get("/heretic")
async def heretic_status() -> dict:
    return {**await asyncio.to_thread(heretic.status), "runs": await asyncio.to_thread(heretic.runs)}


class HereticDir(BaseModel):
    dir: str = Field(max_length=500)


@router.put("/heretic/dir")
async def heretic_dir(body: HereticDir) -> dict:
    prefs.update({"HERETIC_DIR": body.dir.strip()})
    st = await asyncio.to_thread(heretic.status)
    if body.dir.strip() and not st["found"]:
        raise HTTPException(400, "That folder doesn't contain Heretic (pyproject.toml and src/heretic)")
    return st


@router.post("/heretic/setup", status_code=202)
async def heretic_setup() -> dict:
    try:
        job = heretic.setup_job()
    except heretic.HereticError as exc:
        raise _guard(exc) from exc
    return {"job_id": runner.submit("model", "Set up Heretic", job, lane="background")}


class HereticRun(BaseModel):
    model: str = Field(min_length=3, max_length=300)
    preset: str = Field("default", max_length=30)
    trials: int = Field(50, ge=1, le=500)
    bnb_4bit: bool = False
    strategy: str = Field("merge", pattern="^(merge|adapter)$")


@router.post("/heretic/run", status_code=202)
async def heretic_run(body: HereticRun) -> dict:
    try:
        job, run = heretic.run_job(body.model, body.preset, body.trials, body.bnb_4bit, body.strategy)
    except heretic.HereticError as exc:
        raise _guard(exc) from exc
    return {"run": run, "job_id": runner.submit("model", f"Heretic {body.model}", job, lane="background")}


@router.get("/heretic/log/{run}")
async def heretic_log(run: str) -> dict:
    try:
        return {"lines": await asyncio.to_thread(heretic.log_tail, run)}
    except heretic.HereticError as exc:
        raise _guard(exc) from exc


class HereticImport(BaseModel):
    run: str = Field(max_length=120)
    name: str = Field(min_length=2, max_length=120)
    quant: str | None = Field("Q4_K_M", max_length=12)


@router.post("/heretic/import", status_code=202)
async def heretic_import(body: HereticImport) -> dict:
    try:
        job = heretic.import_job(body.run, body.name, body.quant)
    except heretic.HereticError as exc:
        raise _guard(exc) from exc
    return {"job_id": runner.submit("model", f"Import {body.name}", job, lane="background")}
