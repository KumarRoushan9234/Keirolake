"""Data tools: RenderData scrapers and open-data APIs, each saved as a connector that fills a table."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import connectors, datatools
from app.jobs.runner import runner

router = APIRouter(prefix="/api/datatools")


@router.get("")
async def overview() -> dict:
    return {"templates": datatools.TEMPLATES, "renderdata": {"key_set": bool(datatools.rd_key()), "base": datatools.rd_base()}}


@router.get("/renderdata/account")
async def account() -> dict:
    try:
        return await datatools.rd_account()
    except Exception as exc:  # noqa: BLE001 - shown in the page
        return {"key_set": True, "valid": False, "error": f"Couldn't reach RenderData: {exc}"}


@router.get("/renderdata/catalog")
async def catalog(refresh: bool = False) -> dict:
    try:
        return await datatools.rd_catalog(force=refresh)
    except (datatools.DataToolError, Exception) as exc:  # noqa: BLE001 - shown in the page
        raise HTTPException(502, f"Couldn't load the RenderData catalog: {exc}") from exc


def _start(name: str, kind: str, config: dict, key_column: str, interval: int) -> dict:
    try:
        c = connectors.create(name, kind, config, key_column, interval)
    except connectors.ConnectorError as exc:
        raise HTTPException(400, str(exc)) from exc
    c["job_id"] = runner.submit("connector", f"Connector: {c['name']}", connectors.run_job(c["id"]), lane="background")
    return c


class UseTemplate(BaseModel):
    template: str = Field(max_length=40)
    input: str = Field(min_length=1, max_length=300)
    name: str = Field(min_length=1, max_length=80)
    interval_minutes: int = Field(0, ge=0, le=10080)


@router.post("/use", status_code=201)
async def use_template(body: UseTemplate) -> dict:
    try:
        spec = datatools.template_url(body.template, body.input)
    except datatools.DataToolError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _start(body.name, spec["kind"], {"url": spec["url"], "json_path": spec["json_path"]}, spec["key_column"], body.interval_minutes)


class RenderRun(BaseModel):
    scraper: str = Field(max_length=62)
    input: str = Field(min_length=1, max_length=1000)
    name: str = Field(min_length=1, max_length=80)
    interval_minutes: int = Field(0, ge=0, le=10080)
    append: bool = True


@router.post("/renderdata", status_code=201)
async def renderdata(body: RenderRun) -> dict:
    if not datatools.rd_key():
        raise HTTPException(400, "Add your RenderData API key first")
    return _start(body.name, "renderdata", {"scraper": body.scraper, "input": body.input, "append": body.append}, "", body.interval_minutes)
