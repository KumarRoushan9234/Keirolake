"""Safety notices, encrypted vault, local-only routing, folder watches, web step, updates."""
import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import safety, vault
from app.config import get_settings


# ---------- safety ----------
@pytest.mark.parametrize("question, category", [
    ("What dose of ibuprofen is safe for a child?", "medical"),
    ("How do I stop severe bleeding?", "medical"),
    ("How much bleach do I add to purify water?", "chemical"),
    ("Can I connect a generator to my house wiring?", "electrical"),
    ("I smell a gas leak, what now?", "fire"),
    ("Who wrote Hamlet?", None),
    ("Capital of France", None),
])
def test_safety_classification(question, category):
    assert safety.classify(question) == category


# ---------- vault ----------
@pytest.fixture()
def fresh_vault():
    path = get_settings().data_dir / "vault.json"
    path.unlink(missing_ok=True)
    vault.lock()
    yield path
    path.unlink(missing_ok=True)
    vault.lock()


def test_vault_encrypts_and_requires_the_right_passphrase(fresh_vault):
    vault.init("correct horse battery")
    note = vault.save_item("Passport", "Passport number X1234567, expires 2031. Emergency contact: Riya 555-0199.")
    raw = fresh_vault.read_text()
    assert "X1234567" not in raw and "Riya" not in raw and "Passport" not in raw  # nothing readable on disk
    vault.lock()
    with pytest.raises(vault.VaultError, match="locked"):
        vault.list_items()
    assert vault.search("passport number") == []  # locked vaults return nothing
    with pytest.raises(vault.VaultError, match="Wrong passphrase"):
        vault.unlock("not the passphrase")
    vault.unlock("correct horse battery")
    assert vault.get_item(note)["text"].startswith("Passport number")
    assert vault.list_items()[0]["title"] == "Passport"


def test_vault_detects_tampering_and_weak_passphrases(fresh_vault):
    with pytest.raises(vault.VaultError, match="at least"):
        vault.init("short")
    vault.init("long enough passphrase")
    item_id = vault.save_item("Blood type", "O negative")
    other = vault.save_item("Other", "something else")
    data = json.loads(fresh_vault.read_text())
    # swap two ciphertexts: each is bound to its own id, so the swap must be detected
    a, b = data["items"][0], data["items"][1]
    a["ct"], b["ct"] = b["ct"], a["ct"]
    fresh_vault.write_text(json.dumps(data))
    with pytest.raises(vault.VaultError):
        vault.list_items()
    assert item_id and other


def test_vault_search_and_idle_lock(fresh_vault, monkeypatch):
    vault.init("long enough passphrase")
    vault.save_item("Home", "Water shutoff valve is behind the garage door, left side.")
    vault.save_item("Car", "Spare key is in the green toolbox.")
    hits = vault.search("where is the water shutoff valve")
    assert hits and hits[0].title == "Home"
    real = time.monotonic
    monkeypatch.setattr(vault.time, "monotonic", lambda: real() + vault.IDLE_SECONDS + 5)
    assert vault.status()["unlocked"] is False
    assert vault.search("water") == []


def test_vault_api_codes(fresh_vault):
    from app.main import app

    with TestClient(app) as client:
        assert client.get("/api/vault").json() == {"exists": False, "unlocked": False, "items": 0, "idle_minutes": 15}
        assert client.get("/api/vault/items").status_code == 423
        assert client.post("/api/vault/init", json={"passphrase": "tiny"}).status_code == 400
        assert client.post("/api/vault/init", json={"passphrase": "a long passphrase"}).json()["unlocked"] is True
        created = client.post("/api/vault/items", json={"title": "Doctor", "text": "Dr Rao 555-0100"}).json()
        assert client.get(f"/api/vault/items/{created['id']}").json()["text"] == "Dr Rao 555-0100"
        client.post("/api/vault/lock")
        assert client.get("/api/vault/items").status_code == 423
        assert client.post("/api/vault/unlock", json={"passphrase": "wrong one!!"}).status_code == 400


