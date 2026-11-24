"""One controller for the engine's tasks.

answer       quick answer from the lake (and the web when needed)       → saved conversation, streamed
research     multi-round research with verified citations               → saved conversation, streamed
build_sheet  a sheet that fills itself from the web (KeiroLabs, ZDR)    → background job
fill_column  an AI or web column filled for every row                   → background job
ask_sheet    a question about one sheet, answered with SQL               → direct
row_dive     research about one row of a sheet                          → saved conversation, streamed

Streams and jobs keep running when the page is closed; conversations and jobs resume or are marked
after an engine restart (see chat.mark_interrupted and the job runner).
"""
from __future__ import annotations

TASKS = ("answer", "research", "build_sheet", "fill_column", "ask_sheet", "row_dive")


def row_question(sheet: dict, row: dict, focus: str = "") -> tuple[str, str]:
    """(conversation title, research question) for one row: identified by its first few values."""
    cols = [c["name"] for c in sheet["columns"]]
    values = [f"{c}: {row.get(c)}" for c in cols if row.get(c) not in (None, "")][:6]
    ident = " · ".join(str(row.get(c)) for c in cols if row.get(c) not in (None, ""))[:80] or f"row {row.get('_id')}"
    question = (f"Research this entry from my sheet “{sheet['name']}” ({'; '.join(values)}). "
                + (focus.strip() or "Give a well-sourced overview: what it is, key facts and numbers, recent developments, and anything that looks wrong in the row."))
    return f"Deep dive: {ident}", question


def row_dive(sheet_id: str, row_id: int, depth: str = "standard", focus: str = "") -> dict:
    """Start research about one row in a new conversation; returns its ids so the UI can follow it."""
    from app import chat
    from app.sheets import service

    sheet = service.get(sheet_id)
    page = service.rows(sheet_id, 0, 1000)
    row = next((r for r in page["rows"] if r["_id"] == row_id), None)
    if row is None:
        raise KeyError(row_id)
    title, question = row_question(sheet, row, focus)
    conv = chat.create(title=title)
    ids = chat.ask(conv["id"], question, depth=depth if depth in ("quick", "standard", "deep") else "standard")
    return {"conversation_id": conv["id"], **ids}
