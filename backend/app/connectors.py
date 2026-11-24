"""Connectors: saved external sources that fill and refresh a sheet.

  rest    a public JSON address; `json_path` points at the list of records ("data.items")
  rss     an RSS or Atom feed
  folder  a folder on this computer; CSV, Excel and JSON files in it become rows (desktop only)

Every address must resolve to a public host (same rule as the reader). Rows with the same key are updated, new keys
are added; with no key the sheet is replaced after a copy is kept in its history.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx
from sqlalchemy import select

from app import capture
from app.catalog.models import Connector, utcnow
from app.db import session_scope
from app.sheets import service as sheets

KINDS = ("rest", "rss", "folder", "renderdata")
MAX_BYTES = 8 * 2**20
MAX_ROWS = 20000
MIN_INTERVAL = 15
FILE_TYPES = (".csv", ".tsv", ".xlsx", ".xls", ".json", ".jsonl")
UA = "KeiroLake/0.7 (desktop data app; +https://platform.keirolabs.cloud)"


class ConnectorError(ValueError):
    pass


def _dict(c: Connector) -> dict:
    return {"id": c.id, "name": c.name, "kind": c.kind, "config": c.config, "sheet_id": c.sheet_id, "key_column": c.key_column,
            "enabled": bool(c.enabled), "interval_minutes": c.interval_minutes, "last_run_at": c.last_run_at.isoformat() if c.last_run_at else None,
            "last_result": c.last_result}


def list_all() -> list[dict]:
    with session_scope() as s:
        return [_dict(c) for c in s.scalars(select(Connector).order_by(Connector.created_at))]


def get(cid: str) -> dict:
    with session_scope() as s:
        c = s.get(Connector, cid)
        if c is None:
            raise KeyError(cid)
        return _dict(c)


def validate(kind: str, config: dict, interval: int, local_ok: bool) -> dict:
    if kind not in KINDS:
        raise ConnectorError("Kind must be rest, rss, folder or renderdata")
    if interval and not MIN_INTERVAL <= interval <= 10080:
        raise ConnectorError(f"Refresh every {MIN_INTERVAL} to 10080 minutes, or leave it manual")
    if kind == "renderdata":
        from app import datatools

        scraper, value = str(config.get("scraper", "")), str(config.get("input", "")).strip()
        if not datatools.SCRAPER_ID.match(scraper):
            raise ConnectorError("Pick a RenderData scraper")
        if not value or len(value) > 1000:
            raise ConnectorError("Enter the scraper's input (up to 1,000 characters)")
        return {"scraper": scraper, "input": value, "append": bool(config.get("append", True))}
    if kind == "folder":
        if not local_ok:
            raise ConnectorError("Folders can only be connected from the desktop app on this computer")
        path = Path(str(config.get("path", ""))).expanduser()
        if not path.is_dir():
            raise ConnectorError("That folder doesn't exist")
        return {"path": str(path)}
    url = str(config.get("url", "")).strip()
    try:
        capture.check_public_url(url)
    except capture.CaptureError as exc:
        raise ConnectorError(str(exc)) from exc
    out = {"url": url}
    if kind == "rest":
        out["json_path"] = re.sub(r"[^\w.\-\[\]]", "", str(config.get("json_path", "")))[:100]
    return out


def create(name: str, kind: str, config: dict, key_column: str = "", interval: int = 0, local_ok: bool = False) -> dict:
    name = name.strip()[:80]
    if not name:
        raise ConnectorError("Give the connector a name")
    cfg = validate(kind, config, interval, local_ok)
    with session_scope() as s:
        if s.scalar(select(Connector).where(Connector.name == name)):
            raise ConnectorError("A connector with that name already exists")
        c = Connector(name=name, kind=kind, config=cfg, key_column=key_column.strip()[:80], interval_minutes=interval)
        s.add(c)
        s.flush()
        return _dict(c)


def update(cid: str, enabled: bool | None = None, interval: int | None = None, key_column: str | None = None) -> dict:
    with session_scope() as s:
        c = s.get(Connector, cid)
        if c is None:
            raise KeyError(cid)
        if interval is not None:
            if interval and not MIN_INTERVAL <= interval <= 10080:
                raise ConnectorError(f"Refresh every {MIN_INTERVAL} to 10080 minutes, or leave it manual")
            c.interval_minutes = interval
        if enabled is not None:
            c.enabled = int(enabled)
        if key_column is not None:
            c.key_column = key_column.strip()[:80]
        return _dict(c)


def delete(cid: str) -> bool:
    with session_scope() as s:
        c = s.get(Connector, cid)
        if c is None:
            return False
        s.delete(c)
        return True


def due(now: datetime | None = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    out = []
    with session_scope() as s:
        for c in s.scalars(select(Connector).where(Connector.enabled == 1, Connector.interval_minutes > 0)):
            last = c.last_run_at.replace(tzinfo=timezone.utc) if c.last_run_at else None
            if last is None or now - last >= timedelta(minutes=c.interval_minutes):
                out.append(c.id)
    return out


# ---------- fetching ----------
async def _get(url: str) -> bytes:
    await asyncio.to_thread(capture.check_public_url, url)
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": UA}) as client:
        async with client.stream("GET", url) as resp:
            await asyncio.to_thread(capture.check_public_url, str(resp.url))  # redirects must stay public too
            if resp.status_code >= 400:
                raise ConnectorError(f"{url} answered {resp.status_code}")
            data = bytearray()
            async for chunk in resp.aiter_bytes():
                data += chunk
                if len(data) > MAX_BYTES:
                    raise ConnectorError("That response is larger than 8 MB")
    return bytes(data)


def _flatten(value, prefix: str = "", depth: int = 0) -> dict:
    out: dict = {}
    if isinstance(value, dict):
        for k, v in value.items():
            key = f"{prefix}_{k}" if prefix else str(k)
            if isinstance(v, dict) and depth < 2:
                out.update(_flatten(v, key, depth + 1))
            elif isinstance(v, (list, dict)):
                out[key] = json.dumps(v, ensure_ascii=False)[:2000]
            else:
                out[key] = v
    else:
        out[prefix or "value"] = value
    return out


def rows_from_json(data: bytes, json_path: str = "") -> list[dict]:
    try:
        doc = json.loads(data.decode("utf-8", "replace"))
    except ValueError as exc:
        raise ConnectorError("The address didn't return JSON") from exc
    for part in [p for p in re.split(r"\.|\[(\d+)\]", json_path) if p]:
        try:
            doc = doc[int(part)] if isinstance(doc, list) else doc[part]
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise ConnectorError(f"Couldn't find '{json_path}' in the response") from exc
    if isinstance(doc, dict):
        lists = [k for k, v in doc.items() if isinstance(v, list) and v and isinstance(v[0], dict)]
        doc = doc[lists[0]] if len(lists) == 1 else [doc]
    if not isinstance(doc, list) or not doc:
        raise ConnectorError("No list of records found. Set the path to the list, for example data.items")
    return [_flatten(r) for r in doc[:MAX_ROWS]]


def rows_from_feed(data: bytes) -> list[dict]:
    head = data[:4000].lower()
    if b"<!doctype" in head or b"<!entity" in head:
        raise ConnectorError("That feed uses features that aren't allowed")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ConnectorError("That isn't a readable RSS or Atom feed") from exc

    def tag(el) -> str:
        return el.tag.rsplit("}", 1)[-1]

    out = []
    for it in [e for e in root.iter() if tag(e) in ("item", "entry")][:MAX_ROWS]:
        row = {}
        for child in it:
            name = tag(child)
            if name == "link":
                row["link"] = child.get("href") or (child.text or "").strip()
            elif name in ("title", "pubDate", "published", "updated", "author", "description", "summary", "guid", "id"):
                text = "".join(child.itertext()).strip()
                row[{"pubDate": "published", "description": "summary", "guid": "id"}.get(name, name)] = text[:2000]
        if row:
            out.append(row)
    if not out:
        raise ConnectorError("No entries found in that feed")
    return out


def rows_from_folder(path: str, since: float = 0.0) -> list[dict]:
    """Rows from every supported file changed after `since` (a timestamp)."""
    import pandas as pd

    rows: list[dict] = []
    for f in sorted(Path(path).iterdir()):
        if not f.is_file() or f.suffix.lower() not in FILE_TYPES or f.stat().st_mtime <= since or f.stat().st_size > 50 * 2**20:
            continue
        ext = f.suffix.lower()
        try:
            df = (pd.read_csv(f, sep="\t" if ext == ".tsv" else ",") if ext in (".csv", ".tsv") else
                  pd.read_excel(f) if ext in (".xlsx", ".xls") else pd.read_json(f, lines=ext == ".jsonl"))
        except Exception as exc:  # noqa: BLE001 - name the file that failed
            raise ConnectorError(f"Couldn't read {f.name}: {str(exc)[:120]}") from exc
        df = df.astype(object).where(df.notna(), None)
        for rec in df.to_dict("records")[:MAX_ROWS]:
            rows.append({**{str(k): v for k, v in rec.items()}, "source_file": f.name})
    return rows


def _ctype(values: list) -> str:
    vals = [v for v in values if v not in (None, "")]
    if not vals:
        return "text"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
        return "number"
    if all(isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}", v) for v in vals):
        return "date"
    if all(isinstance(v, str) and re.match(r"^https?://", v) for v in vals):
        return "url"
    return "text"


# ---------- running ----------
def _k(v) -> str:
    """A key as text, so 2, 2.0 and "2" are the same row."""
    if v is None:
        return ""
    return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v).strip()


def _write(c: dict, rows: list[dict]) -> dict:
    """Put rows into the connector's sheet: create it, add new columns, then update by key or replace."""
    names: list[str] = []
    for r in rows:
        for k in r:
            if k not in names:
                names.append(k)
    names = names[:60]
    # Two source fields can clean up to the same column name ("+1" and "-1" both become "1"): keep both, numbered.
    taken: dict[str, int] = {}
    rename: dict[str, str] = {}
    for n in names:
        base = sheets._col_name(n)
        taken[base] = taken.get(base, 0) + 1
        rename[n] = base if taken[base] == 1 else f"{base}_{taken[base]}"
    rows = [{rename[k]: v for k, v in r.items() if k in rename} for r in rows]
    names = [rename[n] for n in names]
    sheet = None
    if c["sheet_id"]:
        try:
            sheet = sheets.get(c["sheet_id"])
        except KeyError:
            sheet = None
    types = {n: _ctype([r.get(n) for r in rows[:300]]) for n in names}
    if sheet is None:
        sheet = sheets.create_blank(c["name"], [{"name": n, "type": types[n]} for n in names])
    sid = sheet["id"]
    existing = {col["name"] for col in sheet["columns"]}
    for n in names:
        clean = sheets._col_name(n)
        if clean not in existing:
            sheets.add_column(sid, clean, types[n])
            existing.add(clean)
    clean_rows = [{sheets._col_name(k): v for k, v in r.items() if k in names and sheets._col_name(k) in existing} for r in rows]
    key = sheets._col_name(c["key_column"]) if c["key_column"] else ""
    updated = 0
    if key and key in existing:
        current: dict[str, dict] = {}
        offset = 0
        while True:
            page = sheets.rows(sid, offset, 1000)
            for r in page["rows"]:
                current[_k(r.get(key))] = r
            offset += 1000
            if offset >= page["total"]:
                break
        fresh = []
        for r in clean_rows:
            old = current.get(_k(r.get(key)))
            if old is None:
                fresh.append(r)
                continue
            for col, val in r.items():
                if col != key and val is not None and _k(val) != "" and _k(old.get(col)) != _k(val):
                    sheets.update_cell(sid, old["_id"], col, val)
                    updated += 1
        if fresh:
            sheets.add_rows(sid, fresh)
        added = len(fresh)
    else:
        if c["config"].get("append"):  # keep every run: each one adds its rows (a time series of answers)
            sheets.add_rows(sid, clean_rows)
            return {"sheet_id": sid, "added": len(clean_rows), "updated": 0, "total": len(clean_rows)}
        if (sheet.get("rows") or 0) > 0:
            sheets.snapshot(sid)
            page = sheets.rows(sid, 0, 1000)
            while page["rows"]:
                sheets.delete_rows(sid, [r["_id"] for r in page["rows"]])
                page = sheets.rows(sid, 0, 1000)
        sheets.add_rows(sid, clean_rows)
        added = len(clean_rows)
    return {"sheet_id": sid, "added": added, "updated": updated, "total": len(clean_rows)}


