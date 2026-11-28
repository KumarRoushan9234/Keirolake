"""The monitoring desk's engine: which sources exist, running them on a schedule, alert rules, context for answers and the brief.

Every source is off until the user turns it on (nothing is fetched from the internet behind their back). A run fetches,
stores events or prices in the local database, checks the alert rules and reports what was new.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from sqlalchemy import delete, select

from app.db import session_scope
from app.monitor import markets
from app.monitor import sources as S
from app.monitor.models import MonitorAlert, MonitorBrief, MonitorEvent, MonitorRule, MonitorSource

log = logging.getLogger(__name__)
KEEP_EVENT_DAYS = 120
_running: set[str] = set()
_queued: set[str] = set()  # handed to the job runner, not started yet


@dataclass
class Source:
    id: str
    group: str  # crisis | cyber | market | brief
    label: str
    description: str
    interval: int  # minutes
    run: Callable[[dict], Awaitable[dict]]
    attribution: str
    needs_key: str = ""  # the setting that holds its (free) key, when one is needed
    url: str = ""


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware(d: dt.datetime) -> dt.datetime:
    """SQLite returns times without a zone; everything we store is UTC."""
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


# ---------------------------------------------------------------- storing events
def store_events(events: list[dict]) -> list[dict]:
    """Insert new events, refresh known ones (an alert level can change). Returns only the new ones."""
    new: list[dict] = []
    with session_scope() as s:
        for e in events:
            row = s.scalar(select(MonitorEvent).where(MonitorEvent.source == e["source"], MonitorEvent.ext_id == e["ext_id"]))
            if row is None:
                row = MonitorEvent(**e)
                s.add(row)
                s.flush()
                new.append({**e, "id": row.id})
            else:
                for k in ("title", "summary", "severity", "severity_n", "data"):
                    setattr(row, k, e[k])
    return new


def prune_events() -> None:
    cutoff = _now() - dt.timedelta(days=KEEP_EVENT_DAYS)
    with session_scope() as s:
        s.execute(delete(MonitorEvent).where(MonitorEvent.occurred_at < cutoff))


def _event_group(source: str) -> str:
    return source.split(".", 1)[0]


# ---------------------------------------------------------------- the sources
async def _usgs(cfg: dict) -> dict:
    return _events_result(store_events(S.parse_usgs(await S.get_json(S.USGS_URL), float(cfg.get("min_magnitude", 4.5)))))


async def _gdacs(cfg: dict) -> dict:
    return _events_result(store_events(S.parse_gdacs((await S.get_text(S.GDACS_URL))[1])))


async def _who(cfg: dict) -> dict:
    return _events_result(store_events(S.parse_who(await S.get_json(S.WHO_URL))))


DEFAULT_GDELT = ["earthquake OR flood OR cyclone OR wildfire OR volcano", "ceasefire OR airstrike OR coup OR sanctions OR evacuation", "outbreak OR epidemic OR pandemic"]


async def _gdelt(cfg: dict) -> dict:
    events: list[dict] = []
    for q in cfg.get("queries") or DEFAULT_GDELT:
        try:
            events += S.parse_gdelt(await S.gdelt_search(q, cfg.get("timespan", "1d"), 25), q)
        except Exception as exc:  # noqa: BLE001 - the other queries still count
            log.info("GDELT %r failed: %s", q, exc)
    if not events:
        raise RuntimeError("GDELT gave nothing (it allows one request every 5 seconds); it will try again next time")
    return _events_result(store_events(events))


async def _kev(cfg: dict) -> dict:
    return _events_result(store_events(S.parse_kev(await S.get_json(S.KEV_URL), int(cfg.get("days", 90)))))


async def _nvd(cfg: dict) -> dict:
    now = _now()
    params = {"resultsPerPage": 60, "cvssV3Severity": "CRITICAL", "lastModStartDate": (now - dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S.000"),
              "lastModEndDate": now.strftime("%Y-%m-%dT%H:%M:%S.000")}
    return _events_result(store_events(S.parse_nvd(await S.get_json(S.NVD_URL, params=params))))


async def _threatfox(cfg: dict) -> dict:
    from app import http

    key = os.getenv("ABUSECH_API_KEY", "")
    if not key:
        raise RuntimeError("Add your free abuse.ch Auth-Key to use ThreatFox")
    r = await http.client("monitor").post(S.THREATFOX_URL, json={"query": "get_iocs", "days": 1}, headers={"Auth-Key": key, "User-Agent": S.UA}, timeout=45)
    if r.status_code >= 400:
        raise RuntimeError(f"ThreatFox answered {r.status_code}")
    return _events_result(store_events(S.parse_threatfox(r.json())))


async def _largecap(cfg: dict) -> dict:
    out = await markets.sync_yahoo(markets.large_cap_symbols(), cfg.get("range", "6mo"))
    if not out["rows"]:
        raise RuntimeError(out["failed"][0] if out["failed"] else "Yahoo Finance gave no prices")
    latest = markets.status()["last_day"]
    return {**out, "events": evaluate_prices(latest, markets.watchlist()) if latest else 0, "count": out["rows"]}


async def _yahoo(cfg: dict) -> dict:
    out = await markets.sync_yahoo(range_=cfg.get("range", "3mo"))
    if not out["rows"] and out["failed"]:
        raise RuntimeError(out["failed"][0])
    latest = max((h["day"] for sym in markets.watchlist() for h in markets.history(sym, 10)), default="")
    return {**out, "events": evaluate_prices(latest, markets.watchlist()) if latest else 0, "count": out["rows"]}


async def _brief(cfg: dict) -> dict:
    b = await generate_brief()
    return {"count": 1, "events": 0, "brief": b["id"]}


def _events_result(new: list[dict]) -> dict:
    return {"count": len(new), "events": evaluate_events(new), "new": len(new)}


REGISTRY: dict[str, Source] = {s.id: s for s in [
    Source("market.largecap", "market", "India large companies", "About 50 of India's largest companies, daily: real 1-day, 7-day and 30-day top performers.", 360, _largecap,
           "Yahoo Finance chart data (unofficial, for personal use)", url="https://finance.yahoo.com/"),
    Source("market.yahoo", "market", "Indices and global stocks", "Your watchlist (Nifty, Sensex, S&P 500, Apple…) from Yahoo Finance daily charts.", 120, _yahoo,
           "Yahoo Finance chart data (unofficial, for personal use)", url="https://finance.yahoo.com/"),
    Source("crisis.usgs", "crisis", "Earthquakes", "Earthquakes of magnitude 4.5 and up, worldwide.", 30, _usgs, "USGS Earthquake Hazards Program (public domain)",
           url="https://earthquake.usgs.gov/"),
    Source("crisis.gdacs", "crisis", "Disaster alerts", "Earthquakes, cyclones, floods, volcanoes and fires with a humanitarian alert level.", 60, _gdacs,
           "GDACS (UN and European Commission)", url="https://www.gdacs.org/"),
    Source("crisis.who", "crisis", "Disease outbreaks", "WHO Disease Outbreak News.", 360, _who, "World Health Organization", url="https://www.who.int/emergencies/disease-outbreak-news"),
    Source("crisis.gdelt", "crisis", "World news signals", "News about disasters, conflict and outbreaks from around the world.", 180, _gdelt,
           "The GDELT Project (free, attribution requested)", url="https://www.gdeltproject.org/"),
    Source("cyber.kev", "cyber", "Exploited vulnerabilities", "CISA's catalogue of vulnerabilities attackers are using right now.", 360, _kev,
           "CISA Known Exploited Vulnerabilities (public)", url="https://www.cisa.gov/known-exploited-vulnerabilities-catalog"),
    Source("cyber.nvd", "cyber", "Critical new CVEs", "Newly published or updated CVEs rated critical.", 360, _nvd, "NIST National Vulnerability Database (public)",
           url="https://nvd.nist.gov/"),
    Source("cyber.threatfox", "cyber", "Malware indicators", "Fresh indicators of compromise (needs a free abuse.ch key).", 180, _threatfox, "abuse.ch ThreatFox (CC0)",
           needs_key="ABUSECH_API_KEY", url="https://threatfox.abuse.ch/"),
    Source("brief.daily", "brief", "Daily brief", "A cited morning briefing written from the day's changes.", 1440, _brief, "Written by your model from the sources above"),
]}


def ensure_defaults() -> None:
    """One row per source (all off) and a few starting alert rules. Safe to run on every start."""
    with session_scope() as s:
        have = {r.id for r in s.scalars(select(MonitorSource))}
        for src in REGISTRY.values():
            if src.id not in have:
                s.add(MonitorSource(id=src.id, enabled=0, interval_min=src.interval, config={}))
        if s.scalar(select(MonitorRule).limit(1)) is None:
            s.add_all([
                MonitorRule(kind="severity", scope="crisis", config={"min": "high"}),
                MonitorRule(kind="severity", scope="cyber", config={"min": "critical", "sources": ["cyber.kev"]}),
                MonitorRule(kind="price_move", scope="market", config={"pct": 5.0, "symbols": "watchlist"}),
            ])


def sources_view() -> list[dict]:
    with session_scope() as s:
        rows = {r.id: r for r in s.scalars(select(MonitorSource))}
    out = []
    for src in REGISTRY.values():
        r = rows.get(src.id)
        out.append({"id": src.id, "group": src.group, "label": src.label, "description": src.description, "attribution": src.attribution, "url": src.url,
                    "enabled": bool(r and r.enabled), "interval_min": r.interval_min if r else src.interval, "last_run": r.last_run.isoformat() if r and r.last_run else None,
                    "last_ok": bool(r.last_ok) if r else True, "last_error": r.last_error if r else "", "last_count": r.last_count if r else 0,
                    "needs_key": src.needs_key, "key_set": bool(src.needs_key and os.getenv(src.needs_key)), "running": src.id in _running or src.id in _queued})
    return out


def configure(source_id: str, enabled: bool | None = None, interval_min: int | None = None) -> dict:
    if source_id not in REGISTRY:
        raise KeyError(source_id)
    with session_scope() as s:
        r = s.get(MonitorSource, source_id) or MonitorSource(id=source_id)
        s.add(r)
        if enabled is not None:
            r.enabled = int(enabled)
        if interval_min is not None:
            r.interval_min = max(5, min(int(interval_min), 7 * 1440))
    return next(v for v in sources_view() if v["id"] == source_id)


async def run_source(source_id: str) -> dict:
    """Run one source now: fetch, store, check the rules. Records how it went; never raises."""
    src = REGISTRY[source_id]
    _queued.discard(source_id)
    if source_id in _running:
        return {"skipped": "already running"}
    _running.add(source_id)
    with session_scope() as s:
        cfg = dict((s.get(MonitorSource, source_id) or MonitorSource(id=source_id)).config or {})
    try:
        result = await src.run(cfg)
        ok, error = True, ""
    except Exception as exc:  # noqa: BLE001 - shown on the Monitor page
        log.info("Monitor %s failed: %s", source_id, exc)
        result, ok, error = {"count": 0}, False, str(exc)[:500]
    finally:
        _running.discard(source_id)
    with session_scope() as s:
        r = s.get(MonitorSource, source_id)
        if r:
            r.last_run, r.last_ok, r.last_error, r.last_count = _now(), int(ok), error, int(result.get("count", 0))
    if src.group in ("crisis", "cyber"):
        prune_events()
    return {"ok": ok, "error": error, **result}


def run_job(source_id: str):
    async def job(ctx) -> dict:
        await ctx.progress(0.1, REGISTRY[source_id].label)
        return await run_source(source_id)
    return job


def due(now: dt.datetime | None = None) -> list[str]:
    now = now or _now()
    out = []
    with session_scope() as s:
        for r in s.scalars(select(MonitorSource).where(MonitorSource.enabled == 1)):
            if r.id in REGISTRY and r.id not in _running and r.id not in _queued and (r.last_run is None or now - _aware(r.last_run) >= dt.timedelta(minutes=r.interval_min)):
                out.append(r.id)
    return out


async def monitor_loop() -> None:
    from app.config import offline_mode
    from app.jobs.runner import runner
    from app.net import connectivity

    await asyncio.sleep(20)
    while True:
        try:
            if connectivity.is_online and not offline_mode():
                for sid in due():
                    _queued.add(sid)  # until the job starts, so the next tick doesn't queue it twice
                    runner.submit("monitor", REGISTRY[sid].label, run_job(sid), lane="background")
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("Monitor schedule tick failed")
        await asyncio.sleep(60)


# ---------------------------------------------------------------- rules and alerts
def _terms(text: str) -> set[str]:
    from app.engine.evidence import terms

    return set(terms(text))


def _raise(rule: MonitorRule, ref: str, title: str, body: str, url: str, severity: str, scope: str) -> bool:
    with session_scope() as s:
        if s.scalar(select(MonitorAlert.id).where(MonitorAlert.rule_id == rule.id, MonitorAlert.ref == ref)):
            return False
        s.add(MonitorAlert(rule_id=rule.id, ref=ref, title=title[:300], body=body[:1500], url=url, severity=severity, scope=scope))
    try:
        from app import access

        access.emit("monitor.alert", {"title": title, "severity": severity, "scope": scope, "url": url})
    except Exception:  # noqa: BLE001 - alerts are stored either way
        pass
    return True


def evaluate_events(new: list[dict]) -> int:
    if not new:
        return 0
    with session_scope() as s:
        rules = list(s.scalars(select(MonitorRule).where(MonitorRule.enabled == 1, MonitorRule.kind.in_(("keyword", "severity")))))
    raised = 0
    for e in new:
        group = _event_group(e["source"])
        text = f"{e['title']} {e['summary']}".lower()
        for r in rules:
            if r.scope not in ("any", group):
                continue
            cfg = r.config or {}
            if cfg.get("sources") and e["source"] not in cfg["sources"]:
                continue
            hit = False
            if r.kind == "severity":
                hit = e["severity_n"] >= S.SEVERITY.get(cfg.get("min", "high"), 3)
            elif r.kind == "keyword":
                hit = any(t.strip().lower() in text for t in cfg.get("terms", []) if t.strip())
            if hit and _raise(r, e["id"], e["title"], e["summary"], e["url"], e["severity"], group):
                raised += 1
    return raised


def evaluate_prices(day: str, symbols: list[str] | None = None) -> int:
    """Price alerts for one trading day: big moves, and new highs or lows over what's stored."""
    if not day:
        return 0
    with session_scope() as s:
        rules = list(s.scalars(select(MonitorRule).where(MonitorRule.enabled == 1, MonitorRule.kind.in_(("price_move", "new_high", "new_low")))))
    raised = 0
    for r in rules:
        cfg = r.config or {}
        watch = cfg.get("symbols")
        syms = markets.watchlist() if watch in (None, "watchlist") else [str(x).upper() for x in watch]
        if symbols is not None:
            syms = [x for x in syms if x in symbols]
        for sym in syms:
            hist = markets.history(sym, 400)
            today = next((h for h in hist if h["day"] == day), None)
            if not today:
                continue
            q = markets.quote(sym) or {}
            if r.kind == "price_move":
                pct = q.get("change_pct")
                if pct is not None and abs(pct) >= float(cfg.get("pct", 5)):
                    arrow = "up" if pct > 0 else "down"
                    raised += _raise(r, f"{sym}:{day}", f"{sym} {arrow} {abs(pct):.1f}% on {day}", f"Closed at {today['close']:.2f} (previous close {q.get('prev_close')}).",
                                     "", "high" if abs(pct) >= 8 else "medium", "market")
            elif len(hist) >= 20:
                closes = [h["close"] for h in hist if h["day"] != day]
                if r.kind == "new_high" and today["close"] > max(closes):
                    raised += _raise(r, f"{sym}:{day}:hi", f"{sym} at a {len(hist)}-day high", f"Closed at {today['close']:.2f}.", "", "medium", "market")
                if r.kind == "new_low" and today["close"] < min(closes):
                    raised += _raise(r, f"{sym}:{day}:lo", f"{sym} at a {len(hist)}-day low", f"Closed at {today['close']:.2f}.", "", "medium", "market")
    return raised


