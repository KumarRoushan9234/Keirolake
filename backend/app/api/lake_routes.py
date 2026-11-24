"""App-side endpoints for the Files, Tables and LLM gateway pages (the desktop window or a signed-in browser)."""
from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app import network, tables_api
from app.config import get_settings
from app.jobs.runner import runner
from app.lake.zones import ZONES
from app.sheets import service

router = APIRouter(prefix="/api")


def _inside(rel: str) -> Path:
    """A path inside the lake folder; anything that escapes it is refused."""
    root = get_settings().lake_dir.resolve()
    target = (root / rel.lstrip("/\\")).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(400, "That path is outside the lake")
    return target


@router.get("/lake/files")
async def files(path: str = "") -> dict:
    folder = _inside(path)
    if not folder.is_dir():
        raise HTTPException(404, "No such folder")
    root = get_settings().lake_dir.resolve()

    def scan() -> list[dict]:
        out = []
        for p in sorted(folder.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))[:2000]:
            st = p.stat()
            out.append({"name": p.name, "path": p.relative_to(root).as_posix(), "dir": p.is_dir(), "bytes": st.st_size if p.is_file() else None,
                        "modified": st.st_mtime})
        return out

    return {"path": folder.relative_to(root).as_posix() if folder != root else "", "zones": list(ZONES), "entries": await asyncio.to_thread(scan),
            "folder": str(folder)}


@router.get("/lake/file")
async def file(path: str):
    target = _inside(path)
    if not target.is_file():
        raise HTTPException(404, "No such file")
    return FileResponse(target, media_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream", filename=target.name)


# ---------- saved views and row search, for the Tables page ----------
@router.get("/views")
async def views() -> list[dict]:
    return tables_api.list_views()


class ViewIn(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    sql: str = Field(min_length=1, max_length=20_000)
    description: str = Field("", max_length=500)


@router.post("/views", status_code=201)
async def save_view(body: ViewIn) -> dict:
    try:
        return tables_api.save_view(body.name, body.sql, body.description)
    except tables_api.TableError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/views/{name}", status_code=204)
async def delete_view(name: str) -> None:
    if not tables_api.delete_view(name):
        raise HTTPException(404, "No such view")


@router.get("/sheets/{sheet_id}/search-index")
async def index_status(sheet_id: str) -> dict:
    return {"index": tables_api.index_info(sheet_id)}


@router.post("/sheets/{sheet_id}/search-index", status_code=202)
async def build_index(sheet_id: str) -> dict:
    try:
        info = service.get(sheet_id)
    except KeyError:
        raise HTTPException(404, "Sheet not found") from None
    return {"job_id": runner.submit("table_index", f"Index {info['name']} for search", tables_api.index_job(sheet_id), lane="background")}


@router.get("/sheets/{sheet_id}/meaning")
async def meaning(sheet_id: str, q: str) -> dict:
    try:
        return {"rows": await tables_api.search(sheet_id, q, 20)}
    except tables_api.TableError as exc:
        raise HTTPException(409, str(exc)) from exc


# ---------- the gateway page ----------
@router.get("/gateway")
async def gateway() -> dict:
    """Where apps reach the gateway, and which models it serves."""
    from app.config import LOCAL_PROVIDERS
    from app.llm.registry import get_router
    from app.local import ollama

    st = network.status()
    status = await ollama.status()
    cloud = [f"{r['provider']}:{m}" for r in get_router().describe() if r["provider"] not in LOCAL_PROVIDERS and r["configured"] for m in r["models"]]
    return {"listener": st, "base_urls": [u + "/v1" for u in st["urls"]],
            "models": ["auto", "lake", *[m["name"] for m in status["models"] if m.get("local") and "embed" not in m["name"]], *cloud]}


class TryBody(BaseModel):
    model: str = Field("auto", max_length=200)
    prompt: str = Field(min_length=1, max_length=8000)


@router.post("/gateway/try")
async def gateway_try(body: TryBody) -> dict:
    """The gateway page's playground: the same routing and grounding an app gets, run from the app itself."""
    from types import SimpleNamespace

    from app.api import gateway_routes as gw
    from app.llm.base import LLMError

    me = {"id": None, "name": "app", "scopes": ["llm", "read"], "kb_ids": []}
    chat = gw.ChatBody(model=body.model, messages=[gw.Message(role="user", content=body.prompt)])
    grounding, sources = await gw._ground(me, body.model, SimpleNamespace(headers={}), body.prompt)
    msgs = gw._messages(chat, grounding)
    errors = []
    for a in gw._attempts(body.model):
        try:
            text = await a.backend.complete(a.model, msgs, temperature=0.2, max_tokens=800)
            return {"model": f"{a.provider}:{a.model}", "content": text, "sources": sources}
        except LLMError as exc:
            errors.append(f"{a.provider}:{a.model} -> {exc}")
    raise HTTPException(502, "No model answered: " + "; ".join(errors)[:500])
