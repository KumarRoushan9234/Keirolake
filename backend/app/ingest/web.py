"""Job factories for every ingestion path (files, text, URLs, Keiro web search, feeds)."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.catalog.models import Feed
from app.db import session_scope
from app.ingest.parsers import UnsupportedFile, parse_bytes
from app.ingest.pipeline import IncomingDoc, finish_run, ingest_docs, start_run
from app.jobs.runner import JobContext
from app.keiro.client import DeepResult, WebResult, get_keiro
from app.lake.zones import raw_run_dir, relative, write_raw_bytes, write_raw_json

log = logging.getLogger(__name__)


async def _run(kb_id: str, source_id: str, run_id: str, raw_dir: Path, docs: list[IncomingDoc], ctx: JobContext,
               extra_stats: dict | None = None) -> dict:
    try:
        stats = await ingest_docs(kb_id, source_id, run_id, docs, ctx.progress)
        stats.update(extra_stats or {})
        finish_run(run_id, raw_path=relative(raw_dir), stats=stats)
        return {"run_id": run_id, **stats}
    except Exception as exc:
        finish_run(run_id, raw_path=relative(raw_dir), error=str(exc))
        raise


TABULAR = (".csv", ".tsv", ".xlsx", ".xls")


def _sheet_from_file(name: str, data: bytes) -> dict | None:
    """Turn an uploaded table file into a cleaned sheet. A file that can't be read as a table simply stays a document."""
    from app.sheets import clean, service

    try:
        sheet = service.import_file(name.rsplit(".", 1)[0], name, data, source="import")
        report = clean.apply(sheet["id"])
        return {"id": sheet["id"], "name": sheet["name"], "rows": report["rows_after"]}
    except Exception as exc:  # noqa: BLE001 - never block the document import
        log.info("No sheet made from %s: %s", name, exc)
        return None


def _sheets_from_pdf(name: str, data: bytes) -> list[dict]:
    import csv
    import io as _io

    from app.ingest.parsers import pdf_tables

    try:
        found = pdf_tables(data)
    except Exception as exc:  # noqa: BLE001 - a PDF without readable tables stays a document
        log.info("No tables read from %s: %s", name, exc)
        return []
    made = []
    for i, rows in enumerate(found, 1):
        buf = _io.StringIO()
        csv.writer(buf).writerows(rows)
        sheet = _sheet_from_file(f"{name.rsplit('.', 1)[0]} table {i}.csv", buf.getvalue().encode("utf-8"))
        if sheet:
            made.append(sheet)
    return made


def stage_uploads(kb_id: str, files: list[tuple[str, bytes]]):
    """Write uploads to the raw zone immediately, then return the job to parse and index them."""
    source_id, run_id = start_run("upload", ", ".join(name for name, _ in files)[:200], kb_id)
    raw_dir = raw_run_dir("upload", run_id)
    staged = [(name, write_raw_bytes(raw_dir, name, data)) for name, data in files]

    async def job(ctx: JobContext) -> dict:
        docs, failures, tables = [], [], []
        for name, path in staged:
            try:
                parsed = parse_bytes(name, path.read_bytes())
                docs.append(IncomingDoc(parsed.title, parsed.text, path, parsed.mime, None, parsed.meta))
            except (UnsupportedFile, ValueError) as exc:
                failures.append(f"{name}: {exc}")
            if name.lower().endswith(TABULAR):  # a spreadsheet-like file is also a table: make the sheet and tidy it
                made = await asyncio.to_thread(_sheet_from_file, name, path.read_bytes())
                if made:
                    tables.append(made)
            elif name.lower().endswith(".pdf"):  # tables printed inside a PDF become sheets too
                tables += await asyncio.to_thread(_sheets_from_pdf, name, path.read_bytes())
        return await _run(kb_id, source_id, run_id, raw_dir, docs, ctx, {"parse_failures": failures, "sheets_created": len(tables), "sheets": tables})

    return job


def text_job(kb_id: str, title: str, text: str):
    source_id, run_id = start_run("text", title, kb_id)
    raw_dir = raw_run_dir("text", run_id)
    raw = write_raw_bytes(raw_dir, f"{title}.md", text.encode())

    async def job(ctx: JobContext) -> dict:
        return await _run(kb_id, source_id, run_id, raw_dir, [IncomingDoc(title, text, raw, "text/markdown")], ctx)

    return job


def _web_docs(raw_dir: Path, results: list[WebResult], prefix: str) -> list[IncomingDoc]:
    docs = []
    for i, r in enumerate(results):
        body = r.content or r.excerpt or r.snippet
        if not body:
            continue
        raw = write_raw_json(raw_dir, f"{prefix}_{i}_{r.domain or 'page'}", r.to_dict())
        docs.append(IncomingDoc(r.title or r.url, body, raw, "text/markdown", r.url,
                                {"domain": r.domain, "content": bool(r.content), "position": r.position}))
    return docs


def urls_job(kb_id: str, urls: list[str]):
    """Fetch clean markdown for known URLs via Keiro /extract."""
    source_id, run_id = start_run("url", ", ".join(urls)[:200], kb_id, {"urls": urls})
    raw_dir = raw_run_dir("url", run_id)

    async def job(ctx: JobContext) -> dict:
        await ctx.progress(0.05, f"Extracting {len(urls)} URL(s) with KeiroLabs")
        results = await get_keiro().extract(urls)
        write_raw_json(raw_dir, "keiro_extract_response", [r.to_dict() for r in results])
        for r in results:
            r.title = r.content.lstrip("# ").split("\n", 1)[0][:200] or r.url
        docs = _web_docs(raw_dir, results, "page")
        return await _run(kb_id, source_id, run_id, raw_dir, docs, ctx, {"urls_requested": len(urls), "urls_extracted": len(results)})

    return job


