"""Building by conversation: the draft is agreed in chat, then rows (or notes) are written, kept unique and traced."""
import json

import pytest

from app import builder
from app.llm.registry import LLMResult
from app.sheets import service

pytestmark = pytest.mark.asyncio


class Router:
    def __init__(self):
        self.batches = 0

    async def complete(self, messages, **kw):
        system, user = messages[0].content, messages[-1].content
        if "by conversation" in system:
            return LLMResult(json.dumps({"reply": "Here's a plan: name, state and population. Shall I fill 4 rows?",
                                         "draft": {"name": "Indian cities", "columns": [{"name": "City Name", "type": "text", "description": "The city"},
                                                                                         {"name": "state", "type": "text"}, {"name": "population", "type": "number"},
                                                                                         {"name": "state", "type": "text"}], "rows": 4},
                                         "ready": True}), "ollama", "m", [])
        if "fill rows" in system:
            self.batches += 1
            if self.batches == 1:
                return LLMResult('[{"city_name": "Pune", "state": "MH", "population": 7000000}, {"city_name": "Surat", "state": "GJ", "population": null}]', "ollama", "m", [])
            return LLMResult('[{"city_name": "Pune", "state": "MH"}, {"city_name": "Goa", "state": "GA", "population": 1500000}]', "ollama", "m", [])
        return LLMResult("# Section\n" + "Text about the section. " * 20, "ollama", "m", [])


class Ctx:
    async def progress(self, *a):
        pass


async def test_turn_cleans_the_draft(monkeypatch):
    monkeypatch.setattr(builder, "_router", lambda: Router())
    out = await builder.turn("table", [{"role": "user", "content": "I want a table of Indian cities"}])
    assert out["ready"] and out["draft"]["name"] == "Indian cities"
    assert [c["name"] for c in out["draft"]["columns"]] == ["city_name", "state", "population"]  # snake_case, no duplicates
    with pytest.raises(builder.BuilderError):
        await builder.turn("poem", [{"role": "user", "content": "x"}])


async def test_fill_writes_unique_rows_with_their_origin(monkeypatch):
    r = Router()
    monkeypatch.setattr(builder, "_router", lambda: r)

    async def no_context(q, lake, libs, web=False):
        return "", []

    monkeypatch.setattr(builder, "_context", no_context)
    draft = builder._clean_draft({"name": "Cities built", "columns": [{"name": "city_name"}, {"name": "state"}, {"name": "population", "type": "number"}]}, "table")
    out = await builder.fill_table_job(draft, "Indian cities", 3, False, False)(Ctx())
    try:
        rows = service.rows(out["sheet_id"])["rows"]
        assert sorted(x["city_name"] for x in rows) == ["Goa", "Pune", "Surat"] and out["added"] == 3  # Pune not repeated
        assert rows[0]["_meta"]["city_name"]["sources"] == ["model knowledge"] and rows[0]["_meta"]["city_name"]["confidence"] == 0.4
        more = await builder.fill_table_job(draft, "Indian cities", 2, False, False, out["sheet_id"])(Ctx())  # "add more" goes to the same table
        assert more["sheet_id"] == out["sheet_id"]
    finally:
        service.delete_sheet(out["sheet_id"])


def test_fill_route_needs_an_agreed_draft():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        assert c.post("/api/builder/fill", json={"kind": "table", "draft": {"name": "x"}}).status_code == 400
        assert c.post("/api/builder/fill", json={"kind": "collection", "draft": {"name": "x", "sections": []}}).status_code == 400


def test_language_guard():
    assert builder.language_of("table of Indian states") == "English"
    assert builder.language_of("भारत के राज्य") == "Hindi"
    assert builder._foreign({"name": "泰米尔纳德邦", "pop": 1}, set()) and not builder._foreign({"name": "Tamil Nadu"}, set())
    assert not builder._foreign({"name": "महाराष्ट्र"}, {"Devanagari"})


@pytest.mark.asyncio
async def test_only_relevant_passages_count_as_sources(monkeypatch):
    """A nearest passage that isn't really about the request must not make a guess look sourced."""
    from app.rag import retrieve as r
    from app.rag.retrieve import Hit

    async def fake(query, k):
        return [Hit("1", "d", "Unrelated guide", None, "text", cosine=0.2), Hit("2", "d", "States of India", "https://x", "Kerala...", cosine=0.71)], 0.71

    monkeypatch.setattr(r, "retrieve_all", fake)
    context, sources = await builder._context("Indian states", True, False)
    assert sources == ["https://x"] and "Unrelated guide" not in context
