"""Desktop features: settings, network, local models, offline libraries, reader, capture, export."""
from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from app import prefs
from app.capture import CaptureError, capture_job, export_job, reader_view
from app.catalog.models import KnowledgeBase
from app.config import get_settings
from app.db import session_scope
from app.jobs.runner import runner
from app.library import catalog as lib_catalog
from app.library import service as library
from app.library import zim
from app.local import catalog as model_catalog
from app.local import hardware, llamacpp, ollama
from app.net import connectivity
from app.security import token_ok

router = APIRouter(prefix="/api")

READER_CSP = ("default-src 'self' data: blob:; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
              "font-src 'self' data:; script-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'")


# ---------- settings & network ----------
class SettingsBody(BaseModel):
    values: dict[str, str | None]


@router.get("/settings")
async def get_prefs() -> dict:
    s = get_settings()
    from app import usage
    from app.config import offline_mode
    from app.keiro.client import get_keiro

    keiro = get_keiro().status()
    return {"values": prefs.describe(), "mode": s.llm_mode, "library_dir": str(library.library_dir()),
            "data_dir": str(s.data_dir), "offline": offline_mode(), "usage": usage.today(),
            "usage_history": usage.history(14), "cloud_limit": s.cloud_daily_limit,
            "zdr": {"on": keiro["zdr"], "confirmed": keiro["zdr_confirmed"]}, "keiro_cache": s.keiro_cache}


@router.put("/settings")
async def put_prefs(body: SettingsBody) -> dict:
    try:
        prefs.update(body.values)
    except prefs.PrefsError as exc:
        raise HTTPException(400, str(exc)) from exc
    return await get_prefs()


@router.get("/net")
async def net_status() -> dict:
    return connectivity.status()


@router.post("/net/check")
async def net_check() -> dict:
    await connectivity.check()
    return connectivity.status()


# ---------- local models ----------
@router.get("/local/hardware")
async def local_hardware() -> dict:
    hw = await asyncio.to_thread(hardware.probe)
    return {"hardware": hw, "recommendation": model_catalog.recommend(hw)}


@router.get("/local/catalog")
async def local_catalog() -> dict:
    return {"llms": model_catalog.LLMS, "embedders": model_catalog.EMBEDDERS, "wikipedia": model_catalog.WIKIPEDIA}


@router.get("/local/status")
async def local_status() -> dict:
    s = get_settings()
    return {"mode": s.llm_mode, "local_model": os.getenv("LOCAL_LLM_MODEL", ""), "wiki_embed_model": s.wiki_embed_model,
            "ollama": await ollama.status(), "llamacpp": await llamacpp.status(), "net": connectivity.status()}


@router.post("/local/ollama/start")
async def ollama_start() -> dict:
    try:
        return await ollama.start()
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


class ModelBody(BaseModel):
    model: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._:/-]+$")


@router.post("/local/ollama/pull", status_code=202)
async def ollama_pull(body: ModelBody) -> dict:
    return {"job_id": runner.submit("model", f"Download {body.model}", ollama.pull_job(body.model), lane="background", resume={"type": "model", "model": body.model})}


@router.delete("/local/ollama/models/{name:path}", status_code=204)
async def ollama_delete(name: str) -> None:
    try:
        await ollama.delete(name)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/local/use")
async def use_local_model(body: ModelBody) -> dict:
    prefs.update({"LOCAL_LLM_MODEL": body.model})
    return await local_status()


class UrlBody(BaseModel):
    url: str = Field(min_length=8, max_length=2000)


@router.post("/local/llamacpp/start")
async def llamacpp_start() -> dict:
    try:
        return await llamacpp.start()
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/local/llamacpp/download", status_code=202)
async def llamacpp_download(body: UrlBody) -> dict:
    try:
        job = llamacpp.download_gguf_job(body.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"job_id": runner.submit("model", f"Download {body.url.rsplit('/', 1)[-1]}", job, lane="background")}


# ---------- offline libraries ----------
@router.get("/library/catalog")
async def library_catalog(q: str = "", category: str = "", refresh: bool = False, limit: int = 200) -> dict:
    entries, meta = await lib_catalog.load(refresh)
    installed = {lib["name"]: lib["status"] for lib in library.list_libraries()}
    found = lib_catalog.search(entries, q, category, min(limit, 1000))
    essentials = set(model_catalog.ESSENTIALS)
    for e in found:
        e["installed"] = installed.get(e["name"])
        e["essential"] = e["name"] in essentials
    categories = sorted({e["category"] for e in entries if e["category"]})
    return {"entries": found, "categories": categories, **meta}


@router.get("/libraries")
async def libraries() -> list[dict]:
    return library.list_libraries()


class NameBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)


