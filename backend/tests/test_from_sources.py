"""Sources become sheets: columns chosen for the question, one row per source, every cell linked to its page."""
import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.catalog.models import EvidenceRow
from app.db import session_scope
from app.keiro.client import WebResult
from app.llm.registry import LLMResult
from app.sheets import from_sources, service

pytestmark = pytest.mark.asyncio


class Router:
    """Designs columns, then reads each source (answers keyed by what the page text says)."""

    async def complete(self, messages, **kw):
        system, user = messages[0].content, messages[-1].content
        if "design the columns" in system:
            return LLMResult(json.dumps({"name": "EV scooters", "columns": [
                {"name": "model", "type": "text", "description": "Scooter model"},
                {"name": "price_inr", "type": "number", "description": "Price in rupees"},
                {"name": "source_url", "type": "text"}]}), "ollama", "t", [])  # source_url must be ignored: it is added automatically
        if "Ather" in user:
            return LLMResult("{'model': 'Ather 450X', 'price_inr': '1,32,000',}", "ollama", "t", [])
        return LLMResult('{"model": "Ola S1", "price_inr": 99999', "ollama", "t", [])  # cut off: the price is still recovered


def pages():
    return [WebResult("https://a.example.org/ather", "Ather", content="Ather launches the 450X scooter at a price of 1,32,000. " * 6),
            WebResult("https://b.example.org/ola", "Ola", content="Ola launches the S1 scooter with a price near 99999. " * 6),
            WebResult("https://c.example.org/tiny", "Tiny", content="too short")]


async def test_build_sheet_from_pages(monkeypatch):
    from app.llm import registry

    monkeypatch.setattr(registry, "get_router", lambda: Router())
    out = await from_sources.build_sheet("Which electric scooters are sold in India and at what price?", pages())
    sid = out["id"]
    cols = [c["name"] for c in out["columns"]]
    assert cols == ["source_url", "title", "model", "price_inr"] and out["rows"] == 2 and out["source"] == "research"
    rows = {r["model"]: r for r in service.rows(sid)["rows"]}
    assert rows["Ather 450X"]["price_inr"] == 132000.0 and rows["Ola S1"]["price_inr"] == 99999.0
    assert rows["Ather 450X"]["_meta"]["model"]["sources"] == ["https://a.example.org/ather"]  # each cell knows its page
    assert service.history(sid)  # a first copy is kept
    service.delete_sheet(sid)


async def test_nothing_readable_is_a_clear_error():
    with pytest.raises(from_sources.FromSourcesError, match="had text"):
        await from_sources.build_sheet("q", [WebResult("https://x.org", "x", content="short")])


async def test_evidence_from_research_is_regrouped_per_url():
    with session_scope() as s:
        for i, text in enumerate(["first passage about solar", "second passage about solar"]):
            s.add(EvidenceRow(task="research", query="q", url="https://news.example.org/solar", title="Solar news", domain="news.example.org",
                              text=text, text_hash=f"h-regroup-{i}"))
    found = from_sources.pages_from_evidence(["https://news.example.org/solar"])
    assert len(found) == 1 and "first passage" in found[0].content and "second passage" in found[0].content


@respx.mock
def test_from_urls_route(monkeypatch):
    from app.llm import registry
    from app.main import app

    monkeypatch.setattr(registry, "get_router", lambda: Router())

    class Keiro:
        configured = True

        async def extract(self, urls):
            return pages()[:2]

    from app.keiro import client

    monkeypatch.setattr(client, "get_keiro", lambda: Keiro())
    with TestClient(app) as c:
        r = c.post("/api/sheets/from-urls", json={"urls": ["https://a.example.org/ather", "https://b.example.org/ola"], "question": "Scooter prices in India?"})
        assert r.status_code == 201 and r.json()["rows"] == 2
        c.delete(f"/api/sheets/{r.json()['id']}")
        assert c.post("/api/sheets/from-urls", json={"urls": ["not a url"], "question": "q q q"}).status_code == 400
