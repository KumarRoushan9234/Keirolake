"""More ways to collect data, each landing in a table: RenderData scrapers and free open-data APIs.

RenderData (renderdata.cloud) runs ready-made scrapers: AI answers from ChatGPT, Gemini and Google AI Mode, Google Trends,
any page as markdown, and more. Its catalog is public; running one needs the user's RenderData key and spends their
RenderData credits (a failed run costs nothing). KeiroLake reads the live catalog, so new scrapers appear on their own.

The open-data tools are public APIs that need no key and don't overlap with KeiroLabs (web search and research) or
RenderData (scraping): scholarly works, papers, books, code, places, development indicators. Each one is a connector
template, so a result can be refreshed on a schedule like any other connector.
"""
from __future__ import annotations

import json
import os
import re
import time
from urllib.parse import quote

import httpx

DEFAULT_RENDERDATA = "https://api.renderdata.cloud"
SCRAPER_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")  # "url" means: let RenderData pick the scraper for a link
_catalog: dict = {"at": 0.0, "data": None}


class DataToolError(ValueError):
    pass


# ---------- open-data templates ----------
# {q} is the user's input, URL-encoded. json_path points at the list of records; key_column keeps refreshes from duplicating.
TEMPLATES = [
    {"id": "openalex", "name": "OpenAlex", "about": "Scholarly works: titles, authors, citations, open-access links", "kind": "rest",
     "url": "https://api.openalex.org/works?search={q}&per-page=50", "json_path": "results", "key_column": "id",
     "input": "Topic", "example": "solar panel efficiency", "licence": "CC0"},
    {"id": "arxiv", "name": "arXiv", "about": "Preprints in physics, maths, computer science and more", "kind": "rss",
     "url": "https://export.arxiv.org/api/query?search_query=all:{q}&max_results=50&sortBy=submittedDate", "key_column": "id",
     "input": "Search terms", "example": "retrieval augmented generation", "licence": "Metadata CC0"},
    {"id": "crossref", "name": "Crossref", "about": "Journal articles and books with DOIs", "kind": "rest",
     "url": "https://api.crossref.org/works?query={q}&rows=50", "json_path": "message.items", "key_column": "DOI",
     "input": "Search terms", "example": "battery recycling", "licence": "Metadata CC0"},
    {"id": "openlibrary", "name": "Open Library", "about": "Books: titles, authors, years, editions", "kind": "rest",
     "url": "https://openlibrary.org/search.json?q={q}&limit=50", "json_path": "docs", "key_column": "key",
     "input": "Title, author or subject", "example": "data engineering", "licence": "Open data"},
    {"id": "wikipedia", "name": "Wikipedia search", "about": "Matching articles with short snippets", "kind": "rest",
     "url": "https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={q}&format=json&srlimit=50", "json_path": "query.search",
     "key_column": "pageid", "input": "Search terms", "example": "monsoon India", "licence": "CC BY-SA"},
    {"id": "github", "name": "GitHub repositories", "about": "Open-source projects with stars, language and topics", "kind": "rest",
     "url": "https://api.github.com/search/repositories?q={q}&per_page=50&sort=stars", "json_path": "items", "key_column": "id",
     "input": "Search terms", "example": "vector database", "licence": "Public metadata"},
    {"id": "hackernews", "name": "Hacker News", "about": "Stories and discussion counts", "kind": "rest",
     "url": "https://hn.algolia.com/api/v1/search?query={q}&tags=story&hitsPerPage=50", "json_path": "hits", "key_column": "objectID",
     "input": "Search terms", "example": "local LLM", "licence": "Public"},
    {"id": "npm", "name": "npm packages", "about": "JavaScript packages with scores and links", "kind": "rest",
     "url": "https://registry.npmjs.org/-/v1/search?text={q}&size=50", "json_path": "objects", "key_column": "package_name",
     "input": "Search terms", "example": "markdown parser", "licence": "Public metadata"},
    {"id": "worldbank", "name": "World Bank indicator", "about": "One development indicator for every country, 2000 onwards", "kind": "rest",
     "url": "https://api.worldbank.org/v2/country/all/indicator/{q}?format=json&per_page=20000&date=2000:2030", "json_path": "[1]",
     "key_column": "", "input": "Indicator code", "example": "SP.POP.TOTL", "licence": "CC BY 4.0"},
    {"id": "nominatim", "name": "OpenStreetMap places", "about": "Places with coordinates and addresses", "kind": "rest",
     "url": "https://nominatim.openstreetmap.org/search?q={q}&format=json&limit=50&addressdetails=1", "json_path": "", "key_column": "place_id",
     "input": "Place or address", "example": "libraries in Pune", "licence": "ODbL"},
]
BY_ID = {t["id"]: t for t in TEMPLATES}


