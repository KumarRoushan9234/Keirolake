"""A live list of models you can use, with what each is good for.

Sources, refreshed at most once a day and kept in `<data>/models-catalog.json` (so it also works offline):
- models.dev: an open database of model specifications and prices for every major provider;
- OpenRouter's own /models list (the authority for what OpenRouter offers today);
- the models installed in Ollama on this computer.

Each model gets plain-language use cases worked out from its facts (reasoning, vision, context size, price, release date),
so the list stays right when new models appear, with no hand-kept table to go stale.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from pathlib import Path

from app.config import KEYLESS_PROVIDERS, LOCAL_PROVIDERS, get_settings, provider_config

log = logging.getLogger(__name__)
MODELS_DEV = "https://models.dev/api.json"
OPENROUTER = "https://openrouter.ai/api/v1/models"
MAX_AGE = 24 * 3600
# Our provider name -> models.dev provider id
PROVIDERS = {"anthropic": "anthropic", "openai": "openai", "gemini": "google", "groqcloud": "groq", "deepseek": "deepseek",
             "mistral": "mistral", "together": "togetherai", "openrouter": "openrouter"}
_CODE_WORDS = ("code", "coder", "codex", "devstral", "codestral")
_state = {"refreshing": False, "error": ""}


def _path() -> Path:
    return get_settings().data_dir / "models-catalog.json"


def _load() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"fetched_at": 0, "models": []}


def _save(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)


def _per_million(value) -> float | None:
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def from_models_dev(raw: dict) -> list[dict]:
    out = []
    for ours, theirs in PROVIDERS.items():
        for mid, m in (raw.get(theirs, {}).get("models") or {}).items():
            cost = m.get("cost") or {}
            mods = m.get("modalities") or {}
            out.append({
                "id": f"{ours}:{mid}", "provider": ours, "model": mid, "name": m.get("name") or mid,
                "context": (m.get("limit") or {}).get("context"), "max_output": (m.get("limit") or {}).get("output"),
                "price_in": _per_million(cost.get("input")), "price_out": _per_million(cost.get("output")),
                "reasoning": bool(m.get("reasoning")), "tools": bool(m.get("tool_call")),
                "vision": "image" in (mods.get("input") or []), "open_weights": bool(m.get("open_weights")),
                "released": m.get("release_date") or "", "local": False,
            })
    return out


def from_openrouter(raw: dict) -> list[dict]:
    out = []
    for m in raw.get("data", []):
        pricing = m.get("pricing") or {}
        arch = m.get("architecture") or {}
        params = m.get("supported_parameters") or []

        def per_m(key):
            try:
                value = float(pricing[key]) * 1_000_000
            except (KeyError, TypeError, ValueError):
                return None
            return round(value, 4) if value >= 0 else None  # OpenRouter uses -1 for "varies" (its auto routers)
        created = m.get("created")
        out.append({
            "id": f"openrouter:{m['id']}", "provider": "openrouter", "model": m["id"], "name": m.get("name") or m["id"],
            "context": m.get("context_length"), "max_output": (m.get("top_provider") or {}).get("max_completion_tokens"),
            "price_in": per_m("prompt"), "price_out": per_m("completion"),
            "reasoning": "reasoning" in params or "include_reasoning" in params, "tools": "tools" in params,
            "vision": "image" in (arch.get("input_modalities") or []), "open_weights": False,
            "released": dt.datetime.fromtimestamp(created, dt.timezone.utc).date().isoformat() if created else "", "local": False,
        })
    return out


def use_cases(e: dict) -> list[str]:
    """Plain-language 'good for' tags, from the model's facts."""
    tags = []
    pin, pout = e.get("price_in"), e.get("price_out")
    free = (pin == 0 and pout == 0) or e.get("local")
    if e.get("reasoning") and e.get("tools") and not free and (pout or 0) >= 2:
        tags.append("Hard reasoning")
    elif e.get("reasoning"):
        tags.append("Reasoning")
    if any(w in e["model"].lower() for w in _CODE_WORDS) or (e.get("reasoning") and e.get("tools") and (pout or 0) >= 2):
        tags.append("Coding")
    if (e.get("context") or 0) >= 500_000:
        tags.append("Very long documents")
    elif (e.get("context") or 0) >= 128_000:
        tags.append("Long documents")
    if e.get("vision"):
        tags.append("Reads images")
    if free:
        tags.append("Free" if not e.get("local") else "Runs on this computer")
    elif pin is not None and pin <= 0.5 and (pout or 99) <= 2.5:
        tags.append("Cheap and fast")
    if e.get("tools"):
        tags.append("Tools")
    return tags


def _age_days(e: dict) -> int:
    try:
        return (dt.date.today() - dt.date.fromisoformat((e.get("released") or "")[:10])).days
    except ValueError:
        return 3650


