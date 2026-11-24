"""A topic from question to sheet, cleaned data, searchable pages and a CSV on disk."""
import json

import pytest
from fastapi.testclient import TestClient

from app import topics
from app.keiro.client import WebResult
from app.llm.registry import LLMResult
from app.sheets import service

pytestmark = pytest.mark.asyncio


class Router:
    async def complete(self, messages, **kw):
        system, user = messages[0].content, messages[-1].content
        if "design the columns" in system:
            return LLMResult(json.dumps({"name": "Solar", "columns": [{"name": "panel", "type": "text"}, {"name": "price", "type": "text"}]}), "ollama", "t", [])
        panel = "Alpha" if "Alpha" in user else "Beta"
        return LLMResult(json.dumps({"panel": f" {panel} ", "price": "$1,200" if panel == "Alpha" else "N/A"}), "ollama", "t", [])

    async def probe(self):
        return {}


class Keiro:
    configured = True

    async def search(self, query, max_results=5, mode="lite"):
        return [WebResult("https://a.example.org/alpha", "Alpha", content="Alpha solar panel costs $1,200 and is made in Pune. " * 8, domain="a.example.org"),
                WebResult("https://b.example.org/beta", "Beta", content="Beta solar panel price not announced yet by the maker. " * 8, domain="b.example.org"),
                WebResult("https://c.example.org/x", "Short", content="too short", domain="c.example.org")]


class Ctx:
    async def progress(self, *a):
        pass


async def test_topic_builds_everything(monkeypatch):
    from app.llm import registry

    from qdrant_client import AsyncQdrantClient

    from app.vector import qdrant_store as qs

    monkeypatch.setattr(qs, "_store", qs.QdrantStore(AsyncQdrantClient(location=":memory:")))
    monkeypatch.setattr(registry, "get_router", lambda: Router())
    monkeypatch.setattr(topics, "get_keiro", lambda: Keiro())
    ingested = {}

    async def fake_run(kb_id, source_id, run_id, raw_dir, docs, ctx, extra=None):
        ingested.update(docs=len(docs), kb=kb_id)
        await ctx.progress(1.0, "done")
        return {"chunks": 4, "documents": len(docs)}

    monkeypatch.setattr(topics, "_run", fake_run)
    t = topics.create("Solar panels", "Which solar panels are sold in India and at what price?")
    summary = await topics.build_job(t["id"])(Ctx())
    try:
        assert summary["pages"] == 2 and summary["rows"] == 2 and ingested["docs"] == 2
        done = topics.get(t["id"])
        assert done["status"] == "ready" and done["kb_id"] == ingested["kb"]
        rows = {r["panel"]: r for r in service.rows(done["sheet_id"])["rows"]}
        assert set(rows) == {"Alpha", "Beta"} and rows["Alpha"]["price"] == 1200.0 and rows["Beta"]["price"] is None  # trimmed, retyped, N/A emptied
        csv = (topics.zone_dir("curated") / "topics" / "solar_panels" / "solar_panels.csv").read_text(encoding="utf-8-sig")
        assert "Alpha" in csv and "source_url" in csv
        assert len(json.loads((topics.zone_dir("curated") / "topics" / "solar_panels" / "sources.json").read_text())) == 2
        assert {c["name"]: c["type"] for c in service.get(done["sheet_id"])["columns"]}["price"] == "number"  # tidied while building
    finally:
        await topics.delete(t["id"], remove_data=True)


async def test_clear_errors_when_the_web_is_unavailable(monkeypatch):
    class Off:
        configured = False

    monkeypatch.setattr(topics, "get_keiro", lambda: Off())
    t = topics.create("No key topic")
    try:
        with pytest.raises(topics.TopicError, match="KeiroLabs key"):
            await topics.build_job(t["id"])(Ctx())
        assert topics.get(t["id"])["status"] == "error"
    finally:
        await topics.delete(t["id"])


def test_topic_routes(monkeypatch):
    from app.main import app

    with TestClient(app) as c:
        r = c.post("/api/topics", json={"name": "Route topic", "build": False, "urls": ["https://x.example.org", "nope"]})
        assert r.status_code == 201 and r.json()["urls"] == ["https://x.example.org"]
        assert c.post("/api/topics", json={"name": "Route topic", "build": False}).status_code == 400
        tid = r.json()["id"]
        assert any(t["id"] == tid for t in c.get("/api/topics").json())
        assert c.delete(f"/api/topics/{tid}").status_code == 204 and c.get(f"/api/topics/{tid}").status_code == 404
