"""RenderData scrapers and open-data templates: catalog, runs, rows, connectors and safety."""
import httpx
import pytest
import respx

from app import connectors, datatools
from app.sheets import service

pytestmark = pytest.mark.asyncio
BASE = "https://api.renderdata.cloud"


class Ctx:
    async def progress(self, *a):
        pass


def test_template_urls_are_encoded_and_checked():
    out = datatools.template_url("openalex", "solar & wind")
    assert out["url"] == "https://api.openalex.org/works?search=solar%20%26%20wind&per-page=50" and out["json_path"] == "results"
    assert datatools.template_url("worldbank", "SP.POP.TOTL")["json_path"] == "[1]"
    for bad in (("worldbank", "x; drop"), ("nope", "x"), ("openalex", "")):
        with pytest.raises(datatools.DataToolError):
            datatools.template_url(*bad)
    assert all(t["url"].startswith("https://") for t in datatools.TEMPLATES)


def test_rows_from_result_picks_the_records():
    body = {"ok": True, "data": {"query": "q", "results": [{"title": "a", "rank": 1}, {"title": "b", "rank": 2}], "related": [{"x": 1}]}}
    assert datatools.rows_from_result(body) == [{"title": "a", "rank": 1}, {"title": "b", "rank": 2}]
    one = datatools.rows_from_result({"ok": True, "data": {"answer": "Yes", "sources": ["u1", "u2"]}})
    assert one[0]["answer"] == "Yes" and "u1" in one[0]["sources"]
    with pytest.raises(datatools.DataToolError):
        datatools.rows_from_result({"ok": True, "data": {}})


def test_base_url_must_be_https(monkeypatch):
    monkeypatch.setenv("RENDERDATA_BASE_URL", "http://evil.example")
    assert datatools.rd_base() == BASE
    monkeypatch.setenv("RENDERDATA_BASE_URL", "http://127.0.0.1:4710/")
    assert datatools.rd_base() == "http://127.0.0.1:4710"


@respx.mock
async def test_catalog_and_run(monkeypatch):
    monkeypatch.delenv("RENDERDATA_BASE_URL", raising=False)
    datatools._catalog.update(at=0.0, data=None)
    respx.get(f"{BASE}/v1/scrapers").mock(return_value=httpx.Response(200, json={"groups": ["AI"], "scrapers": [
        {"id": "google-trends", "group": "AI", "name": "Google Trends", "credits": 1, "input": {"label": "Keyword"}}, {"id": "../bad"}]}))
    cat = await datatools.rd_catalog()
    assert [s["id"] for s in cat["scrapers"]] == ["google-trends"]
    monkeypatch.delenv("RENDERDATA_API_KEY", raising=False)
    with pytest.raises(datatools.DataToolError, match="key"):
        await datatools.rd_run("google-trends", "solar")
    monkeypatch.setenv("RENDERDATA_API_KEY", "sb_test")
    route = respx.post(f"{BASE}/v1/scrapers/google-trends/run").mock(return_value=httpx.Response(200, json={"ok": True, "data": {"timeline": [{"date": "2026-11", "value": 80}]}}))
    out = await datatools.rd_run("google-trends", "solar")
    assert route.calls[0].request.headers["authorization"] == "Bearer sb_test" and out["data"]["timeline"][0]["value"] == 80
    respx.post(f"{BASE}/v1/scrapers/chatgpt/run").mock(return_value=httpx.Response(402, json={"error": "quota"}))
    with pytest.raises(datatools.DataToolError, match="credits"):
        await datatools.rd_run("chatgpt", "hello")


@respx.mock
async def test_renderdata_connector_keeps_every_run(monkeypatch):
    monkeypatch.setenv("RENDERDATA_API_KEY", "sb_test")
    monkeypatch.delenv("RENDERDATA_BASE_URL", raising=False)
    respx.post(f"{BASE}/v1/scrapers/ai-answer/run").mock(return_value=httpx.Response(200, json={"ok": True, "data": {"answer": "Pune", "model": "x"}}))
    c = connectors.create("rd test", "renderdata", {"scraper": "ai-answer", "input": "largest city?"})
    try:
        await connectors.run_job(c["id"])(Ctx())
        second = await connectors.run_job(c["id"])(Ctx())
        rows = service.rows(second["sheet_id"])["rows"]
        assert len(rows) == 2 and rows[0]["answer"] == "Pune" and rows[0]["collected_at"] and rows[0]["input"] == "largest city?"
    finally:
        sid = connectors.get(c["id"])["sheet_id"]
        connectors.delete(c["id"])
        if sid:
            service.delete_sheet(sid)
    with pytest.raises(connectors.ConnectorError):
        connectors.validate("renderdata", {"scraper": "../x", "input": "q"}, 0, False)


def test_routes(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.delenv("RENDERDATA_API_KEY", raising=False)
    with TestClient(app) as c:
        out = c.get("/api/datatools").json()
        assert len(out["templates"]) >= 8 and out["renderdata"]["key_set"] is False
        assert c.post("/api/datatools/renderdata", json={"scraper": "chatgpt", "input": "x", "name": "x"}).status_code == 400
        assert c.post("/api/datatools/use", json={"template": "nope", "input": "x", "name": "y"}).status_code == 400


@respx.mock
async def test_paste_a_link_and_errors_from_the_docs(monkeypatch):
    monkeypatch.setenv("RENDERDATA_API_KEY", "sb_test")
    monkeypatch.delenv("RENDERDATA_BASE_URL", raising=False)
    route = respx.post(f"{BASE}/v1/run-url").mock(return_value=httpx.Response(200, json={"ok": True, "data": {"title": "T", "markdown": "m"}}))
    out = await datatools.rd_run("url", "https://example.org/post")
    assert route.calls[0].request.read() == b'{"url":"https://example.org/post"}' and out["data"]["title"] == "T"
    respx.post(f"{BASE}/v1/scrapers/gemini/run").mock(return_value=httpx.Response(503, json={"error": "down", "code": "tool_unavailable"}))
    with pytest.raises(datatools.DataToolError, match="unavailable"):
        await datatools.rd_run("gemini", "q")
    respx.post(f"{BASE}/v1/scrapers/chatgpt/run").mock(return_value=httpx.Response(502, json={"ok": False, "error": "empty page"}))
    with pytest.raises(datatools.DataToolError, match="Not billed"):
        await datatools.rd_run("chatgpt", "q")
    respx.get(f"{BASE}/me").mock(return_value=httpx.Response(200, json={"email": "a@b.c", "apiKeyPrefix": "sb_test123"}))
    assert (await datatools.rd_account())["valid"] is True