def alerts(limit: int = 100, unseen_only: bool = False) -> list[dict]:
    with session_scope() as s:
        q = select(MonitorAlert).order_by(MonitorAlert.created_at.desc()).limit(limit)
        if unseen_only:
            q = q.where(MonitorAlert.seen == 0)
        return [{"id": a.id, "title": a.title, "body": a.body, "url": a.url, "severity": a.severity, "scope": a.scope, "seen": bool(a.seen),
                 "created_at": a.created_at.isoformat()} for a in s.scalars(q)]


def mark_seen(ids: list[str] | None = None) -> int:
    with session_scope() as s:
        q = select(MonitorAlert).where(MonitorAlert.seen == 0)
        if ids:
            q = q.where(MonitorAlert.id.in_(ids))
        rows = list(s.scalars(q))
        for a in rows:
            a.seen = 1
        return len(rows)


def rules_view() -> list[dict]:
    with session_scope() as s:
        return [{"id": r.id, "kind": r.kind, "scope": r.scope, "config": r.config, "enabled": bool(r.enabled)} for r in s.scalars(select(MonitorRule).order_by(MonitorRule.created_at))]


class RuleError(ValueError):
    pass


def add_rule(kind: str, scope: str, config: dict) -> dict:
    if kind not in ("keyword", "severity", "price_move", "new_high", "new_low"):
        raise RuleError("Unknown rule type")
    if scope not in ("any", "crisis", "cyber", "market"):
        raise RuleError("Unknown scope")
    cfg: dict = {}
    if kind == "keyword":
        terms = [str(t).strip() for t in config.get("terms", []) if str(t).strip()][:20]
        if not terms:
            raise RuleError("Add at least one word to watch for")
        cfg["terms"] = terms
    elif kind == "severity":
        cfg["min"] = config.get("min") if config.get("min") in S.SEVERITY else "high"
    else:
        scope = "market"
        if kind == "price_move":
            cfg["pct"] = max(0.5, min(float(config.get("pct", 5)), 50))
        syms = config.get("symbols", "watchlist")
        cfg["symbols"] = "watchlist" if syms in ("watchlist", None, "") else [str(x).upper() for x in syms][:50]
    with session_scope() as s:
        r = MonitorRule(kind=kind, scope=scope, config=cfg)
        s.add(r)
        s.flush()
        return {"id": r.id, "kind": kind, "scope": scope, "config": cfg, "enabled": True}


