import httpx
import pytest
import respx

from app.keiro.client import KeiroClient, KeiroError

BASE = "https://api.keirolabs.cloud/api/v2"


@respx.mock
async def test_lite_search_parses_results_and_credits():
    respx.post(f"{BASE}/search/lite").mock(return_value=httpx.Response(200, json={
        "results": [{"title": "Qdrant", "url": "https://qdrant.tech/", "snippet": "vector db",
                     "domain": "qdrant.tech", "position": 1}],
        "creditsRemaining": 123.456}))
    client = KeiroClient()
    results = await client.search("qdrant", 3)
    assert results[0].url == "https://qdrant.tech/" and results[0].snippet == "vector db"
    assert client.state.credits_remaining == 123.46 and client.state.capabilities["search_lite"] is True


@respx.mock
async def test_out_of_credits_and_forbidden_are_clear():
    respx.post(f"{BASE}/search/lite").mock(return_value=httpx.Response(402, json={}))
    respx.post(f"{BASE}/extract").mock(return_value=httpx.Response(403, json={"error": "plan"}))
    client = KeiroClient()
    with pytest.raises(KeiroError, match="credits") as exc:
        await client.search("q")
    assert exc.value.status == 402
    with pytest.raises(KeiroError, match="not allowed"):
        await client.extract(["https://a.com"])
    assert client.state.capabilities["extract"] is False


@respx.mock
async def test_disabled_content_mode_error_is_surfaced():
    respx.post(f"{BASE}/search/lite").mock(return_value=httpx.Response(
        400, json={"error": "content mode temporarily disabled", "message": "Lite is SERP-only"}))
    with pytest.raises(KeiroError, match="SERP-only"):
        await KeiroClient().search("q")


@respx.mock
async def test_deep_search_parses_ranked_pages_evidence_and_confidence():
    respx.post(f"{BASE}/deep").mock(return_value=httpx.Response(200, json={
        "query": "q", "answer_status": "found", "confidence": 0.9, "agreeing_domains": ["a.com", "b.com"],
        "evidence": [{"text": "RRF sums reciprocal ranks."}],
        "results": [{"position": "1", "title": "RRF", "url": "https://a.com/rrf", "excerpt": "sums ranks",
                     "content": "full page", "answer_likelihood": "0.87"}],
        "creditsRemaining": 50}))
    client = KeiroClient()
    deep = await client.deep("What is RRF?")
    assert deep.confidence == 0.9 and deep.evidence == ["RRF sums reciprocal ranks."]
    page = deep.results[0]
    assert (page.likelihood, page.position, page.excerpt, page.content) == (0.87, 1, "sums ranks", "full page")
    assert client.state.capabilities["deep"] is True


@respx.mock
async def test_sitemap_returns_newest_first_and_sends_filters():
    route = respx.post(f"{BASE}/sitemap").mock(return_value=httpx.Response(200, json={"urls": [
        {"url": "https://x.com/old", "lastmod": "2024-01-01"}, {"url": "https://x.com/new", "lastmod": "2026-05-01"},
        {"url": "https://x.com/none", "lastmod": None}]}))
    rows = await KeiroClient().sitemap("x.com", path_prefix="/blog", limit=3)
    assert [r["url"] for r in rows] == ["https://x.com/new", "https://x.com/old", "https://x.com/none"]
    import json
    assert json.loads(route.calls[0].request.content) == {"url": "x.com", "limit": 3, "path_prefix": "/blog", "zdr": True}


def test_dossier_puts_evidence_before_ranked_sources():
    from app.ingest.web import dossier_markdown
    from app.keiro.client import DeepResult, WebResult
    md = dossier_markdown(DeepResult("What is RRF?", [WebResult("https://a.com", "A page", excerpt="sums ranks", likelihood=0.9)],
                                     ["RRF sums reciprocal ranks."], 0.9, "found", ["a.com"]))
    assert md.index("## Evidence") < md.index("## Sources") and "[A page](https://a.com) (likelihood 0.90)" in md


@respx.mock
async def test_extract_sends_one_url_per_request_and_skips_failures():
    import json

    seen = []

    def reply(request):
        url = json.loads(request.content)["url"]
        seen.append(url)
        if "bad" in url:
            return httpx.Response(200, json={"results": [{"url": url, "content": "", "error": "timeout"}]})
        return httpx.Response(200, json={"results": [{"url": url, "markdown_content": f"# {url}", "content": "<p>html</p>", "error": None}]})

    respx.post(f"{BASE}/extract").mock(side_effect=reply)
    results = await KeiroClient().extract(["https://ok.com", "https://bad.com", "https://ok2.com", "https://ok.com"])
    assert sorted(seen) == ["https://bad.com", "https://ok.com", "https://ok2.com"]  # one request each, duplicates dropped
    assert sorted(r.url for r in results) == ["https://ok.com", "https://ok2.com"]
    assert results[0].content.startswith("# ")  # the clean markdown is preferred over raw HTML


@respx.mock
async def test_extract_raises_when_every_url_fails():
    respx.post(f"{BASE}/extract").mock(return_value=httpx.Response(402, json={}))
    with pytest.raises(KeiroError, match="credits"):
        await KeiroClient().extract(["https://a.com", "https://b.com"])
