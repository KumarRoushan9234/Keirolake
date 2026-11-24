"""REST API. Everything the web app and the CLI do goes through here."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sse_starlette.sse import EventSourceResponse

from app import backup as backup_service
from app.catalog import kb as kb_service
from app.catalog.models import (Chunk, Dataset, Document, Feed, IngestionRun, Job, KBModelStat, KnowledgeBase,
                                SavedQuery, Source)
from app.config import get_settings
from app.db import session_scope
from app.embeddings.chain import get_chain
from app.ingest import web as ingest_web
from app.ingest.pipeline import delete_document
from app.jobs.runner import runner
from app.keiro.client import KeiroError, get_keiro
from app.lake import warehouse
from app.lake.zones import absolute, start_dataset_raw, zone_summary
from app.llm.base import ChatMessage
from app.llm.registry import AllModelsFailed, get_router
from app.rag.answer import answer_stream
from app.rag.retrieve import retrieve
from app.rag.text2sql import text_to_sql
from app.vector.qdrant_store import get_store

router = APIRouter(prefix="/api")


async def _read_upload(f: UploadFile) -> bytes:
    """Read an upload, refusing anything over MAX_UPLOAD_MB (reads one byte past the limit, never the whole file)."""
    limit = get_settings().max_upload_mb * 1024 * 1024
    data = await f.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"{f.filename} is larger than {get_settings().max_upload_mb} MB")
    return data


def _iso(dt):
    return dt.isoformat() if dt else None


def _kb_dict(kb: KnowledgeBase, docs: int = 0, chunks: int = 0) -> dict:
    return {
        "id": kb.id, "name": kb.name, "slug": kb.slug, "description": kb.description,
        "collection": kb.collection_name, "alias": kb.collection_alias, "chunk_size": kb.chunk_size,
        "chunk_overlap": kb.chunk_overlap, "vectors": kb.vector_config, "documents": docs, "chunks": chunks,
        "models": [{"model": s.model, "vector_name": s.vector_name, "points": s.points} for s in kb.model_stats],
        "created_at": _iso(kb.created_at),
    }


def _require_kb(kb_id: str) -> None:
    with session_scope() as s:
        if s.get(KnowledgeBase, kb_id) is None:
            raise HTTPException(404, "Knowledge base not found")


# ---------- health & providers ----------
@router.get("/health")
async def health() -> dict:
    qdrant = await get_store().healthy()
    return {"status": "ok" if qdrant["ok"] else "degraded", "qdrant": qdrant, "keiro": get_keiro().status(),
            "embedding_chain": [s.label for s in get_chain().specs]}


@router.get("/providers")
async def providers(probe: bool = False) -> dict:
    """Configured providers. With probe=true each model/embedder/Keiro is called once (costs a few tokens)."""
    out: dict = {"llm_order": get_settings().llm_fallback_order, "llm": get_router().describe(),
                 "embeddings": [{"model": s.label, "vector_name": s.vector_name, "dim": s.dim} for s in get_chain().specs],
                 "keiro": get_keiro().status()}
    if probe:
        out["llm_probe"] = await get_router().probe()
        out["embeddings_probe"] = await get_chain().probe()
        out["keiro"] = await get_keiro().probe()
    return out


@router.get("/stats")
async def stats() -> dict:
    with session_scope() as s:
        counts = {
            "knowledge_bases": s.scalar(select(func.count()).select_from(KnowledgeBase)),
            "documents": s.scalar(select(func.count()).select_from(Document)),
            "chunks": s.scalar(select(func.count()).select_from(Chunk)),
            "datasets": s.scalar(select(func.count()).select_from(Dataset)),
            "runs": s.scalar(select(func.count()).select_from(IngestionRun)),
        }
    return {"counts": counts, "zones": zone_summary(), "keiro": get_keiro().status()}


# ---------- knowledge bases ----------
class KBCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = ""
    chunk_size: int | None = Field(None, ge=200, le=6000)
    chunk_overlap: int | None = Field(None, ge=0, le=1000)


@router.get("/kbs")
async def list_kbs() -> list[dict]:
    with session_scope() as s:
        out = []
        for kb in s.scalars(select(KnowledgeBase).order_by(KnowledgeBase.created_at)):
            docs = s.scalar(select(func.count()).select_from(Document).where(Document.kb_id == kb.id))
            chunks = s.scalar(select(func.count()).select_from(Chunk).where(Chunk.kb_id == kb.id))
            out.append(_kb_dict(kb, docs, chunks))
        return out


@router.post("/kbs", status_code=201)
async def create_kb(body: KBCreate) -> dict:
    try:
        kb = await kb_service.create_kb(body.name, body.description, body.chunk_size, body.chunk_overlap)
    except kb_service.KBExists as exc:
        raise HTTPException(409, str(exc)) from exc
    with session_scope() as s:
        return _kb_dict(s.get(KnowledgeBase, kb.id))


@router.delete("/kbs/{kb_id}", status_code=204)
async def delete_kb(kb_id: str) -> None:
    await kb_service.delete_kb(kb_id)


@router.post("/kbs/{kb_id}/reembed", status_code=202)
async def reembed(kb_id: str) -> dict:
    _require_kb(kb_id)

    async def job(ctx):
        return await kb_service.reembed_kb(kb_id, ctx.progress)

    return {"job_id": runner.submit("reembed", "Re-embed knowledge base", job)}


@router.get("/kbs/{kb_id}/documents")
async def list_documents(kb_id: str) -> list[dict]:
    _require_kb(kb_id)
    with session_scope() as s:
        docs = s.scalars(select(Document).where(Document.kb_id == kb_id).order_by(Document.created_at.desc()))
        return [{"id": d.id, "title": d.title, "url": d.url, "mime": d.mime, "chars": d.chars, "chunks": d.chunk_count,
                 "raw_path": d.raw_path, "clean_path": d.clean_path, "run_id": d.run_id, "created_at": _iso(d.created_at)}
                for d in docs]


@router.delete("/documents/{doc_id}", status_code=204)
async def remove_document(doc_id: str) -> None:
    await delete_document(doc_id)


# ---------- ingestion ----------
@router.post("/kbs/{kb_id}/ingest/files", status_code=202)
async def ingest_files(kb_id: str, files: list[UploadFile] = File(...)) -> dict:
    _require_kb(kb_id)
    payload = [(f.filename or "upload.txt", await _read_upload(f)) for f in files]
    if not payload:
        raise HTTPException(400, "No files")
    return {"job_id": runner.submit("ingest", f"Ingest {len(payload)} file(s)", ingest_web.stage_uploads(kb_id, payload))}


class TextIngest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1)


@router.post("/kbs/{kb_id}/ingest/text", status_code=202)
async def ingest_text(kb_id: str, body: TextIngest) -> dict:
    _require_kb(kb_id)
    return {"job_id": runner.submit("ingest", f"Ingest text: {body.title}", ingest_web.text_job(kb_id, body.title, body.text))}


class UrlIngest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=20)


@router.post("/kbs/{kb_id}/ingest/urls", status_code=202)
async def ingest_urls(kb_id: str, body: UrlIngest) -> dict:
    _require_kb(kb_id)
    if any(not u.startswith(("http://", "https://")) for u in body.urls):
        raise HTTPException(400, "URLs must start with http:// or https://")
    return {"job_id": runner.submit("ingest", f"Ingest {len(body.urls)} URL(s)", ingest_web.urls_job(kb_id, body.urls))}


class WebSearchIngest(BaseModel):
    query: str = Field(min_length=2)
    max_results: int = Field(5, ge=1, le=10)
    mode: str = Field("content", pattern="^(lite|fast|content)$")


@router.post("/web/search")
async def web_search(body: WebSearchIngest) -> dict:
    """Preview Keiro results without ingesting (lite = cheapest)."""
    try:
        results = await get_keiro().search(body.query, body.max_results, body.mode)
    except KeiroError as exc:
        raise HTTPException(exc.status if exc.status in (401, 402, 403) else 502, str(exc)) from exc
    return {"results": [r.to_dict() for r in results], "credits_remaining": get_keiro().state.credits_remaining}


@router.post("/kbs/{kb_id}/ingest/web-search", status_code=202)
async def ingest_web_search(kb_id: str, body: WebSearchIngest) -> dict:
    _require_kb(kb_id)
    return {"job_id": runner.submit("ingest", f"Web search: {body.query}",
                                    ingest_web.web_search_job(kb_id, body.query, body.max_results, body.mode))}


class ResearchIngest(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    max_pages: int = Field(5, ge=0, le=10)


@router.post("/kbs/{kb_id}/ingest/research", status_code=202)
async def ingest_research(kb_id: str, body: ResearchIngest) -> dict:
    """KeiroLabs deep search: a dossier (evidence + ranked sources) plus the top pages in full."""
    _require_kb(kb_id)
    return {"job_id": runner.submit("ingest", f"Research: {body.question}",
                                    ingest_web.research_job(kb_id, body.question, body.max_pages))}


class SiteIngest(BaseModel):
    site: str = Field(min_length=3, max_length=300, pattern=r"^(https?://)?[A-Za-z0-9.-]+\.[A-Za-z]{2,}(/.*)?$")
    path_prefix: str = Field("", max_length=200)
    limit: int = Field(20, ge=1, le=200)


@router.post("/kbs/{kb_id}/ingest/site", status_code=202)
async def ingest_site(kb_id: str, body: SiteIngest) -> dict:
    """Index the newest pages a website lists in its sitemap."""
    _require_kb(kb_id)
    return {"job_id": runner.submit("ingest", f"Website: {body.site}{body.path_prefix}",
                                    ingest_web.site_job(kb_id, body.site, body.path_prefix, body.limit))}


# ---------- feeds ----------
class FeedCreate(BaseModel):
    query: str = Field(min_length=2)
    mode: str = Field("fast", pattern="^(lite|fast|content)$")
    max_results: int = Field(5, ge=1, le=10)
    interval_minutes: int = Field(1440, ge=15)


@router.get("/kbs/{kb_id}/feeds")
async def list_feeds(kb_id: str) -> list[dict]:
    with session_scope() as s:
        return [{"id": f.id, "query": f.query, "mode": f.mode, "max_results": f.max_results,
                 "interval_minutes": f.interval_minutes, "enabled": bool(f.enabled), "last_run_at": _iso(f.last_run_at)}
                for f in s.scalars(select(Feed).where(Feed.kb_id == kb_id))]


@router.post("/kbs/{kb_id}/feeds", status_code=201)
async def create_feed(kb_id: str, body: FeedCreate) -> dict:
    _require_kb(kb_id)
    with session_scope() as s:
        feed = Feed(kb_id=kb_id, **body.model_dump())
        s.add(feed)
        s.flush()
        return {"id": feed.id}


@router.delete("/feeds/{feed_id}", status_code=204)
async def delete_feed(feed_id: str) -> None:
    with session_scope() as s:
        feed = s.get(Feed, feed_id)
        if feed:
            s.delete(feed)


@router.post("/feeds/{feed_id}/run", status_code=202)
async def run_feed(feed_id: str) -> dict:
    with session_scope() as s:
        feed = s.get(Feed, feed_id)
        if feed is None:
            raise HTTPException(404, "Feed not found")
        feed.last_run_at = datetime.now(timezone.utc)
        query = feed.query
    return {"job_id": runner.submit("feed", f"Feed: {query}", ingest_web.feed_job(feed_id))}


# ---------- search & chat ----------
class SearchBody(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(8, ge=1, le=30)
    keyword: bool = True


@router.post("/kbs/{kb_id}/search")
async def search(kb_id: str, body: SearchBody) -> dict:
    _require_kb(kb_id)
    result = await retrieve(kb_id, body.query, body.top_k, use_keyword=body.keyword)
    return {"hits": [h.to_dict() for h in result.hits], "best_cosine": result.best_cosine, "warnings": result.warnings}


class ChatBody(BaseModel):
    question: str = Field(min_length=1)
    kb_id: str | None = None
    history: list[dict] = []
    top_k: int | None = None
    web: str = Field("auto", pattern="^(auto|always|never)$")
    library: bool = True
    private: bool = False
    style: str = Field("", pattern="^(|checklist)$")


@router.post("/chat")
async def chat(body: ChatBody):
    if body.kb_id:
        _require_kb(body.kb_id)
    history = [ChatMessage(m["role"], m["content"]) for m in body.history if m.get("role") in {"user", "assistant"}]

    async def events():
        async for event in answer_stream(body.kb_id, body.question, history, body.top_k, body.web, body.library, body.private, body.style):
            yield {"event": event["type"], "data": json.dumps(event)}

    return EventSourceResponse(events())


# ---------- datasets & warehouse ----------
@router.get("/datasets")
async def list_datasets() -> list[dict]:
    with session_scope() as s:
        return [{"id": d.id, "name": d.name, "table": d.table_name, "zone": d.zone, "rows": d.row_count, "columns": d.columns,
                 "description": d.description, "sql": d.sql, "raw_path": d.raw_path, "created_at": _iso(d.created_at)}
                for d in s.scalars(select(Dataset).order_by(Dataset.created_at.desc()))]


@router.post("/datasets", status_code=201)
async def upload_dataset(file: UploadFile = File(...), name: str = Form(""), description: str = Form("")) -> dict:
    data = await _read_upload(file)
    filename = file.filename or "data.csv"
    label = name.strip() or filename.rsplit(".", 1)[0]
    raw_path, source_id = start_dataset_raw(label, filename, data)
    try:
        ds = warehouse.load_dataframe(label, filename, data, raw_path, source_id, description)
    except Exception as exc:  # noqa: BLE001 - pandas raises many types for bad files
        raise HTTPException(400, f"Could not load dataset: {exc}") from exc
    return {"id": ds.id, "table": ds.table_name, "rows": ds.row_count, "columns": ds.columns}


@router.delete("/datasets/{dataset_id}", status_code=204)
async def delete_dataset(dataset_id: str) -> None:
    warehouse.drop_dataset(dataset_id)


@router.get("/datasets/{dataset_id}/download")
async def download_dataset(dataset_id: str):
    with session_scope() as s:
        ds = s.get(Dataset, dataset_id)
        if ds is None or not ds.raw_path:
            raise HTTPException(404, "Dataset not found")
        path = absolute(ds.raw_path)
    if not path.exists():
        raise HTTPException(404, "File missing from lake")
    return FileResponse(path)


@router.get("/warehouse/schema")
async def warehouse_schema(system: bool = False) -> list[dict]:
    return warehouse.schema(include_system=system)


class SQLBody(BaseModel):
    sql: str
    limit: int = 1000


@router.post("/warehouse/query")
async def warehouse_query(body: SQLBody) -> dict:
    try:
        return warehouse.run_query(body.sql, body.limit)
    except warehouse.SQLValidationError as exc:
        raise HTTPException(400, str(exc)) from exc


class CuratedBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    sql: str
    description: str = ""


@router.post("/warehouse/curated", status_code=201)
async def create_curated(body: CuratedBody) -> dict:
    try:
        ds = warehouse.create_curated(body.name, body.sql, body.description)
    except warehouse.SQLValidationError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": ds.id, "table": ds.table_name, "rows": ds.row_count}


class AskSQL(BaseModel):
    question: str = Field(min_length=3)
    execute: bool = True


@router.post("/warehouse/ask")
async def ask_sql(body: AskSQL) -> dict:
    try:
        return await text_to_sql(body.question, body.execute)
    except AllModelsFailed as exc:
        raise HTTPException(502, str(exc)) from exc


class SavedQueryBody(BaseModel):
    name: str
    sql: str


@router.get("/warehouse/saved")
async def list_saved() -> list[dict]:
    with session_scope() as s:
        return [{"id": q.id, "name": q.name, "sql": q.sql} for q in s.scalars(select(SavedQuery).order_by(SavedQuery.created_at.desc()))]


@router.post("/warehouse/saved", status_code=201)
async def save_query(body: SavedQueryBody) -> dict:
    with session_scope() as s:
        q = SavedQuery(name=body.name, sql=body.sql)
        s.add(q)
        s.flush()
        return {"id": q.id}


@router.delete("/warehouse/saved/{query_id}", status_code=204)
async def delete_saved(query_id: str) -> None:
    with session_scope() as s:
        q = s.get(SavedQuery, query_id)
        if q:
            s.delete(q)


# ---------- backups ----------
@router.get("/backups")
async def list_backups() -> list[dict]:
    return backup_service.list_backups()


@router.post("/backups", status_code=202)
async def create_backup() -> dict:
    """SQLite copy + a Qdrant snapshot per knowledge base, written to data/backups/."""
    return {"job_id": runner.submit("backup", "Back up database and vectors", lambda ctx: backup_service.run_backup(ctx.progress))}


class BackupCopyBody(BaseModel):
    target: str = Field(min_length=1, max_length=500)
    passphrase: str = Field("", max_length=200)
    include_lake: bool = True
    remember: bool = False  # copy every automatic backup here too


@router.post("/backups/{stamp}/copy", status_code=202)
async def copy_backup(stamp: str, body: BackupCopyBody) -> dict:
    """Write a backup as one file (encrypted when a passphrase is given) to another drive or folder."""
    from app import backup_copy, prefs

    if body.remember:
        prefs.update({"BACKUP_TARGET": body.target, "BACKUP_INCLUDE_LAKE": "1" if body.include_lake else "0",
                      "BACKUP_PASSPHRASE": body.passphrase})

    async def job(ctx) -> dict:
        await ctx.progress(0.1, f"Copying to {body.target}")
        return await asyncio.to_thread(backup_copy.copy_backup, stamp, body.target, body.passphrase, body.include_lake)

    return {"job_id": runner.submit("backup", f"Copy backup {stamp}", job, lane="background")}


class VerifyBody(BaseModel):
    stamp: str | None = None
    path: str | None = Field(None, max_length=500)
    passphrase: str = Field("", max_length=200)


@router.post("/backups/verify")
async def verify_backup(body: VerifyBody) -> dict:
    """Restore a backup into a temporary folder and check it, without touching live data."""
    from app import backup_copy

    if not body.stamp and not body.path:
        raise HTTPException(400, "Pick a backup or a backup file")
    try:
        return await asyncio.to_thread(backup_copy.verify, stamp=body.stamp, path=body.path, passphrase=body.passphrase)
    except backup_copy.BackupCopyError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/backups/target")
async def backup_target() -> dict:
    return {"target": os.getenv("BACKUP_TARGET", ""), "include_lake": os.getenv("BACKUP_INCLUDE_LAKE", "1") != "0",
            "encrypted": bool(os.getenv("BACKUP_PASSPHRASE"))}


# ---------- jobs & lineage ----------
def _job_dict(j: Job) -> dict:
    return {"id": j.id, "kind": j.kind, "title": j.title, "status": j.status, "progress": j.progress, "message": j.message,
            "result": j.result, "created_at": _iso(j.created_at), "finished_at": _iso(j.finished_at)}


@router.get("/jobs")
async def list_jobs(limit: int = 50) -> list[dict]:
    with session_scope() as s:
        return [_job_dict(j) for j in s.scalars(select(Job).order_by(Job.created_at.desc()).limit(limit))]


@router.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "Job not found")
        return _job_dict(job)


@router.get("/lineage/runs")
async def list_runs(limit: int = 50) -> list[dict]:
    with session_scope() as s:
        rows = s.execute(select(IngestionRun, Source).join(Source, Source.id == IngestionRun.source_id)
                         .order_by(IngestionRun.started_at.desc()).limit(limit)).all()
        return [{"id": r.id, "source": {"id": src.id, "kind": src.kind, "name": src.name}, "kb_id": r.kb_id,
                 "status": r.status, "raw_path": r.raw_path, "stats": r.stats, "error": r.error,
                 "started_at": _iso(r.started_at), "finished_at": _iso(r.finished_at)} for r, src in rows]


@router.get("/lineage/chunks/{chunk_id}")
async def chunk_lineage(chunk_id: str) -> dict:
    """Trace a chunk back through document -> run -> source -> raw file."""
    with session_scope() as s:
        chunk = s.scalar(select(Chunk).where(Chunk.id == chunk_id))
        if chunk is None:
            raise HTTPException(404, "Chunk not found")
        doc = chunk.document
        run, source = s.get(IngestionRun, doc.run_id), s.get(Source, doc.source_id)
        return {"chunk": {"id": chunk.id, "ordinal": chunk.ordinal, "embed_model": chunk.embed_model, "text": chunk.text},
                "document": {"id": doc.id, "title": doc.title, "url": doc.url, "raw_path": doc.raw_path, "clean_path": doc.clean_path},
                "run": {"id": run.id, "status": run.status, "started_at": _iso(run.started_at)},
                "source": {"id": source.id, "kind": source.kind, "name": source.name}}
