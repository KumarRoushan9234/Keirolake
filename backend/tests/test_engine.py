"""The engine: evidence ranking, source tiers, claim checks, the research loop, and evidence reuse."""
import json

import pytest

from app.engine import evidence, research
from app.engine.evidence import Passage, Pool
from app.keiro.client import WebResult
from app.llm.registry import LLMResult

pytestmark = pytest.mark.asyncio


class NoEmbeddings:
    specs = [object()]

    def available(self):
        return self.specs

    async def embed_with(self, *a, **k):
        raise RuntimeError("embedding service down")


async def test_pool_dedupes_numbers_sources_and_ranks_by_keywords_when_embeddings_fail(monkeypatch):
    from app.embeddings import chain

    monkeypatch.setattr(chain, "get_chain", lambda: NoEmbeddings())
    pool = Pool()
    added = pool.add([
        Passage("Mangroves protect coasts from storm surge and erosion.", "https://a.org/1", "A"),
        Passage("Mangroves protect coasts from storm surge and erosion.", "https://b.org/x", "dup"),  # same text
        Passage("Recipes for mango lassi and other summer drinks.", "https://a.org/2", "B"),
        Passage("Storm surge height falls quickly behind wide mangrove belts.", "https://a.org/1", "A again"),
    ])
    assert added == 3 and [p.sid for p in pool.passages] == [1, 2, 1]
    ranked = await pool.rank("how do mangroves reduce storm surge")
    assert "lassi" in ranked[-1].text
    assert any("keywords only" in w for w in pool.warnings)  # said out loud, not hidden


async def test_source_tiers_pin_and_block(monkeypatch):
    monkeypatch.setenv("ENGINE_BLOCK_DOMAINS", "spam.example")
    monkeypatch.setenv("ENGINE_PIN_DOMAINS", "mytrusted.org")
    assert evidence.tier("data.gov.in") == 2 and evidence.tier("cdc.gov") == 2
    assert evidence.tier("news.mytrusted.org") == 2 and evidence.tier("blog.spam.example") == 0
    pool = Pool()
    assert pool.add([Passage("useful text here", "https://blog.spam.example/a")]) == 0


def test_claims_are_checked_against_the_passages_they_cite():
    pool = Pool()
    pool.add([Passage("The Ganges river is about 2525 kilometres long and flows into the Bay of Bengal.", "https://r.org", "R")])
    answer = ("The Ganges is about 2525 kilometres long and flows into the Bay of Bengal [S1]. "
              "It was first mapped by Martian explorers riding purple dolphins in 1203 [S1].")
    ok, borderline, bad = research.verify_claims(answer, pool)
    assert len(ok) == 1 and "Martian" in (bad + borderline)[0]


class FakeKeiro:
    configured = True

    def __init__(self):
        self.queries = []

    async def search(self, q, n=5, mode="lite"):
        self.queries.append(q)
        return [WebResult(f"https://news.example/{len(self.queries)}", f"Result {q}",
                          content=f"Solar capacity in India reached 100 gigawatts in 2026 according to the ministry. {q}.")]


class Router:
    def __init__(self):
        self.round = 0

    async def complete(self, messages, **kw):
        system = messages[0].content
        if "decide whether research is finished" in system:
            self.round += 1
            if self.round == 1:
                return LLMResult(json.dumps({"sufficient": False, "queries": ["india solar capacity 2026", "india solar capacity 2026 "]}), "ollama", "t", [])
            return LLMResult(json.dumps({"sufficient": True}), "ollama", "t", [])
        return LLMResult("YES", "ollama", "t", [])

    async def stream(self, messages, **kw):
        yield ("meta", "ollama:t")
        yield "India's solar capacity reached 100 gigawatts in 2026 according to the ministry [S1]. "
        yield "Every rooftop was painted gold by royal decree in 1066 [S1]."


async def test_research_loop_searches_gaps_writes_checks_and_keeps_evidence(monkeypatch):
    from app.embeddings import chain
    from app.keiro import client
    from app.llm import registry
    from app.net import connectivity

    keiro = FakeKeiro()
    monkeypatch.setattr(client, "get_keiro", lambda: keiro)
    monkeypatch.setattr(registry, "get_router", lambda: Router())
    monkeypatch.setattr(chain, "get_chain", lambda: NoEmbeddings())
    monkeypatch.setattr(type(connectivity), "is_online", property(lambda self: True))

    async def no_lake(q, kb, lib):
        return []

    monkeypatch.setattr(research, "_lake", no_lake)
    events = [e async for e in research.research_stream("How much solar capacity does India have?", depth="standard")]
    assert keiro.queries == ["india solar capacity 2026"]  # the duplicate query was dropped
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert "100 gigawatts" in text and "Not confirmed by the cited sources" in text and "royal decree" in text.split("Not confirmed")[1]
    verify = next(e for e in events if e["type"] == "verify")
    assert verify["supported"] == 1 and verify["unsupported"] == 1
    assert next(e for e in events if e["type"] == "sources")["research"][0]["url"].startswith("https://news.example/")
    assert evidence.recall("india solar capacity")  # kept for next time


def test_row_question_identifies_the_row():
    from app.engine.controller import row_question

    sheet = {"name": "EV makers", "columns": [{"name": "company"}, {"name": "country"}]}
    title, q = row_question(sheet, {"_id": 3, "company": "Ather", "country": "India"})
    assert title == "Deep dive: Ather · India" and "company: Ather" in q and "EV makers" in q
