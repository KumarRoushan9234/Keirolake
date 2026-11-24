"""/v1/tables and /v1/views: the lake as a database for your apps. Reads need `read`, changes need `write`."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app import tables_api
from app.api.public_routes import caller, export, need
from app.jobs.runner import runner
from app.lake import warehouse
from app.sheets import service

router = APIRouter(prefix="/v1", tags=["Tables"])


def _wrap(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KeyError:
        raise HTTPException(404, "Not found") from None
    except (tables_api.TableError, service.SheetError, warehouse.SQLValidationError) as exc:
        raise HTTPException(400, str(exc)) from exc


def _public(s: dict) -> dict:
    return {"id": s["id"], "name": s["name"], "table": s["table"], "rows": s["rows"], "updated_at": s["updated_at"],
            "columns": [{"name": c["name"], "type": c["type"]} for c in s["columns"]], "search": tables_api.index_info(s["id"])}


@router.get("/tables")
async def list_tables(key: dict = Depends(caller)) -> list[dict]:
    need(key, "read")
    return [_public(s) for s in await asyncio.to_thread(service.list_sheets)]


class NewTable(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    columns: list[dict] = Field(min_length=1, max_length=100)


@router.post("/tables", status_code=201)
async def create_table(body: NewTable, key: dict = Depends(caller)) -> dict:
    need(key, "write")
    return _public(_wrap(service.create_blank, body.name, body.columns))


@router.get("/tables/{sheet_id}")
async def get_table(sheet_id: str, key: dict = Depends(caller)) -> dict:
    need(key, "read")
    return _public(_wrap(service.get, sheet_id))


@router.get("/tables/{sheet_id}/rows")
async def rows(sheet_id: str, where: list[str] = Query(default=[]), order: str = "", limit: int = 200, offset: int = 0,
               format: str = "json", key: dict = Depends(caller)):
    need(key, "read")
    out = await asyncio.to_thread(_wrap, tables_api.query, sheet_id, where, order, limit, offset)
    if format == "json":
        return out
    cols = ["_id"] + [c["name"] for c in out["columns"]]
    return export({"columns": cols, "rows": [[r.get(c) for c in cols] for r in out["rows"]], "row_count": len(out["rows"]), "truncated": False},
                  format, out["table"])


class RowsIn(BaseModel):
    rows: list[dict] = Field(min_length=1, max_length=5000)


@router.post("/tables/{sheet_id}/rows", status_code=201)
async def insert(sheet_id: str, body: RowsIn, key: dict = Depends(caller)) -> dict:
    need(key, "write")
    return {"ids": _wrap(service.add_rows, sheet_id, body.rows)}


@router.patch("/tables/{sheet_id}/rows/{row_id}")
async def update(sheet_id: str, row_id: int, values: dict, key: dict = Depends(caller)) -> dict:
    need(key, "write")
    if not values:
        raise HTTPException(400, "Send the columns to change")
    for col, val in values.items():
        _wrap(service.update_cell, sheet_id, row_id, col, val)
    found = tables_api.rows_by_id(sheet_id, [row_id])
    if not found:
        raise HTTPException(404, "Row not found")
    return found[0]


@router.delete("/tables/{sheet_id}/rows/{row_id}", status_code=204)
async def delete(sheet_id: str, row_id: int, key: dict = Depends(caller)) -> None:
    need(key, "write")
    if not _wrap(service.delete_rows, sheet_id, [row_id]):
        raise HTTPException(404, "Row not found")


@router.post("/tables/{sheet_id}/index", status_code=202)
async def index(sheet_id: str, key: dict = Depends(caller)) -> dict:
    need(key, "write")
    info = _wrap(service.get, sheet_id)
    return {"job_id": runner.submit("table_index", f"API ({key['name']}): index {info['name']}", tables_api.index_job(sheet_id), lane="background")}


@router.get("/tables/{sheet_id}/search")
async def search(sheet_id: str, q: str = Query(min_length=1, max_length=1000), top_k: int = Query(10, ge=1, le=50), key: dict = Depends(caller)) -> dict:
    need(key, "read")
    _wrap(service.get, sheet_id)
    try:
        return {"rows": await tables_api.search(sheet_id, q, top_k)}
    except tables_api.TableError as exc:
        raise HTTPException(409, str(exc)) from exc


# ---------- saved views ----------
@router.get("/views")
async def views(key: dict = Depends(caller)) -> list[dict]:
    need(key, "read")
    return tables_api.list_views()


class ViewIn(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    sql: str = Field(min_length=1, max_length=20_000)
    description: str = Field("", max_length=500)


@router.post("/views", status_code=201)
async def save_view(body: ViewIn, key: dict = Depends(caller)) -> dict:
    need(key, "sql")
    return _wrap(tables_api.save_view, body.name, body.sql, body.description)


@router.get("/views/{name}")
async def run_view(name: str, format: str = "json", limit: int = 1000, key: dict = Depends(caller)):
    need(key, "read")
    return export(await asyncio.to_thread(_wrap, tables_api.run_view, name, limit), format, name)


@router.delete("/views/{name}", status_code=204)
async def delete_view(name: str, key: dict = Depends(caller)) -> None:
    need(key, "sql")
    if not tables_api.delete_view(name):
        raise HTTPException(404, "No such view")
