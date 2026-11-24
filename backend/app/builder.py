"""Build a dataset or a library by talking: the model asks what you need, proposes the columns, fills the rows, then
asks what to add next. Works offline: the local model writes from what it knows, and from your own collections and
offline libraries when you allow them. The web is used only when you turn it on.

Two kinds:
  table       a sheet. Columns are agreed in the chat, then rows are written in batches and every cell records where it
              came from (a passage from your lake, or "model knowledge" with low confidence).
  collection  a small offline library: reference notes written section by section into a searchable collection.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from app.llm.base import ChatMessage
from app.llm.jsonfix import loads_lenient

log = logging.getLogger(__name__)
TYPES = ("text", "number", "date", "url", "boolean")
MAX_ROWS = 200
BATCH = 10
MAX_NOTES = 20


class BuilderError(ValueError):
    pass


SCRIPTS = {"CJK": re.compile(r"[぀-ヿ㐀-鿿가-힯]"), "Cyrillic": re.compile(r"[Ѐ-ӿ]"),
           "Arabic": re.compile(r"[؀-ۿ]"), "Devanagari": re.compile(r"[ऀ-ॿ]")}


def _scripts(text: str) -> set[str]:
    return {name for name, rx in SCRIPTS.items() if rx.search(text or "")}


def language_of(text: str) -> str:
    """The language to write in, from the person's own words (small local models drift into other languages)."""
    found = _scripts(text)
    if "Devanagari" in found:
        return "Hindi"
    if "CJK" in found:
        return "the person's language (Chinese, Japanese or Korean, as they wrote)"
    return "English" if not found else "the same language the person wrote in"


def _foreign(row: dict, allowed: set[str]) -> bool:
    """A row written in a script the person didn't use."""
    return any(_scripts(str(v)) - allowed for v in row.values() if isinstance(v, str))


def _router():
    from app.llm.registry import get_router

    return get_router()


CHAT_SYSTEM = """You help a person build a {kind} in their private data lake, by conversation. Reply in the person's language.
Talk like a helpful colleague: short, plain sentences. Ask at most two questions at a time.

For a table: agree on a name, the columns (3 to 10, snake_case names, each with a type from text, number, date, url,
boolean and a one-line description), and roughly how many rows. Put the most identifying column first (it is the key).
For a collection (an offline library of notes): agree on a name and 4 to 12 section titles to write.

Always answer with one JSON object and nothing else:
{{"reply": "what you say to the person",
  "draft": {{"name": "...", "columns": [{{"name": "...", "type": "text", "description": "..."}}], "sections": ["..."], "rows": 20}},
  "ready": true or false}}
Keep the draft up to date with everything agreed so far (fields you don't need can be empty). Set "ready" to true only
when the person has agreed and the draft is complete enough to start filling."""


def _clean_draft(raw: dict, kind: str) -> dict:
    name = str(raw.get("name") or "").strip()[:80]
    cols, seen = [], set()
    for c in raw.get("columns") or []:
        if not isinstance(c, dict):
            continue
        n = re.sub(r"[^a-z0-9_]+", "_", str(c.get("name", "")).strip().lower()).strip("_")[:40]
        if not n or n in seen:
            continue
        seen.add(n)
        cols.append({"name": n, "type": c.get("type") if c.get("type") in TYPES else "text", "description": str(c.get("description") or "")[:200]})
    sections = [str(s).strip()[:120] for s in (raw.get("sections") or []) if str(s).strip()][:MAX_NOTES]
    try:
        rows = max(1, min(int(raw.get("rows") or 20), MAX_ROWS))
    except (TypeError, ValueError):
        rows = 20
    return {"name": name, "columns": cols[:12], "sections": sections, "rows": rows, "kind": kind}


