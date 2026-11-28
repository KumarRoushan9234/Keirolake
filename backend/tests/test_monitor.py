"""The monitoring desk: parsers (on samples shaped like the real feeds), storing, rules, movers and answer context."""
import asyncio
import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db import init_db, session_scope
from app.main import app
from app.monitor import markets, service
from app.monitor import sources as S
from app.monitor.models import MarketPrice, MonitorAlert, MonitorEvent, MonitorRule, MonitorSource

TODAY = dt.date.today()

USGS = {"features": [
    {"id": "us1", "properties": {"mag": 6.7, "title": "M 6.7 - 62 km NNW of Yelizovo, Russia", "place": "62 km NNW of Yelizovo, Russia", "time": 1795880000000,
                                  "url": "https://earthquake.usgs.gov/e/us1", "tsunami": 0}, "geometry": {"coordinates": [158.1, 53.7, 188.4]}},
    {"id": "us2", "properties": {"mag": 4.6, "title": "M 4.6 - Chile", "place": "Chile", "time": 1795880000000, "url": "u2"}, "geometry": {"coordinates": [-70, -30, 10]}},
    {"id": "us3", "properties": {"mag": 3.0, "title": "tiny", "place": "x"}, "geometry": {"coordinates": [0, 0, 1]}}]}

GDACS = """﻿<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:gdacs="http://www.gdacs.org" xmlns:geo="http://www.w3.org/2003/01/geo/wgs84_pos#"><channel>
<item><title>Orange flood in Bangladesh</title><description>Floods affect 1 million people.</description>
<link>http://www.gdacs.org/report.aspx?eventtype=FL&amp;eventid=77</link><pubDate>Sat, 28 Nov 2026 13:26:16 GMT</pubDate>
<geo:Point><geo:lat>23.7</geo:lat><geo:long>90.4</geo:long></geo:Point>
<gdacs:eventtype>FL</gdacs:eventtype><gdacs:eventid>77</gdacs:eventid><gdacs:episodeid>3</gdacs:episodeid>
<gdacs:alertlevel>Orange</gdacs:alertlevel><gdacs:country>Bangladesh</gdacs:country><gdacs:severity unit="km2" value="1">Flood area</gdacs:severity></item>
<item><title>Green forest fire in Mali</title><link>http://x</link><pubDate>Sat, 28 Nov 2026 10:00:00 GMT</pubDate>
<gdacs:eventtype>WF</gdacs:eventtype><gdacs:eventid>5</gdacs:eventid><gdacs:alertlevel>Green</gdacs:alertlevel></item></channel></rss>"""

WHO = {"value": [{"UrlName": "2026-DON629", "Title": "Influenza A(H5N1) - United States of America", "PublicationDateAndTime": "2026-11-24T10:52:39Z",
                  "Summary": "<p>Three <b>confirmed</b> human infections.</p>"}]}

KEV = {"vulnerabilities": [
    {"cveID": "CVE-2026-1111", "vendorProject": "Fortinet", "product": "FortiOS", "vulnerabilityName": "Auth bypass", "dateAdded": TODAY.isoformat(),
     "shortDescription": "Bypass.", "requiredAction": "Patch.", "dueDate": "2026-12-20", "knownRansomwareCampaignUse": "Known"},
    {"cveID": "CVE-2026-2222", "vendorProject": "Acme", "product": "Widget", "vulnerabilityName": "RCE", "dateAdded": TODAY.isoformat(),
     "shortDescription": "RCE.", "requiredAction": "Patch.", "dueDate": "2026-12-20", "knownRansomwareCampaignUse": "Unknown"},
    {"cveID": "CVE-2020-0001", "vendorProject": "Old", "product": "Thing", "vulnerabilityName": "Old", "dateAdded": "2020-01-01", "shortDescription": "", "requiredAction": "", "dueDate": ""}]}

NVD = {"vulnerabilities": [{"cve": {"id": "CVE-2026-3333", "published": "2026-11-27T10:00:00.000", "descriptions": [{"lang": "en", "value": "Heap overflow in Foo."}],
                                     "metrics": {"cvssMetricV31": [{"cvssData": {"baseScore": 9.8, "baseSeverity": "CRITICAL"}}]}}}]}


def bhav(day: dt.date, rows: dict[str, tuple[float, float, float]]) -> str:
    head = "SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER"
    lines = [head]
    for sym, (prev, close, turnover) in rows.items():
        lines.append(f"{sym}, EQ, {day:%d-%b-%Y}, {prev}, {prev}, {close}, {prev}, {close}, {close}, {close}, 1000, {turnover}, 10, 500, 50.0")
    lines.append(f"NOTEQ, BE, {day:%d-%b-%Y}, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1")
    return "\n".join(lines)