def template_url(template_id: str, value: str) -> dict:
    t = BY_ID.get(template_id)
    if t is None:
        raise DataToolError("Unknown data tool")
    value = value.strip()
    if not value or len(value) > 300:
        raise DataToolError(f"Enter {t['input'].lower()} (up to 300 characters)")
    if t["id"] == "worldbank" and not re.fullmatch(r"[A-Za-z0-9._]{2,40}", value):
        raise DataToolError("Use an indicator code such as SP.POP.TOTL or NY.GDP.MKTP.CD")
    q = quote(value, safe="") if t["id"] != "arxiv" else quote(value.replace(" ", "+"), safe="+")
    return {"kind": t["kind"], "url": t["url"].replace("{q}", q), "json_path": t.get("json_path", ""), "key_column": t["key_column"]}


# ---------- RenderData ----------
def rd_base() -> str:
    """The RenderData API address. Only https (or this computer, for a local RenderData) so the key never travels in clear."""
    raw = os.getenv("RENDERDATA_BASE_URL", "").strip().rstrip("/")
    if raw.startswith("https://") or raw.startswith(("http://127.0.0.1", "http://localhost")):
        return raw
    return DEFAULT_RENDERDATA


def rd_key() -> str:
    return os.getenv("RENDERDATA_API_KEY", "").strip()


async def rd_catalog(force: bool = False) -> dict:
    """The live scraper list (public, cached for ten minutes)."""
    if not force and _catalog["data"] is not None and time.monotonic() - _catalog["at"] < 600:
        return _catalog["data"]
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "KeiroLake"}) as client:
        resp = await client.get(rd_base() + "/v1/scrapers")
    if resp.status_code != 200:
        raise DataToolError(f"RenderData answered {resp.status_code}")
    body = resp.json()
    data = {"groups": body.get("groups", []), "scrapers": [
        {k: s.get(k) for k in ("id", "group", "site", "name", "about", "credits", "input", "fields", "available", "unavailableReason")}
        for s in body.get("scrapers", []) if SCRAPER_ID.match(str(s.get("id", "")))]}
    _catalog.update(at=time.monotonic(), data=data)
    return data


async def rd_run(scraper: str, value: str) -> dict:
    """Run one RenderData scraper. Spends the user's RenderData credits; failed runs are refunded by RenderData."""
    if not SCRAPER_ID.match(scraper):
        raise DataToolError("Unknown scraper")
    if not rd_key():
        raise DataToolError("Add your RenderData API key first (Data tools page)")
    value = value.strip()
    if not value or len(value) > 1000:
        raise DataToolError("Enter the input for this scraper (up to 1,000 characters)")
    # "url" is RenderData's auto mode: paste any link and it picks the scraper (unknown sites are read as an article).
    path, payload = ("/v1/run-url", {"url": value}) if scraper == "url" else (f"/v1/scrapers/{scraper}/run", {"input": value})
    async with httpx.AsyncClient(timeout=240, headers={"Authorization": f"Bearer {rd_key()}", "User-Agent": "KeiroLake"}) as client:
        resp = await client.post(rd_base() + path, json=payload)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    messages = {401: "RenderData didn't accept the key", 402: "Your RenderData credits have run out. Top up at renderdata.cloud.",
                403: "RenderData refused this run (a blocked scraper, a suspended account, or a trial key outside runs)",
                404: "RenderData doesn't have that scraper", 502: f"The page returned no data ({body.get('error') or 'empty'}). Not billed.",
                503: "That RenderData tool is unavailable right now. Not billed; try again later."}
    if resp.status_code == 429:
        raise DataToolError(f"RenderData is rate limiting this key ({body.get('code', 'rate_limited')}); try again in {resp.headers.get('Retry-After', 'a few')} seconds")
    if resp.status_code in messages:
        raise DataToolError(messages[resp.status_code])
    if resp.status_code >= 400 or body.get("ok") is False:
        raise DataToolError(f"RenderData: {body.get('error') or resp.status_code}")
    return body


async def rd_account() -> dict:
    """Who the key belongs to (GET /me): used to check a key when it is saved."""
    if not rd_key():
        return {"key_set": False}
    async with httpx.AsyncClient(timeout=20, headers={"Authorization": f"Bearer {rd_key()}", "User-Agent": "KeiroLake"}) as client:
        resp = await client.get(rd_base() + "/me")
    if resp.status_code != 200:
        return {"key_set": True, "valid": False, "error": "RenderData didn't accept the key" if resp.status_code in (401, 403) else f"RenderData answered {resp.status_code}"}
    me = resp.json()
    return {"key_set": True, "valid": True, "email": me.get("email"), "key_prefix": me.get("apiKeyPrefix")}


def rows_from_result(body: dict) -> list[dict]:
    """A run's data as rows: the biggest list of records inside it, or the whole answer as one row."""
    from app.connectors import _flatten

    data = body.get("data")
    if isinstance(data, list):
        lists = [data]
    elif isinstance(data, dict):
        lists = [v for v in data.values() if isinstance(v, list) and v and all(isinstance(x, dict) for x in v)]
    else:
        lists = []
    if lists:
        best = max(lists, key=len)
        return [_flatten(x) for x in best[:20000]]
    if isinstance(data, dict) and data:
        return [_flatten({k: (json.dumps(v, ensure_ascii=False)[:4000] if isinstance(v, (list, dict)) else v) for k, v in data.items()})]
    raise DataToolError("That run returned no data")
