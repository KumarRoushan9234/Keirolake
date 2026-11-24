"""Connection pooling, KeiroLabs cache and retry, embedding cache, support diagnostics and bundle."""
import asyncio
import io
import json
import zipfile

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import http, support
from app.keiro.client import KeiroClient, KeiroError

BASE = "https://api.keirolabs.cloud/api/v2"


async def test_pool_reuses_one_client_per_loop():
    a, b = http.client("x"), http.client("x")
    assert a is b and http.client("y") is not a
    await http.close_all()
    assert http.client("x") is not a


@respx.mock
async def test_keiro_identical_requests_are_served_from_cache():
    route = respx.post(f"{BASE}/search/lite").mock(return_value=httpx.Response(200, json={"results": [{"url": "https://a.example", "title": "A"}]}))
    client = KeiroClient()
    first = await client.search("water filters", 3)
    second = await client.search("water filters", 3)
    third = await client.search("other question", 3)
    assert first[0].url == second[0].url and route.call_count == 2  # the repeat cost nothing
    assert third and client.clear_cache() == 2


@respx.mock
async def test_keiro_retries_once_on_rate_limit_and_server_errors():
    route = respx.post(f"{BASE}/search/lite").mock(side_effect=[httpx.Response(503, json={}), httpx.Response(200, json={"results": [{"url": "https://ok.example"}]})])
    results = await KeiroClient().search("q", 1)
    assert results[0].url == "https://ok.example" and route.call_count == 2

    respx.post(f"{BASE}/extract").mock(return_value=httpx.Response(429, json={"error": "slow down"}))
    with pytest.raises(KeiroError):
        await KeiroClient().extract(["https://x.example"])


@respx.mock
async def test_keiro_does_not_retry_or_cache_errors_that_will_not_change():
    route = respx.post(f"{BASE}/search/lite").mock(return_value=httpx.Response(402, json={}))
    client = KeiroClient()
    for _ in range(2):
        with pytest.raises(KeiroError, match="credits"):
            await client.search("q")
    assert route.call_count == 2  # no retry per call, and the failure wasn't cached


@respx.mock
async def test_ollama_embed_only_sends_texts_it_has_not_seen(monkeypatch):
    from app.local import ollama

    ollama._VECTORS.data.clear()
    sent = []

    def reply(request):
        texts = json.loads(request.content)["input"]
        sent.append(list(texts))
        return httpx.Response(200, json={"embeddings": [[float(len(t)), 1.0, 0.0] for t in texts]})

    respx.post("http://127.0.0.1:9/api/embed").mock(side_effect=reply)
    one = await ollama.embed("m", ["alpha", "beta"], 2)
    two = await ollama.embed("m", ["beta", "gamma"], 2)
    assert sent == [["alpha", "beta"], ["gamma"]]
    assert two[0] == one[1] and abs(sum(x * x for x in two[1]) - 1.0) < 1e-9  # cached vector reused, new one normalised


# ---------- support ----------
@pytest.mark.parametrize("raw", [
    "Authorization: Bearer sk-or-v1-0123456789abcdef0123456789abcdef",
    "key=keiro_Abcdefghijklmnop1234", "gsk_ABCDEFGHIJKLMNOPQRSTUV", "api_key: 'abcdefghijklmnopqrstu'",
    "GET /api/zim/0123456789abcdef0123456789abcdef/lib/x", "X-KeiroLake-Token: 0123456789abcdef0123",
])
def test_redaction_removes_secrets(raw):
    out = support.redact(raw)
    assert "[redacted]" in out or "/api/zim/" not in out or out.count("0123456789abcdef") == 0
    for secret in ("0123456789abcdef0123456789abcdef", "Abcdefghijklmnop1234", "ABCDEFGHIJKLMNOPQRSTUV", "abcdefghijklmnopqrstu"):
        assert secret not in out


async def test_diagnostics_cover_every_area_and_suggest_fixes():
    checks = await support.diagnostics()
    ids = [c["id"] for c in checks]
    for expected in ("database", "vectors", "disk", "internet", "runtime", "local_model", "cloud", "keirolabs", "libraries", "backups", "jobs"):
        assert expected in ids
    assert all(c["status"] in ("ok", "warn", "fail") for c in checks)
    assert all(c["fix"] for c in checks if c["status"] != "ok")
    assert next(c for c in checks if c["id"] == "database")["status"] == "ok"


def test_support_api_bundle_has_no_secrets(tmp_path):
    from app.config import get_settings
    from app.main import app

    logs = get_settings().data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "engine.log").write_text("starting\nAuthorization: Bearer sk-or-v1-SECRETSECRETSECRETSECRET1234\nok\n")
    with TestClient(app) as client:
        about = client.get("/api/about").json()
        assert about["version"] and about["support"]["keirolabs"].startswith("https://")
        res = client.get("/api/support/bundle")
        assert res.status_code == 200 and res.headers["content-type"] == "application/zip"
        zf = zipfile.ZipFile(io.BytesIO(res.content))
        names = set(zf.namelist())
        assert {"about.json", "diagnostics.json", "settings.json", "jobs.json", "hardware.json", "logs/engine.log"} <= names
        everything = "".join(zf.read(n).decode("utf-8", "replace") for n in names)
        assert "SECRETSECRET" not in everything and "test-or" not in everything
        assert client.post("/api/support/clear-caches").json().keys() >= {"web_results", "vectors"}
        assert client.post("/api/support/restart").status_code == 400  # only under the desktop shell
