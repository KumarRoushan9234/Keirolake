"""The OpenAI-compatible gateway: shape, streaming, grounding with sources, scopes, cloud limit, embeddings."""
import json

import pytest
from fastapi.testclient import TestClient

from app import access
from app.api import gateway_routes as gw
from app.llm.base import LLMError
from app.llm.registry import Attempt


class Echo:
    def __init__(self, fail=False):
        self.fail, self.seen = fail, []

    async def complete(self, model, messages, temperature=0.2, max_tokens=1024):
        if self.fail:
            raise LLMError("down")
        self.seen.append(messages)
        return f"{model} says: " + messages[-1].content

    async def stream(self, model, messages, temperature=0.2, max_tokens=1024):
        self.seen.append(messages)
        for part in ("Hel", "lo"):
            yield part


class Router:
    def __init__(self, attempts):
        self._a = attempts

    def attempts(self, include_cooling=False, local_only=False):
        return self._a

    def describe(self):
        return [{"provider": "ollama", "configured": True, "has_key": False, "models": ["m1"]},
                {"provider": "openrouter", "configured": True, "has_key": True, "models": ["big"]}]

    @staticmethod
    def _count(a):
        pass


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def key(scopes):
    k, raw = access.create_key("gw test", scopes)
    return k, {"Authorization": f"Bearer {raw}"}


def test_chat_completion_shape_and_named_model(client, monkeypatch):
    echo = Echo()
    monkeypatch.setattr(gw, "get_router", lambda: Router([Attempt("ollama", "m1", echo)]))
    k, h = key(["llm"])
    try:
        r = client.post("/v1/chat/completions", headers=h, json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        body = r.json()
        assert r.status_code == 200 and body["object"] == "chat.completion" and body["choices"][0]["message"]["content"] == "m1 says: hi"
        assert body["model"] == "ollama:m1" and body["usage"]["total_tokens"] >= 0
        named = client.post("/v1/chat/completions", headers=h, json={"model": "ollama:m1", "messages": [{"role": "user", "content": [{"type": "text", "text": "parts"}]}]})
        assert named.json()["choices"][0]["message"]["content"] == "m1 says: parts"
    finally:
        access.revoke(k["id"])


def test_streaming_sends_openai_chunks(client, monkeypatch):
    monkeypatch.setattr(gw, "get_router", lambda: Router([Attempt("ollama", "m1", Echo())]))
    k, h = key(["llm"])
    try:
        with client.stream("POST", "/v1/chat/completions", headers=h, json={"messages": [{"role": "user", "content": "x"}], "stream": True}) as r:
            lines = [l for l in r.iter_lines() if l.startswith("data: ")]
        assert lines[-1] == "data: [DONE]"
        chunks = [json.loads(l[6:]) for l in lines[:-1]]
        assert all(c["object"] == "chat.completion.chunk" for c in chunks)
        assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks) == "Hello" and chunks[-1]["choices"][0]["finish_reason"] == "stop"
    finally:
        access.revoke(k["id"])


def test_fallback_and_scope(client, monkeypatch):
    monkeypatch.setattr(gw, "get_router", lambda: Router([Attempt("ollama", "bad", Echo(fail=True)), Attempt("ollama", "m1", Echo())]))
    k, h = key(["read"])
    k2, h2 = key(["llm"])
    try:
        assert client.post("/v1/chat/completions", headers=h, json={"messages": [{"role": "user", "content": "x"}]}).status_code == 403
        assert client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]}).status_code == 401
        ok = client.post("/v1/chat/completions", headers=h2, json={"messages": [{"role": "user", "content": "x"}]})
        assert ok.json()["model"] == "ollama:m1"
    finally:
        access.revoke(k["id"]); access.revoke(k2["id"])


def test_cloud_model_refused_when_the_daily_limit_is_used(client, monkeypatch):
    monkeypatch.setattr(gw, "get_router", lambda: Router([Attempt("ollama", "m1", Echo())]))  # router already dropped cloud
    monkeypatch.setattr(gw.usage, "cloud_allowed", lambda: False)
    k, h = key(["llm"])
    try:
        r = client.post("/v1/chat/completions", headers=h, json={"model": "openrouter:big", "messages": [{"role": "user", "content": "x"}]})
        assert r.status_code == 429
    finally:
        access.revoke(k["id"])


def test_grounding_adds_passages_and_sources(client, monkeypatch):
    from app.rag import retrieve as rv

    echo = Echo()
    monkeypatch.setattr(gw, "get_router", lambda: Router([Attempt("ollama", "m1", echo)]))
    monkeypatch.setattr(gw, "allowed_kbs", lambda key: [{"id": "kb1", "name": "Solar"}])

    async def fake_retrieve(kb_id, q, top_k=8, **kw):
        return rv.RetrievalResult([rv.Hit("c1", "d1", "Panel prices", "https://x.example/p", "Alpha costs 1200.", 1.0, 0.8)], 0.8, [])

    monkeypatch.setattr(rv, "retrieve", fake_retrieve)
    k, h = key(["llm", "read"])
    try:
        r = client.post("/v1/chat/completions", headers=h, json={"model": "lake", "messages": [{"role": "user", "content": "price?"}]})
        msg = r.json()["choices"][0]["message"]
        assert msg["sources"] == [{"n": 1, "title": "Panel prices", "url": "https://x.example/p", "collection": "Solar"}]
        assert "Alpha costs 1200." in echo.seen[-1][0].content and echo.seen[-1][0].role == "system"
        assert client.post("/v1/chat/completions", headers=h, json={"model": "lake:nope", "messages": [{"role": "user", "content": "x"}]}).status_code == 404
    finally:
        access.revoke(k["id"])


def test_models_list(client, monkeypatch):
    monkeypatch.setattr(gw, "get_router", lambda: Router([]))
    k, h = key(["llm"])
    try:
        ids = [m["id"] for m in client.get("/v1/models", headers=h).json()["data"]]
        assert ids[:2] == ["auto", "lake"] and "openrouter:big" in ids
    finally:
        access.revoke(k["id"])


def test_embeddings(client, monkeypatch):
    from app.config import EmbedModelSpec
    from app.embeddings import chain as ch

    class Chain:
        specs = [EmbedModelSpec("ollama", "e1", 3)]

        async def embed(self, texts, kind="passage", allowed=None):
            return ch.EmbedResult(self.specs[0], [[0.1, 0.2, 0.3] for _ in texts], [])

    monkeypatch.setattr(ch, "get_chain", lambda: Chain())
    k, h = key(["llm"])
    try:
        r = client.post("/v1/embeddings", headers=h, json={"input": ["a", "b"]}).json()
        assert r["object"] == "list" and len(r["data"]) == 2 and r["data"][1]["index"] == 1 and r["model"] == "ollama:e1"
        assert client.post("/v1/embeddings", headers=h, json={"input": "a", "model": "nope"}).status_code == 404
    finally:
        access.revoke(k["id"])