def set_rule(rule_id: str, enabled: bool | None = None, delete: bool = False) -> bool:
    with session_scope() as s:
        r = s.get(MonitorRule, rule_id)
        if r is None:
            return False
        if delete:
            s.delete(r)
        elif enabled is not None:
            r.enabled = int(enabled)
        return True


# ---------------------------------------------------------------- reading events
def events(group: str = "", kind: str = "", min_severity: str = "info", days: int = 7, q: str = "", limit: int = 100) -> list[dict]:
    cutoff = _now() - dt.timedelta(days=days)
    with session_scope() as s:
        query = select(MonitorEvent).where(MonitorEvent.occurred_at >= cutoff, MonitorEvent.severity_n >= S.SEVERITY.get(min_severity, 0))
        if group:
            query = query.where(MonitorEvent.source.like(f"{group}.%"))
        if kind:
            query = query.where(MonitorEvent.kind == kind)
        rows = list(s.scalars(query.order_by(MonitorEvent.severity_n.desc(), MonitorEvent.occurred_at.desc()).limit(600)))
    if q.strip():
        want = _terms(q)
        rows = [r for r in rows if want and len(want & _terms(f"{r.title} {r.summary} {r.country}")) / len(want) >= 0.5]
    return [{"id": r.id, "source": r.source, "kind": r.kind, "title": r.title, "summary": r.summary, "url": r.url, "severity": r.severity, "country": r.country,
             "lat": r.lat, "lon": r.lon, "occurred_at": r.occurred_at.isoformat(), "data": r.data} for r in rows[:limit]]


