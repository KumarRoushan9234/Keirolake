"""Open datasets: list the catalog and add one to the lake as a sheet (only when asked)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.jobs.runner import runner
from app.opendata import service

router = APIRouter(prefix="/api/opendata")


@router.get("")
async def listing() -> list[dict]:
    return service.listing()


@router.post("/{dataset_id}/import", status_code=202)
async def import_dataset(dataset_id: str) -> dict:
    try:
        entry = service.entry(dataset_id)
    except service.OpenDataError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"job_id": runner.submit("dataset", f"Add open data: {entry['title']}", service.import_job(dataset_id), lane="background")}