def web_search_job(kb_id: str, query: str, max_results: int = 5, mode: str = "content", source_kind: str = "web_search"):
    """Keiro search -> raw JSON -> index. mode=content brings full page markdown;
    lite/fast index snippets only (cheap, good for news-style feeds)."""
    source_id, run_id = start_run(source_kind, query, kb_id, {"query": query, "mode": mode, "max_results": max_results})
    raw_dir = raw_run_dir(source_kind, run_id)

    async def job(ctx: JobContext) -> dict:
        await ctx.progress(0.05, f"Searching the web ({mode}): {query}")
        results = await get_keiro().search(query, max_results=max_results, mode=mode)
        write_raw_json(raw_dir, "keiro_search_response", {"query": query, "mode": mode, "results": [r.to_dict() for r in results]})
        docs = _web_docs(raw_dir, results, "result")
        return await _run(kb_id, source_id, run_id, raw_dir, docs, ctx, {"query": query, "web_results": len(results)})

    return job


def dossier_markdown(deep: DeepResult) -> str:
    """One readable document per research question: evidence first, then ranked sources."""
    lines = [f"Question: {deep.query}", ""]
    if deep.confidence is not None:
        lines.append(f"Answer status: {deep.answer_status or 'unknown'} (confidence {deep.confidence})")
    if deep.agreeing_domains:
        lines.append(f"Sources that agree: {', '.join(deep.agreeing_domains)}")
    if deep.evidence:
        lines += ["", "## Evidence", ""] + [f"- {e.strip()}" for e in deep.evidence]
    lines += ["", "## Sources, most likely to answer first", ""]
    for r in deep.results:
        score = f" (likelihood {r.likelihood:.2f})" if r.likelihood is not None else ""
        lines.append(f"- [{r.title or r.url}]({r.url}){score}")
        if r.excerpt:
            lines.append(f"  > {r.excerpt.strip()}")
    return "\n".join(lines)


def research_job(kb_id: str, question: str, max_pages: int = 5):
    """Keiro deep search -> a dossier document plus the top pages in full, all indexed."""
    source_id, run_id = start_run("research", question, kb_id, {"question": question, "max_pages": max_pages})
    raw_dir = raw_run_dir("research", run_id)

    async def job(ctx: JobContext) -> dict:
        await ctx.progress(0.05, f"Researching (takes 15-50 s): {question}")
        deep = await get_keiro().deep(question, content=True)
        write_raw_json(raw_dir, "keiro_deep_response", {
            "query": deep.query, "answer_status": deep.answer_status, "confidence": deep.confidence,
            "agreeing_domains": deep.agreeing_domains, "evidence": deep.evidence,
            "results": [r.to_dict() for r in deep.results]})
        dossier = dossier_markdown(deep)
        dossier_raw = write_raw_bytes(raw_dir, "dossier.md", dossier.encode())
        docs = [IncomingDoc(f"Research: {question[:150]}", dossier, dossier_raw, "text/markdown", None,
                            {"kind": "dossier", "confidence": deep.confidence, "sources": len(deep.results)})]
        docs += _web_docs(raw_dir, deep.results[:max_pages], "page")
        return await _run(kb_id, source_id, run_id, raw_dir, docs, ctx,
                          {"question": question, "confidence": deep.confidence, "web_results": len(deep.results)})

    return job


def site_job(kb_id: str, site: str, path_prefix: str = "", limit: int = 20):
    """Keiro sitemap -> newest pages of a site -> extract -> index."""
    source_id, run_id = start_run("site", f"{site}{path_prefix}", kb_id, {"site": site, "path_prefix": path_prefix, "limit": limit})
    raw_dir = raw_run_dir("site", run_id)

    async def job(ctx: JobContext) -> dict:
        keiro = get_keiro()
        await ctx.progress(0.03, f"Reading the sitemap of {site}")
        urls = [r["url"] for r in await keiro.sitemap(site, path_prefix, limit=limit)][:limit]
        write_raw_json(raw_dir, "keiro_sitemap_response", {"site": site, "path_prefix": path_prefix, "urls": urls})
        if not urls:
            raise ValueError(f"No URLs found in the sitemap of {site}{path_prefix}")
        pages = []
        for start in range(0, len(urls), 10):
            batch = urls[start : start + 10]
            await ctx.progress(0.05 + 0.45 * start / len(urls), f"Extracting pages {start + 1}-{start + len(batch)} of {len(urls)}")
            pages += await keiro.extract(batch)
        for r in pages:
            r.title = r.content.lstrip("# ").split("\n", 1)[0][:200] or r.url
        write_raw_json(raw_dir, "keiro_extract_response", [r.to_dict() for r in pages])
        docs = _web_docs(raw_dir, pages, "page")
        return await _run(kb_id, source_id, run_id, raw_dir, docs, ctx, {"site": site, "urls": len(urls), "pages_extracted": len(pages)})

    return job


def feed_job(feed_id: str):
    async def job(ctx: JobContext) -> dict:
        with session_scope() as s:
            feed = s.get(Feed, feed_id)
            if feed is None:
                return {"skipped": "feed deleted"}
            kb_id, query, mode, n = feed.kb_id, feed.query, feed.mode, feed.max_results
        # Content dedup (sha256 per KB) makes re-running a feed idempotent.
        return await web_search_job(kb_id, query, n, mode, source_kind="feed")(ctx)

    return job