def lookup_threat(term: str) -> list[dict]:
    """A CVE id, vendor or product against everything stored."""
    t = term.strip().lower()
    if not t:
        return []
    with session_scope() as s:
        rows = [r for r in s.scalars(select(MonitorEvent).where(MonitorEvent.kind.in_(("cve", "ioc"))).order_by(MonitorEvent.occurred_at.desc()).limit(3000))
                if t in f"{r.title} {r.summary}".lower()]
    return [{"title": r.title, "severity": r.severity, "summary": r.summary, "url": r.url, "source": r.source, "occurred_at": r.occurred_at.isoformat()} for r in rows[:25]]


# ---------------------------------------------------------------- context for answers
ALIASES = {"reliance": "RELIANCE.NS", "tcs": "TCS.NS", "infosys": "INFY.NS", "hdfc bank": "HDFCBANK.NS", "icici bank": "ICICIBANK.NS", "sbi": "SBIN.NS",
           "wipro": "WIPRO.NS", "itc": "ITC.NS", "bajaj finance": "BAJFINANCE.NS", "maruti": "MARUTI.NS", "axis bank": "AXISBANK.NS", "kotak": "KOTAKBANK.NS",
           "tata motors": "TATAMOTORS.NS", "adani enterprises": "ADANIENT.NS", "larsen": "LT.NS", "nifty": "^NSEI", "sensex": "^BSESN", "s&p 500": "^GSPC",
           "nasdaq": "^IXIC", "apple": "AAPL", "microsoft": "MSFT", "nvidia": "NVDA", "tesla": "TSLA", "amazon": "AMZN", "google": "GOOGL"}