YAHOO = {"chart": {"result": [{"meta": {"symbol": "AAPL", "currency": "USD", "gmtoffset": -18000},
                                "timestamp": [1795000000 + i * 86400 for i in range(3)],
                                "indicators": {"quote": [{"open": [1, 2, 3], "high": [2, 3, 4], "low": [1, 1, 2], "close": [10.0, 11.0, None], "volume": [5, 6, 7]}]}}]}}


@pytest.fixture(autouse=True)
def clean():
    init_db()
    with session_scope() as s:
        for model in (MonitorAlert, MonitorEvent, MarketPrice, MonitorRule, MonitorSource):
            s.execute(delete(model))
    service.ensure_defaults()
    yield


# ---------- parsers ----------
def test_usgs_keeps_real_quakes_with_severity_and_place():
    ev = S.parse_usgs(USGS)
    assert [e["ext_id"] for e in ev] == ["us1", "us2"]  # the 3.0 is below the floor
    assert ev[0]["severity"] == "high" and ev[0]["country"] == "Russia" and ev[0]["lat"] == 53.7 and ev[1]["severity"] == "low"


def test_gdacs_levels_and_coordinates():
    ev = S.parse_gdacs(GDACS)
    assert ev[0]["severity"] == "high" and ev[0]["lat"] == 23.7 and ev[0]["lon"] == 90.4 and ev[0]["country"] == "Bangladesh"
    assert ev[0]["ext_id"] == "FL77-3" and ev[1]["severity"] == "low" and ev[1]["lat"] is None


def test_who_outbreaks():
    ev = S.parse_who(WHO)
    assert ev[0]["title"].startswith("Influenza A(H5N1)") and ev[0]["country"] == "United States of America"
    assert ev[0]["url"].endswith("2026-DON629") and "<" not in ev[0]["summary"]


def test_kev_marks_ransomware_critical_and_skips_old_entries():
    ev = S.parse_kev(KEV)
    assert [e["ext_id"] for e in ev] == ["CVE-2026-1111", "CVE-2026-2222"]
    assert ev[0]["severity"] == "critical" and ev[1]["severity"] == "high" and ev[0]["data"]["vendor"] == "Fortinet"


def test_nvd_uses_cvss_severity():
    e = S.parse_nvd(NVD)[0]
    assert e["severity"] == "critical" and "9.8" in e["title"]


def test_bhavcopy_reads_equities_only():
    day, rows = S.parse_bhavcopy(bhav(TODAY, {"RELIANCE": (100.0, 110.0, 5000.0), "TCS": (200.0, 190.0, 100.0)}))
    assert day == TODAY.isoformat() and [r["symbol"] for r in rows] == ["RELIANCE.NS", "TCS.NS"]
    assert rows[0]["close"] == 110.0 and rows[0]["prev_close"] == 100.0 and rows[0]["turnover"] == 5000.0 and rows[0]["currency"] == "INR"


def test_yahoo_chart_rows_skip_missing_closes():
    rows = S.parse_yahoo_chart(YAHOO)
    assert [r["close"] for r in rows] == [10.0, 11.0] and rows[1]["prev_close"] == 10.0 and rows[0]["currency"] == "USD"


# ---------- storing and rules ----------
def test_events_are_stored_once_and_refreshed():
    first = service.store_events(S.parse_gdacs(GDACS))
    assert len(first) == 2
    assert service.store_events(S.parse_gdacs(GDACS)) == []  # nothing new the second time
    changed = S.parse_gdacs(GDACS.replace("Orange", "Red"))
    assert service.store_events(changed) == []
    assert next(e for e in service.events(days=30) if e["source"] == "crisis.gdacs" and "flood" in e["title"].lower())["severity"] == "critical"


def test_default_rules_alert_on_serious_events_only():
    new = service.store_events(S.parse_gdacs(GDACS) + S.parse_kev(KEV) + S.parse_nvd(NVD))
    assert service.evaluate_events(new) == 2  # the orange flood and the ransomware KEV; not the green fire, the plain KEV or the NVD critical
    titles = {a["title"] for a in service.alerts()}
    assert any("flood" in t.lower() for t in titles) and any("CVE-2026-1111" in t for t in titles)
    assert service.evaluate_events(new) == 0  # idempotent


def test_keyword_rule_matches_what_you_watch():
    service.add_rule("keyword", "cyber", {"terms": ["acme"]})
    new = service.store_events(S.parse_kev(KEV))
    service.evaluate_events(new)
    assert any("CVE-2026-2222" in a["title"] for a in service.alerts())


