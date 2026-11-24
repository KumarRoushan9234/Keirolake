"""Tables as a local database: filtered reads, saved views, and finding rows by meaning.

Filters are `col:op:value`, joined with AND. Only known column names are used and every value is a bound parameter,
so a filter can never become SQL. Saved views are read-only SELECTs checked by the warehouse's SQL authorizer.
"""
from __future__ import annotations

import re

from sqlalchemy import select

from app.catalog.models import SavedView, TableIndex, utcnow
from app.db import readonly_connection, session_scope
from app.lake import warehouse
from app.sheets import service

OPS = {"eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "like": "LIKE"}
MAX_ROWS = 5000
VIEW_NAME = re.compile(r"^[a-z][a-z0-9_]{1,59}$")


class TableError(ValueError):
    pass


def _where(filters: list[str], columns: set[str]) -> tuple[str, list]:
    parts, args = [], []
    for f in filters:
        col, _, rest = f.partition(":")
        op, _, value = rest.partition(":")
        if col not in columns:
            raise TableError(f"No column called {col!r}")
        if op in OPS:
            parts.append(f"{service._q(col)} {OPS[op]} ?")
            args.append(value)
        elif op == "in":
            vals = [v for v in value.split(",") if v != ""][:200]
            if not vals:
                raise TableError("'in' needs at least one value")
            parts.append(f"{service._q(col)} IN ({', '.join('?' for _ in vals)})")
            args += vals
        elif op == "null":
            parts.append(f"{service._q(col)} IS {'NOT ' if value in ('false', '0', 'no') else ''}NULL")
        else:
            raise TableError(f"Unknown operator {op!r}; use {', '.join([*OPS, 'in', 'null'])}")
    return (" WHERE " + " AND ".join(parts)) if parts else "", args


def query(sheet_id: str, filters: list[str] | None = None, order: str = "", limit: int = 200, offset: int = 0) -> dict:
    info = service.get(sheet_id)
    columns = {c["name"] for c in info["columns"]}
    where, args = _where(filters or [], columns)
    desc = order.startswith("-")
    col = order.lstrip("-")
    if col and col not in columns:
        raise TableError(f"Can't order by {col!r}")
    order_sql = f" ORDER BY {service._q(col)} {'DESC' if desc else 'ASC'}" if col else " ORDER BY rowid"
    limit, offset = max(1, min(limit, MAX_ROWS)), max(0, offset)
    table = service._q(info["table"])
    conn = readonly_connection()
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM {table}{where}", args).fetchone()[0]
        cur = conn.execute(f"SELECT rowid AS _id, * FROM {table}{where}{order_sql} LIMIT ? OFFSET ?", [*args, limit, offset])
        names = [d[0] for d in cur.description]
        rows = [dict(zip(names, r)) for r in cur.fetchall()]
    finally:
        conn.close()
    return {"table": info["name"], "columns": [{"name": c["name"], "type": c["type"]} for c in info["columns"]], "rows": rows,
            "total": total, "limit": limit, "offset": offset}


def rows_by_id(sheet_id: str, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    info = service.get(sheet_id)
    conn = readonly_connection()
    try:
        cur = conn.execute(f"SELECT rowid AS _id, * FROM {service._q(info['table'])} WHERE rowid IN ({', '.join('?' for _ in ids)})", ids)
        names = [d[0] for d in cur.description]
        found = {r[0]: dict(zip(names, r)) for r in cur.fetchall()}
    finally:
        conn.close()
    return [found[i] for i in ids if i in found]


# ---------- saved views ----------
def list_views() -> list[dict]:
    with session_scope() as s:
        return [{"name": v.name, "sql": v.sql, "description": v.description, "created_at": v.created_at.isoformat()}
                for v in s.scalars(select(SavedView).order_by(SavedView.name))]


def save_view(name: str, sql: str, description: str = "") -> dict:
    name = name.strip().lower()
    if not VIEW_NAME.match(name):
        raise TableError("Name a view with lowercase letters, numbers and _ (2 to 60 characters, starting with a letter)")
    try:
        warehouse.run_query(sql, 1, user_tables_only=True)  # validates: one read-only SELECT over the user's tables
    except warehouse.SQLValidationError as exc:
        raise TableError(f"That query can't be saved: {exc}") from exc
    with session_scope() as s:
        v = s.get(SavedView, name)
        if v is None:
            v = SavedView(name=name, sql=sql.strip())
            s.add(v)
        v.sql, v.description = sql.strip(), description[:500]
    return {"name": name, "sql": sql.strip(), "description": description[:500]}


def run_view(name: str, limit: int = 1000) -> dict:
    with session_scope() as s:
        v = s.get(SavedView, name)
        if v is None:
            raise KeyError(name)
        sql = v.sql
    return warehouse.run_query(sql, limit, user_tables_only=True)


def delete_view(name: str) -> bool:
    with session_scope() as s:
        v = s.get(SavedView, name)
        if v is None:
            return False
        s.delete(v)
        return True


# ---------- finding rows by meaning ----------
def index_info(sheet_id: str) -> dict | None:
    with session_scope() as s:
        t = s.get(TableIndex, sheet_id)
        return {"kb_id": t.kb_id, "rows": t.rows, "indexed_at": t.indexed_at.isoformat()} if t else None


def row_text(row: dict) -> str:
    return "; ".join(f"{k}: {v}" for k, v in row.items() if not k.startswith("_") and v not in (None, ""))[:4000]


def index_job(sheet_id: str):
    """Embed every row (as 'column: value; ...') into a collection linked to the table. Rebuilt from scratch each time."""
    from app.catalog import kb as kb_service
    from app.ingest.pipeline import IncomingDoc, start_run
    from app.ingest.web import _run
    from app.lake.zones import raw_run_dir, write_raw_json

    async def job(ctx) -> dict:
        info = service.get(sheet_id)
        old = index_info(sheet_id)
        if old:
            try:
                await kb_service.delete_kb(old["kb_id"])
            except Exception:  # noqa: BLE001 - already gone
                pass
        await ctx.progress(0.05, f"Reading {info['name']}")
        rows, offset = [], 0
        while True:
            page = query(sheet_id, limit=MAX_ROWS, offset=offset)
            rows += page["rows"]
            offset += MAX_ROWS
            if offset >= page["total"]:
                break
        kb = await kb_service.create_kb(f"Rows of {info['name']} ({sheet_id[:6]})", f"Rows of the table {info['name']}, for search by meaning")
        source_id, run_id = start_run("table_index", info["name"], kb.id)
        raw_dir = raw_run_dir("table_index", run_id)
        raw = write_raw_json(raw_dir, "rows", rows[:50])
        docs = [IncomingDoc(f"{info['name']} row {r['_id']}", row_text(r), raw, "text/plain", f"row:{r['_id']}") for r in rows if row_text(r)]
        await _run(kb.id, source_id, run_id, raw_dir, docs, ctx, {"table": info["name"]})
        with session_scope() as s:
            t = s.get(TableIndex, sheet_id) or TableIndex(sheet_id=sheet_id, kb_id=kb.id)
            t.kb_id, t.rows, t.indexed_at = kb.id, len(docs), utcnow()
            s.merge(t)
        return {"rows": len(docs), "kb_id": kb.id}

    return job


async def search(sheet_id: str, q: str, top_k: int = 10) -> list[dict]:
    from app.rag.retrieve import retrieve

    info = index_info(sheet_id)
    if not info:
        raise TableError("This table isn't indexed yet. Index it first.")
    hits = (await retrieve(info["kb_id"], q, top_k)).hits
    ids = [int(h.url.split(":", 1)[1]) for h in hits if (h.url or "").startswith("row:")]
    scores = {int(h.url.split(":", 1)[1]): round(h.cosine or h.score, 4) for h in hits if (h.url or "").startswith("row:")}
    return [{**r, "_score": scores.get(r["_id"])} for r in rows_by_id(sheet_id, list(dict.fromkeys(ids)))]


async def drop_index(sheet_id: str) -> None:
    """When a table goes, its search collection goes with it."""
    from app.catalog import kb as kb_service

    with session_scope() as s:
        t = s.get(TableIndex, sheet_id)
        kb_id = t.kb_id if t else None
        if t:
            s.delete(t)
    if kb_id:
        try:
            await kb_service.delete_kb(kb_id)
        except Exception:  # noqa: BLE001 - already gone
            pass