_MOVERS = re.compile(r"\b(top|best|biggest|highest|leading)\b.{0,30}\b(gain\w*|perform\w*|return\w*|winners?|stocks?|shares?)\b|\b(gainers|losers|top performers?|worst performing)\b", re.I)
_MARKETQ = re.compile(r"\b(stock|stocks|share|shares|nifty|sensex|nse|bse|equity|equities|ticker|market cap|quarterly results?|earnings|eps|p/?e ratio)\b", re.I)
_RESULTS = re.compile(r"\b(quarterly|quarter|q[1-4]|earnings|results?|eps|profit)\b", re.I)
_CATEGORY = [(re.compile(r"\b(earthquakes?|quakes?|seismic)\b", re.I), "quake"), (re.compile(r"\b(outbreaks?|epidemics?|pandemics?|disease|virus|cholera|ebola|measles)\b", re.I), "outbreak"),
             (re.compile(r"\b(vulnerabilit\w+|cves?|exploit\w*|ransomware|zero[- ]day|cyber\w*|malware|breach\w*)\b", re.I), "cve"),
             (re.compile(r"\b(disasters?|floods?|cyclones?|hurricanes?|typhoons?|wildfires?|volcano\w*|tsunamis?|drought)\b", re.I), "disaster")]


def looks_like_market(question: str) -> bool:
    return bool(_MOVERS.search(question) or _MARKETQ.search(question) or _find_symbols(question))


