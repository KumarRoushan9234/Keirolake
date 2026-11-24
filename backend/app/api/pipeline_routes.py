"""Pipelines: plan from a goal, save, run (or dry-run), schedule, preview rows."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import pipelines
from app.jobs.runner import runner
from app.keiro.client import KeiroError
from app.lake import warehouse

router = APIRouter(prefix="/api")


def _bad(exc: pipelines.SpecError) -> HTTPException:
    return HTTPException(400, {"message": str(exc), "problems": exc.problems})


@router.get("/pipelines")
async def list_pipelines() -> list[dict]:
    return pipelines.list_pipelines()


class Answer(BaseModel):
    question: str = Field(max_length=300)
    answer: str = Field(max_length=300)


class PlanBody(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)
    answers: list[Answer] = Field(default_factory=list, max_length=5)


@router.post("/pipelines/clarify")
async def clarify(body: PlanBody) -> dict:
    try:
        return await pipelines.clarify(body.goal)
    except pipelines.SpecError as exc:
        raise _bad(exc) from exc
    except Exception as exc:  # noqa: BLE001 - model errors are shown to the user
        raise HTTPException(502, f"Couldn't think of questions: {exc}") from exc


@router.post("/pipelines/plan")
async def plan(body: PlanBody) -> dict:
    try:
        return await pipelines.plan(body.goal, [a.model_dump() for a in body.answers])
    except pipelines.SpecError as exc:
        raise _bad(exc) from exc
    except Exception as exc:  # noqa: BLE001 - model errors are shown to the user
        raise HTTPException(502, f"Planning failed: {exc}") from exc


class ValidateBody(BaseModel):
    spec: dict


@router.post("/pipelines/validate")
async def validate(body: ValidateBody) -> dict:
    try:
        spec = pipelines.validate_spec(body.spec)
    except pipelines.SpecError as exc:
        raise _bad(exc) from exc
    return {"spec": spec, "estimate": pipelines.estimate(spec)}


class SaveBody(BaseModel):
    spec: dict
    kb_id: str | None = None


@router.post("/pipelines", status_code=201)
async def create(body: SaveBody) -> dict:
    try:
        return pipelines.save_pipeline(body.spec, body.kb_id)
    except pipelines.SpecError as exc:
        raise _bad(exc) from exc


@router.put("/pipelines/{pipeline_id}")
async def update(pipeline_id: str, body: SaveBody) -> dict:
    if pipelines.get_pipeline(pipeline_id) is None:
        raise HTTPException(404, "Pipeline not found")
    try:
        return pipelines.save_pipeline(body.spec, body.kb_id, pipeline_id)
    except pipelines.SpecError as exc:
        raise _bad(exc) from exc


class TogglePatch(BaseModel):
    enabled: bool


@router.patch("/pipelines/{pipeline_id}")
async def toggle(pipeline_id: str, body: TogglePatch) -> dict:
    from app.catalog.models import Pipeline
    from app.db import session_scope

    with session_scope() as s:
        p = s.get(Pipeline, pipeline_id)
        if p is None:
            raise HTTPException(404, "Pipeline not found")
        p.enabled = int(body.enabled)
    return pipelines.get_pipeline(pipeline_id)


@router.delete("/pipelines/{pipeline_id}", status_code=204)
async def delete(pipeline_id: str, drop_table: bool = False) -> None:
    pipelines.delete_pipeline(pipeline_id, drop_table)


@router.post("/pipelines/{pipeline_id}/run", status_code=202)
async def run(pipeline_id: str, dry: bool = False) -> dict:
    p = pipelines.get_pipeline(pipeline_id)
    if p is None:
        raise HTTPException(404, "Pipeline not found")
    title = f"{'Dry run' if dry else 'Run'}: {p['name']}"
    return {"job_id": runner.submit("pipeline", title, pipelines.run_job(pipeline_id, dry), lane="background", resume={"type": "pipeline", "id": pipeline_id, "dry": dry})}


@router.get("/pipelines/{pipeline_id}/rows")
async def rows(pipeline_id: str, limit: int = 50) -> dict:
    p = pipelines.get_pipeline(pipeline_id)
    if p is None:
        raise HTTPException(404, "Pipeline not found")
    if not p["table"]:
        return {"columns": [], "rows": [], "row_count": 0}
    try:
        return warehouse.run_query(f'SELECT * FROM "{p["table"]}" ORDER BY collected_at DESC', min(max(limit, 1), 500))
    except Exception:  # noqa: BLE001 - table not created until the first run
        return {"columns": [], "rows": [], "row_count": 0}
