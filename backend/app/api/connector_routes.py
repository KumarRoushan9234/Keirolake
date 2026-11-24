"""Connectors and the Map's own settings (positions, custom boxes, custom links)."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app import connectors, lakemap, network
from app.jobs.runner import runner

router = APIRouter(prefix="/api")


def _wrap(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KeyError:
        raise HTTPException(404, "Connector not found") from None
    except connectors.ConnectorError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/connectors")
async def list_connectors() -> list[dict]:
    return connectors.list_all()


class NewConnector(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: str = Field(pattern="^(rest|rss|folder)$")
    url: str = Field("", max_length=1000)
    json_path: str = Field("", max_length=100)
    path: str = Field("", max_length=1000)
    key_column: str = Field("", max_length=80)
    interval_minutes: int = Field(0, ge=0, le=10080)
    run_now: bool = True


@router.post("/connectors", status_code=201)
async def create_connector(body: NewConnector, request: Request) -> dict:
    config = {"path": body.path} if body.kind == "folder" else {"url": body.url, "json_path": body.json_path}
    c = await asyncio.to_thread(_wrap, connectors.create, body.name, body.kind, config, body.key_column, body.interval_minutes,
                                not network.is_network_request(request.scope))
    if body.run_now:
        c["job_id"] = runner.submit("connector", f"Connector: {c['name']}", connectors.run_job(c["id"]), lane="background")
    return c


class ConnectorPatch(BaseModel):
    enabled: bool | None = None
    interval_minutes: int | None = Field(None, ge=0, le=10080)
    key_column: str | None = Field(None, max_length=80)


@router.patch("/connectors/{cid}")
async def patch_connector(cid: str, body: ConnectorPatch) -> dict:
    return _wrap(connectors.update, cid, body.enabled, body.interval_minutes, body.key_column)


@router.post("/connectors/{cid}/run", status_code=202)
async def run_connector(cid: str) -> dict:
    c = _wrap(connectors.get, cid)
    return {"job_id": runner.submit("connector", f"Connector: {c['name']}", connectors.run_job(cid), lane="background")}


@router.delete("/connectors/{cid}", status_code=204)
async def delete_connector(cid: str) -> None:
    if not connectors.delete(cid):
        raise HTTPException(404, "Connector not found")


# ---------- the Map's own settings ----------
class MapState(BaseModel):
    positions: dict[str, dict[str, float]] = Field(default_factory=dict, max_length=500)
    hidden: list[str] = Field(default_factory=list, max_length=500)
    links: list[dict] = Field(default_factory=list, max_length=300)
    custom: list[dict] = Field(default_factory=list, max_length=100)


@router.get("/map/state")
async def get_map_state() -> dict:
    return lakemap.load_state()


@router.put("/map/state")
async def put_map_state(body: MapState) -> dict:
    try:
        return lakemap.save_state(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
