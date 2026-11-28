"""Parsers and fetchers for each open data source. Parsers are pure functions (text in, rows out) so they can be tested.

Every source here is public and free to read:
- USGS earthquakes (GeoJSON), GDACS disaster alerts (RSS), WHO news feed (disease outbreak items), GDELT DOC news search;
- CISA Known Exploited Vulnerabilities (JSON), NVD recent CVEs (API 2.0), abuse.ch ThreatFox (free key, optional);
- NSE end-of-day "bhavcopy" files and Yahoo Finance daily charts for prices.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import logging
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote

log = logging.getLogger(__name__)
UA = "Mozilla/5.0 (compatible; KeiroLake monitor; +https://github.com/KumarRoushan9234/Keirolake)"
SEVERITY = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def sev(name: str) -> tuple[str, int]:
    name = name if name in SEVERITY else "info"
    return name, SEVERITY[name]


def _day(ts: float | int | str | None) -> dt.datetime:
    if isinstance(ts, (int, float)):
        return dt.datetime.fromtimestamp(ts / 1000 if ts > 1e11 else ts, dt.timezone.utc)
    if isinstance(ts, str):
        for fmt in ("%Y-%m-%d", "%Y%m%dT%H%M%SZ", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
            try:
                return dt.datetime.strptime(ts[:len(dt.datetime.now().strftime(fmt))], fmt).replace(tzinfo=dt.timezone.utc)
            except ValueError:
                continue
    return dt.datetime.now(dt.timezone.utc)


def _event(source: str, ext_id: str, kind: str, title: str, **kw) -> dict:
    severity, n = sev(kw.pop("severity", "info"))
    return {"source": source, "ext_id": str(ext_id)[:200], "kind": kind, "title": title.strip()[:500], "severity": severity,
            "severity_n": n, "summary": (kw.pop("summary", "") or "").strip()[:2000], "url": kw.pop("url", "") or "",
            "country": kw.pop("country", "") or "", "lat": kw.pop("lat", None), "lon": kw.pop("lon", None),
            "occurred_at": kw.pop("occurred_at", None) or dt.datetime.now(dt.timezone.utc), "data": kw.pop("data", {})}


# ---------------------------------------------------------------- crises
def parse_usgs(raw: dict, min_mag: float = 4.5) -> list[dict]:
    out = []
    for f in raw.get("features", []):
        p, coords = f.get("properties") or {}, (f.get("geometry") or {}).get("coordinates") or [None, None, None]
        mag = p.get("mag")
        if mag is None or mag < min_mag:
            continue
        level = "critical" if mag >= 7 else "high" if mag >= 6 else "medium" if mag >= 5 else "low"
        place = p.get("place") or ""
        out.append(_event("crisis.usgs", f["id"], "quake", p.get("title") or f"M {mag} {place}", severity=level, url=p.get("url", ""),
                          summary=f"Magnitude {mag}, depth {coords[2]} km" + (", tsunami warning issued" if p.get("tsunami") else ""),
                          country=place.rsplit(",", 1)[-1].strip(), lat=coords[1], lon=coords[0], occurred_at=_day(p.get("time")),
                          data={"magnitude": mag, "depth_km": coords[2], "tsunami": bool(p.get("tsunami"))}))
    return out


_NS = {"gdacs": "http://www.gdacs.org", "geo": "http://www.w3.org/2003/01/geo/wgs84_pos#", "dc": "http://purl.org/dc/elements/1.1/"}
_GDACS_TYPES = {"EQ": "Earthquake", "TC": "Tropical cyclone", "FL": "Flood", "VO": "Volcano", "DR": "Drought", "WF": "Wildfire", "TS": "Tsunami"}


def _xml(text: str) -> ET.Element:
    return ET.fromstring(text.lstrip("﻿").encode("utf-8"))


def _rfc822(text: str | None) -> dt.datetime:
    from email.utils import parsedate_to_datetime

    try:
        d = parsedate_to_datetime(text or "")
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return dt.datetime.now(dt.timezone.utc)


def parse_gdacs(text: str) -> list[dict]:
    out = []
    for item in _xml(text).iter("item"):
        def g(tag, ns=None):
            el = item.find(f"{{{_NS[ns]}}}{tag}" if ns else tag)
            return (el.text or "").strip() if el is not None and el.text else ""
        kind = g("eventtype", "gdacs")
        level = g("alertlevel", "gdacs").lower()
        severity = {"red": "critical", "orange": "high", "green": "low"}.get(level, "info")
        title = g("title") or f"{_GDACS_TYPES.get(kind, kind)} alert"
        point = item.find(f"{{{_NS['geo']}}}Point")
        lat = (point.findtext(f"{{{_NS['geo']}}}lat") or "").strip() if point is not None else ""
        lon = (point.findtext(f"{{{_NS['geo']}}}long") or "").strip() if point is not None else ""
        sv = item.find(f"{{{_NS['gdacs']}}}severity")
        out.append(_event("crisis.gdacs", f"{kind}{g('eventid', 'gdacs')}-{g('episodeid', 'gdacs')}", "disaster", title, severity=severity,
                          url=g("link"), summary=g("description"), country=g("country", "gdacs"),
                          lat=float(lat) if lat else None, lon=float(lon) if lon else None, occurred_at=_rfc822(g("pubDate")),
                          data={"type": _GDACS_TYPES.get(kind, kind), "alert": level,
                                "severity": (sv.text if sv is not None else ""), "unit": (sv.get("unit") if sv is not None else "")}))
    return out


def parse_who(raw: dict) -> list[dict]:
    """Disease Outbreak News from WHO's own API (the public news feed no longer carries them)."""
    out = []
    for it in raw.get("value", []):
        slug = it.get("UrlName") or ""
        title = (it.get("Title") or "").strip() or slug
        out.append(_event("crisis.who", slug, "outbreak", title, severity="medium",
                          url=f"https://www.who.int/emergencies/disease-outbreak-news/item/{slug}",
                          summary=re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", it.get("Summary") or ""))[:1500],
                          country=title.rsplit(" - ", 1)[-1] if " - " in title else "",
                          occurred_at=_day((it.get("PublicationDateAndTime") or "")[:19])))
    return out