# ---------- local-only routing and private answers ----------
def test_local_only_order_ignores_cloud_providers(monkeypatch):
    from app.llm.registry import LLMRouter

    monkeypatch.setenv("LOCAL_LLM_MODEL", "qwen3.5:4b")
    get_settings.cache_clear()
    try:
        providers = [a.provider for a in LLMRouter().attempts(local_only=True)]
        assert providers == ["ollama"]
        assert "openrouter" in [a.provider for a in LLMRouter().attempts()]
    finally:
        monkeypatch.setenv("LOCAL_LLM_MODEL", "")
        get_settings.cache_clear()


async def test_private_notes_are_only_sent_to_local_models(fresh_vault, monkeypatch):
    from app.rag import answer

    vault.init("long enough passphrase")
    vault.save_item("Allergies", "Riya is allergic to penicillin and peanuts.")
    seen = {}

    class FakeRouter:
        async def stream(self, messages, **kwargs):
            seen.update(kwargs)
            seen["prompt"] = messages[-1].content
            yield ("meta", "ollama:test")
            yield "Penicillin [P1]."

    monkeypatch.setattr(answer, "get_router", lambda: FakeRouter())
    events = [e async for e in answer.answer_stream(None, "What is Riya allergic to?", use_web="never", use_library=False, use_private=True)]
    assert seen["local_only"] is True and "[P1] Allergies" in seen["prompt"]
    assert next(e for e in events if e["type"] == "sources")["private"][0]["title"] == "Allergies"
    assert next(e for e in events if e["type"] == "safety")["category"] == "medical"

    seen.clear()
    _ = [e async for e in answer.answer_stream(None, "Hello there", use_web="never", use_library=False, use_private=True)]
    assert seen["local_only"] is False  # nothing private matched, so the normal order applies


async def test_safety_notice_and_rule_reach_the_model(monkeypatch):
    from app.rag import answer

    seen = {}

    class FakeRouter:
        async def stream(self, messages, **kwargs):
            seen["system"] = messages[0].content
            yield "ok"

    monkeypatch.setattr(answer, "get_router", lambda: FakeRouter())
    events = [e async for e in answer.answer_stream(None, "What dose of paracetamol can I give?", use_web="never", use_library=False)]
    notice = next(e for e in events if e["type"] == "safety")
    assert notice["category"] == "medical" and "first-aid" in notice["message"]
    assert "only numbers" in seen["system"] and "appear in the sources" in seen["system"]


# ---------- web step ----------
class _Web:
    def __init__(self, title, content="", snippet="s"):
        self.title, self.url, self.content, self.snippet = title, "https://x.example", content, snippet


class FakeKeiro:
    configured = True

    def __init__(self, content_delay):
        self.content_delay = content_delay

    async def search(self, query, max_results=3, mode="content"):
        if mode == "content":
            await asyncio.sleep(self.content_delay)
            return [_Web("full", content="full page text")]
        return [_Web("quick", snippet="snippet")]


async def test_web_step_uses_full_pages_when_they_arrive_in_time(monkeypatch):
    from app.rag import answer

    monkeypatch.setattr(answer, "get_keiro", lambda: FakeKeiro(0.05))
    monkeypatch.setattr(answer, "CONTENT_GRACE_SECONDS", 1.0)
    web, err = await answer._web_context("q")
    assert err is None and web[0].title == "full"


async def test_web_step_falls_back_to_snippets_when_pages_are_slow(monkeypatch):
    from app.rag import answer

    monkeypatch.setattr(answer, "get_keiro", lambda: FakeKeiro(5))
    monkeypatch.setattr(answer, "CONTENT_GRACE_SECONDS", 0.1)
    started = time.monotonic()
    web, err = await answer._web_context("q")
    assert err is None and web[0].title == "quick" and time.monotonic() - started < 1.5


# ---------- watched folders ----------
def test_diff_state():
    from app.watch import diff_state

    old = {"a.md": [1, 10], "b.md": [1, 10], "c.md": [1, 10]}
    new = {"a.md": [1, 10], "b.md": [2, 10], "d.md": [1, 5]}
    assert diff_state(old, new) == (["d.md"], ["b.md"], ["c.md"])


def test_scan_folder_skips_hidden_unsupported_and_big_files(tmp_path):
    from app.watch import scan_folder

    (tmp_path / "note.md").write_text("hello")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "doc.txt").write_text("world")
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian" / "x.md").write_text("config")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "y.md").write_text("git")
    (tmp_path / "image.png").write_bytes(b"\x89PNG")
    (tmp_path / ".hidden.md").write_text("hidden")
    assert sorted(scan_folder(tmp_path)) == ["note.md", "sub/doc.txt"]