def test_rule_validation():
    with pytest.raises(service.RuleError):
        service.add_rule("keyword", "cyber", {"terms": [" "]})
    with pytest.raises(service.RuleError):
        service.add_rule("nonsense", "any", {})
    assert service.add_rule("price_move", "crisis", {"pct": 3})["scope"] == "market"  # price rules are always about markets


# ---------- markets ----------
def seed_history(days=40, movers=None):
    movers = movers or {"RISER": (100.0, 150.0), "FALLER": (100.0, 60.0), "FLAT": (100.0, 100.0)}
    for i in range(days):
        d = TODAY - dt.timedelta(days=days - 1 - i)
        frac = i / (days - 1)
        rows = {sym: (a + (b - a) * max(0, frac - 0.02), round(a + (b - a) * frac, 2), 5000.0) for sym, (a, b) in movers.items()}
        rows["THIN"] = (10.0, 10.0 + 90 * frac, 1.0)  # a huge jump on almost no trading
        markets.store_prices(S.parse_bhavcopy(bhav(d, rows))[1])


def test_movers_are_computed_from_stored_closes_and_skip_thin_stocks():
    seed_history()
    m = markets.movers(30, 5)
    assert m["ok"] and m["gainers"][0]["symbol"] == "RISER" and m["losers"][0]["symbol"] == "FALLER"
    assert all(r["symbol"] != "THIN" for r in m["gainers"] + m["losers"])
    assert 30 < m["gainers"][0]["pct"] < 52 and m["latest"] == TODAY.isoformat()
    assert "RISER:" in markets.top_text(m) and "₹" in markets.top_text(m)


def test_a_split_or_bonus_is_left_out_not_shown_as_a_crash():
    seed_history()
    for i in range(40):  # steady, then the price halves overnight 5 days ago: a bonus issue, not a 50% loss
        d = TODAY - dt.timedelta(days=39 - i)
        markets.store_prices([{"symbol": "BONUS.NS", "day": d.isoformat(), "close": 1000.0 if i < 35 else 500.0, "prev_close": None, "source": "yahoo", "currency": "INR"}])
    m = markets.movers(30, 10)
    assert all(r["symbol"] != "BONUS" for r in m["gainers"] + m["losers"])
    assert m["excluded"][0]["symbol"] == "BONUS" and m["excluded"][0]["change_pct"] == -50.0
    assert "BONUS (-50% on" in markets.top_text(m) and "split or bonus" in markets.top_text(m)


def test_movers_say_what_is_missing_instead_of_guessing():
    assert markets.movers(30)["ok"] is False and "Turn on" in markets.movers(30)["reason"]
    markets.store_prices(S.parse_bhavcopy(bhav(TODAY, {"AAA": (1.0, 2.0, 500.0)}))[1])
    m = markets.movers(30)
    assert m["ok"] is False and "history" in m["reason"]


def test_quote_and_symbol_resolution():
    seed_history()
    assert markets.resolve_symbol("riser") == "RISER.NS" and markets.resolve_symbol("nothing") == ""
    q = markets.quote("RISER")
    assert q["symbol"] == "RISER.NS" and q["close"] > 140 and q["range_low"] < q["range_high"] and q["history_days"] > 30


def test_an_nse_file_you_download_can_be_imported():
    out = markets.import_bhavcopy(bhav(TODAY, {"RELIANCE": (100.0, 110.0, 5000.0), "TCS": (200.0, 190.0, 100.0)}))
    assert out == {"day": TODAY.isoformat(), "rows": 2}
    assert markets.quote("reliance")["close"] == 110.0 and markets.status()["indian_symbols"] == 2
    with pytest.raises(markets.MarketError, match="NSE end-of-day file"):
        markets.import_bhavcopy("this,is,not\na,bhavcopy,file")


def test_imported_files_over_the_whole_market_give_full_market_movers():
    for i in range(35):  # a month of files for 400 stocks
        d = TODAY - dt.timedelta(days=34 - i)
        rows = {f"S{n:03d}": (100.0, round(100 + (n % 50 - 25) * i / 34, 2), 5000.0) for n in range(400)}
        markets.import_bhavcopy(bhav(d, rows))
    m = markets.movers(30, 5)
    assert m["ok"] and m["scope"] == "all NSE stocks" and m["universe"] == 400 and "imported" in m["source"]