def parse_gdelt(raw: dict, query: str = "") -> list[dict]:
    out = []
    for a in raw.get("articles", []):
        url = a.get("url", "")
        if not url:
            continue
        out.append(_event("crisis.gdelt", hashlib.sha1(url.encode()).hexdigest()[:20], "news", a.get("title", "") or url, severity="info", url=url,
                          summary=f"{a.get('domain', '')} · {a.get('language', '')}", country=a.get("sourcecountry", ""),
                          occurred_at=_day(a.get("seendate")), data={"query": query, "domain": a.get("domain", "")}))
    return out


# ---------------------------------------------------------------- cyber
def parse_kev(raw: dict, days: int = 90) -> list[dict]:
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    out = []
    for v in raw.get("vulnerabilities", []):
        if (v.get("dateAdded") or "") < cutoff:
            continue
        ransom = (v.get("knownRansomwareCampaignUse") or "").lower() == "known"
        cve = v.get("cveID", "")
        out.append(_event("cyber.kev", cve, "cve", f"{cve}: {v.get('vendorProject', '')} {v.get('product', '')} - {v.get('vulnerabilityName', '')}",
                          severity="critical" if ransom else "high", url=f"https://nvd.nist.gov/vuln/detail/{cve}",
                          summary=f"{v.get('shortDescription', '')} Required action: {v.get('requiredAction', '')} Due: {v.get('dueDate', '')}."
                                  + (" Known to be used in ransomware campaigns." if ransom else ""),
                          occurred_at=_day(v.get("dateAdded")),
                          data={"cve": cve, "vendor": v.get("vendorProject", ""), "product": v.get("product", ""), "ransomware": ransom,
                                "due": v.get("dueDate", "")}))
    return out