def test_watch_rejects_missing_and_relative_folders(tmp_path):
    from app.watch import WatchError, check_folder

    with pytest.raises(WatchError):
        check_folder(str(tmp_path / "nope"))
    with pytest.raises(WatchError):
        check_folder("relative/path")
    assert check_folder(str(tmp_path)) == tmp_path.resolve()


async def test_scan_job_indexes_changes_and_removes_deleted_files(tmp_path, monkeypatch):
    from sqlalchemy import select

    from app import watch
    from app.catalog.models import Document, KnowledgeBase, Watch
    from app.db import session_scope

    indexed, deleted = [], []

    async def fake_run(kb_id, source_id, run_id, raw_dir, docs, ctx, extra=None):
        indexed.extend((d.title, d.url) for d in docs)
        with session_scope() as s:  # what real ingestion would record
            for d in docs:
                s.add(Document(kb_id=kb_id, run_id=run_id, source_id=source_id, title=d.title, url=d.url, raw_path="r",
                               clean_path="c", content_hash=d.title + str(len(d.text)), chars=len(d.text)))
        return {"documents": len(docs)}

    async def fake_delete(doc_id):
        deleted.append(doc_id)
        with session_scope() as s:
            s.delete(s.get(Document, doc_id))

    class Ctx:
        async def progress(self, *_a):
            pass

    monkeypatch.setattr(watch, "_run", fake_run)
    monkeypatch.setattr(watch, "delete_document", fake_delete)
    with session_scope() as s:
        kb = KnowledgeBase(name="watchkb", slug="watchkb", collection_alias="kb_w", collection_name="kb_w_v1")
        s.add(kb)
        s.flush()
        kb_id = kb.id
    (tmp_path / "a.md").write_text("# Alpha\n\nfirst note")
    (tmp_path / "b.txt").write_text("second note")
    watch_id = watch.add_watch(kb_id, str(tmp_path))

    first = await watch.scan_job(watch_id)(Ctx())
    assert (first["added"], first["changed"], first["removed"]) == (2, 0, 0) and len(indexed) == 2
    assert (await watch.scan_job(watch_id)(Ctx()))["added"] == 0  # nothing changed

    time.sleep(0.02)
    (tmp_path / "a.md").write_text("# Alpha\n\nfirst note, edited")
    (tmp_path / "b.txt").unlink()
    (tmp_path / "c.md").write_text("third note")
    third = await watch.scan_job(watch_id)(Ctx())
    assert (third["added"], third["changed"], third["removed"]) == (1, 1, 1)
    assert len(deleted) == 2  # the old version of a.md and the removed b.txt
    with session_scope() as s:
        titles = sorted(s.scalars(select(Document.title).where(Document.kb_id == kb_id)))
        assert s.get(Watch, watch_id).last_result["added"] == 1
    assert titles == ["a", "c"]
    watch.delete_watch(watch_id)


# ---------- library updates ----------
def test_library_update_detection(monkeypatch):
    from app.library import service

    monkeypatch.setattr(service, "list_libraries", lambda: [
        {"id": "1", "name": "wikipedia_en_all_nopic", "title": "Wikipedia", "issued": "2026-06-26", "status": "ready"},
        {"id": "2", "name": "ifixit_en_all", "title": "iFixit", "issued": "2025-12-23", "status": "ready"},
        {"id": "3", "name": "gone", "title": "Gone", "issued": "2020-01-01", "status": "ready"},
        {"id": "4", "name": "mdwiki_en_all_maxi", "title": "MDWiki", "issued": "2020-01-01", "status": "downloading"},
    ])
    entries = [{"name": "wikipedia_en_all_nopic", "issued": "2026-09-30", "size_bytes": 5},
               {"name": "ifixit_en_all", "issued": "2025-12-23", "size_bytes": 3},
               {"name": "mdwiki_en_all_maxi", "issued": "2026-01-01", "size_bytes": 2}]
    result = service.updates(entries)
    assert [(u["name"], u["current"], u["available"]) for u in result] == [("wikipedia_en_all_nopic", "2026-06-26", "2026-09-30")]
