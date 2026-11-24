"""Clean a sheet: tidy text, turn placeholders into empty cells, fix number and date columns, drop duplicates.

`plan` only counts what would change; `apply` keeps a dated copy in the sheet's history first, so it can be undone.
Every rule is conservative: it never invents values and never touches a column it can't read confidently.
"""
from __future__ import annotations

import re
from datetime import datetime

from app.db import writer_connection
from app.sheets import service

PLACEHOLDERS = {"", "n/a", "na", "#n/a", "null", "none", "nan", "nil", "-", "--", "—", "–", "tbd", "?"}
NUMBER = re.compile(r"^[\s(+-]*[$€£₹]?\s*[+-]?\d[\d,\s]*(\.\d+)?\s*(%|[kKmMbB])?\)?\s*$")
DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S")
MULTI = {"k": 1e3, "m": 1e6, "b": 1e9}
MIN_SHARE = 0.9  # a column is retyped only when this share of its filled cells reads cleanly


def _tidy(v):
    if isinstance(v, str):
        t = re.sub(r"[ \t ]+", " ", v.replace("\r\n", "\n")).strip()
        return None if t.lower() in PLACEHOLDERS else t
    return v


def _to_number(v) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if not isinstance(v, str) or not NUMBER.match(v):
        return None
    s = v.strip()
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-")
    mult = MULTI.get(s[-1].lower(), 1) if s[-1].lower() in MULTI else 1
    digits = re.sub(r"[^\d.]", "", s.rstrip("%kKmMbB)"))
    if digits.count(".") > 1 or not digits:
        return None
    x = float(digits) * mult
    return -x if neg else x


def _to_date(v) -> str | None:
    if not isinstance(v, str):
        return None
    s = v.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s[:len(datetime.now().strftime(fmt))] if "T" in fmt else s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _share(values: list, fn) -> float:
    filled = [v for v in values if v is not None]
    return (sum(1 for v in filled if fn(v) is not None) / len(filled)) if filled else 0.0


def _run(sheet_id: str, apply: bool, drop_empty_columns: bool) -> dict:
    info = service.get(sheet_id)
    table = info["table"]
    rowids, cols, data = _read(table)
    conn_cols = [c["name"] for c in info["columns"] if c["name"] in cols]
    index = {c: cols.index(c) for c in conn_cols}
    types = {c["name"]: c["type"] for c in info["columns"]}
    report = {"rows_before": len(data), "trimmed_cells": 0, "emptied_cells": 0, "duplicates_removed": 0, "empty_rows_removed": 0,
              "retyped": [], "empty_columns": [], "dropped_columns": []}

    clean: list[list] = []
    for row in data:
        out = list(row)
        for c, i in index.items():
            before = out[i]
            after = _tidy(before)
            if after != before:
                report["emptied_cells" if after is None else "trimmed_cells"] += 1
            out[i] = after
        clean.append(out)

    for c, i in index.items():
        col = [r[i] for r in clean]
        if types.get(c) == "text" and _share(col, _to_number) >= MIN_SHARE and any(v is not None for v in col):
            report["retyped"].append({"name": c, "to": "number"})
            for r in clean:
                if r[i] is not None:
                    n = _to_number(r[i])
                    r[i] = n if n is not None else None
        elif types.get(c) == "text" and _share(col, _to_date) >= MIN_SHARE and any(v is not None for v in col):
            report["retyped"].append({"name": c, "to": "date"})
            for r in clean:
                if r[i] is not None:
                    r[i] = _to_date(r[i]) or r[i]
        if not any(v is not None for v in col) and clean:
            report["empty_columns"].append(c)

    seen: set = set()
    keep: list[int] = []
    for n, row in enumerate(clean):
        values = tuple(row[i] for i in index.values())
        if all(v is None for v in values):
            report["empty_rows_removed"] += 1
            continue
        if values in seen:
            report["duplicates_removed"] += 1
            continue
        seen.add(values)
        keep.append(n)
    report["rows_after"] = len(keep)
    if not apply:
        return report

    service.snapshot(sheet_id)
    keep_set = set(keep)
    conn = writer_connection()
    try:
        for n, row in enumerate(clean):
            if n not in keep_set:
                conn.execute(f"DELETE FROM {service._q(table)} WHERE rowid = ?", [rowids[n]])
                continue
            if row != data[n]:
                sets = ", ".join(f"{service._q(c)} = ?" for c in index)
                conn.execute(f"UPDATE {service._q(table)} SET {sets} WHERE rowid = ?", [row[i] for i in index.values()] + [rowids[n]])
        conn.commit()
    finally:
        conn.close()
    if report["retyped"]:
        _rebuild(table, [(c, next((r["to"] for r in report["retyped"] if r["name"] == c), types.get(c, "text"))) for c in cols])
        for item in report["retyped"]:
            service.update_column(sheet_id, item["name"], ctype=item["to"])
    if drop_empty_columns:
        for c in report["empty_columns"]:
            if len(info["columns"]) - len(report["dropped_columns"]) > 1:
                service.drop_column(sheet_id, c)
                report["dropped_columns"].append(c)
    service._touch(sheet_id)
    return report


def _rebuild(table: str, columns: list[tuple[str, str]]) -> None:
    """Give columns their new storage type (SQLite can't alter one), keeping column order and row ids."""
    q, tmp = service._q, service._q(f"{table}__new")
    names = ", ".join(q(c) for c, _ in columns)
    conn = writer_connection()
    try:
        conn.execute(f"CREATE TABLE {tmp} ({', '.join(f'{q(c)} {service.TYPES.get(t, service.TYPES['text'])}' for c, t in columns)})")
        conn.execute(f"INSERT INTO {tmp} (rowid, {names}) SELECT rowid, {names} FROM {q(table)}")
        conn.execute(f"DROP TABLE {q(table)}")
        conn.execute(f"ALTER TABLE {tmp} RENAME TO {q(table)}")
        conn.commit()
    finally:
        conn.close()


def _read(table: str) -> tuple[list[int], list[str], list[list]]:
    from app.db import readonly_connection

    conn = readonly_connection()
    try:
        cur = conn.execute(f"SELECT rowid, * FROM {service._q(table)} ORDER BY rowid")
        names = [d[0] for d in cur.description][1:]
        rows = cur.fetchall()
        return [r[0] for r in rows], names, [list(r[1:]) for r in rows]
    finally:
        conn.close()


def plan(sheet_id: str, drop_empty_columns: bool = False) -> dict:
    return _run(sheet_id, False, drop_empty_columns)


def apply(sheet_id: str, drop_empty_columns: bool = False) -> dict:
    return _run(sheet_id, True, drop_empty_columns)
