"""Fill a column for every row: AI columns use the row itself, web columns look each row up with KeiroLabs (ZDR).

The model is the local-first router; each cell gets its sources and the model's own confidence.
Rows are processed in small parallel batches and progress is reported per batch.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from app.llm.base import ChatMessage
from app.sheets import service

log = logging.getLogger(__name__)
PARALLEL = 3
MAX_ROWS = 2000
WEB_RESULTS = 3

PROMPT = """You fill one cell of a spreadsheet. {use}
Reply with ONLY a JSON object: {{"value": <the cell value, or null if it can't be determined>, "confidence": <0 to 1>}}
Keep text values short (a word, number, date or short phrase) unless the instruction asks for more.
Never invent facts. {rule}"""

AI_USE = "Use the row as context together with your general knowledge."
AI_RULE = "Answer when you are fairly sure and set the confidence honestly; use null only if you don't know or it can't be known."
WEB_USE = "Use only the row data and the web results given."
WEB_RULE = ("The web results are about this row's subject. Read them and copy the requested value as the results state it "
            "(include the year or unit if the instruction asks); if results differ, take the most recent from the most official "
            "source and lower the confidence. Use null only if no result mentions it.")


def system_prompt(web: bool) -> str:
    return PROMPT.format(use=WEB_USE if web else AI_USE, rule=WEB_RULE if web else AI_RULE)


def _identity(row: dict, skip: str) -> str:
    """The row's key: its first filled value. Searching with every column would drag in unrelated names (a capital for a country)."""
    for k, v in row.items():
        if k not in (skip, "_id", "_meta") and v not in (None, "") and len(str(v)) < 160:
            return str(v)
    return ""


def _parse(text: str) -> tuple[object, float | None]:
    from app.llm.jsonfix import loads_lenient

    data = loads_lenient(text)
    if data is None:
        plain = text.strip().strip("`\"'")
        return (plain, None) if plain and len(plain) <= 120 and "\n" not in plain and plain.lower() != "null" else (None, None)
    conf = data.get("confidence")
    return data.get("value"), float(conf) if isinstance(conf, (int, float)) else None


async def _best_passages(query: str, results) -> tuple[str, list[str]]:
    """The passages of the search results that best match the instruction (the whole page rarely fits), with their URLs."""
    from app.engine.evidence import Passage, Pool, context
    from app.ingest.chunking import chunk_text

    pool = Pool()
    for r in results:
        text = r.content or r.snippet or ""
        for chunk in chunk_text(text, 900, 100)[:12]:
            pool.add([Passage(chunk, r.url, r.title, "web")])
    ranked = await pool.rank(query)
    best = ranked[:6]
    used = {p.url for p in best if p.url}
    return context(best, 5000, per_source=3), [u for u in dict.fromkeys(r.url for r in results if r.url) if u in used]


def estimate(sheet_id: str, column: str, only_empty: bool = True) -> dict:
    info = service.get(sheet_id)
    col = next((c for c in info["columns"] if c["name"] == column), None)
    if col is None:
        raise service.SheetError(f"No column {column}")
    page = service.rows(sheet_id, 0, MAX_ROWS)
    todo = [r for r in page["rows"] if not only_empty or r.get(column) in (None, "")]
    return {"rows": len(todo), "kind": col["kind"], "keirolabs_calls": len(todo) if col["kind"] == "web" else 0}


def fill_job(sheet_id: str, column: str, only_empty: bool = True):
    async def job(ctx) -> dict:
        from app.keiro.client import KeiroError, get_keiro
        from app.llm.registry import get_router

        info = service.get(sheet_id)
        col = next((c for c in info["columns"] if c["name"] == column), None)
        if col is None or col["kind"] == "plain" or not col["prompt"]:
            raise service.SheetError("Pick an AI or web column with an instruction")
        web = col["kind"] == "web"
        keiro = get_keiro() if web else None
        if web and not keiro.configured:
            raise service.SheetError("Web columns need a KeiroLabs key (Models page)")
        todo = [r for r in service.rows(sheet_id, 0, MAX_ROWS)["rows"] if not only_empty or r.get(column) in (None, "")]
        router = get_router()
        done = filled = 0
        errors: list[str] = []
        sem = asyncio.Semaphore(PARALLEL)

        async def one(row: dict) -> None:
            nonlocal done, filled
            async with sem:
                data = {k: v for k, v in row.items() if k not in ("_id", "_meta", column)}
                sources, snippets = [], ""
                if web:
                    query = f"{_identity(row, column)} {col['prompt']}"[:300]
                    try:
                        results = await keiro.search(query, WEB_RESULTS, "content")
                        snippets, sources = await _best_passages(query, results)
                    except KeiroError as exc:
                        errors.append(f"row {row['_id']}: {exc}")
                ask = f"Column: {column} ({col['type']})\nInstruction: {col['prompt']}\nRow: {json.dumps(data, default=str)[:3000]}"
                if web:
                    ask += f"\n\nWeb results:\n{snippets or '(none found)'}"
                try:
                    value, confidence, reply = None, None, None
                    for attempt in range(2):  # a small local model sometimes answers null or garbles the JSON: ask once more
                        reply = await router.complete([ChatMessage("system", system_prompt(web)),
                                                       ChatMessage("user", ask)], temperature=0 if attempt == 0 else 0.3, max_tokens=200)
                        value, confidence = _parse(reply.text)
                        if value not in (None, ""):
                            break
                    if value is not None and value != "":
                        service.update_cell(sheet_id, row["_id"], column, value if not isinstance(value, (dict, list)) else json.dumps(value))
                        service.set_cell_meta(info["table"], row["_id"], column, sources, confidence, f"{reply.provider}:{reply.model}")
                        filled += 1
                    else:
                        errors.append(f"row {row['_id']}: no value found" + (" in the web results" if web else ""))
                except service.SheetError as exc:
                    errors.append(f"row {row['_id']}: {exc}")
                except Exception as exc:  # noqa: BLE001 - one bad row must not stop the column
                    errors.append(f"row {row['_id']}: {exc}")
                done += 1
                if done % 5 == 0 or done == len(todo):
                    await ctx.progress(done / max(1, len(todo)), f"Filled {filled} of {done} rows")

        await asyncio.gather(*(one(r) for r in todo))
        if todo:
            service.snapshot(sheet_id)
        return {"column": column, "rows": len(todo), "filled": filled, "errors": errors[:10]}

    return job
