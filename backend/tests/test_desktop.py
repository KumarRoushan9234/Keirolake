"""Desktop features: model modes, prefs, API token, hardware advisor, catalog, ZIM libraries, capture."""
import json
import os

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from libzim.writer import Creator, Hint, Item, StringProvider

from app import prefs
from app.config import get_settings
from app.llm.registry import AllModelsFailed, LLMRouter, effective_order
from app.local.catalog import recommend


# ---------- model modes ----------
def test_effective_order_by_mode_and_network():
    base = ["openrouter", "groqcloud"]
    assert effective_order(base, "auto", True) == ["openrouter", "groqcloud", "ollama", "llamacpp"]
    assert effective_order(base, "auto", False) == ["ollama", "llamacpp"]
    assert effective_order(base, "local", True) == ["ollama", "llamacpp"]
    assert effective_order(base, "cloud", False) == ["openrouter", "groqcloud"]


async def test_local_mode_without_a_local_model_explains_what_to_do(monkeypatch):
    monkeypatch.setenv("LLM_MODE", "local")
    get_settings.cache_clear()
    try:
        with pytest.raises(AllModelsFailed, match="Models page"):
            await LLMRouter().complete([])
    finally:
        monkeypatch.setenv("LLM_MODE", "auto")
        get_settings.cache_clear()


async def test_offline_auto_mode_uses_the_local_model(monkeypatch):
    from app.net import connectivity

    monkeypatch.setenv("LOCAL_LLM_MODEL", "qwen3.5:4b")
    monkeypatch.setenv("KEIROLAKE_FORCE_OFFLINE", "1")
    get_settings.cache_clear()
    try:
        attempts = LLMRouter().attempts()
        assert [(a.provider, a.model) for a in attempts] == [("ollama", "qwen3.5:4b")]
        assert connectivity.is_online is False
    finally:
        monkeypatch.delenv("KEIROLAKE_FORCE_OFFLINE")
        monkeypatch.setenv("LOCAL_LLM_MODEL", "")
        get_settings.cache_clear()


# ---------- prefs ----------
def test_prefs_store_values_hide_secrets_and_reject_unknown_keys():
    prefs.update({"OPENROUTER_MODELS": "a/b,c/d", "OPENROUTER_API_KEY": "sk-secret-123456789"})
    described = prefs.describe()
    assert described["OPENROUTER_MODELS"]["value"] == "a/b,c/d"
    assert described["OPENROUTER_API_KEY"] == {"secret": True, "set": True, "hint": "…6789"}
    assert "sk-secret" not in json.dumps(described)
    saved = json.loads((get_settings().data_dir / "prefs.json").read_text())
    assert saved["values"]["OPENROUTER_MODELS"] == "a/b,c/d"
    with pytest.raises(prefs.PrefsError):
        prefs.update({"PATH": "x"})
    prefs.update({"OPENROUTER_MODELS": "model-a,model-b", "OPENROUTER_API_KEY": "test-or"})


# ---------- token protection ----------
def test_token_middleware_requires_token_and_loopback_host(monkeypatch):
    from app.main import app

    monkeypatch.setenv("KEIROLAKE_TOKEN", "t0ken")
    get_settings.cache_clear()
    try:
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            assert client.get("/api/net").status_code == 401
            assert client.get("/api/net", headers={"X-KeiroLake-Token": "wrong"}).status_code == 401
            assert client.get("/api/net", headers={"X-KeiroLake-Token": "t0ken"}).status_code == 200
            evil = client.get("/api/net", headers={"X-KeiroLake-Token": "t0ken", "Host": "evil.example:8765"})
            assert evil.status_code == 403
            assert client.get("/api/zim/wrong/x/").status_code == 401
    finally:
        monkeypatch.setenv("KEIROLAKE_TOKEN", "")
        get_settings.cache_clear()


# ---------- hardware advisor ----------
@pytest.mark.parametrize("hw, llm", [
    ({"vram_gb": 8.0, "ram_gb": 23, "threads": 32, "disk_free_gb": 128}, "qwen3.5:9b"),
    ({"vram_gb": 0, "ram_gb": 8, "threads": 4, "disk_free_gb": 40}, "qwen3.5:4b"),
    ({"vram_gb": 0, "ram_gb": 4, "threads": 2, "disk_free_gb": 20}, "qwen3.5:0.8b"),
    ({"vram_gb": 24, "ram_gb": 64, "threads": 16, "disk_free_gb": 500}, "qwen3.5:27b"),
])
def test_recommendation_fits_the_hardware(hw, llm):
    rec = recommend(hw)
    assert rec["llm"]["id"] == llm
    assert rec["llm"]["need_gb"] <= max(hw["vram_gb"] if hw["vram_gb"] >= 3.5 else hw["ram_gb"] * 0.6, 2.0)


