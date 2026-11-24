"""Library helpers: plain-language find and gathering source text."""
import pytest

from app import assist

pytestmark = pytest.mark.asyncio


async def test_find_without_libraries_says_so(monkeypatch):
    monkeypatch.setattr(assist.library, "ready_libraries", lambda: [])
    out = await assist.find_in_libraries("water")
    assert out["articles"] == [] and out["message"]


async def test_gather_source_combines_libraries_and_collections(monkeypatch):
    from app.library.service import LibraryHit
    from app.rag import retrieve as retrieve_mod
    from app.rag.retrieve import Hit

    async def lib(question, limit=6):
        return [LibraryHit("l1", "Wiki", "A/Water", "Water", "Boil water for one minute.", 0.9, True)], []

    async def kbs(question, top_k=8):
        return [Hit("c1", "d1", "My notes", None, "Keep 4 litres a day.")], 0.8

    monkeypatch.setattr(assist.library, "retrieve", lib)
    monkeypatch.setattr(retrieve_mod, "retrieve_all", kbs)
    title, text = await assist.gather_source("water")
    assert "Boil water" in text and "4 litres" in text and title.startswith("Water")
