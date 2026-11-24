"""SQLite as the warehouse.

  ds_<name>   clean tables loaded from uploaded CSV / JSON / Excel files
  cur_<name>  curated tables created from SQL (CREATE TABLE ... AS SELECT)

User SQL always runs on a read-only connection; only curated-table creation
writes, and only through a validated SELECT.
"""
from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import time

from sqlalchemy import select

from app.catalog.models import Dataset
from app.db import readonly_connection, session_scope, writer_connection
from app.lake.zones import relative, slugify, zone_dir

MAX_ROWS = 5000
SECRET_TABLES = {"api_keys", "webhooks", "access_log"}  # never readable from user SQL


def _catalog_tables() -> set[str]:
    from app.catalog.models import Base

    return set(Base.metadata.tables)


SYSTEM_TABLES = _catalog_tables() | {"saved_queries", "jobs", "feeds", "datasets"}


class SQLValidationError(ValueError):
    pass


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"--[^\n]*", " ", sql)


def validate_select(sql: str) -> str:
    cleaned = _strip_comments(sql).strip().rstrip(";").strip()
    if not cleaned:
        raise SQLValidationError("Empty query")
    if ";" in cleaned:
        raise SQLValidationError("Only one statement is allowed")
    if not re.match(r"^(select|with|values)\b", cleaned, re.I):
        raise SQLValidationError("Only SELECT / WITH queries are allowed here")
    # Defence in depth: the read-only connection is the real guard.
    if re.search(r"\b(attach|detach|pragma|insert|update|delete|drop|alter|create|vacuum|reindex)\b", cleaned, re.I):
        raise SQLValidationError("Write and admin statements are not allowed")
    return cleaned


def _guard(conn: sqlite3.Connection, user_tables_only: bool) -> None:
    """Refuse reads of secret tables, and (for outside apps) of anything that isn't the user's own data."""
    blocked = SYSTEM_TABLES | SECRET_TABLES if user_tables_only else SECRET_TABLES

    def authorize(action, table, _column, _db, _trigger):
        if action == sqlite3.SQLITE_READ and table and (table in blocked or table.startswith("chunks_fts")
                                                        and user_tables_only):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorize)


def run_query(sql: str, limit: int = 1000, *, user_tables_only: bool = False) -> dict:
    """Run a validated SELECT on a read-only connection. `user_tables_only` is for API keys."""
    query = validate_select(sql)
    limit = max(1, min(limit, MAX_ROWS))
    started = time.perf_counter()
    conn = readonly_connection()
    _guard(conn, user_tables_only)
    try:
        cur = conn.execute(query)
        columns = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(limit + 1)
    except sqlite3.Error as exc:
        raise SQLValidationError(str(exc)) from exc
    finally:
        conn.close()
    truncated = len(rows) > limit
    return {"columns": columns, "rows": [list(r) for r in rows[:limit]], "row_count": min(len(rows), limit),
            "truncated": truncated, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)}


def table_columns(conn: sqlite3.Connection, table: str) -> list[dict]:
    return [{"name": r[1], "type": r[2] or "TEXT"} for r in conn.execute(f'PRAGMA table_info("{table}")')]


def schema(include_system: bool = False) -> list[dict]:
    conn = readonly_connection()
    try:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' "
            "AND name NOT LIKE 'chunks_fts%' ORDER BY name")]
        out = []
        for name in names:
            is_system = name in SYSTEM_TABLES
            if is_system and not include_system:
                continue
            count = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            kind = "system" if is_system else "curated" if name.startswith("cur_") else "clean" if name.startswith("ds_") else "other"
            out.append({"table": name, "kind": kind, "rows": count, "columns": table_columns(conn, name)})
        return out
    finally:
        conn.close()


