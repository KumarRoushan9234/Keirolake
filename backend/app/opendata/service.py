"""Download an open dataset and add it to the lake as a sheet, keeping its licence and credit with it."""
from __future__ import annotations

import asyncio
import io
import json
import zipfile
from pathlib import Path

import httpx
from sqlalchemy import select

from app.catalog.models import Sheet
from app.db import session_scope
from app.sheets import service as sheets

CATALOG = Path(__file__).with_name("catalog.json")
MAX_BYTES = 200 * 2**20


class OpenDataError(ValueError):
    pass


def catalog() -> list[dict]:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def entry(dataset_id: str) -> dict:
    found = next((e for e in catalog() if e["id"] == dataset_id), None)
    if found is None:
        raise OpenDataError("Unknown dataset")
    return found


def listing() -> list[dict]:
    """The catalog, with which ones are already sheets."""
    with session_scope() as s:
        have = {a for a in s.scalars(select(Sheet.attribution)) if a}
    return [{**{k: v for k, v in e.items() if k not in ("url", "member", "columns")}, "added": e["attribution"] in have} for e in catalog()]


def to_csv_bytes(e: dict, data: bytes) -> bytes:
    """CSV bytes for any recipe: plain CSV as is; zipped, header-less TSV gets its column names."""
    if e["format"] == "csv":
        return data
    if e["format"] == "zip-tsv":
        import pandas as pd

        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            with zf.open(e["member"]) as fh:
                df = pd.read_csv(fh, sep="\t", header=None, names=e["columns"], dtype=str, keep_default_na=False, quoting=3)
        for col in ("population", "elevation", "dem", "latitude", "longitude"):
            if col in df:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df.to_csv(index=False).encode("utf-8")
    raise OpenDataError(f"Unsupported format {e['format']}")


def import_job(dataset_id: str):
    e = entry(dataset_id)

    async def job(ctx) -> dict:
        await ctx.progress(0.05, f"Downloading {e['title']} ({e['size_mb']} MB)")
        data = bytearray()
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, read=120), follow_redirects=True) as client:
            async with client.stream("GET", e["url"]) as resp:
                if resp.status_code != 200:
                    raise OpenDataError(f"{e['publisher']} answered {resp.status_code}")
                async for chunk in resp.aiter_bytes():
                    data += chunk
                    if len(data) > MAX_BYTES:
                        raise OpenDataError("That file is larger than 200 MB")
        await ctx.progress(0.7, "Reading it into a sheet")
        csv = to_csv_bytes(e, bytes(data))
        sheet = await asyncio.to_thread(sheets.import_file, e["title"], f"{e['id']}.csv", csv, e["description"], e["attribution"], "dataset")
        return {"sheet_id": sheet["id"], "rows": sheet["rows"], "license": e["license"]}

    return job
