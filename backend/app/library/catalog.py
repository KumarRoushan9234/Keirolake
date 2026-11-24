"""Kiwix library catalog (OPDS v2), cached on disk so it can be browsed offline."""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlencode

import httpx

from app.config import get_settings

CATALOG_URL = "https://library.kiwix.org/catalog/v2/entries"
NS = {"a": "http://www.w3.org/2005/Atom", "dc": "http://purl.org/dc/terms/"}
ACQ = "http://opds-spec.org/acquisition/open-access"
CACHE_TTL = 24 * 3600


def _cache_path() -> Path:
    path = get_settings().data_dir / "library" / "catalog-eng.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def parse(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    entries = []
    for e in root.findall("a:entry", NS):
        acq = next((link for link in e.findall("a:link", NS) if link.get("rel") == ACQ), None)
        if acq is None:
            continue
        href = acq.get("href", "")
        url = href[:-6] if href.endswith(".meta4") else href
        tags = (e.findtext("a:tags", "", NS) or "").split(";")
        # The catalog's <name> is shared by every flavour (maxi/nopic/mini); the file name is unique.
        filename = url.rsplit("/", 1)[-1]
        entries.append({
            "name": re.sub(r"_\d{4}-\d{2}\.zim$", "", filename) or e.findtext("a:name", "", NS),
            "title": e.findtext("a:title", "", NS) + (f" ({e.findtext('a:flavour', '', NS)})" if e.findtext("a:flavour", "", NS) else ""),
            "summary": e.findtext("a:summary", "", NS),
            "language": e.findtext("a:language", "", NS),
            "category": e.findtext("a:category", "", NS),
            "flavour": e.findtext("a:flavour", "", NS),
            "articles": int(e.findtext("a:articleCount", "0", NS) or 0),
            "media": int(e.findtext("a:mediaCount", "0", NS) or 0),
            "issued": (e.findtext("dc:issued", "", NS) or e.findtext("a:updated", "", NS))[:10],
            "size_bytes": int(acq.get("length") or 0),
            "url": url,
            "fulltext": "_ftindex:yes" in tags,
            "pictures": "_pictures:yes" in tags,
            "videos": "_videos:yes" in tags,
        })
    return entries


async def load(refresh: bool = False) -> tuple[list[dict], dict]:
    """English catalog entries. Uses the cache when offline or fresh enough."""
    cache = _cache_path()
    fresh = cache.exists() and time.time() - cache.stat().st_mtime < CACHE_TTL
    error = None
    if refresh or not fresh:
        from app.net import connectivity

        if connectivity.is_online:
            try:
                async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                    resp = await client.get(f"{CATALOG_URL}?{urlencode({'lang': 'eng', 'count': 5000})}")
                resp.raise_for_status()
                parse(resp.text)  # validate before replacing the cache
                cache.write_text(resp.text, encoding="utf-8")
            except (httpx.HTTPError, ET.ParseError) as exc:
                error = f"Could not refresh the catalog: {exc}"
        else:
            error = "Offline: showing the saved catalog"
    if not cache.exists():
        return [], {"error": error or "Catalog not downloaded yet", "cached_at": None}
    entries = parse(cache.read_text(encoding="utf-8"))
    return entries, {"error": error, "cached_at": cache.stat().st_mtime, "count": len(entries)}


def search(entries: list[dict], q: str = "", category: str = "", limit: int = 200) -> list[dict]:
    terms = q.lower().split()
    out = []
    for e in entries:
        hay = f"{e['name']} {e['title']} {e['summary']} {e['category']}".lower()
        if all(t in hay for t in terms) and (not category or e["category"] == category):
            out.append(e)
    out.sort(key=lambda e: (-(e["articles"] > 0), e["name"]))
    return out[:limit]