def parse_nvd(raw: dict) -> list[dict]:
    out = []
    for item in raw.get("vulnerabilities", []):
        c = item.get("cve") or {}
        cve = c.get("id", "")
        desc = next((d["value"] for d in c.get("descriptions", []) if d.get("lang") == "en"), "")
        metrics = c.get("metrics") or {}
        m = next((metrics[k][0] for k in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30") if metrics.get(k)), None)
        data = (m or {}).get("cvssData") or {}
        severity = (data.get("baseSeverity") or "").lower()
        score = data.get("baseScore")
        out.append(_event("cyber.nvd", cve, "cve", f"{cve} (CVSS {score}): {desc[:140]}", severity=severity if severity in SEVERITY else "info",
                          url=f"https://nvd.nist.gov/vuln/detail/{cve}", summary=desc, occurred_at=_day(c.get("published")),
                          data={"cve": cve, "score": score}))
    return out


def parse_threatfox(raw: dict) -> list[dict]:
    out = []
    for r in raw.get("data", []) if isinstance(raw.get("data"), list) else []:
        out.append(_event("cyber.threatfox", r.get("id"), "ioc", f"{r.get('threat_type_desc') or 'IOC'}: {r.get('ioc')} ({r.get('malware_printable', '')})",
                          severity="high" if (r.get("confidence_level") or 0) >= 75 else "medium", url=f"https://threatfox.abuse.ch/ioc/{r.get('id')}/",
                          summary=f"{r.get('ioc_type')} · confidence {r.get('confidence_level')} · tags {', '.join(r.get('tags') or [])}",
                          occurred_at=_day((r.get("first_seen") or "")[:10]), data={"ioc": r.get("ioc"), "malware": r.get("malware_printable")}))
    return out


# ---------------------------------------------------------------- markets
def parse_bhavcopy(text: str) -> tuple[str, list[dict]]:
    """NSE's end-of-day file for all equities: (day, rows). Rows are EQ-series stocks with close, previous close and turnover."""
    rows, day = [], ""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")), skipinitialspace=True)
    reader.fieldnames = [f.strip() for f in (reader.fieldnames or [])]
    for r in reader:
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        if r.get("SERIES") != "EQ":
            continue
        try:
            d = dt.datetime.strptime(r["DATE1"], "%d-%b-%Y").date().isoformat()
            close = float(r["CLOSE_PRICE"])
        except (KeyError, ValueError):
            continue
        day = day or d

        def num(key):
            try:
                return float(r[key])
            except (KeyError, ValueError):
                return None
        rows.append({"symbol": f"{r['SYMBOL']}.NS", "day": d, "close": close, "prev_close": num("PREV_CLOSE"), "open": num("OPEN_PRICE"),
                     "high": num("HIGH_PRICE"), "low": num("LOW_PRICE"), "volume": num("TTL_TRD_QNTY"), "turnover": num("TURNOVER_LACS"),
                     "source": "nse", "currency": "INR"})
    return day, rows


def bhavcopy_url(day: dt.date) -> str:
    return f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{day:%d%m%Y}.csv"


def parse_yahoo_chart(raw: dict) -> list[dict]:
    res = ((raw.get("chart") or {}).get("result") or [None])[0]
    if not res:
        return []
    meta, ts = res.get("meta") or {}, res.get("timestamp") or []
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    offset = meta.get("gmtoffset") or 0
    out, prev = [], None
    for i, t in enumerate(ts):
        close = (q.get("close") or [None] * len(ts))[i]
        if close is None:
            continue

        def at(key):
            vals = q.get(key) or []
            return vals[i] if i < len(vals) else None
        out.append({"symbol": meta.get("symbol", ""), "day": dt.datetime.fromtimestamp(t + offset, dt.timezone.utc).date().isoformat(),
                    "close": float(close), "prev_close": prev, "open": at("open"), "high": at("high"), "low": at("low"),
                    "volume": at("volume"), "turnover": None, "source": "yahoo", "currency": meta.get("currency", "")})
        prev = float(close)
    return out


def yahoo_url(symbol: str, range_: str = "3mo") -> str:
    return f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol)}?range={range_}&interval=1d"


# ---------------------------------------------------------------- fetching
async def get_text(url: str, *, params: dict | None = None, headers: dict | None = None, timeout: float = 45) -> tuple[int, str]:
    from app import http

    r = await http.client("monitor", follow_redirects=True).get(url, params=params, headers={"User-Agent": UA, **(headers or {})}, timeout=timeout)
    return r.status_code, r.text


async def get_json(url: str, **kw) -> dict:
    status, text = await get_text(url, **kw)
    if status >= 400:
        raise RuntimeError(f"{url.split('/')[2]} answered {status}" + (" (too many requests: it allows one every 5 seconds)" if status == 429 else ""))
    try:
        return json.loads(text)
    except ValueError:
        raise RuntimeError(f"{url.split('/')[2]} didn't answer with data: {text[:120]!r}") from None


_gdelt_last = 0.0


async def gdelt_search(query: str, timespan: str = "1d", maxrecords: int = 25) -> dict:
    """GDELT DOC 2.0 allows one request every 5 seconds: wait our turn, and wrap OR queries in parentheses."""
    import asyncio
    import time

    global _gdelt_last
    wait = _gdelt_last + 5.5 - time.monotonic()
    if wait > 0:
        await asyncio.sleep(wait)
    _gdelt_last = time.monotonic()
    q = f"({query})" if " OR " in query and not query.startswith("(") else query
    return await get_json(GDELT_URL, params={"query": q, "mode": "artlist", "format": "json", "maxrecords": maxrecords,
                                              "timespan": timespan, "sort": "datedesc"})


USGS_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson"
GDACS_URL = "https://www.gdacs.org/xml/rss.xml"
WHO_URL = "https://www.who.int/api/news/diseaseoutbreaknews?sf_culture=en&$orderby=PublicationDateAndTime%20desc&$top=30&$select=Title,PublicationDateAndTime,UrlName,Summary"
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
NVD_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
THREATFOX_URL = "https://threatfox-api.abuse.ch/api/v1/"