def score(slot: str, e: dict) -> float:
    """How well a model fits a slot: higher is better. Recent, capable and sensibly priced wins."""
    recent = max(0.0, 1 - _age_days(e) / 540)
    known = e.get("price_out") is not None
    price = e.get("price_out") or 0
    ctx = min((e.get("context") or 0) / 1_000_000, 1)
    reasoning, tools = e.get("reasoning", False), e.get("tools", False)
    if slot == "hard":
        return 3 * reasoning + 1.5 * tools + 2 * recent + min(price / 40, 1) * 1.5 + 0.5 * ctx
    if slot == "research":
        return 2 * reasoning + 1.5 * ctx + 2 * recent + tools - min(price / 80, 1) * 0.5
    if slot == "coding":
        name = e["model"].lower()
        return 3 * any(w in name for w in _CODE_WORDS) + 2 * reasoning + 1.5 * tools + 1.5 * recent
    # everyday: fast, cheap or local
    return 3 * bool(e.get("local")) + 2 * (known and price <= 2.5) + 1.5 * recent + tools * 0.5 - min(price / 20, 1)


def usable_providers() -> set[str]:
    out = set()
    for name in PROVIDERS:
        cfg = provider_config(name)
        if cfg.api_key and cfg.base_url:
            out.add(name)
    return out


async def installed_local() -> list[dict]:
    from app.local import ollama

    try:
        status = await ollama.status()
    except Exception:  # noqa: BLE001 - Ollama may not be running
        return []
    out = []
    for m in status.get("models", []):
        if not m.get("local") or any(k in m["name"].lower() for k in ("embed", "bge-", "minilm", "nomic")):
            continue  # embedding models can't chat
        out.append({"id": f"ollama:{m['name']}", "provider": "ollama", "model": m["name"], "name": m["name"], "context": None,
                    "max_output": None, "price_in": 0, "price_out": 0, "reasoning": any(k in m["name"].lower() for k in ("r1", "qwq", "reason", "think")),
                    "tools": True, "vision": any(k in m["name"].lower() for k in ("vl", "vision", "llava", "gemma3", "minicpm", "moondream")),
                    "open_weights": True, "released": "", "local": True, "size_gb": m.get("size_gb")})
    return out


async def refresh(force: bool = False) -> dict:
    """Fetch the catalogue if it is older than a day. Never raises: the cached list stays in use."""
    data = _load()
    if not force and time.time() - data.get("fetched_at", 0) < MAX_AGE:
        return data
    if _state["refreshing"]:
        return data
    _state["refreshing"] = True
    try:
        import httpx

        async with httpx.AsyncClient(timeout=60, follow_redirects=True, headers={"User-Agent": "KeiroLake"}) as client:
            models: list[dict] = []
            try:
                models = from_models_dev((await client.get(MODELS_DEV)).raise_for_status().json())
            except Exception as exc:  # noqa: BLE001 - OpenRouter alone still gives a list
                log.info("models.dev unavailable: %s", exc)
                _state["error"] = f"models.dev: {exc}"
            try:
                router = from_openrouter((await client.get(OPENROUTER)).raise_for_status().json())
                models = [m for m in models if m["provider"] != "openrouter"] + router
            except Exception as exc:  # noqa: BLE001
                log.info("OpenRouter list unavailable: %s", exc)
        if models:
            data = {"fetched_at": time.time(), "models": models}
            _save(data)
            _state["error"] = ""
    finally:
        _state["refreshing"] = False
    return data


async def entries() -> dict:
    """The catalogue for the Models page: cached cloud models plus what's installed locally, each with use cases."""
    data = _load()
    if time.time() - data.get("fetched_at", 0) >= MAX_AGE:
        asyncio.create_task(refresh())  # in the background; the cached list answers now
    usable = usable_providers()
    models = [dict(m) for m in data.get("models", [])] + await installed_local()
    for m in models:
        for key in ("price_in", "price_out"):  # a negative price means "varies" (older cached lists kept it as -1)
            if isinstance(m.get(key), (int, float)) and m[key] < 0:
                m[key] = None
        m["use_cases"] = use_cases(m)
        m["usable"] = m["local"] or m["provider"] in usable
    return {"fetched_at": data.get("fetched_at", 0), "error": _state["error"], "refreshing": _state["refreshing"],
            "usable_providers": sorted(usable), "models": models}


def suggest(slot: str, models: list[dict], limit: int = 5) -> list[dict]:
    """The best models for a slot among those the user can use now (a key is set, or it runs here)."""
    # Routers ("auto", "…-router") pick a model themselves, so they say nothing about fit: leave them out of suggestions.
    ok = [m for m in models if m.get("usable") and not m["model"].startswith("openrouter/") and "router" not in m["model"].lower()]
    if slot == "everyday":
        ok = [m for m in ok if m["local"] or (m.get("price_out") or 0) <= 5]
    return sorted(ok, key=lambda m: score(slot, m), reverse=True)[:limit]
