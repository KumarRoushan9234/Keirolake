"""Prices kept on this computer, and what can be worked out from them: day, week and month movers, quotes and history.

Prices come from sources that allow programs to read them: Yahoo Finance daily charts (a watchlist, plus about 50 large
Indian companies) and, with a key, Finnhub. NSE's end-of-day file for the whole exchange can be added by importing the
file you download yourself from nseindia.com. Nothing here is invented: when there isn't enough stored history, the
answer says so and says what to switch on.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os

from sqlalchemy import delete, func, select

from app.db import session_scope
from app.monitor import sources as S
from app.monitor.models import MarketPrice

log = logging.getLogger(__name__)
SPLIT_JUMP = 0.40  # a one-day change this big is a split or bonus issue (exchanges cap daily moves near 20%), not a real gain or loss
MIN_TURNOVER_LAKH = 100.0  # ₹1 crore traded that day: leaves out thinly traded stocks that jump on a few trades
DEFAULT_WATCHLIST = "^NSEI,^BSESN,RELIANCE.NS,TCS.NS,INFY.NS,HDFCBANK.NS,ICICIBANK.NS,^GSPC,^IXIC,AAPL,MSFT,NVDA"
KEEP_DAYS = 400


class MarketError(RuntimeError):
    pass


def provider() -> str:
    value = os.getenv("MARKET_PROVIDER", "free").lower()
    return value if value in ("free", "finnhub", "web") else "free"


def watchlist() -> list[str]:
    raw = os.getenv("MARKET_WATCHLIST") or DEFAULT_WATCHLIST
    return [s.strip().upper() for s in raw.split(",") if s.strip()][:60]


# ---------------------------------------------------------------- storing
def store_prices(rows: list[dict]) -> int:
    """Insert or replace one row per (symbol, day). Returns how many rows were written."""
    if not rows:
        return 0
    with session_scope() as s:
        for r in rows:
            s.merge(MarketPrice(**{k: r.get(k) for k in ("symbol", "day", "close", "prev_close", "open", "high", "low", "volume", "turnover", "source", "currency")}))
    return len(rows)


def prune(keep_days: int = KEEP_DAYS) -> None:
    cutoff = (dt.date.today() - dt.timedelta(days=keep_days)).isoformat()
    with session_scope() as s:
        s.execute(delete(MarketPrice).where(MarketPrice.day < cutoff))


def stored_days(suffix: str = ".NS") -> list[str]:
    with session_scope() as s:
        return [d for (d,) in s.execute(select(MarketPrice.day).where(MarketPrice.symbol.like(f"%{suffix}")).group_by(MarketPrice.day).order_by(MarketPrice.day))]


def status() -> dict:
    days = stored_days()
    with session_scope() as s:
        symbols = s.scalar(select(func.count(func.distinct(MarketPrice.symbol)))) or 0
        indian = s.scalar(select(func.count(func.distinct(MarketPrice.symbol))).where(MarketPrice.symbol.like("%.NS"))) or 0
    return {"provider": provider(), "days": len(days), "first_day": days[0] if days else "", "last_day": days[-1] if days else "",
            "symbols": symbols, "indian_symbols": indian, "watchlist": watchlist(), "finnhub_key": bool(os.getenv("FINNHUB_API_KEY"))}


# ---------------------------------------------------------------- fetching
LARGE_CAPS = ("ADANIENT ADANIPORTS APOLLOHOSP ASIANPAINT AXISBANK BAJAJ-AUTO BAJFINANCE BAJAJFINSV BEL BHARTIARTL BPCL BRITANNIA CIPLA COALINDIA "
              "DRREDDY EICHERMOT ETERNAL GRASIM HCLTECH HDFCBANK HDFCLIFE HEROMOTOCO HINDALCO HINDUNILVR ICICIBANK INDUSINDBK INFY ITC JIOFIN "
              "JSWSTEEL KOTAKBANK LT M&M MARUTI NESTLEIND NTPC ONGC POWERGRID RELIANCE SBILIFE SBIN SHRIRAMFIN SUNPHARMA TATACONSUMER TATAMOTORS "
              "TATASTEEL TCS TECHM TITAN TRENT ULTRACEMCO WIPRO").split()


def large_cap_symbols() -> list[str]:
    """About 50 of India's largest companies, as Yahoo symbols. A fixed list: it says "large companies", not "the Nifty 50"."""
    return [f"{sym}.NS" for sym in LARGE_CAPS]


