"""Sheets on top of SQLite tables.

Every user table (ds_*, cur_*) is a sheet. The `sheets` row keeps what SQLite can't:
the display name, column kinds (plain, ai, web) with their prompts, where the sheet
came from, and the build (pipeline) that refreshes it. Rows are addressed by SQLite's
rowid. Identifiers are always slugified and quoted; values always go in as parameters.
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select

from app.catalog.models import Pipeline, Sheet, SheetCellMeta, utcnow
from app.db import readonly_connection, session_scope, writer_connection
from app.lake import warehouse
from app.lake.zones import slugify, zone_dir

TYPES = {"text": "TEXT", "number": "REAL", "date": "TEXT", "boolean": "INTEGER", "url": "TEXT"}
KINDS = ("plain", "ai", "web")
PAGE_MAX = 1000
KEEP_SNAPSHOTS = 10
EXPORT_FORMATS = ("csv", "xlsx", "json", "jsonl", "pdf")


class SheetError(ValueError):
    pass


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _col_name(name: str) -> str:
    col = slugify(str(name), 60)
    if not col or col.startswith("_") or col in {"rowid", "oid"}:
        raise SheetError("Column names need letters or numbers and can't start with _")
    return col


def _infer_type(sqlite_type: str) -> str:
    t = (sqlite_type or "").upper()
    return "number" if any(x in t for x in ("REAL", "INT", "NUM", "FLOA", "DOUB")) else "text"


def _table_columns(table: str) -> list[dict]:
    conn = readonly_connection()
    try:
        return [{"name": r[1], "sqlite_type": r[2]} for r in conn.execute(f"PRAGMA table_info({_q(table)})")]
    finally:
        conn.close()


def _user_tables() -> dict[str, int]:
    return {t["table"]: t["rows"] for t in warehouse.schema() if t["kind"] != "system"}


def sync() -> None:
    """Register user tables that have no sheet yet (uploads and builds from before 0.5)."""
    tables = _user_tables()
    with session_scope() as s:
        known = set(s.scalars(select(Sheet.table_name)))
        pipes = {p.table_name: p.id for p in s.scalars(select(Pipeline)) if p.table_name}
        for table in tables:
            if table not in known:
                s.add(Sheet(name=table.removeprefix("ds_").removeprefix("cur_").replace("_", " "), table_name=table,
                            source="build" if table in pipes else "import", pipeline_id=pipes.get(table)))
        for sheet in s.scalars(select(Sheet)):
            if sheet.table_name not in tables:
                s.delete(sheet)  # the table was dropped elsewhere


def _merged_columns(sheet: Sheet) -> list[dict]:
    meta = {c["name"]: c for c in sheet.columns or []}
    out = []
    for col in _table_columns(sheet.table_name):
        m = meta.get(col["name"], {})
        out.append({"name": col["name"], "type": m.get("type") or _infer_type(col["sqlite_type"]),
                    "kind": m.get("kind", "plain"), "prompt": m.get("prompt", "")})
    return out


def _dict(sheet: Sheet, rows: int | None = None) -> dict:
    pipe = None
    if sheet.pipeline_id:
        with session_scope() as s:
            p = s.get(Pipeline, sheet.pipeline_id)
            if p is not None:
                pipe = {"id": p.id, "goal": p.goal, "interval_minutes": p.interval_minutes, "enabled": bool(p.enabled),
                        "last_run_at": p.last_run_at.isoformat() if p.last_run_at else None, "last_result": p.last_result}
    return {"id": sheet.id, "name": sheet.name, "table": sheet.table_name, "description": sheet.description,
            "source": sheet.source, "attribution": sheet.attribution, "rows": rows,
            "columns": _merged_columns(sheet), "build": pipe,
            "created_at": sheet.created_at.isoformat(), "updated_at": sheet.updated_at.isoformat()}


def list_sheets() -> list[dict]:
    sync()
    counts = _user_tables()
    with session_scope() as s:
        sheets = list(s.scalars(select(Sheet).order_by(Sheet.updated_at.desc())))
        return [_dict(sh, counts.get(sh.table_name, 0)) for sh in sheets]


def _get(s, sheet_id: str) -> Sheet:
    sheet = s.get(Sheet, sheet_id)
    if sheet is None:
        raise KeyError(sheet_id)
    return sheet


def get(sheet_id: str) -> dict:
    with session_scope() as s:
        sheet = _get(s, sheet_id)
        return _dict(sheet, _user_tables().get(sheet.table_name, 0))


def _touch(sheet_id: str) -> None:
    with session_scope() as s:
        sheet = s.get(Sheet, sheet_id)
        if sheet is not None:
            sheet.updated_at = utcnow()


def _unique_table(name: str) -> str:
    base = f"ds_{slugify(name, 50) or 'sheet'}"
    existing = set(_user_tables()) | warehouse.SYSTEM_TABLES
    table, n = base, 2
    while table in existing:
        table, n = f"{base}_{n}", n + 1
    return table


def create_blank(name: str, columns: list[dict]) -> dict:
    name = name.strip()[:80]
    if not name:
        raise SheetError("Give the sheet a name")
    cols = columns or [{"name": "name", "type": "text"}]
    defs, meta, seen = [], [], set()
    for c in cols:
        col = _col_name(c.get("name", ""))
        ctype = c.get("type", "text") if c.get("type") in TYPES else "text"
        if col in seen:
            raise SheetError(f"Two columns are called {col}")
        seen.add(col)
        defs.append(f"{_q(col)} {TYPES[ctype]}")
        meta.append({"name": col, "type": ctype, "kind": c.get("kind", "plain") if c.get("kind") in KINDS else "plain",
                     "prompt": str(c.get("prompt", ""))[:500]})
    table = _unique_table(name)
    conn = writer_connection()
    try:
        conn.execute(f"CREATE TABLE {_q(table)} ({', '.join(defs)})")
        conn.commit()
    finally:
        conn.close()
    with session_scope() as s:
        sheet = Sheet(name=name, table_name=table, columns=meta, source="manual")
        s.add(sheet)
        s.flush()
        return _dict(sheet, 0)


def import_file(name: str, filename: str, data: bytes, description: str = "", attribution: str = "", source: str = "import") -> dict:
    """CSV, TSV, Excel, JSON or JSONL into a new sheet (via the warehouse loader, which keeps the raw file)."""
    from app.lake.zones import start_dataset_raw

    label = (name.strip() or filename.rsplit(".", 1)[0])[:80]
    if filename.lower().endswith(".pdf"):
        raise SheetError("PDFs go into a collection, where their text can be searched")
    raw_path, source_id = start_dataset_raw(label, filename, data)
    try:
        ds = warehouse.load_dataframe(label, filename, data, raw_path, source_id, description)
    except warehouse.SQLValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 - pandas raises many types for bad files
        raise SheetError(f"Couldn't read {filename}: {exc}") from exc
    sync()
    with session_scope() as s:
        sheet = s.scalar(select(Sheet).where(Sheet.table_name == ds.table_name))
        sheet.name, sheet.source, sheet.description, sheet.attribution = label, source, description, attribution
        sheet.updated_at = utcnow()
        return _dict(sheet, ds.row_count)


def rename(sheet_id: str, name: str, description: str | None = None) -> dict:
    with session_scope() as s:
        sheet = _get(s, sheet_id)
        sheet.name = name.strip()[:80] or sheet.name
        if description is not None:
            sheet.description = description[:2000]
        sheet.updated_at = utcnow()
    return get(sheet_id)


def delete_sheet(sheet_id: str) -> None:
    with session_scope() as s:
        sheet = _get(s, sheet_id)
        table = sheet.table_name
        s.execute(delete(SheetCellMeta).where(SheetCellMeta.table_name == table))
        s.delete(sheet)
        from app.catalog.models import Dataset

        for ds in s.scalars(select(Dataset).where(Dataset.table_name == table)):
            s.delete(ds)
    conn = writer_connection()
    try:
        conn.execute(f"DROP TABLE IF EXISTS {_q(table)}")
        conn.commit()
    finally:
        conn.close()


# ---------- rows ----------
def rows(sheet_id: str, offset: int = 0, limit: int = 200, sort: str | None = None, desc: bool = False, q: str = "") -> dict:
    with session_scope() as s:
        sheet = _get(s, sheet_id)
        table = sheet.table_name
    cols = [c["name"] for c in _table_columns(table)]
    limit, offset = max(1, min(limit, PAGE_MAX)), max(0, offset)
    where, args = "", []
    if q.strip():
        where = "WHERE " + " OR ".join(f"CAST({_q(c)} AS TEXT) LIKE ?" for c in cols)
        args = [f"%{q.strip()}%"] * len(cols)
    direction = "DESC" if desc else "ASC"
    order = f"ORDER BY {_q(sort)} {direction}" if sort in cols else f"ORDER BY rowid {direction if sort == '_id' else 'ASC'}"  # _id: order added
    conn = readonly_connection()
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM {_q(table)} {where}", args).fetchone()[0]
        data = conn.execute(f"SELECT rowid, * FROM {_q(table)} {where} {order} LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
    finally:
        conn.close()
    ids = [r[0] for r in data]
    meta: dict[tuple[int, str], dict] = {}
    if ids:
        with session_scope() as s:
            for m in s.scalars(select(SheetCellMeta).where(SheetCellMeta.table_name == table, SheetCellMeta.row_id.in_(ids))):
                meta[(m.row_id, m.column)] = {"sources": m.sources, "confidence": m.confidence, "model": m.model}
    out = []
    for r in data:
        cells = dict(zip(cols, r[1:]))
        row_meta = {c: meta[(r[0], c)] for c in cols if (r[0], c) in meta}
        out.append({"_id": r[0], **cells, **({"_meta": row_meta} if row_meta else {})})
    return {"columns": cols, "rows": out, "total": total, "offset": offset}


CHART_AGGS = {"count": "COUNT(*)", "sum": "SUM({y})", "avg": "AVG({y})", "min": "MIN({y})", "max": "MAX({y})"}


def chart(sheet_id: str, x: str, y: str | None = None, agg: str = "count", limit: int = 30, bins: int = 0) -> dict:
    """Group the whole sheet by one column (or count values in `bins` equal ranges of a number column) for a chart."""
    with session_scope() as s:
        sheet = _get(s, sheet_id)
        table = sheet.table_name
        cols = {c["name"]: c for c in _merged_columns(sheet)}
    if x not in cols:
        raise SheetError(f"There's no column named {x}")
    if agg not in CHART_AGGS:
        raise SheetError("Use count, sum, avg, min or max")
    if agg != "count" and (not y or y not in cols):
        raise SheetError("Pick a number column to " + agg)
    limit = max(2, min(limit, 100))
    qx, qt = _q(x), _q(table)
    conn = readonly_connection()
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM {qt}").fetchone()[0]
        if bins:
            lo, hi = conn.execute(f"SELECT MIN({qx}), MAX({qx}) FROM {qt} WHERE typeof({qx}) IN ('integer','real')").fetchone()
            if lo is None or hi is None:
                raise SheetError(f"{x} has no numbers to chart")
            bins = max(2, min(bins, 50))
            width = (hi - lo) / bins or 1
            data = conn.execute(f"SELECT MIN(CAST(({qx} - ?) / ? AS INTEGER), ?) AS b, COUNT(*) FROM {qt} WHERE typeof({qx}) IN ('integer','real') GROUP BY b ORDER BY b",
                                [lo, width, bins - 1]).fetchall()
            counts = {b: n for b, n in data}
            series = [{"label": f"{lo + width * i:,.4g}", "value": counts.get(i, 0)} for i in range(bins)]
            return {"series": series, "x": x, "y": "rows", "agg": "count", "total": total, "kind": "hist"}
        expr = CHART_AGGS[agg].format(y=_q(y or x))
        order = "label" if cols[x].get("type") == "date" else "value DESC"
        data = conn.execute(f"SELECT CAST({qx} AS TEXT) AS label, {expr} AS value FROM {qt} WHERE {qx} IS NOT NULL AND {qx} != '' GROUP BY {qx} ORDER BY {order} LIMIT ?", [limit]).fetchall()
    finally:
        conn.close()
    series = [{"label": r[0], "value": round(float(r[1] or 0), 4)} for r in data]
    return {"series": series, "x": x, "y": "rows" if agg == "count" else f"{agg} of {y}", "agg": agg, "total": total, "kind": "group",
            "line": bool(series) and cols[x].get("type") == "date"}


def _coerce(value, ctype: str):
    if value is None or value == "":
        return None
    if ctype == "number":
        try:
            return float(str(value).replace(",", ""))
        except ValueError as exc:
            raise SheetError(f"{value!r} isn't a number") from exc
    if ctype == "boolean":
        return 1 if str(value).lower() in ("1", "true", "yes", "y") else 0
    return str(value)


def _types(sheet_id: str) -> tuple[str, dict[str, str]]:
    info = get(sheet_id)
    return info["table"], {c["name"]: c["type"] for c in info["columns"]}


def update_cell(sheet_id: str, row_id: int, column: str, value) -> None:
    table, types = _types(sheet_id)
    if column not in types:
        raise SheetError(f"No column {column}")
    conn = writer_connection()
    try:
        conn.execute(f"UPDATE {_q(table)} SET {_q(column)} = ? WHERE rowid = ?", (_coerce(value, types[column]), row_id))
        conn.commit()
    finally:
        conn.close()
    with session_scope() as s:  # a hand edit replaces whatever filled the cell
        s.execute(delete(SheetCellMeta).where(SheetCellMeta.table_name == table, SheetCellMeta.row_id == row_id,
                                              SheetCellMeta.column == column))
    _touch(sheet_id)


def add_rows(sheet_id: str, new_rows: list[dict]) -> list[int]:
    table, types = _types(sheet_id)
    ids = []
    conn = writer_connection()
    try:
        for row in new_rows or [{}]:
            cols = [c for c in row if c in types]
            if cols:
                cur = conn.execute(f"INSERT INTO {_q(table)} ({', '.join(_q(c) for c in cols)}) VALUES ({', '.join('?' for _ in cols)})",
                                   [_coerce(row[c], types[c]) for c in cols])
            else:
                cur = conn.execute(f"INSERT INTO {_q(table)} DEFAULT VALUES")
            ids.append(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    _touch(sheet_id)
    return ids


def delete_rows(sheet_id: str, row_ids: list[int]) -> int:
    table, _ = _types(sheet_id)
    conn = writer_connection()
    try:
        n = conn.executemany(f"DELETE FROM {_q(table)} WHERE rowid = ?", [(int(i),) for i in row_ids]).rowcount
        conn.commit()
    finally:
        conn.close()
    with session_scope() as s:
        s.execute(delete(SheetCellMeta).where(SheetCellMeta.table_name == table, SheetCellMeta.row_id.in_([int(i) for i in row_ids])))
    _touch(sheet_id)
    return n


# ---------- columns ----------
def add_column(sheet_id: str, name: str, ctype: str = "text", kind: str = "plain", prompt: str = "") -> dict:
    col = _col_name(name)
    ctype = ctype if ctype in TYPES else "text"
    kind = kind if kind in KINDS else "plain"
    if kind != "plain" and not prompt.strip():
        raise SheetError("AI and web columns need an instruction, such as 'the company's founding year'")
    with session_scope() as s:
        sheet = _get(s, sheet_id)
        table = sheet.table_name
    if col in {c["name"] for c in _table_columns(table)}:
        raise SheetError(f"There is already a column called {col}")
    conn = writer_connection()
    try:
        conn.execute(f"ALTER TABLE {_q(table)} ADD COLUMN {_q(col)} {TYPES[ctype]}")
        conn.commit()
    finally:
        conn.close()
    _set_column_meta(sheet_id, {"name": col, "type": ctype, "kind": kind, "prompt": prompt.strip()[:500]})
    return get(sheet_id)


def _set_column_meta(sheet_id: str, meta: dict, old_name: str | None = None) -> None:
    with session_scope() as s:
        sheet = _get(s, sheet_id)
        cols = [c for c in (sheet.columns or []) if c["name"] not in (meta["name"], old_name)]
        sheet.columns = [*cols, meta]
        sheet.updated_at = utcnow()


def update_column(sheet_id: str, name: str, new_name: str | None = None, kind: str | None = None, prompt: str | None = None,
                  ctype: str | None = None) -> dict:
    info = get(sheet_id)
    current = next((c for c in info["columns"] if c["name"] == name), None)
    if current is None:
        raise SheetError(f"No column {name}")
    target = _col_name(new_name) if new_name else name
    if target != name:
        if target in {c["name"] for c in info["columns"]}:
            raise SheetError(f"There is already a column called {target}")
        conn = writer_connection()
        try:
            conn.execute(f"ALTER TABLE {_q(info['table'])} RENAME COLUMN {_q(name)} TO {_q(target)}")
            conn.commit()
        finally:
            conn.close()
        with session_scope() as s:
            for m in s.scalars(select(SheetCellMeta).where(SheetCellMeta.table_name == info["table"], SheetCellMeta.column == name)):
                m.column = target
    meta = {"name": target, "type": ctype if ctype in TYPES else current["type"],
            "kind": kind if kind in KINDS else current["kind"], "prompt": (prompt if prompt is not None else current["prompt"])[:500]}
    _set_column_meta(sheet_id, meta, old_name=name)
    return get(sheet_id)


def drop_column(sheet_id: str, name: str) -> dict:
    info = get(sheet_id)
    if name not in {c["name"] for c in info["columns"]}:
        raise SheetError(f"No column {name}")
    if len(info["columns"]) == 1:
        raise SheetError("A sheet needs at least one column")
    conn = writer_connection()
    try:
        conn.execute(f"ALTER TABLE {_q(info['table'])} DROP COLUMN {_q(name)}")
        conn.commit()
    except sqlite3.OperationalError as exc:
        raise SheetError(f"Couldn't remove {name}: {exc}") from exc
    finally:
        conn.close()
    with session_scope() as s:
        sheet = _get(s, sheet_id)
        sheet.columns = [c for c in (sheet.columns or []) if c["name"] != name]
        s.execute(delete(SheetCellMeta).where(SheetCellMeta.table_name == info["table"], SheetCellMeta.column == name))
    return get(sheet_id)


# ---------- export, history ----------
def _all_rows(table: str) -> tuple[list[str], list[list]]:
    conn = readonly_connection()
    try:
        cur = conn.execute(f"SELECT * FROM {_q(table)}")
        return [d[0] for d in cur.description], [list(r) for r in cur.fetchall()]
    finally:
        conn.close()


def export(sheet_id: str, fmt: str) -> tuple[bytes, str, str]:
    """(file bytes, media type, file name)."""
    if fmt not in EXPORT_FORMATS:
        raise SheetError(f"Export as one of: {', '.join(EXPORT_FORMATS)}")
    info = get(sheet_id)
    cols, data = _all_rows(info["table"])
    stem = slugify(info["name"], 60) or info["table"]
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        w.writerows(data)
        return buf.getvalue().encode("utf-8-sig"), "text/csv", f"{stem}.csv"
    if fmt == "json":
        return json.dumps([dict(zip(cols, r)) for r in data], ensure_ascii=False, default=str, indent=2).encode(), "application/json", f"{stem}.json"
    if fmt == "jsonl":
        body = "\n".join(json.dumps(dict(zip(cols, r)), ensure_ascii=False, default=str) for r in data) + "\n"
        return body.encode(), "application/x-ndjson", f"{stem}.jsonl"
    if fmt == "xlsx":
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.title = (info["name"] or "Sheet")[:31]
        ws.append(cols)
        for r in data:
            ws.append(r)
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", f"{stem}.xlsx"
    return _pdf(info, cols, data), "application/pdf", f"{stem}.pdf"


def _pdf(info: dict, cols: list[str], data: list[list]) -> bytes:
    """A printable table report. Core PDF fonts are Latin-1, so other characters print as '?'."""
    from fpdf import FPDF

    def clean(v) -> str:
        text = "" if v is None else str(v)
        return text.encode("latin-1", "replace").decode("latin-1")

    pdf = FPDF(orientation="L" if len(cols) > 5 else "P", format="A4")
    pdf.set_auto_page_break(True, margin=12)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 8, clean(info["name"]), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 8)
    meta = f"{len(data)} rows - exported {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    if info.get("attribution"):
        meta += f" - {info['attribution']}"
    pdf.multi_cell(0, 4, clean(meta), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    shown = cols[:12]
    pdf.set_font("Helvetica", "", 7)
    with pdf.table(line_height=4, text_align="LEFT", first_row_as_headings=True) as table:
        head = table.row()
        for c in shown:
            head.cell(clean(c))
        for r in data[:5000]:
            row = table.row()
            for v in r[:len(shown)]:
                row.cell(clean(v)[:300])
    return bytes(pdf.output())


def _snap_dir(table: str) -> Path:
    path = zone_dir("curated") / "history" / table
    path.mkdir(parents=True, exist_ok=True)
    return path


def snapshot(sheet_id: str) -> str:
    """Save the sheet as a dated CSV in the lake; the newest KEEP_SNAPSHOTS are kept."""
    info = get(sheet_id)
    body, _, _ = export(sheet_id, "csv")
    folder = _snap_dir(info["table"])
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".csv"
    (folder / name).write_bytes(body)
    for old in sorted(folder.glob("*.csv"), reverse=True)[KEEP_SNAPSHOTS:]:
        old.unlink(missing_ok=True)
    return name


def history(sheet_id: str) -> list[dict]:
    info = get(sheet_id)
    folder = _snap_dir(info["table"])
    return [{"name": f.name, "bytes": f.stat().st_size, "rows": max(0, sum(1 for _ in f.open(encoding="utf-8-sig")) - 1)}
            for f in sorted(folder.glob("*.csv"), reverse=True)]


def snapshot_file(sheet_id: str, name: str) -> Path:
    info = get(sheet_id)
    path = _snap_dir(info["table"]) / Path(name).name
    if not path.is_file() or path.suffix != ".csv":
        raise SheetError("Snapshot not found")
    return path


def save_to_folder(sheet_id: str, fmt: str, folder: str) -> str:
    """Write an export into a folder the user picked."""
    target = Path(folder).expanduser()
    if not target.is_dir():
        raise SheetError(f"Folder not found: {target}")
    body, _, name = export(sheet_id, fmt)
    path = target / name
    path.write_bytes(body)
    return str(path)


def set_cell_meta(table: str, row_id: int, column: str, sources: list, confidence: float | None, model: str) -> None:
    with session_scope() as s:
        m = s.scalar(select(SheetCellMeta).where(SheetCellMeta.table_name == table, SheetCellMeta.row_id == row_id,
                                                SheetCellMeta.column == column))
        if m is None:
            m = SheetCellMeta(table_name=table, row_id=row_id, column=column)
            s.add(m)
        m.sources, m.confidence, m.model, m.updated_at = sources[:5], confidence, model, utcnow()


def snapshot_table(table: str) -> str | None:
    """Snapshot by table name (used after a build run refreshes the sheet)."""
    sync()
    with session_scope() as s:
        sheet = s.scalar(select(Sheet).where(Sheet.table_name == table))
        sheet_id = sheet.id if sheet else None
    return snapshot(sheet_id) if sheet_id else None
