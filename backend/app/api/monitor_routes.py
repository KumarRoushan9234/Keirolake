"""The monitoring desk: sources, markets, events, alerts, rules and the brief."""
from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app import prefs
from app.jobs.runner import runner
from app.monitor import markets, service

router = APIRouter(prefix="/api/monitor")


@router.get("")
async def overview() -> dict:
    return {"sources": service.sources_view(), "markets": markets.status(), "alerts_unseen": len(service.alerts(200, unseen_only=True))}


class SourceIn(BaseModel):
    enabled: bool | None = None
    interval_min: int | None = Field(None, ge=5, le=10080)


@router.put("/sources/{source_id}")
async def configure(source_id: str, body: SourceIn) -> dict:
    try:
        return service.configure(source_id, body.enabled, body.interval_min)
    except KeyError:
        raise HTTPException(404, "No such source") from None


@router.post("/sources/{source_id}/run", status_code=202)
async def run(source_id: str) -> dict:
    if source_id not in service.REGISTRY:
        raise HTTPException(404, "No such source")
    return {"job_id": runner.submit("monitor", service.REGISTRY[source_id].label, service.run_job(source_id), lane="background")}


# ---------------- markets
class MarketSettings(BaseModel):
    provider: str | None = Field(None, pattern="^(free|finnhub|web)$")
    watchlist: str | None = Field(None, max_length=600)
    finnhub_key: str | None = Field(None, max_length=200)


@router.put("/markets/settings")
async def market_settings(body: MarketSettings) -> dict:
    changes: dict[str, str | None] = {}
    if body.provider is not None:
        changes["MARKET_PROVIDER"] = body.provider
    if body.watchlist is not None:
        changes["MARKET_WATCHLIST"] = ",".join(s.strip().upper() for s in body.watchlist.replace("\n", ",").split(",") if s.strip())
    if body.finnhub_key is not None:
        changes["FINNHUB_API_KEY"] = body.finnhub_key
    try:
        prefs.update(changes)
    except prefs.PrefsError as exc:
        raise HTTPException(400, str(exc)) from exc
    return markets.status()


@router.post("/markets/import")
async def import_files(files: list[UploadFile] = File(...)) -> dict:
    """NSE end-of-day files the user downloaded from nseindia.com (sec_bhavdata_full_DDMMYYYY.csv), one or many."""
    done, problems = [], []
    for f in files[:120]:
        data = await f.read(20_000_000 + 1)
        if len(data) > 20_000_000:
            problems.append(f"{f.filename}: larger than 20 MB")
            continue
        try:
            done.append({"file": f.filename, **markets.import_bhavcopy(data.decode("utf-8", "replace"))})
        except markets.MarketError as exc:
            problems.append(f"{f.filename}: {exc}")
    if not done:
        raise HTTPException(400, problems[0] if problems else "No files")
    return {"imported": done, "problems": problems, "days": len(done), "status": markets.status()}


@router.get("/markets/movers")
async def movers(days: int = 30, limit: int = 10) -> dict:
    return markets.movers(max(1, min(days, 365)), max(1, min(limit, 50)))


@router.get("/markets/quote/{symbol}")
async def quote(symbol: str) -> dict:
    q = markets.quote(symbol)
    if q is None:
        raise HTTPException(404, "No prices stored for that symbol yet")
    return {**q, "history": markets.history(symbol, 120)}


@router.get("/markets/watchlist")
async def watch() -> list[dict]:
    return [q for q in (markets.quote(s) for s in markets.watchlist()) if q]


@router.get("/markets/earnings/{symbol}")
async def earnings(symbol: str) -> list[dict]:
    try:
        return await markets.earnings(symbol)
    except Exception as exc:  # noqa: BLE001 - shown to the user
        raise HTTPException(400, str(exc)) from exc


# ---------------- events, alerts, rules, brief
@router.get("/events")
async def events(group: str = "", kind: str = "", min_severity: str = "info", days: int = 7, q: str = "", limit: int = 100) -> list[dict]:
    return service.events(group, kind, min_severity, max(1, min(days, 120)), q, max(1, min(limit, 300)))


@router.get("/threat/{term}")
async def threat(term: str) -> list[dict]:
    return service.lookup_threat(term)


@router.get("/alerts")
async def alerts(unseen: bool = False, limit: int = 100) -> list[dict]:
    return service.alerts(max(1, min(limit, 300)), unseen)


class SeenIn(BaseModel):
    ids: list[str] | None = None


@router.post("/alerts/seen")
async def seen(body: SeenIn) -> dict:
    return {"marked": service.mark_seen(body.ids)}


@router.get("/rules")
async def rules() -> list[dict]:
    return service.rules_view()


class RuleIn(BaseModel):
    kind: str
    scope: str = "any"
    config: dict = Field(default_factory=dict)


@router.post("/rules", status_code=201)
async def add_rule(body: RuleIn) -> dict:
    try:
        return service.add_rule(body.kind, body.scope, body.config)
    except (service.RuleError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


class RulePatch(BaseModel):
    enabled: bool


@router.put("/rules/{rule_id}")
async def patch_rule(rule_id: str, body: RulePatch) -> dict:
    if not service.set_rule(rule_id, enabled=body.enabled):
        raise HTTPException(404, "No such rule")
    return {"ok": True}


@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(rule_id: str) -> None:
    if not service.set_rule(rule_id, delete=True):
        raise HTTPException(404, "No such rule")


@router.get("/briefs")
async def briefs() -> list[dict]:
    return service.briefs()


@router.post("/briefs", status_code=202)
async def write_brief() -> dict:
    async def job(ctx) -> dict:
        await ctx.progress(0.2, "Gathering today's changes")
        b = await service.generate_brief()
        return {"brief": b["id"], "title": b["title"]}

    return {"job_id": runner.submit("monitor", "Write the brief", job, lane="background")}