def import_bhavcopy(text: str) -> dict:
    """Store an NSE end-of-day file the user downloaded from nseindia.com (all listed equities for one day)."""
    day, rows = S.parse_bhavcopy(text)
    if not rows:
        raise MarketError("That doesn't look like an NSE end-of-day file (sec_bhavdata_full_DDMMYYYY.csv)")
    return {"day": day, "rows": store_prices(rows)}


async def sync_yahoo(symbols: list[str] | None = None, range_: str = "3mo") -> dict:
    """Daily charts for each symbol, four at a time. An unknown symbol is reported, not fatal."""
    written, failed = 0, []
    gate = asyncio.Semaphore(4)

    async def one(sym: str) -> None:
        nonlocal written
        async with gate:
            try:
                written += store_prices(S.parse_yahoo_chart(await S.get_json(S.yahoo_url(sym, range_))))
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{sym}: {exc or type(exc).__name__}")
            await asyncio.sleep(0.25)

    await asyncio.gather(*(one(sym) for sym in (symbols or watchlist())))
    return {"rows": written, "failed": failed}


async def finnhub(path: str, **params) -> dict | list:
    key = os.getenv("FINNHUB_API_KEY", "")
    if not key:
        raise MarketError("Add a Finnhub API key in Monitor → Markets to use it")
    return await S.get_json(f"https://finnhub.io/api/v1/{path}", params={**params, "token": key})


async def earnings(symbol: str) -> list[dict]:
    """Quarterly results (actual vs. expected EPS) from Finnhub. Needs a key; US companies are covered best."""
    rows = await finnhub("stock/earnings", symbol=symbol.upper().removesuffix(".NS"), limit=8)
    return [{"period": r.get("period"), "quarter": r.get("quarter"), "year": r.get("year"), "actual": r.get("actual"),
             "estimate": r.get("estimate"), "surprise_pct": r.get("surprisePercent")} for r in rows or []]


async def live_quote(symbol: str) -> dict:
    q = await finnhub("quote", symbol=symbol.upper().removesuffix(".NS"))
    return {"symbol": symbol.upper(), "price": q.get("c"), "change": q.get("d"), "change_pct": q.get("dp"), "high": q.get("h"),
            "low": q.get("l"), "open": q.get("o"), "prev_close": q.get("pc"), "at": q.get("t")}


# ---------------------------------------------------------------- analysis
def movers(days: int = 30, limit: int = 10, min_turnover: float = MIN_TURNOVER_LAKH) -> dict:
    """Best and worst Indian stocks over `days` calendar days, from stored closes. Never guesses: it says what's missing."""
    with session_scope() as s:
        latest = s.scalar(select(func.max(MarketPrice.day)).where(MarketPrice.symbol.like("%.NS"))) or ""
    if not latest:
        return {"ok": False, "reason": "No Indian prices are stored yet. Turn on 'India large companies' in Monitor → Markets, or import NSE's end-of-day file.",
                "gainers": [], "losers": []}
    since = (dt.date.fromisoformat(latest) - dt.timedelta(days=days + 12)).isoformat()
    with session_scope() as s:
        rows = list(s.scalars(select(MarketPrice).where(MarketPrice.symbol.like("%.NS"), MarketPrice.day >= since).order_by(MarketPrice.symbol, MarketPrice.day)))
    by: dict[str, list[MarketPrice]] = {}
    for r in rows:
        by.setdefault(r.symbol, []).append(r)
    target = (dt.date.fromisoformat(latest) - dt.timedelta(days=days)).isoformat()
    cutoff_fresh = (dt.date.fromisoformat(latest) - dt.timedelta(days=4)).isoformat()
    out, base_days, excluded = [], [], []
    for sym, rs in by.items():
        last = rs[-1]
        if last.day < cutoff_fresh:
            continue  # stopped trading, or no recent price
        base = next((r for r in reversed(rs) if r.day <= target), None)
        if base is None or base.close <= 0 or base.day == last.day:
            continue
        if last.turnover is not None and last.turnover < min_turnover:
            continue  # thinly traded: skip (only known for NSE files)
        window = [r for r in rs if r.day >= base.day]
        jump = max(((b.close / a.close - 1, b.day) for a, b in zip(window, window[1:]) if a.close > 0), key=lambda x: abs(x[0]), default=(0.0, ""))
        if abs(jump[0]) >= SPLIT_JUMP:
            excluded.append({"symbol": sym.removesuffix(".NS"), "day": jump[1], "change_pct": round(jump[0] * 100, 1)})
            continue  # almost certainly a split or bonus issue: its return would be wrong, so it is left out and listed
        base_days.append(base.day)
        out.append({"symbol": sym.removesuffix(".NS"), "close": last.close, "base_close": base.close, "pct": round((last.close / base.close - 1) * 100, 2),
                    "turnover_cr": round((last.turnover or 0) / 100, 1)})
    if not out:
        have = len(stored_days())
        return {"ok": False, "latest": latest, "reason": f"{days}-day movers need about {days} days of history; {have} trading days are stored. "
                                                         "Switch on 'India large companies' (it fetches six months), or import more NSE files.", "gainers": [], "losers": []}
    out.sort(key=lambda x: x["pct"], reverse=True)
    full = len(out) > 300
    return {"ok": True, "latest": latest, "base": min(base_days), "days": days, "universe": len(out), "min_turnover_cr": round(min_turnover / 100, 2),
            "scope": "all NSE stocks" if full else "large Indian companies", "gainers": out[:limit], "losers": out[::-1][:limit], "excluded": excluded[:20],
            "source": "NSE files you imported" if full else "Yahoo Finance daily closes"}


