"""Sheets: create, import, edit, fill columns, build from a description, refresh, export, history, ask."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from app import pipelines
from app.jobs.runner import runner
from app.lake import warehouse
from app.sheets import clean, fill, service

router = APIRouter(prefix="/api/sheets")
MAX_UPLOAD = 200 * 2**20


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KeyError:
        raise HTTPException(404, "Sheet not found") from None
    except (service.SheetError, warehouse.SQLValidationError) as exc:
        raise HTTPException(400, str(exc)) from exc


class ColumnIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    type: str = "text"
    kind: str = "plain"
    prompt: str = Field("", max_length=500)


class NewSheet(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    columns: list[ColumnIn] = Field(default_factory=list, max_length=100)


@router.get("")
async def list_sheets() -> list[dict]:
    return service.list_sheets()


@router.post("", status_code=201)
async def create(body: NewSheet) -> dict:
    return _guard(service.create_blank, body.name, [c.model_dump() for c in body.columns])


@router.post("/import", status_code=201)
async def import_file(file: UploadFile = File(...), name: str = Form(""), description: str = Form("")) -> dict:
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Files up to 200 MB")
    return await asyncio.to_thread(_guard, service.import_file, name, file.filename or "data.csv", data, description)


# ---------- build from a description (KeiroLabs, ZDR) ----------
class Goal(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)
    answers: list[dict] = Field(default_factory=list, max_length=5)


@router.post("/build/clarify")
async def clarify(body: Goal) -> dict:
    try:
        return await pipelines.clarify(body.goal)
    except pipelines.SpecError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - model errors are shown
        raise HTTPException(502, f"Couldn't plan: {exc}") from exc


@router.post("/build/plan")
async def plan(body: Goal) -> dict:
    try:
        return await pipelines.plan(body.goal, body.answers)
    except pipelines.SpecError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - model errors are shown
        raise HTTPException(502, f"Couldn't plan: {exc}") from exc


class BuildBody(BaseModel):
    spec: dict
    kb_id: str | None = None
    run: bool = True


@router.post("/build", status_code=201)
async def build(body: BuildBody) -> dict:
    """Save the plan as a sheet that fills itself, and run it now."""
    from sqlalchemy import select

    from app.catalog.models import Sheet
    from app.db import session_scope

    try:
        pipe = pipelines.save_pipeline(body.spec, body.kb_id)
    except pipelines.SpecError as exc:
        raise HTTPException(400, str(exc)) from exc
    if pipe.get("table"):
        pipelines.write_rows(pipe["table"], pipe["spec"], [])  # create the empty table so the sheet shows at once
    service.sync()
    with session_scope() as s:
        sheet = s.scalar(select(Sheet).where(Sheet.table_name == pipe["table"])) if pipe.get("table") else None
        if sheet is not None:
            sheet.name, sheet.source, sheet.pipeline_id = pipe["name"].replace("_", " "), "build", pipe["id"]
            sheet.description = pipe["goal"]
        sheet_id = sheet.id if sheet else None
    job_id = None
    if body.run:
        job_id = runner.submit("pipeline", f"Build: {pipe['name']}", pipelines.run_job(pipe["id"]), lane="background",
                               resume={"type": "pipeline", "id": pipe["id"]})
    return {"sheet_id": sheet_id, "pipeline_id": pipe["id"], "job_id": job_id}


# ---------- one sheet ----------
@router.get("/{sheet_id}")
async def get(sheet_id: str) -> dict:
    return _guard(service.get, sheet_id)


class SheetPatch(BaseModel):
    name: str = Field("", max_length=80)
    description: str | None = Field(None, max_length=2000)


@router.patch("/{sheet_id}")
async def patch(sheet_id: str, body: SheetPatch) -> dict:
    return _guard(service.rename, sheet_id, body.name, body.description)


@router.delete("/{sheet_id}", status_code=204)
async def delete(sheet_id: str) -> None:
    from app import tables_api

    _guard(service.delete_sheet, sheet_id)
    await tables_api.drop_index(sheet_id)


@router.get("/{sheet_id}/rows")
async def rows(sheet_id: str, offset: int = 0, limit: int = 200, sort: str | None = None, desc: bool = False, q: str = "") -> dict:
    return _guard(service.rows, sheet_id, offset, limit, sort, desc, q)


class RowsIn(BaseModel):
    rows: list[dict] = Field(default_factory=list, max_length=5000)


class CleanIn(BaseModel):
    apply: bool = False
    drop_empty_columns: bool = False


@router.post("/{sheet_id}/clean")
async def clean_sheet(sheet_id: str, body: CleanIn) -> dict:
    """Preview (apply=false) or run the cleanup. A dated copy is kept in History first."""
    fn = clean.apply if body.apply else clean.plan
    return await asyncio.to_thread(_guard, fn, sheet_id, body.drop_empty_columns)


@router.get("/{sheet_id}/chart")
async def chart(sheet_id: str, x: str, y: str | None = None, agg: str = "count", limit: int = 30, bins: int = 0) -> dict:
    return _guard(service.chart, sheet_id, x, y, agg, limit, bins)


@router.post("/{sheet_id}/rows", status_code=201)
async def add_rows(sheet_id: str, body: RowsIn) -> dict:
    return {"ids": _guard(service.add_rows, sheet_id, body.rows)}


class CellIn(BaseModel):
    column: str
    value: object = None


@router.patch("/{sheet_id}/rows/{row_id}")
async def update_cell(sheet_id: str, row_id: int, body: CellIn) -> dict:
    _guard(service.update_cell, sheet_id, row_id, body.column, body.value)
    return {"ok": True}


class IdsIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=10000)


@router.post("/{sheet_id}/rows/delete")
async def delete_rows(sheet_id: str, body: IdsIn) -> dict:
    return {"deleted": _guard(service.delete_rows, sheet_id, body.ids)}


@router.post("/{sheet_id}/columns", status_code=201)
async def add_column(sheet_id: str, body: ColumnIn) -> dict:
    return _guard(service.add_column, sheet_id, body.name, body.type, body.kind, body.prompt)


class ColumnPatch(BaseModel):
    name: str | None = Field(None, max_length=60)
    type: str | None = None
    kind: str | None = None
    prompt: str | None = Field(None, max_length=500)


@router.patch("/{sheet_id}/columns/{name}")
async def update_column(sheet_id: str, name: str, body: ColumnPatch) -> dict:
    return _guard(service.update_column, sheet_id, name, body.name, body.kind, body.prompt, body.type)


@router.delete("/{sheet_id}/columns/{name}")
async def drop_column(sheet_id: str, name: str) -> dict:
    return _guard(service.drop_column, sheet_id, name)


@router.get("/{sheet_id}/columns/{name}/estimate")
async def estimate(sheet_id: str, name: str, only_empty: bool = True) -> dict:
    return _guard(fill.estimate, sheet_id, name, only_empty)


class FillIn(BaseModel):
    only_empty: bool = True


@router.post("/{sheet_id}/columns/{name}/fill", status_code=202)
async def fill_column(sheet_id: str, name: str, body: FillIn) -> dict:
    info = _guard(service.get, sheet_id)
    return {"job_id": runner.submit("sheet", f"Fill {name} in {info['name']}", fill.fill_job(sheet_id, name, body.only_empty),
                                    lane="background")}


@router.post("/{sheet_id}/refresh", status_code=202)
async def refresh(sheet_id: str) -> dict:
    info = _guard(service.get, sheet_id)
    if not info["build"]:
        raise HTTPException(400, "Only sheets built from a description can refresh themselves")
    pid = info["build"]["id"]
    return {"job_id": runner.submit("pipeline", f"Refresh: {info['name']}", pipelines.run_job(pid), lane="background",
                                    resume={"type": "pipeline", "id": pid})}


class ScheduleIn(BaseModel):
    interval_minutes: int = Field(ge=0, le=10080)


@router.put("/{sheet_id}/schedule")
async def schedule(sheet_id: str, body: ScheduleIn) -> dict:
    info = _guard(service.get, sheet_id)
    if not info["build"]:
        raise HTTPException(400, "Only sheets built from a description can refresh on a schedule")
    pipe = pipelines.get_pipeline(info["build"]["id"])
    try:
        pipelines.save_pipeline({**pipe["spec"], "interval_minutes": body.interval_minutes}, pipe["kb_id"], pipe["id"])
    except pipelines.SpecError as exc:
        raise HTTPException(400, str(exc)) from exc
    return service.get(sheet_id)


@router.get("/{sheet_id}/export")
async def export(sheet_id: str, format: str = "csv") -> Response:
    body, media, name = await asyncio.to_thread(_guard, service.export, sheet_id, format)
    return Response(body, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})


class SaveIn(BaseModel):
    format: str = "csv"
    folder: str = Field(min_length=1, max_length=500)


@router.post("/{sheet_id}/save")
async def save(sheet_id: str, body: SaveIn) -> dict:
    return {"path": await asyncio.to_thread(_guard, service.save_to_folder, sheet_id, body.format, body.folder)}


@router.get("/{sheet_id}/history")
async def history(sheet_id: str) -> list[dict]:
    return _guard(service.history, sheet_id)


@router.post("/{sheet_id}/history", status_code=201)
async def take_snapshot(sheet_id: str) -> dict:
    return {"name": _guard(service.snapshot, sheet_id)}


@router.get("/{sheet_id}/history/{name}")
async def history_file(sheet_id: str, name: str) -> FileResponse:
    path = _guard(service.snapshot_file, sheet_id, name)
    return FileResponse(path, media_type="text/csv", filename=name)


class AskIn(BaseModel):
    question: str = Field(min_length=2, max_length=1000)


@router.post("/{sheet_id}/ask")
async def ask(sheet_id: str, body: AskIn) -> dict:
    from app.rag.text2sql import text_to_sql

    info = _guard(service.get, sheet_id)
    try:
        return await text_to_sql(body.question, True, tables=[info["table"]])
    except Exception as exc:  # noqa: BLE001 - model errors are shown
        raise HTTPException(502, f"Couldn't answer: {exc}") from exc


class DiveIn(BaseModel):
    depth: str = Field("standard", pattern="^(quick|standard|deep)$")
    focus: str = Field("", max_length=500)


@router.post("/{sheet_id}/rows/{row_id}/dive", status_code=202)
async def row_dive(sheet_id: str, row_id: int, body: DiveIn) -> dict:
    """Research one row; the answer is a saved conversation in Ask."""
    from app.engine import controller

    try:
        return controller.row_dive(sheet_id, row_id, body.depth, body.focus)
    except KeyError:
        raise HTTPException(404, "Sheet or row not found") from None


# ---------- sheets from sources ----------
class FromUrls(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=15)
    question: str = Field(min_length=3, max_length=500)
    name: str = Field("", max_length=80)


@router.post("/from-urls", status_code=201)
async def from_urls(body: FromUrls) -> dict:
    """Read these pages with KeiroLabs (zero data retention) and put what they say into a sheet."""
    from app.sheets import from_sources

    try:
        pages = await from_sources.pages_from_urls(body.urls)
        return await from_sources.build_sheet(body.question, pages, body.name)
    except from_sources.FromSourcesError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - KeiroLabs and model errors are shown
        raise HTTPException(502, f"Couldn't read those pages: {exc}") from exc