def test_disk_space_picks_the_wikipedia_flavour():
    assert recommend({"vram_gb": 8, "ram_gb": 23, "threads": 32, "disk_free_gb": 128})["wikipedia"]["name"] == "wikipedia_en_all_nopic"
    assert recommend({"vram_gb": 8, "ram_gb": 23, "threads": 32, "disk_free_gb": 300})["wikipedia"]["name"] == "wikipedia_en_all_maxi"
    assert recommend({"vram_gb": 0, "ram_gb": 8, "threads": 4, "disk_free_gb": 10})["wikipedia"] is None


# ---------- Kiwix catalog ----------
OPDS = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/terms/">
  <entry>
    <title>Water Treatment Library</title><summary>Water collection and purification</summary>
    <language>eng</language><name>zimgit-water_en</name><category></category>
    <tags>preppers;_ftindex:no;_pictures:yes</tags><articleCount>1</articleCount><mediaCount>369</mediaCount>
    <dc:issued>2024-08-29T00:00:00Z</dc:issued>
    <link rel="http://opds-spec.org/acquisition/open-access" type="application/x-zim"
          href="https://download.kiwix.org/zim/other/zimgit-water_en_2024-08.zim.meta4" length="20971520" />
  </entry>
</feed>"""


def test_opds_catalog_parsing_and_search():
    from app.library.catalog import parse, search

    entries = parse(OPDS)
    assert entries[0]["url"] == "https://download.kiwix.org/zim/other/zimgit-water_en_2024-08.zim"
    assert entries[0]["size_bytes"] == 20971520 and entries[0]["fulltext"] is False and entries[0]["issued"] == "2024-08-29"
    assert search(entries, "purification")[0]["name"] == "zimgit-water_en"
    assert search(entries, "volcano") == []


# ---------- downloads ----------
@respx.mock
async def test_resumable_download_continues_and_verifies(tmp_path):
    import hashlib

    from app.library.download import DownloadError, fetch_resumable

    body = b"0123456789" * 100
    target = tmp_path / "f.zim"
    (tmp_path / "f.zim.part").write_bytes(body[:400])

    def reply(request):
        assert request.headers["Range"] == "bytes=400-"
        return httpx.Response(206, content=body[400:], headers={"content-length": str(len(body) - 400)})

    respx.get("https://example.org/f.zim").mock(side_effect=reply)
    await fetch_resumable("https://example.org/f.zim", target, None, hashlib.sha256(body).hexdigest())
    assert target.read_bytes() == body

    respx.get("https://example.org/g.zim").mock(return_value=httpx.Response(200, content=b"x"))
    with pytest.raises(DownloadError, match="Checksum"):
        await fetch_resumable("https://example.org/g.zim", tmp_path / "g.zim", None, "00")
    assert not (tmp_path / "g.zim.part").exists()


# ---------- ZIM libraries ----------
class _Page(Item):
    def __init__(self, path, title, html):
        super().__init__()
        self._path, self._title, self._html = path, title, html

    def get_path(self): return self._path
    def get_title(self): return self._title
    def get_mimetype(self): return "text/html"
    def get_contentprovider(self): return StringProvider(self._html)
    def get_hints(self): return {Hint.FRONT_ARTICLE: True}


ARTICLES = {
    "Water_purification": ("Water purification", "<html><body><h1>Water purification</h1>"
        "<p>Water purification removes contaminants. Boiling water for one minute kills most pathogens.</p>"
        "<h2>Methods</h2><p>Sand and charcoal filters remove particles; chlorination disinfects water stored in tanks.</p>"
        "<sup class='reference'>[1]</sup><div class='navbox'>Navigation junk</div></body></html>"),
    "Volcano": ("Volcano", "<html><body><p>A volcano is a rupture in the crust of a planet where lava escapes. "
        "Volcanoes erupt magma, ash and gases from below the surface.</p></body></html>"),
    "Main_Page": ("Main page", "<html><body><p>Welcome. See <a href='Water_purification'>Water purification</a>.</p></body></html>"),
}


@pytest.fixture(scope="module")
def zim_file(tmp_path_factory):
    path = tmp_path_factory.mktemp("zim") / "test_en_all_2026-01.zim"
    with Creator(str(path)).config_indexing(True, "eng") as creator:
        creator.set_mainpath("Main_Page")
        for key, (title, html) in ARTICLES.items():
            creator.add_item(_Page(key, title, html * 3))
        for name, value in (("Name", "test_en_all"), ("Title", "Test Wiki"), ("Language", "eng"),
                            ("Description", "Tiny test library"), ("Date", "2026-01-01")):
            creator.add_metadata(name, value)
    return path


def test_zim_text_extraction_and_search(zim_file):
    from app.library import zim

    art = zim.read_article(str(zim_file), "Water_purification")
    assert "Boiling water" in art.text and "## Methods" in art.text
    assert "Navigation junk" not in art.text and "[1]" not in art.text
    assert "Water_purification" in zim.search(str(zim_file), "purification", 5)
    meta = zim.metadata(str(zim_file))
    assert meta["title"] == "Test Wiki" and meta["main"] == "Main_Page" and meta["fulltext"] is True


async def test_library_register_retrieve_and_answer(zim_file, monkeypatch):
    from app.library import service
    from app.local import ollama
    from app.rag import answer

    lib = service.register_file(str(zim_file))
    assert lib["status"] == "ready" and lib["title"] == "Test Wiki"

    async def no_model(_name):
        return False

    monkeypatch.setattr(ollama, "has_model", no_model)  # no embedder: keyword ranking
    hits, warnings = await service.retrieve("How do I purify water with charcoal and sand?")
    assert hits and hits[0].path == "Water_purification"
    assert "charcoal" in " ".join(h.text for h in hits).lower()

    class FakeRouter:
        async def stream(self, messages, **_):
            assert "[Z1] Water purification" in messages[-1].content
            yield ("meta", "ollama:test")
            yield "Boil it [Z1]."

    monkeypatch.setattr(answer, "get_router", lambda: FakeRouter())
    events = [e async for e in answer.answer_stream(None, "How do I purify water?", use_web="never")]
    sources = next(e for e in events if e["type"] == "sources")
    assert sources["library"][0]["title"] == "Water purification"
    assert "".join(e.get("text", "") for e in events if e["type"] == "token") == "Boil it [Z1]."


def test_reader_serves_library_pages_with_script_blocking_csp(zim_file):
    from app.library import service
    from app.main import app

    lib = service.register_file(str(zim_file))
    with TestClient(app) as client:
        page = client.get(f"/api/zim/-/{lib['id']}/Water_purification")
        assert page.status_code == 200 and "Boiling water" in page.text
        assert "script-src 'none'" in page.headers["content-security-policy"]
        home = client.get(f"/api/zim/-/{lib['id']}/", follow_redirects=False)
        assert home.status_code == 307 and home.headers["location"].endswith("/Main_Page")
        found = client.get(f"/api/libraries/{lib['id']}/search", params={"q": "volcano"}).json()
        assert found["results"][0]["title"] == "Volcano"
        assert client.get(f"/api/zim/-/{lib['id']}/Nope").status_code == 404


# ---------- capture ----------
@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://localhost:8765/api", "file:///etc/passwd", "http://10.0.0.1/"])
def test_reader_refuses_local_and_private_addresses(url):
    from app.capture import CaptureError, check_public_url

    with pytest.raises(CaptureError):
        check_public_url(url)


def test_capture_extracts_readable_markdown():
    from app.capture import extract_markdown

    html = ("<html><head><title>Rain barrels</title></head><body><nav>Menu Home About</nav><article><h1>Rain barrels</h1>"
            + "<p>Collect rainwater from the roof with a first-flush diverter and a screened inlet.</p>" * 5
            + "</article><footer>Copyright</footer></body></html>")
    title, text = extract_markdown(html, "https://example.org/rain")
    assert title == "Rain barrels" and "first-flush diverter" in text


def test_settings_api_round_trip():
    from app.main import app

    with TestClient(app) as client:
        res = client.put("/api/settings", json={"values": {"GROQCLOUD_MODELS": "groq-a,groq-b"}})
        assert res.status_code == 200 and res.json()["values"]["GROQCLOUD_MODELS"]["value"] == "groq-a,groq-b"
        assert client.put("/api/settings", json={"values": {"HOME": "/tmp"}}).status_code == 400
        client.put("/api/settings", json={"values": {"GROQCLOUD_MODELS": "groq-a"}})
        hw = client.get("/api/local/hardware").json()
        assert hw["hardware"]["ram_gb"] > 0 and hw["recommendation"]["llm"]["id"]
        assert os.environ["GROQCLOUD_MODELS"] == "groq-a"


def test_catalog_names_are_unique_per_flavour():
    from app.library.catalog import parse

    xml = OPDS.replace("zimgit-water_en_2024-08.zim.meta4", "wikipedia_en_all_nopic_2026-06.zim.meta4")
    assert parse(xml)[0]["name"] == "wikipedia_en_all_nopic"