def _finish(cid: str, result: dict, sheet_id: str | None) -> None:
    with session_scope() as s:
        c = s.get(Connector, cid)
        if c is not None:
            c.last_run_at, c.last_result = utcnow(), result
            if sheet_id:
                c.sheet_id = sheet_id


def run_job(cid: str):
    async def job(ctx) -> dict:
        c = get(cid)
        await ctx.progress(0.05, f"Reading {c['name']}")
        try:
            if c["kind"] == "folder":
                started = datetime.now(timezone.utc).timestamp()
                rows = await asyncio.to_thread(rows_from_folder, c["config"]["path"], float(c["last_result"].get("since", 0.0)))
                if not rows:
                    result = {"added": 0, "updated": 0, "total": 0, "note": "No new files", "since": started}
                    _finish(cid, result, None)
                    return result
            elif c["kind"] == "renderdata":
                from app import datatools

                try:
                    body = await datatools.rd_run(c["config"]["scraper"], c["config"]["input"])
                    rows = datatools.rows_from_result(body)
                except datatools.DataToolError as exc:
                    raise ConnectorError(str(exc)) from exc
                stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                rows = [{"collected_at": stamp, "input": c["config"]["input"], **r} for r in rows]
            elif c["kind"] == "rest":
                rows = rows_from_json(await _get(c["config"]["url"]), c["config"].get("json_path", ""))
            else:
                rows = rows_from_feed(await _get(c["config"]["url"]))
            await ctx.progress(0.6, f"Writing {len(rows)} rows")
            result = await asyncio.to_thread(_write, c, rows)
            if c["kind"] == "folder":
                result["since"] = started
        except Exception as exc:
            _finish(cid, {**{k: v for k, v in c["last_result"].items() if k == "since"}, "error": str(exc)[:300]}, None)
            raise
        _finish(cid, result, result["sheet_id"])
        return result

    return job