def _period_days(question: str) -> int:
    m = re.search(r"(\d+)\s*(day|week|month)s?", question, re.I)
    if m:
        return max(1, min(int(m.group(1)) * {"day": 1, "week": 7, "month": 30}[m.group(2).lower()], 365))
    q = question.lower()
    return 7 if "week" in q else 1 if re.search(r"\b(today|yesterday|1 day)\b", q) else 30 if "month" in q or "30" in q else 30


def _find_symbols(question: str) -> list[str]:
    found = []
    low = question.lower()
    for name, sym in ALIASES.items():
        if re.search(rf"\b{re.escape(name)}\b", low) and sym not in found:
            found.append(sym)
    for tok in re.findall(r"\b[A-Z][A-Z&-]{2,14}\b", question):  # symbols typed in capitals, such as HDFCBANK or NVDA
        sym = markets.resolve_symbol(tok)
        if sym and sym not in found:
            found.append(sym)
    return found[:3]


async def context_for(question: str, search_text: str = "") -> tuple[list, str, str]:
    """Real stored data relevant to a question: (sources, why, hint). `hint` explains what's missing when the question
    is about markets but nothing is stored, so the answer can say what to turn on instead of guessing."""
    from app.keiro.client import WebResult

    out: list = []
    why, hint = "", ""
    if markets.provider() != "web" and looks_like_market(question):
        st = markets.status()
        if _MOVERS.search(question):
            days = _period_days(question)
            m = markets.movers(days, 10)
            if m.get("ok"):
                full = m["scope"] == "all NSE stocks"
                out.append(WebResult(url="https://www.nseindia.com/all-reports" if full else "https://finance.yahoo.com/markets/stocks/gainers/",
                                     title=f"Top performers over {days} days among {m['universe']} {m['scope']} (closes to {m['latest']})",
                                     snippet=f"{m['universe']} stocks compared", content=markets.top_text(m), domain="nseindia.com" if full else "finance.yahoo.com"))
                why = f"Real closing prices stored on this computer ({m['base']} to {m['latest']}), {m['universe']} {m['scope']}"
            else:
                hint = m["reason"]
        for sym in _find_symbols(question):
            qt = markets.quote(sym)
            if not qt:
                continue
            h = markets.history(sym, 35)
            line = (f"{sym} closed at {qt['close']} {qt['currency']} on {qt['day']} ({qt['change_pct']:+.2f}% on the day)" if qt["change_pct"] is not None
                    else f"{sym} closed at {qt['close']} {qt['currency']} on {qt['day']}")
            if len(h) > 1:
                first = h[0]
                line += f". Since {first['day']}: {(qt['close'] / first['close'] - 1) * 100:+.1f}%. Range over {qt['history_days']} stored days: {qt['range_low']} to {qt['range_high']}."
            out.append(WebResult(url=("https://finance.yahoo.com/quote/" + sym), title=f"{sym} price history stored on this computer", snippet=line, content=line, domain=qt["source"]))
            why = why or "Real prices stored on this computer"
        if _RESULTS.search(question) and _find_symbols(question):
            if os.getenv("FINNHUB_API_KEY") and markets.provider() == "finnhub":
                try:
                    rows = await markets.earnings(_find_symbols(question)[0])
                    text = "Quarterly EPS (actual vs expected): " + "; ".join(f"{r['year']} Q{r['quarter']}: {r['actual']} vs {r['estimate']}" for r in rows if r["actual"] is not None)
                    out.append(WebResult(url="https://finnhub.io/", title=f"{_find_symbols(question)[0]} quarterly earnings (Finnhub)", snippet=text, content=text, domain="finnhub.io"))
                except Exception as exc:  # noqa: BLE001
                    hint = hint or f"Quarterly results unavailable: {exc}"
            else:
                hint = hint or ("Quarterly results aren't in the free price data. Add a Finnhub key in Monitor → Markets for quarterly earnings, or read the company's "
                                "filing on the NSE or BSE website.")
        if not out and not hint and not st["days"]:
            hint = "Market data is off. Turn on 'India large companies' in Monitor → Markets for real prices."
    cat = next((k for rx, k in _CATEGORY if rx.search(question)), "")
    rows = events(kind=cat, days=7, limit=8) if cat else events(days=14, q=search_text or question, limit=6)
    for e in rows[:6]:
        out.append(WebResult(url=e["url"] or "https://localhost/", title=f"{e['title']} ({e['severity']})", snippet=e["summary"][:240],
                             content=f"{e['title']}. {e['summary']} Severity: {e['severity']}. Reported {e['occurred_at'][:10]} by {e['source']}.",
                             domain=e["source"]))
    if rows and not why:
        why = "Events stored on this computer by your monitors"
    return out, why, hint


