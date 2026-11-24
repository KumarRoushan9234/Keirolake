"""Data lake zones on local disk.

    raw/      exactly what came in (uploads, Keiro JSON, crawled pages); never modified
    clean/    normalized text documents and chunk JSONL, one folder per KB or dataset
    curated/  CSV exports of curated warehouse tables
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings

ZONES = ("raw", "clean", "curated")


def slugify(value: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return (slug or "item")[:max_len]


def zone_dir(zone: str) -> Path:
    if zone not in ZONES:
        raise ValueError(f"unknown zone {zone}")
    path = get_settings().lake_dir / zone
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_zones() -> None:
    for zone in ZONES:
        zone_dir(zone)


def raw_run_dir(source_kind: str, run_id: str) -> Path:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = zone_dir("raw") / slugify(source_kind) / day / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_raw_bytes(run_dir: Path, filename: str, data: bytes) -> Path:
    stem, dot, ext = Path(filename).name.rpartition(".")
    safe = f"{slugify(stem or ext)}.{slugify(ext, 10)}" if dot else slugify(filename)
    path = run_dir / safe
    counter = 1
    while path.exists():
        path = run_dir / f"{counter}_{safe}"
        counter += 1
    path.write_bytes(data)
    return path


def write_raw_json(run_dir: Path, name: str, payload: object) -> Path:
    path = run_dir / f"{slugify(name)}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def clean_dir(namespace: str) -> Path:
    path = zone_dir("clean") / slugify(namespace)
    path.mkdir(parents=True, exist_ok=True)
    return path


def relative(path: Path) -> str:
    """Store lake paths relative to the data dir so the catalog stays portable."""
    try:
        return path.resolve().relative_to(get_settings().data_dir.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def absolute(rel: str) -> Path:
    return get_settings().data_dir / rel


def start_dataset_raw(name: str, filename: str, data: bytes) -> tuple[Path, str]:
    """Land an uploaded dataset file in the raw zone and register its source."""
    from app.ingest.pipeline import finish_run, start_run

    source_id, run_id = start_run("dataset", name, None)
    path = write_raw_bytes(raw_run_dir("dataset", run_id), filename, data)
    finish_run(run_id, raw_path=relative(path), stats={"bytes": len(data)})
    return path, source_id


def zone_summary() -> list[dict]:
    out = []
    for zone in ZONES:
        root = zone_dir(zone)
        files = [p for p in root.rglob("*") if p.is_file()]
        out.append({"zone": zone, "files": len(files), "bytes": sum(p.stat().st_size for p in files)})
    return out