def schema_prompt(max_tables: int = 30, only: list[str] | None = None) -> str:
    """Compact schema + sample rows for text-to-SQL. `only` limits it to these tables (one sheet)."""
    conn = readonly_connection()
    try:
        parts = []
        tables = [t for t in schema() if only is None or t["table"] in only]
        for t in tables[:max_tables]:
            cols = ", ".join(f"{c['name']} {c['type']}" for c in t["columns"])
            sample = conn.execute(f'SELECT * FROM "{t["table"]}" LIMIT 3').fetchall()
            parts.append(f"TABLE {t['table']} ({cols})  -- {t['rows']} rows; sample: {[list(r) for r in sample]}"[:1500])
        return "\n".join(parts) or "(no tables yet)"
    finally:
        conn.close()


def load_dataframe(name: str, filename: str, data: bytes, raw_path, source_id: str | None, description: str = "") -> Dataset:
    import pandas as pd

    ext = filename.lower().rsplit(".", 1)[-1]
    buf = io.BytesIO(data)
    if ext == "csv":
        df = pd.read_csv(buf)
    elif ext == "tsv":
        df = pd.read_csv(buf, sep="\t")
    elif ext in {"xlsx", "xls"}:
        df = pd.read_excel(buf)
    elif ext == "json":
        df = pd.read_json(buf)
    elif ext == "jsonl":
        df = pd.read_json(buf, lines=True)
    else:
        raise SQLValidationError("Supported dataset files: csv, tsv, xlsx, json, jsonl")
    df.columns = [slugify(str(c), 60) for c in df.columns]
    # Nested JSON values become JSON text so SQLite can store them.
    for col in df.columns:
        if df[col].map(lambda v: isinstance(v, (dict, list))).any():
            df[col] = df[col].map(lambda v: json.dumps(v) if isinstance(v, (dict, list)) else v)
    table = f"ds_{slugify(name, 50)}"
    conn = writer_connection()
    try:
        df.to_sql(table, conn, if_exists="replace", index=False)
        conn.commit()
        columns = table_columns(conn, table)
    finally:
        conn.close()
    with session_scope() as s:
        ds = s.scalar(select(Dataset).where(Dataset.table_name == table))
        if ds is None:
            ds = Dataset(name=name, table_name=table)
            s.add(ds)
        ds.zone, ds.description, ds.source_id = "clean", description, source_id
        ds.raw_path, ds.row_count, ds.columns = relative(raw_path), len(df), columns
    _announce(table, len(df))
    return ds


def _announce(table: str, rows: int) -> None:
    from app import access

    access.emit("dataset.created", {"table": table, "rows": rows})


def create_curated(name: str, sql: str, description: str = "") -> Dataset:
    query = validate_select(sql)
    table = f"cur_{slugify(name, 50)}"
    conn = writer_connection()
    try:
        conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        conn.execute(f'CREATE TABLE "{table}" AS {query}')
        conn.commit()
        rows = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        columns = table_columns(conn, table)
        export_csv(conn, table)
    except sqlite3.Error as exc:
        raise SQLValidationError(str(exc)) from exc
    finally:
        conn.close()
    with session_scope() as s:
        ds = s.scalar(select(Dataset).where(Dataset.table_name == table))
        if ds is None:
            ds = Dataset(name=name, table_name=table)
            s.add(ds)
        ds.zone, ds.sql, ds.description, ds.row_count, ds.columns = "curated", query, description, rows, columns
        ds.raw_path = relative(zone_dir("curated") / f"{table}.csv")
    _announce(table, rows)
    return ds


def export_csv(conn: sqlite3.Connection, table: str) -> str:
    path = zone_dir("curated") / f"{table}.csv"
    cur = conn.execute(f'SELECT * FROM "{table}"')
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([d[0] for d in cur.description])
        writer.writerows(cur)
    return str(path)


def drop_dataset(dataset_id: str) -> None:
    with session_scope() as s:
        ds = s.get(Dataset, dataset_id)
        if ds is None:
            return
        table = ds.table_name
        s.delete(ds)
    if not re.fullmatch(r"(ds|cur)_[a-z0-9_]+", table):
        return
    conn = writer_connection()
    try:
        conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        conn.commit()
    finally:
        conn.close()
