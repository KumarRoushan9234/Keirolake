"""A topic, start to finish: find the pages, make a sheet of facts, clean it, keep the pages searchable, save a CSV.

    question or pages  ->  KeiroLabs (zero data retention)  ->  sheet (columns chosen for the question, cells linked
    to their page)  ->  cleanup  ->  collection of the page text (embedded locally)  ->  CSV + sources in the lake

Every step reports progress as a job, and the topic row records what was made so it can be opened, refreshed or removed.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.catalog import kb as kb_service
from app.catalog.models import KnowledgeBase, Topic, utcnow
from app.config import offline_mode
from app.db import session_scope
from app.ingest.pipeline import start_run
from app.ingest.web import _run, _web_docs
from app.jobs.runner import JobContext
from app.keiro.client import KeiroError, WebResult, get_keiro
from app.lake.zones import raw_run_dir, slugify, write_raw_json, zone_dir
from app.sheets import clean, from_sources, service

log = logging.getLogger(__name__)
MAX_PAGES = 12
MIN_CHARS = 200


class TopicError(ValueError):
    pass


def _dict(t: Topic) -> dict:
    return {"id": t.id, "name": t.name, "question": t.question, "urls": t.urls, "sheet_id": t.sheet_id, "kb_id": t.kb_id, "status": t.status,
            "summary": t.summary, "created_at": t.created_at.isoformat(), "updated_at": t.updated_at.isoformat()}


def list_all() -> list[dict]:
    with session_scope() as s:
        return [_dict(t) for t in s.scalars(select(Topic).order_by(Topic.updated_at.desc()))]


def get(tid: str) -> dict:
    with session_scope() as s:
        t = s.get(Topic, tid)
        if t is None:
            raise KeyError(tid)
        return _dict(t)


def create(name: str, question: str = "", urls: list[str] | None = None) -> dict:
    name, question = name.strip()[:80], (question or "").strip()[:500]
    urls = [u for u in dict.fromkeys(urls or []) if u.lower().startswith(("http://", "https://"))][:MAX_PAGES]
    if not name:
        raise TopicError("Give the topic a name")
    question = question or f"Key facts about {name}"
    with session_scope() as s:
        if s.scalar(select(Topic).where(Topic.name == name)):
            raise TopicError("A topic with that name already exists")
        t = Topic(name=name, question=question, urls=urls)
        s.add(t)
        s.flush()
        return _dict(t)


def _set(tid: str, **fields) -> None:
    with session_scope() as s:
        t = s.get(Topic, tid)
        if t is not None:
            for k, v in fields.items():
                setattr(t, k, v)
            t.updated_at = utcnow()


async def delete(tid: str, remove_data: bool = False) -> bool:
    with session_scope() as s:
        t = s.get(Topic, tid)
        if t is None:
            return False
        sheet_id, kb_id = t.sheet_id, t.kb_id
        s.delete(t)
    if remove_data:
        if sheet_id:
            try:
                service.delete_sheet(sheet_id)
            except KeyError:
                pass
        if kb_id:
            try:
                await kb_service.delete_kb(kb_id)
            except Exception:  # noqa: BLE001 - already gone
                log.info("Collection %s was already removed", kb_id)
    return True


async def _pages(t: dict, ctx: JobContext) -> list[WebResult]:
    keiro = get_keiro()
    if offline_mode():
        raise TopicError("Offline mode is on, so the web can't be searched. Turn it off in Settings.")
    if not keiro.configured:
        raise TopicError("Add a KeiroLabs key in Settings to gather pages from the web")
    try:
        if t["urls"]:
            await ctx.progress(0.08, f"Reading {len(t['urls'])} pages")
            found = await keiro.extract(t["urls"])
        else:
            await ctx.progress(0.08, "Searching the web")
            found = await keiro.search(t["question"], max_results=MAX_PAGES, mode="content")
    except KeiroError as exc:
        raise TopicError(f"KeiroLabs: {exc}") from exc
    pages = [p for p in found if len((p.content or "").strip()) >= MIN_CHARS]
    if len(pages) < 2:
        raise TopicError(f"Only {len(pages)} readable page(s) came back. Try a more specific question or paste page addresses.")
    return pages[:MAX_PAGES]


def _save_files(t: dict, sheet_id: str, pages: list[WebResult]) -> dict:
    """A CSV and the sources list, in the lake's curated zone under topics/<name>/."""
    folder = zone_dir("curated") / "topics" / slugify(t["name"])
    folder.mkdir(parents=True, exist_ok=True)
    body, _, _ = service.export(sheet_id, "csv")
    (folder / f"{slugify(t['name'])}.csv").write_bytes(body)
    (folder / "sources.json").write_text(json.dumps([{"url": p.url, "title": p.title, "domain": p.domain} for p in pages], indent=2), encoding="utf-8")
    return {"folder": str(folder), "csv": f"{slugify(t['name'])}.csv"}


class _Sub:
    """Maps the indexing step's 0..1 progress into the last stretch of the whole job."""

    def __init__(self, ctx: JobContext):
        self.ctx = ctx

    async def progress(self, frac: float, message: str = "") -> None:
        await self.ctx.progress(0.65 + 0.3 * max(0.0, min(1.0, frac)), message or "Embedding pages")


def build_job(tid: str):
    async def job(ctx: JobContext) -> dict:
        t = get(tid)
        _set(tid, status="building", summary={})
        try:
            pages = await _pages(t, ctx)

            await ctx.progress(0.25, f"Making a sheet from {len(pages)} pages")
            if t["sheet_id"]:
                try:
                    service.delete_sheet(t["sheet_id"])
                except KeyError:
                    pass
            sheet = await from_sources.build_sheet(t["question"], pages, t["name"])

            await ctx.progress(0.55, "Cleaning the data")
            cleaned = await asyncio.to_thread(clean.apply, sheet["id"])

            await ctx.progress(0.65, "Keeping the pages searchable")
            try:
                kb_id = (await kb_service.create_kb(t["name"], f"Pages gathered for {t['name']}")).id
            except kb_service.KBExists:
                with session_scope() as s:
                    kb_id = s.scalar(select(KnowledgeBase.id).where(KnowledgeBase.name == t["name"]))
            source_id, run_id = start_run("topic", t["name"], kb_id, {"question": t["question"]})
            raw_dir = raw_run_dir("topic", run_id)
            write_raw_json(raw_dir, "pages", [p.to_dict() for p in pages])
            indexed = await _run(kb_id, source_id, run_id, raw_dir, _web_docs(raw_dir, pages, "page"), _Sub(ctx), {"topic": t["name"]})

            await ctx.progress(0.97, "Saving the CSV")
            files = await asyncio.to_thread(_save_files, t, sheet["id"], pages)
            summary = {"pages": len(pages), "rows": cleaned["rows_after"], "columns": len(sheet["columns"]), "cleaned": cleaned,
                       "indexed": {k: v for k, v in indexed.items() if isinstance(v, (int, float))}, **files,
                       "built_at": datetime.now(timezone.utc).isoformat()}
            _set(tid, status="ready", sheet_id=sheet["id"], kb_id=kb_id, summary=summary)
            return summary
        except Exception as exc:
            _set(tid, status="error", summary={"error": str(exc)[:400]})
            raise

    return job