# ---------------------------------------------------------------- the brief
BRIEF_PROMPT = """You write a short morning briefing from numbered EVIDENCE only.
Sections, each with 2 to 5 bullets: **Markets**, **World**, **Cyber**, **Needs attention**. Skip a section with no evidence.
Cite every bullet with its evidence number like [3]. Use only facts in the evidence: no invented prices, names or numbers.
If something important is missing, say what's missing in one line at the end."""


def gather_evidence(hours: int = 24) -> tuple[list[dict], str]:
    items: list[dict] = []
    for d, label in ((1, "Indian stocks, 1 day"), (30, "Indian stocks, 30 days")):
        mv = markets.movers(d, 5)
        if mv.get("ok"):
            items.append({"text": markets.top_text(mv), "url": "https://finance.yahoo.com/", "title": f"{label}, to {mv['latest']}"})
    for e in events(group="crisis", min_severity="medium", days=max(1, hours // 24 or 1), limit=8) + events(group="cyber", min_severity="high", days=max(1, hours // 24 or 1), limit=6):
        items.append({"text": f"{e['title']}. {e['summary'][:300]} ({e['severity']})", "url": e["url"], "title": e["title"]})
    for a in alerts(30):
        if (_now() - _aware(dt.datetime.fromisoformat(a["created_at"]))) < dt.timedelta(hours=hours):
            items.append({"text": f"ALERT: {a['title']}. {a['body'][:200]}", "url": a["url"], "title": a["title"]})
    text = "\n\n".join(f"[{i}] {it['text']}" for i, it in enumerate(items, 1))
    return items, text


async def generate_brief(hours: int = 24) -> dict:
    from app.llm.base import ChatMessage
    from app.llm.registry import get_router

    items, evidence = gather_evidence(hours)
    if not items:
        raise RuntimeError("Nothing to brief on yet. Turn on a few monitors in the Monitor page first.")
    result = await get_router().complete([ChatMessage("system", BRIEF_PROMPT), ChatMessage("user", f"EVIDENCE:\n{evidence}")], temperature=0.2, max_tokens=1500, task="research")
    body = result.text.strip()
    title = f"Brief for {dt.date.today():%d %b %Y}"
    sources = [{"n": i, "title": it["title"], "url": it["url"]} for i, it in enumerate(items, 1)]
    with session_scope() as s:
        b = MonitorBrief(title=title, body=body, sources=sources, model=f"{result.provider}:{result.model}")
        s.add(b)
        s.flush()
        out = {"id": b.id, "title": title, "body": body, "sources": sources, "model": b.model, "created_at": b.created_at.isoformat()}
    try:
        from app import access

        access.emit("monitor.brief", {"title": title, "body": body})
    except Exception:  # noqa: BLE001
        pass
    return out


def briefs(limit: int = 10) -> list[dict]:
    with session_scope() as s:
        return [{"id": b.id, "title": b.title, "body": b.body, "sources": b.sources, "model": b.model, "created_at": b.created_at.isoformat()}
                for b in s.scalars(select(MonitorBrief).order_by(MonitorBrief.created_at.desc()).limit(limit))]