async def turn(kind: str, messages: list[dict], draft: dict | None = None, local_only: bool = False) -> dict:
    """One step of the conversation: the model's reply, the updated draft, and whether it is ready to fill."""
    if kind not in ("table", "collection"):
        raise BuilderError("Build a table or a collection")
    history = [ChatMessage("system", CHAT_SYSTEM.format(kind="table" if kind == "table" else "collection (an offline library of notes)"))]
    if draft:
        history.append(ChatMessage("system", "Current draft: " + json.dumps(draft)[:3000]))
    for m in messages[-16:]:
        if m.get("role") in ("user", "assistant") and str(m.get("content", "")).strip():
            history.append(ChatMessage(m["role"], str(m["content"])[:4000]))
    result = await _router().complete(history, temperature=0.3, max_tokens=900, local_only=local_only)
    data = loads_lenient(result.text, want=dict) or {}
    reply = str(data.get("reply") or "").strip() or result.text.strip()[:800]
    new = _clean_draft(data.get("draft") or draft or {}, kind)
    ready = bool(data.get("ready")) and bool(new["name"]) and (bool(new["columns"]) if kind == "table" else bool(new["sections"]))
    return {"reply": reply, "draft": new, "ready": ready, "model": f"{result.provider}:{result.model}"}


async def _context(query: str, use_lake: bool, use_libraries: bool, use_web: bool = False) -> tuple[str, list[str]]:
    """Passages for this request from the person's own data (and the web when allowed), and where they came from."""
    parts, sources = [], []
    if use_lake:
        try:
            from app.rag.retrieve import retrieve_all

            from app.config import get_settings

            hits, _ = await retrieve_all(query, 6)
            relevant = get_settings().rag_web_threshold  # only passages that are really about this (not just the nearest ones)
            for h in hits:
                if (h.cosine or 0) < relevant:
                    continue
                parts.append(f"[{h.title}] {h.text[:900]}")
                sources.append(h.url or h.title)
        except Exception as exc:  # noqa: BLE001 - the model can still write from what it knows
            log.info("Lake search skipped: %s", exc)
    if use_libraries:
        try:
            from app.library import service as library

            hits, _ = await library.retrieve(query, 4)
            for h in hits:
                parts.append(f"[{h.library}: {h.title}] {h.text[:900]}")
                sources.append(f"{h.library}: {h.title}")
        except Exception as exc:  # noqa: BLE001
            log.info("Library search skipped: %s", exc)
    if use_web:
        try:
            from app.keiro.client import get_keiro

            client = get_keiro()
            if client.configured:
                for r in await client.search(query, max_results=5, mode="content"):
                    text = (r.content or r.snippet or "").strip()
                    if text:
                        parts.append(f"[{r.title or r.domain}] {text[:1200]}")
                        sources.append(r.url)
        except Exception as exc:  # noqa: BLE001 - offline, no key or out of credits: write from what is known
            log.info("Web search skipped: %s", exc)
    return "\n\n".join(parts)[:9000], sources


ROWS_SYSTEM = """You fill rows for a table in the person's data lake. Write every value in {language}. Use the reference
passages when they contain the facts; otherwise use what you reliably know. Never invent precise numbers, dates or addresses you are unsure of: leave
those cells null. Answer with a JSON array of row objects only, using exactly these keys: {keys}."""


async def _rows_batch(draft: dict, brief: str, context: str, have: list[str], n: int, local_only: bool = False) -> list[dict]:
    keys = [c["name"] for c in draft["columns"]]
    spec = "\n".join(f"- {c['name']} ({c['type']}): {c['description']}" for c in draft["columns"])
    avoid = ", ".join(have[-60:])
    user = (f"Table: {draft['name']}\nWhat it is for: {brief}\nColumns:\n{spec}\n\n"
            f"Write {n} new rows.{' Do not repeat these: ' + avoid if avoid else ''}\n\n"
            f"Reference passages:\n{context or '(none: use what you know, and leave uncertain cells null)'}")
    allowed = _scripts(brief)
    system = ROWS_SYSTEM.format(keys=", ".join(keys), language=language_of(brief))
    rows: list[dict] = []
    for attempt in range(2):  # one more try if the model drifts into another language
        result = await _router().complete([ChatMessage("system", system), ChatMessage("user", user)], temperature=0.3 if attempt else 0.4,
                                          max_tokens=2500, local_only=local_only)
        data = loads_lenient(result.text, want=list) or []
        got = [{k: r.get(k) for k in keys} for r in (data if isinstance(data, list) else []) if isinstance(r, dict)]
        known = {str(r.get(keys[0])).lower() for r in rows}
        for r in got:
            if not _foreign(r, allowed) and str(r.get(keys[0])).lower() not in known:
                rows.append(r)
                known.add(str(r.get(keys[0])).lower())
        if len(rows) >= n or len(got) == 0:
            break
        user += f"\n\nWrite only in {language_of(brief)}."
    return rows[:n]


