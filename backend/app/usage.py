"""Daily usage counters: cloud model calls and KeiroLabs credits.

Kept in `<DATA_DIR>/usage.json` as {"YYYY-MM-DD": {"cloud_calls": n, "keiro_credits": x}} for the last 30 days.
"""
from __future__ import annotations

import json
import threading
from datetime import date

from app.config import get_settings

_lock = threading.Lock()
KEEP_DAYS = 30


def _path():
    return get_settings().data_dir / "usage.json"


def _load() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    keep = dict(sorted(data.items())[-KEEP_DAYS:])
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(keep), encoding="utf-8")
    tmp.replace(path)


def add(field: str, amount: float = 1) -> None:
    with _lock:
        data = _load()
        day = data.setdefault(date.today().isoformat(), {})
        day[field] = round(day.get(field, 0) + amount, 3)
        _save(data)


def today() -> dict:
    with _lock:
        day = _load().get(date.today().isoformat(), {})
    limit = get_settings().cloud_daily_limit
    calls = int(day.get("cloud_calls", 0))
    return {"cloud_calls": calls, "cloud_limit": limit, "keiro_credits": day.get("keiro_credits", 0),
            "local_calls": int(day.get("local_calls", 0)), "cloud_left": None if limit <= 0 else max(0, limit - calls)}


def cloud_allowed() -> bool:
    limit = get_settings().cloud_daily_limit
    return limit <= 0 or today()["cloud_calls"] < limit


def history(days: int = 14) -> list[dict]:
    with _lock:
        data = _load()
    return [{"day": k, **v} for k, v in sorted(data.items())[-days:]]