def test_large_company_list_and_yahoo_sync(monkeypatch):
    syms = markets.large_cap_symbols()
    assert 45 <= len(syms) <= 60 and "RELIANCE.NS" in syms and "TCS.NS" in syms and all(x.endswith(".NS") for x in syms)
    calls = []

    async def fake_json(url, **kw):
        calls.append(url)
        if "BAD" in url:
            raise RuntimeError("no data for BAD")
        sym = url.split("/chart/")[1].split("?")[0]
        return {"chart": {"result": [{"meta": {"symbol": sym.replace("%5E", "^"), "currency": "INR", "gmtoffset": 19800},
                                      "timestamp": [1795000000 + i * 86400 for i in range(5)],
                                      "indicators": {"quote": [{"close": [1.0, 2.0, 3.0, 4.0, 5.0]}]}}]}}

    monkeypatch.setattr(S, "get_json", fake_json)
    out = asyncio.run(markets.sync_yahoo(["AAA.NS", "BAD.NS", "CCC.NS"]))
    assert out["rows"] == 10 and len(out["failed"]) == 1 and "BAD.NS" in out["failed"][0]


def test_large_company_movers_need_history_and_say_so():
    for sym in ("AAA.NS", "BBB.NS"):
        markets.store_prices([{"symbol": sym, "day": TODAY.isoformat(), "close": 10.0, "prev_close": 9.0, "source": "yahoo", "currency": "INR"}])
    m = markets.movers(30)
    assert m["ok"] is False and "history" in m["reason"]
    for sym, end in (("AAA.NS", 15.0), ("BBB.NS", 6.0)):
        markets.store_prices([{"symbol": sym, "day": (TODAY - dt.timedelta(days=40 - i)).isoformat(), "close": 10.0 + (end - 10.0) * i / 40, "prev_close": None,
                               "source": "yahoo", "currency": "INR"} for i in range(41)])
    m = markets.movers(30, 5)
    assert m["ok"] and m["scope"] == "large Indian companies" and m["source"].startswith("Yahoo")
    assert m["gainers"][0]["symbol"] == "AAA" and m["losers"][0]["symbol"] == "BBB"


def test_price_move_rule_raises_one_alert_per_day():
    markets.store_prices(S.parse_yahoo_chart(YAHOO))
    markets.store_prices([{"symbol": "AAPL", "day": TODAY.isoformat(), "close": 120.0, "prev_close": 100.0, "source": "yahoo", "currency": "USD"}])
    assert service.evaluate_prices(TODAY.isoformat(), ["AAPL"]) == 1
    assert service.evaluate_prices(TODAY.isoformat(), ["AAPL"]) == 0
    assert "AAPL up 20.0%" in service.alerts()[0]["title"]


# ---------- answers ----------
def test_a_movers_question_gets_real_numbers():
    seed_history()
    sources, why, hint = asyncio.run(service.context_for("pull the top performing stocks for the last 30 days", "top performing stocks last 30 days"))
    assert sources and "RISER:" in sources[0].content and "real" in why.lower() and not hint
    assert sources[0].url.startswith("https://finance.yahoo.com") and "large Indian companies" in sources[0].title


def test_a_market_question_without_data_says_how_to_get_it():
    sources, why, hint = asyncio.run(service.context_for("what are the top gaining stocks in the last 30 days", ""))
    assert not sources and "Turn on" in hint


def test_a_named_stock_and_quarterly_results_are_honest():
    seed_history()
    sources, why, hint = asyncio.run(service.context_for("How did RISER stock do and what were its quarterly results?", ""))
    assert any("RISER.NS" in s.title for s in sources)
    assert "Finnhub" in hint  # no key: it says so instead of making results up


def test_event_questions_use_stored_events():
    service.store_events(S.parse_usgs(USGS) + S.parse_who(WHO))
    sources, why, hint = asyncio.run(service.context_for("any earthquakes today?", "earthquakes"))
    assert sources and "Yelizovo" in sources[0].title
    sources, why, hint = asyncio.run(service.context_for("is there a bird flu outbreak?", "bird flu outbreak"))
    assert any("H5N1" in s.title for s in sources)


def test_web_only_mode_keeps_market_questions_on_the_web(monkeypatch):
    seed_history()
    monkeypatch.setenv("MARKET_PROVIDER", "web")
    assert asyncio.run(service.context_for("top gaining stocks last 30 days", ""))[0] == []


def test_threat_lookup_finds_cves_and_vendors():
    service.store_events(S.parse_kev(KEV))
    assert service.lookup_threat("fortinet")[0]["title"].startswith("CVE-2026-1111")
    assert service.lookup_threat("CVE-2026-2222") and service.lookup_threat("") == []


