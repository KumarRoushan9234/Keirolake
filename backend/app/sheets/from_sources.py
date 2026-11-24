"""Turn sources into a sheet: found pages, a research answer's evidence, or URLs you paste.

1. The model looks at what the sources are about and the question, and proposes columns.
2. Each source is read once (the same extraction used by builds) and becomes a row.
3. The rows land in a new sheet, every cell linked back to the page it came from.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from sqlalchemy import select

from app import pipelines
from app.catalog.models import EvidenceRow, Sheet
from app.db import session_scope
from app.keiro.client import WebResult
from app.llm.base import ChatMessage
from app.llm.jsonfix import loads_lenient
from app.lake.zones import slugify
from app.sheets import clean, service

log = logging.getLogger(__name__)
MAX_SOURCES = 15
MAX_COLUMNS = 10

DESIGN = """You design the columns of a spreadsheet that will hold facts from several sources.
Reply with ONLY JSON: {"name": "short sheet name", "columns": [{"name": "snake_case", "type": "text|number|date|boolean|url", "description": "what goes in it"}]}
Pick 3 to 8 columns that make the sources comparable (names, amounts, dates, places, ratings...), shaped by the question.
Never add columns for the source URL or title; those are added automatically."""


class FromSourcesError(ValueError):
    pass


def pages_from_evidence(urls: list[str]) -> list[WebResult]:
    """Stored research evidence, grouped per URL, as pages."""
    out: dict[str, WebResult] = {}
    with session_scope() as s:
        for row in s.scalars(select(EvidenceRow).where(EvidenceRow.url.in_(urls)).order_by(EvidenceRow.id)):
            page = out.setdefault(row.url, WebResult(row.url, row.title or row.url, content=""))
            page.content += ("\n\n" if page.content else "") + row.text
    return list(out.values())[:MAX_SOURCES]


async def pages_from_urls(urls: list[str]) -> list[WebResult]:
    from app.keiro.client import get_keiro

    keiro = get_keiro()
    if not keiro.configured:
        raise FromSourcesError("Reading web pages needs a KeiroLabs key (Models page)")
    clean = [u.strip() for u in urls if u.strip().lower().startswith(("http://", "https://"))][:MAX_SOURCES]
    if not clean:
        raise FromSourcesError("Paste full page addresses starting with http:// or https://")
    return await keiro.extract(clean)


async def design_columns(question: str, pages: list[WebResult]) -> tuple[str, list[dict]]:
    from app.llm.registry import get_router

    sample = "\n\n".join(f"[{i + 1}] {p.title or p.url}\n{(p.content or p.snippet)[:700]}" for i, p in enumerate(pages[:6]))
    reply = await get_router().complete([ChatMessage("system", DESIGN), ChatMessage("user", f"QUESTION: {question}\n\nSOURCES:\n{sample}")],
                                        temperature=0.1, max_tokens=700)
    data = loads_lenient(reply.text) or {}
    cols, seen = [], {"source_url", "title", "collected_at"}
    for c in (data.get("columns") or [])[:MAX_COLUMNS]:
        if not isinstance(c, dict):
            continue
        name = slugify(str(c.get("name", "")), 40)
        ctype = c.get("type") if c.get("type") in service.TYPES else "text"
        if name and name not in seen:
            seen.add(name)
            cols.append({"name": name, "type": ctype, "description": str(c.get("description", ""))[:200]})
    if not cols:
        cols = [{"name": "summary", "type": "text", "description": "One-sentence summary of what the source says about the question"},
                {"name": "key_facts", "type": "text", "description": "The most important facts, numbers or names"}]
    return str(data.get("name") or question)[:60], cols


async def build_sheet(question: str, pages: list[WebResult], name: str = "") -> dict:
    pages = [p for p in pages if len((p.content or p.snippet or "").strip()) >= 80]
    if not pages:
        raise FromSourcesError("None of those sources had text to read")
    title, cols = await design_columns(question, pages)
    spec = {"fields": cols, "instructions": f"Collect what each source says in answer to: {question}"[:600]}
    rows, errors = await pipelines.extract_rows(spec, pages)
    if not rows:
        raise FromSourcesError("No rows could be read from those sources" + (f" ({pipelines.summarize_problems(errors)[0]})" if errors else ""))
    sheet = service.create_blank((name or title).strip()[:80] or "Sources",
                                 [{"name": "source_url", "type": "url"}, {"name": "title", "type": "text"},
                                  *[{"name": c["name"], "type": c["type"]} for c in cols]])
    ids = service.add_rows(sheet["id"], [{k: v for k, v in r.items() if k != "collected_at"} for r in rows])
    for rid, r in zip(ids, rows):
        for c in cols:
            if r.get(c["name"]) is not None:
                service.set_cell_meta(sheet["table"], rid, c["name"], [r["source_url"]], None, "extracted")
    with session_scope() as s:
        record = s.get(Sheet, sheet["id"])
        record.source, record.description = "research", f"From {len(pages)} sources: {question}"[:300]
    try:  # tidy what was just gathered (spaces, N/A, number and date columns, duplicates); a failure leaves the raw sheet
        report = await asyncio.to_thread(clean.apply, sheet["id"])
        rows = rows[: report["rows_after"]]
    except Exception:  # noqa: BLE001
        pass
    service.snapshot(sheet["id"])
    return {**service.get(sheet["id"]), "rows": service.get(sheet["id"])["rows"] or len(rows), "problems": pipelines.summarize_problems(errors)}