def fill_table_job(draft: dict, brief: str, count: int, use_lake: bool, use_libraries: bool, sheet_id: str | None = None, local_only: bool = False,
                   use_web: bool = False):
    """Create the table (or extend it) and fill `count` rows in batches, recording where each row came from."""
    from app.sheets import clean, service

    count = max(1, min(count, MAX_ROWS))

    async def job(ctx) -> dict:
        sid = sheet_id
        if sid is None:
            sheet = service.create_blank(draft["name"], [{"name": c["name"], "type": c["type"]} for c in draft["columns"]])
            sid = sheet["id"]
            with_desc = brief[:1500] or draft["name"]
            service.rename(sid, sheet["name"], f"Built in conversation: {with_desc}")
        info = service.get(sid)
        key = draft["columns"][0]["name"]
        have = [str(r.get(key)) for r in service.rows(sid, 0, 1000)["rows"] if r.get(key) is not None]
        await ctx.progress(0.05, "Reading your data for this table")
        context, sources = await _context(f"{draft['name']}. {brief}", use_lake, use_libraries, use_web)
        added = 0
        while added < count:
            n = min(BATCH, count - added)
            await ctx.progress(0.1 + 0.85 * added / count, f"Writing rows {added + 1} to {added + n}")
            rows = await _rows_batch(draft, brief, context, have, n, local_only)
            fresh, seen = [], set(have)
            for r in rows:  # new keys only, and each key once
                k = str(r.get(key) or "").strip()
                if k and k.lower() not in {x.lower() for x in seen}:
                    fresh.append(r)
                    seen.add(k)
            rows = fresh
            if not rows:
                break
            ids = service.add_rows(sid, rows)
            for rid in ids:  # where each row came from: the passages used, or the model's own knowledge
                for c in draft["columns"]:
                    service.set_cell_meta(info["table"], rid, c["name"], sources[:3] or ["model knowledge"], 0.8 if sources else 0.4, "builder")
            have += [str(r[key]) for r in rows]
            added += len(rows)
        await asyncio.to_thread(clean.apply, sid)
        return {"sheet_id": sid, "added": added, "sources": len(sources), "note": "" if added >= count else "The model ran out of new rows to add."}

    return job


NOTES_SYSTEM = """You write one section of an offline reference library for the person, in the language they used. Use the reference passages
when they help; otherwise write from what you reliably know, and say plainly when something is uncertain. Plain
Markdown, 200 to 450 words, starting with the section title as a heading."""


def fill_collection_job(draft: dict, brief: str, use_lake: bool, use_libraries: bool, kb_id: str | None = None, local_only: bool = False,
                        use_web: bool = False):
    """Write each agreed section as a note and add it to a searchable collection."""
    from app.catalog import kb as kb_service
    from app.ingest import web as ingest_web

    async def job(ctx) -> dict:
        target = kb_id
        if target is None:
            target = (await kb_service.create_kb(draft["name"], f"Written in conversation: {brief[:300]}")).id
        sections = draft["sections"][:MAX_NOTES]
        written = 0
        for i, title in enumerate(sections):
            await ctx.progress(0.05 + 0.9 * i / max(1, len(sections)), f"Writing: {title}")
            context, _ = await _context(f"{draft['name']}: {title}", use_lake, use_libraries, use_web)
            result = await _router().complete([ChatMessage("system", NOTES_SYSTEM), ChatMessage(
                "user", f"Library: {draft['name']}\nWhat it is for: {brief}\nSection: {title}\n\nReference passages:\n{context or '(none)'}")],
                temperature=0.4, max_tokens=1200, local_only=local_only)
            text = result.text.strip()
            if len(text) > 80:
                await ingest_web.text_job(target, f"{draft['name']}: {title}", text)(ctx)
                written += 1
        return {"kb_id": target, "notes": written}

    return job