async def _catalog_entry(name: str) -> dict:
    entries, _ = await lib_catalog.load()
    entry = next((e for e in entries if e["name"] == name), None)
    if entry is None:
        raise HTTPException(404, f"{name} is not in the catalog")
    return entry


def _queue_download(entry: dict) -> dict:
    lib_id, job = library.start_download(entry)
    job_id = runner.submit("library", f"Download {entry['title'] or entry['name']}", job, lane="background", resume={"type": "library", "entry": entry})
    return {"library_id": lib_id, "job_id": job_id}


@router.post("/libraries/download", status_code=202)
async def library_download(body: NameBody) -> dict:
    try:
        return _queue_download(await _catalog_entry(body.name))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/libraries/essentials", status_code=202)
async def library_essentials() -> dict:
    entries, _ = await lib_catalog.load()
    by_name = {e["name"]: e for e in entries}
    have = {lib["name"] for lib in library.list_libraries()}
    queued = [_queue_download(by_name[n]) for n in model_catalog.ESSENTIALS if n in by_name and n not in have]
    return {"queued": len(queued), "jobs": queued}


class PathBody(BaseModel):
    path: str = Field(min_length=1, max_length=1000)


@router.post("/libraries/register", status_code=201)
async def library_register(body: PathBody) -> dict:
    try:
        return await asyncio.to_thread(library.register_file, body.path)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc


def _require_lib(lib_id: str):
    lib = library.get(lib_id)
    if lib is None:
        raise HTTPException(404, "Library not found")
    return lib


class LibraryPatch(BaseModel):
    enabled: bool


@router.patch("/libraries/{lib_id}")
async def library_patch(lib_id: str, body: LibraryPatch) -> dict:
    _require_lib(lib_id)
    library._set(lib_id, enabled=int(body.enabled))
    return library.to_dict(library.get(lib_id))


@router.delete("/libraries/{lib_id}", status_code=204)
async def library_delete(lib_id: str, delete_file: bool = True) -> None:
    _require_lib(lib_id)
    await library.remove(lib_id, delete_file)


class TierBody(BaseModel):
    tier: int = Field(ge=1, le=2)


@router.post("/libraries/{lib_id}/index", status_code=202)
async def library_index(lib_id: str, body: TierBody) -> dict:
    lib = _require_lib(lib_id)
    if lib.status != "ready":
        raise HTTPException(400, "Library is not downloaded yet")
    title = f"Index {'intros' if body.tier == 1 else 'passages'}: {lib.title or lib.name}"
    return {"job_id": runner.submit("index", title, library.index_job(lib_id, body.tier), lane="background", resume={"type": "index", "library_id": lib_id, "tier": body.tier})}


@router.post("/libraries/{lib_id}/index/pause")
async def library_index_pause(lib_id: str) -> dict:
    _require_lib(lib_id)
    library.pause_index(lib_id)
    return {"ok": True}


@router.delete("/libraries/{lib_id}/index", status_code=204)
async def library_index_reset(lib_id: str) -> None:
    _require_lib(lib_id)
    await library.reset_index(lib_id)


@router.post("/libraries/{lib_id}/check", status_code=202)
async def library_check(lib_id: str) -> dict:
    lib = _require_lib(lib_id)

    async def job(ctx) -> dict:
        await ctx.progress(0.1, "Verifying checksum (large files take a while)")
        ok = await asyncio.to_thread(zim.check, lib.path)
        library._set(lib_id, message="Integrity check passed" if ok else "Integrity check FAILED: download it again")
        if not ok:
            raise RuntimeError("The file is damaged")
        return {"ok": True}

    return {"job_id": runner.submit("library", f"Check {lib.title or lib.name}", job, lane="background")}


@router.get("/libraries/{lib_id}/search")
async def library_search(lib_id: str, q: str, limit: int = 20) -> dict:
    lib = _require_lib(lib_id)
    if lib.status != "ready":
        raise HTTPException(400, "Library is not downloaded yet")

    def run() -> list[dict]:
        archive = zim.open_archive(lib.path)
        out = []
        for path in zim.search(lib.path, q, min(limit, 50)):
            try:
                out.append({"path": path, "title": archive.get_entry_by_path(path).title or path})
            except KeyError:
                continue
        return out

    return {"results": await asyncio.to_thread(run), "main": (await asyncio.to_thread(zim.metadata, lib.path))["main"]}


@router.get("/zim/{token}/{lib_id}/{path:path}")
async def zim_content(token: str, lib_id: str, path: str = "") -> Response:
    if not token_ok(None if token == "-" else token):
        raise HTTPException(401, "Wrong token")
    lib = _require_lib(lib_id)
    found = await asyncio.to_thread(zim.content, lib.path, path)
    if found is None:
        raise HTTPException(404, "Not in this library")
    data, mime, canonical = found
    if canonical != path and mime.startswith("text/html"):
        prefix = f"/api/zim/{token}/{lib_id}/"
        return Response(status_code=307, headers={"Location": prefix + canonical})
    headers = {"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"}
    if mime.startswith("text/html") or mime.startswith("image/svg"):
        headers["Content-Security-Policy"] = READER_CSP
    return Response(content=data, media_type=mime, headers=headers)