# ---------- running sources ----------
def test_a_run_records_success_and_failure(monkeypatch):
    async def ok(cfg):
        return {"count": 3, "events": 1}

    async def boom(cfg):
        raise RuntimeError("feed is down")

    monkeypatch.setitem(service.REGISTRY, "crisis.usgs", type(service.REGISTRY["crisis.usgs"])(**{**service.REGISTRY["crisis.usgs"].__dict__, "run": ok}))
    out = asyncio.run(service.run_source("crisis.usgs"))
    assert out["ok"] and out["count"] == 3
    monkeypatch.setitem(service.REGISTRY, "crisis.usgs", type(service.REGISTRY["crisis.usgs"])(**{**service.REGISTRY["crisis.usgs"].__dict__, "run": boom}))
    bad = asyncio.run(service.run_source("crisis.usgs"))
    assert not bad["ok"] and "feed is down" in bad["error"]
    view = next(v for v in service.sources_view() if v["id"] == "crisis.usgs")
    assert view["last_ok"] is False and "feed is down" in view["last_error"]


def test_only_enabled_sources_are_due_after_their_interval():
    assert service.due() == []  # everything starts off
    service.configure("crisis.usgs", enabled=True, interval_min=30)
    assert service.due() == ["crisis.usgs"]
    asyncio.run(service.run_source("crisis.usgs"))  # (offline in tests: records a failure, still counts as a run)
    assert service.due() == []


def test_the_brief_is_written_from_evidence_and_refuses_without_any(monkeypatch):
    with pytest.raises(RuntimeError, match="Nothing to brief"):
        asyncio.run(service.generate_brief())

    class Router:
        async def complete(self, messages, **kw):
            assert "[1]" in messages[1].content and kw["task"] == "research"
            from app.llm.registry import LLMResult

            return LLMResult("**World**\n- A flood hit Bangladesh [1].", "test", "m", [])

    import app.llm.registry as registry

    monkeypatch.setattr(registry, "get_router", lambda: Router())
    service.store_events([{**e, "occurred_at": dt.datetime.now(dt.timezone.utc)} for e in S.parse_gdacs(GDACS)])
    b = asyncio.run(service.generate_brief())
    assert "Bangladesh" in b["body"] and b["sources"][0]["n"] == 1 and service.briefs()[0]["id"] == b["id"]


# ---------- API ----------
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_monitor_api(client):
    out = client.get("/api/monitor").json()
    assert len(out["sources"]) == 10 and all(not s["enabled"] for s in out["sources"])
    assert client.put("/api/monitor/sources/market.largecap", json={"enabled": True, "interval_min": 120}).json()["enabled"] is True
    assert client.put("/api/monitor/sources/nope", json={"enabled": True}).status_code == 404
    assert client.get("/api/monitor/markets/movers?days=30").json()["ok"] is False
    assert client.get("/api/monitor/markets/quote/NOPE").status_code == 404
    rule = client.post("/api/monitor/rules", json={"kind": "keyword", "scope": "cyber", "config": {"terms": ["exchange"]}}).json()
    assert rule["config"]["terms"] == ["exchange"] and client.post("/api/monitor/rules", json={"kind": "keyword", "config": {}}).status_code == 400
    assert client.put(f"/api/monitor/rules/{rule['id']}", json={"enabled": False}).json()["ok"]
    assert client.delete(f"/api/monitor/rules/{rule['id']}").status_code == 204
    service.store_events(S.parse_usgs(USGS))
    assert client.get("/api/monitor/events?group=crisis&q=yelizovo").json()[0]["kind"] == "quake"
    assert client.put("/api/monitor/markets/settings", json={"provider": "web", "watchlist": "aapl, nvda"}).json()["watchlist"] == ["AAPL", "NVDA"]
    assert client.put("/api/monitor/markets/settings", json={"provider": "bogus"}).status_code == 422
    client.put("/api/monitor/markets/settings", json={"provider": "free", "watchlist": ""})
    assert client.get("/api/monitor/briefs").status_code == 200


def test_importing_files_over_the_api(client):
    ok = bhav(TODAY, {"INFY": (100.0, 105.0, 5000.0)}).encode()
    out = client.post("/api/monitor/markets/import", files=[("files", ("sec_bhavdata_full_x.csv", ok, "text/csv")), ("files", ("junk.csv", b"a,b\n1,2", "text/csv"))]).json()
    assert out["days"] == 1 and out["problems"] and "junk.csv" in out["problems"][0] and out["status"]["indian_symbols"] == 1
    assert client.post("/api/monitor/markets/import", files=[("files", ("junk.csv", b"a,b\n1,2", "text/csv"))]).status_code == 400