def history(symbol: str, days: int = 90) -> list[dict]:
    symbol = symbol.upper()
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    with session_scope() as s:
        rows = list(s.scalars(select(MarketPrice).where(MarketPrice.symbol == symbol, MarketPrice.day >= cutoff).order_by(MarketPrice.day)))
    return [{"day": r.day, "close": r.close, "open": r.open, "high": r.high, "low": r.low, "volume": r.volume} for r in rows]


def resolve_symbol(text: str) -> str:
    """"reliance", "RELIANCE.NS" and "^NSEI" all find a stored symbol; "" when we have nothing for it."""
    t = text.strip().upper()
    with session_scope() as s:
        for candidate in (t, f"{t}.NS"):
            if s.scalar(select(MarketPrice.symbol).where(MarketPrice.symbol == candidate).limit(1)):
                return candidate
    return ""


def quote(symbol: str) -> dict | None:
    sym = resolve_symbol(symbol)
    if not sym:
        return None
    with session_scope() as s:
        rows = list(s.scalars(select(MarketPrice).where(MarketPrice.symbol == sym).order_by(MarketPrice.day.desc()).limit(2)))
    if not rows:
        return None
    last = rows[0]
    prev = last.prev_close or (rows[1].close if len(rows) > 1 else None)
    hist = history(sym, 365)
    closes = [h["close"] for h in hist]
    return {"symbol": sym, "day": last.day, "close": last.close, "currency": last.currency, "prev_close": prev,
            "change_pct": round((last.close / prev - 1) * 100, 2) if prev else None, "high": last.high, "low": last.low, "volume": last.volume,
            "range_high": max(closes) if closes else None, "range_low": min(closes) if closes else None, "history_days": len(closes), "source": last.source}


def top_text(m: dict) -> str:
    """The movers table as plain text a model (or a person) can quote."""
    if not m.get("ok"):
        return m.get("reason", "No data.")
    lines = [f"Top performers among {m['universe']} {m['scope']} over {m['days']} days, closing prices up to {m['latest']} (source: {m['source']}):"]
    lines += [f"{i}. {r['symbol']}: {r['pct']:+.2f}% (₹{r['base_close']:.2f} → ₹{r['close']:.2f})" for i, r in enumerate(m["gainers"], 1)]
    lines.append("Weakest over the same period:")
    lines += [f"{i}. {r['symbol']}: {r['pct']:+.2f}% (₹{r['base_close']:.2f} → ₹{r['close']:.2f})" for i, r in enumerate(m["losers"], 1)]
    if m.get("excluded"):
        lines.append("Left out because the price jumped overnight, which is almost always a split or bonus issue and not a real gain or loss: "
                     + ", ".join(f"{e['symbol']} ({e['change_pct']:+.0f}% on {e['day']})" for e in m["excluded"]) + ".")
    return "\n".join(lines)