# ---------- reader, capture, export ----------
@router.get("/reader")
async def reader(url: str) -> dict:
    try:
        return await reader_view(url)
    except CaptureError as exc:
        raise HTTPException(400, str(exc)) from exc


class CaptureBody(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    title: str = Field("", max_length=300)
    html: str | None = None
    markdown: str | None = None


@router.post("/kbs/{kb_id}/capture", status_code=202)
async def capture(kb_id: str, body: CaptureBody) -> dict:
    with session_scope() as s:
        if s.get(KnowledgeBase, kb_id) is None:
            raise HTTPException(404, "Knowledge base not found")
    try:
        job = capture_job(kb_id, body.url, body.title, body.html, body.markdown)
    except CaptureError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"job_id": runner.submit("ingest", f"Save page: {body.title or body.url}"[:200], job)}


class ExportBody(BaseModel):
    target: str = Field(min_length=1, max_length=1000)
    include_libraries: bool = True


@router.post("/export", status_code=202)
async def export(body: ExportBody) -> dict:
    try:
        job = export_job(body.target, body.include_libraries)
    except CaptureError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"job_id": runner.submit("export", f"Export to {body.target}", job, lane="background")}


from app import vault  # noqa: E402
from app import watch as folder_watch  # noqa: E402


# ---------- watched folders ----------
class WatchBody(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    interval_minutes: int = Field(15, ge=1, le=1440)


@router.get("/kbs/{kb_id}/watches")
async def list_watches(kb_id: str) -> list[dict]:
    return folder_watch.list_watches(kb_id)


@router.post("/kbs/{kb_id}/watches", status_code=201)
async def add_watch(kb_id: str, body: WatchBody) -> dict:
    with session_scope() as s:
        if s.get(KnowledgeBase, kb_id) is None:
            raise HTTPException(404, "Knowledge base not found")
    try:
        watch_id = folder_watch.add_watch(kb_id, body.path, body.interval_minutes)
    except folder_watch.WatchError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": watch_id, "job_id": runner.submit("watch", "Scan folder", folder_watch.scan_job(watch_id))}


@router.post("/watches/{watch_id}/scan", status_code=202)
async def scan_watch(watch_id: str) -> dict:
    return {"job_id": runner.submit("watch", "Scan folder", folder_watch.scan_job(watch_id))}


@router.delete("/watches/{watch_id}", status_code=204)
async def delete_watch(watch_id: str) -> None:
    folder_watch.delete_watch(watch_id)


# ---------- private vault ----------
class PassBody(BaseModel):
    passphrase: str = Field(min_length=1, max_length=500)


class NoteBody(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=vault.MAX_NOTE_CHARS)
    id: str | None = None


def _vault_call(fn, *args):
    try:
        return fn(*args)
    except vault.VaultError as exc:
        code = 423 if "locked" in str(exc) else 400
        raise HTTPException(code, str(exc)) from exc


@router.get("/vault")
async def vault_status() -> dict:
    return vault.status()


@router.post("/vault/init")
async def vault_init(body: PassBody) -> dict:
    await asyncio.to_thread(_vault_call, vault.init, body.passphrase)
    return vault.status()


@router.post("/vault/unlock")
async def vault_unlock(body: PassBody) -> dict:
    await asyncio.to_thread(_vault_call, vault.unlock, body.passphrase)
    return vault.status()


@router.post("/vault/lock")
async def vault_lock() -> dict:
    vault.lock()
    return vault.status()


@router.get("/vault/items")
async def vault_items() -> list[dict]:
    return _vault_call(vault.list_items)


@router.get("/vault/items/{item_id}")
async def vault_item(item_id: str) -> dict:
    return _vault_call(vault.get_item, item_id)


@router.post("/vault/items", status_code=201)
async def vault_save(body: NoteBody) -> dict:
    return {"id": _vault_call(vault.save_item, body.title, body.text, body.id)}


@router.delete("/vault/items/{item_id}", status_code=204)
async def vault_delete(item_id: str) -> None:
    _vault_call(vault.delete_item, item_id)


# ---------- library updates ----------
@router.get("/libraries-updates")
async def library_updates() -> list[dict]:
    entries, _ = await lib_catalog.load()
    return library.updates(entries)


@router.post("/libraries/{lib_id}/update", status_code=202)
async def library_update(lib_id: str) -> dict:
    lib = _require_lib(lib_id)
    try:
        return _queue_download(await _catalog_entry(lib.name))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
