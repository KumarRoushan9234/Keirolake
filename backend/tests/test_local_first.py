"""Local-first routing, the daily cloud limit, local embeddings and KeiroLabs ZDR."""
import json

import httpx
import pytest
import respx

from app import usage
from app.config import EmbedModelSpec, get_settings
from app.llm.registry import effective_order


def test_local_first_order_puts_local_models_first():
    assert effective_order(["openrouter", "groqcloud"], "local-first", True) == ["ollama", "llamacpp", "openrouter", "groqcloud"]
    assert effective_order(["openrouter"], "local-first", False) == ["ollama", "llamacpp"]
    assert effective_order(["openrouter"], "local", True) == ["ollama", "llamacpp"]
    assert effective_order(["openrouter"], "auto", True)[0] == "openrouter"


def test_cloud_limit_blocks_cloud_providers(monkeypatch):
    from app.llm.registry import LLMRouter

    monkeypatch.setattr(usage, "cloud_allowed", lambda: False)
    router = LLMRouter(["openrouter", "groqcloud"])
    assert all(a.provider in ("ollama", "llamacpp") for a in router.attempts())


def test_usage_counts_per_day():
    before = usage.today()["cloud_calls"]
    usage.add("cloud_calls")
    assert usage.today()["cloud_calls"] == before + 1
    assert usage.history()[-1]["cloud_calls"] >= 1


def test_local_first_embed_chain_starts_with_ollama(monkeypatch):
    from app import config

    monkeypatch.setenv("LLM_MODE", "local-first")
    chain = config._build_embed_chain()
    assert chain[0].provider == "ollama" and chain[0].dim == 1024
    assert any(c.provider == "openrouter" for c in chain)
    monkeypatch.setenv("LLM_MODE", "local")
    assert [c.provider for c in config._build_embed_chain()] == ["ollama"]


@pytest.mark.asyncio
async def test_chain_only_uses_models_the_collection_has():
    from app.embeddings.chain import EmbeddingChain
    from app.embeddings.providers import EmbeddingError

    specs = [EmbedModelSpec("ollama", "qwen3-embedding:0.6b", 8), EmbedModelSpec("openrouter", "baai/bge-m3", 8)]
    chain = EmbeddingChain(specs)
    with pytest.raises(EmbeddingError):
        await chain.embed(["x"], allowed={"something_else"})


@pytest.mark.asyncio
@respx.mock
async def test_keiro_sends_zdr_and_reads_confirmation():
    from app.keiro.client import KeiroClient

    route = respx.post(url__regex=r".*/search/lite").mock(return_value=httpx.Response(
        200, json={"results": [{"url": "https://a.org", "title": "A"}], "creditsCharged": 0.5}, headers={"x-keiro-zdr": "on"}))
    client = KeiroClient(api_key="k", base_url="https://keiro.test/api/v2")
    await client.search("zdr check", max_results=1)
    assert json.loads(route.calls[0].request.content)["zdr"] is True
    assert client.status()["zdr_confirmed"] is True
    assert get_settings().keiro_zdr is True
