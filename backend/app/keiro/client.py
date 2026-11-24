"""Async KeiroLabs client.

Uses the v2 REST API (Bearer auth), checked against the live API:
  POST /search/lite     {query, maxResults} -> SERP only (title/url/snippet), very cheap
  POST /search/fast     {query, maxResults} -> SERP with richer snippets
  POST /search/content  {query, maxResults} -> results with full markdown `content`
  POST /extract         {urls: [...]}        -> clean markdown for known URLs
  POST /deep            {query, content}     -> pages crawled in full, ranked by answer likelihood,
                                                plus evidence passages and a confidence (15-50 s, 2 credits)
  POST /sitemap         {url, path_prefix, since, limit} -> every URL a site publishes, with lastmod
"""
from __future__ import annotations

import asyncio
import hashlib
import json as jsonlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field

import httpx

from app import http, usage
from app.config import get_settings

log = logging.getLogger(__name__)


class KeiroError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class KeiroNotConfigured(KeiroError):
    pass


@dataclass
class WebResult:
    url: str
    title: str = ""
    snippet: str = ""
    content: str = ""
    domain: str = ""
    position: int | None = None
    excerpt: str = ""  # deep search: the passage most likely to hold the answer
    likelihood: float | None = None  # deep search: answer likelihood 0..1

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class DeepResult:
    query: str
    results: list[WebResult]
    evidence: list[str]
    confidence: float | None
    answer_status: str
    agreeing_domains: list[str]


@dataclass
class KeiroState:
    credits_remaining: float | None = None
    last_error: str | None = None
    zdr: bool | None = None  # True once KeiroLabs confirmed zero data retention (x-keiro-zdr: on)
    capabilities: dict[str, bool | None] = field(
        default_factory=lambda: {"search_lite": None, "search_fast": None, "search_content": None,
                                 "extract": None, "deep": None, "sitemap": None}
    )


class KeiroClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, timeout: float = 60.0):
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.keiro_api_key
        self.base_url = (base_url or settings.keiro_base_url).rstrip("/")
        self.timeout = timeout
        self.state = KeiroState()
        self._cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()

    CACHE_TTL = 900  # seconds: repeating a question within 15 minutes costs no credits and no wait
    CACHE_SIZE = 256
    CACHEABLE = {"search_lite", "search_fast", "search_content", "extract", "deep", "sitemap"}

    def _cache_key(self, url: str, body: dict) -> str:
        return hashlib.sha256(f"{url}|{jsonlib.dumps(body, sort_keys=True)}".encode()).hexdigest()

    def clear_cache(self) -> int:
        n = len(self._cache)
        self._cache.clear()
        return n

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _track(self, capability: str, ok: bool, data: dict | None = None, error: str | None = None) -> None:
        self.state.capabilities[capability] = ok
        if data and isinstance(data.get("creditsRemaining"), (int, float)):
            self.state.credits_remaining = round(float(data["creditsRemaining"]), 2)
        self.state.last_error = error

    async def _post(self, capability: str, url: str, body: dict, *, timeout: float | None = None) -> dict:
        if not self.configured:
            raise KeiroNotConfigured("Set KEIROLABS_SEARCH_LITE_API_KEY or KEIROLABS_API_KEY in .env")
        settings = get_settings()
        if settings.keiro_zdr:
            body = {**body, "zdr": True}  # KeiroLabs keeps no copy of the query or results
        key = self._cache_key(url, body) if capability in self.CACHEABLE and settings.keiro_cache else None
        if key and key in self._cache and time.monotonic() - self._cache[key][0] < self.CACHE_TTL:
            self._cache.move_to_end(key)
            return self._cache[key][1]
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}
        resp = None
        for attempt in range(2):  # one retry for dropped connections, rate limits and server errors
            try:
                resp = await http.client("keiro").post(url, headers=headers, json=body, timeout=timeout or self.timeout)
            except httpx.HTTPError as exc:
                if attempt == 0 and not isinstance(exc, httpx.ReadTimeout):
                    await asyncio.sleep(0.8)
                    continue
                self._track(capability, False, error=str(exc))
                raise KeiroError(f"Keiro connection failed: {exc}") from exc
            if resp.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                await asyncio.sleep(1.5 if resp.status_code == 429 else 0.8)
                continue
            break
        try:
            data = resp.json()
        except ValueError:
            data = {"error": resp.text[:300]}
        if resp.status_code >= 400 or (isinstance(data, dict) and data.get("error") and not data.get("results")):
            message = {
                401: "Invalid KeiroLabs API key",
                402: "Out of KeiroLabs credits",
                403: "This KeiroLabs key is not allowed to use this endpoint",
            }.get(resp.status_code) or str(data.get("message") or data.get("error") or resp.text)[:300]
            self._track(capability, False, data, message)
            raise KeiroError(message, resp.status_code)
        self._track(capability, True, data)
        self.state.zdr = resp.headers.get("x-keiro-zdr") == "on" if settings.keiro_zdr else False
        if isinstance(data, dict):
            spent = data.get("creditsCharged", data.get("credits_used"))
            if isinstance(spent, (int, float)) and spent > 0:
                usage.add("keiro_credits", float(spent))
        if key:
            self._cache[key] = (time.monotonic(), data)
            while len(self._cache) > self.CACHE_SIZE:
                self._cache.popitem(last=False)
        return data

    @staticmethod
    def _results(data: dict) -> list[WebResult]:
        out = []
        for row in data.get("results") or []:
            if not row.get("url"):
                continue
            out.append(WebResult(
                url=row["url"], title=row.get("title") or "", snippet=row.get("snippet") or "",
                content=row.get("content") or "", domain=row.get("domain") or "", position=row.get("position"),
            ))
        return out

    async def search(self, query: str, max_results: int = 5, mode: str = "lite") -> list[WebResult]:
        """mode: lite | fast | content"""
        if mode not in {"lite", "fast", "content"}:
            raise ValueError("mode must be lite, fast or content")
        data = await self._post(f"search_{mode}", f"{self.base_url}/search/{mode}",
                                {"query": query, "maxResults": max_results})
        return self._results(data)

    EXTRACT_PARALLEL = 4

    async def extract(self, urls: list[str]) -> list[WebResult]:
        """Read pages as clean text. KeiroLabs takes one URL per request, so several are fetched in parallel."""
        urls = list(dict.fromkeys(u for u in urls if u))
        if not urls:
            return []
        gate = asyncio.Semaphore(self.EXTRACT_PARALLEL)

        async def one(url: str) -> tuple[list[WebResult], KeiroError | None]:
            async with gate:
                try:
                    data = await self._post("extract", f"{self.base_url}/extract", {"url": url})
                except KeiroError as exc:
                    return [], exc
            out = []
            for row in data.get("results") or []:
                text = row.get("markdown_content") or row.get("content") or ""
                if row.get("error") or not text:
                    log.warning("Keiro extract failed for %s: %s", row.get("url"), row.get("error"))
                    continue
                out.append(WebResult(url=row.get("url") or url, title=row.get("title") or "", content=text))
            return out, None

        done = await asyncio.gather(*(one(u) for u in urls))
        results = [r for rows, _ in done for r in rows]
        errors = [e for _, e in done if e is not None]
        if errors and not results:
            raise errors[0]  # every page failed: surface the reason (key, credits, ...)
        return results

    async def deep(self, query: str, content: bool = True) -> DeepResult:
        """Deep search: answer-ranked pages crawled in full. Slow (15-50 s), so a long timeout."""
        data = await self._post("deep", f"{self.base_url}/deep", {"query": query[:500], "content": content}, timeout=180)
        results = []
        for row in data.get("results") or []:
            if not row.get("url"):
                continue
            try:
                likelihood = float(row.get("answer_likelihood"))
            except (TypeError, ValueError):
                likelihood = None
            position = row.get("position")
            results.append(WebResult(
                url=row["url"], title=row.get("title") or "", snippet=row.get("snippet") or "",
                content=row.get("content") or "", excerpt=row.get("excerpt") or "", likelihood=likelihood,
                position=int(position) if str(position).isdigit() else None,
            ))
        evidence = [e.get("text", "") if isinstance(e, dict) else str(e) for e in data.get("evidence") or []]
        return DeepResult(query=query, results=results, evidence=[e for e in evidence if e],
                          confidence=data.get("confidence"), answer_status=data.get("answer_status") or "",
                          agreeing_domains=list(data.get("agreeing_domains") or []))

    async def sitemap(self, url: str, path_prefix: str = "", since: str = "", limit: int = 50) -> list[dict]:
        """Every URL a site publishes (robots.txt + sitemaps), newest lastmod first."""
        body: dict = {"url": url, "limit": limit}
        if path_prefix:
            body["path_prefix"] = path_prefix
        if since:
            body["since"] = since
        data = await self._post("sitemap", f"{self.base_url}/sitemap", body)
        rows = [r if isinstance(r, dict) else {"url": str(r), "lastmod": None} for r in data.get("urls") or []]
        rows = [r for r in rows if r.get("url")]
        return sorted(rows, key=lambda r: r.get("lastmod") or "", reverse=True)

    async def probe(self) -> dict:
        """Cheapest possible live check: one lite search with one result."""
        started = time.perf_counter()
        try:
            await self.search("keirolabs", max_results=1, mode="lite")
            ok, error = True, None
        except KeiroError as exc:
            ok, error = False, str(exc)
        return {"configured": self.configured, "ok": ok, "error": error,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "credits_remaining": self.state.credits_remaining, "capabilities": self.state.capabilities}

    def status(self) -> dict:
        return {"configured": self.configured, "credits_remaining": self.state.credits_remaining,
                "last_error": self.state.last_error, "capabilities": self.state.capabilities,
                "zdr": get_settings().keiro_zdr, "zdr_confirmed": self.state.zdr}


_client: KeiroClient | None = None


def get_keiro() -> KeiroClient:
    global _client
    if _client is None:
        _client = KeiroClient()
    return _client
