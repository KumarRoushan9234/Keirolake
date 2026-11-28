"""Model slots and the live model catalogue (Settings → Models)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import prefs
from app.config import LOCAL_PROVIDERS
from app.llm import catalog, tasks

router = APIRouter(prefix="/api/model-slots")


@router.get("")
async def overview() -> dict:
    cat = await catalog.entries()
    slots = []
    for slot in tasks.SLOTS:
        value = tasks.slot_value(slot)
        current = next((m for m in cat["models"] if m["id"] == value), None) if value else None
        slots.append({"slot": slot, "label": tasks.SLOT_LABELS[slot], "help": tasks.SLOT_HELP[slot], "value": value,
                      "current": current, "suggestions": catalog.suggest(slot, cat["models"])})
    return {"slots": slots, "catalog": {k: cat[k] for k in ("fetched_at", "error", "refreshing", "usable_providers")}, "models": cat["models"]}


class SlotIn(BaseModel):
    model: str = ""  # "provider:model", or empty to clear the slot


@router.put("/{slot}")
async def set_slot(slot: str, body: SlotIn) -> dict:
    if slot not in tasks.SLOTS:
        raise HTTPException(404, "No such slot")
    value = body.model.strip()
    if value:
        parsed = tasks.parse(value)
        if parsed is None:
            raise HTTPException(400, "Choose a model from the list")
        provider = parsed[0]
        if provider not in LOCAL_PROVIDERS and provider not in catalog.usable_providers():
            raise HTTPException(400, f"Add your {provider} API key in Settings → Models first")
    try:
        prefs.update({tasks.ENV[slot]: value})
    except prefs.PrefsError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"slot": slot, "value": value}


@router.post("/refresh")
async def refresh() -> dict:
    data = await catalog.refresh(force=True)
    return {"fetched_at": data.get("fetched_at", 0), "count": len(data.get("models", [])), "error": catalog._state["error"]}


@router.post("/classify")
async def classify(body: SlotIn) -> dict:
    t = tasks.classify(body.model)
    return {"slot": t.slot, "reason": t.reason, "model": tasks.slot_value(t.slot)}
