import json

import httpx
import pytest
import respx

from app.config import EmbedModelSpec, get_settings
from app.embeddings.chain import EmbeddingCache, EmbeddingChain
from app.embeddings.providers import EmbeddingError, embed_raw

URL = "https://openrouter.ai/api/v1/embeddings"


def vectors(n, dim=8):
    return httpx.Response(200, json={"data": [{"index": i, "embedding": [0.1] * dim} for i in range(n)]})


def make_chain(tmp_path):
    return EmbeddingChain(cache=EmbeddingCache(tmp_path / "c.db"))


@respx.mock
async def test_primary_used_when_healthy(tmp_path):
    respx.post(URL).mock(side_effect=lambda r: vectors(len(json.loads(r.content)["input"])))
    result = await make_chain(tmp_path).embed(["a", "b"])
    assert result.spec.model == "baai/bge-m3" and len(result.vectors) == 2 and result.errors == []


@respx.mock
async def test_falls_back_and_trips_breaker(tmp_path):
    def handler(request):
        body = json.loads(request.content)
        if body["model"] == "baai/bge-m3":
            return httpx.Response(400, json={"error": {"message": "model unavailable"}})
        return vectors(len(body["input"]))

    route = respx.post(URL).mock(side_effect=handler)
    chain = make_chain(tmp_path)
    result = await chain.embed(["x"])
    assert result.spec.model == "qwen/qwen3-embedding-8b" and "bge-m3" in result.errors[0]
    await chain.embed(["y"])  # breaker open: the primary is skipped entirely
    assert route.call_count == 3


@respx.mock
async def test_dimension_mismatch_is_an_error():
    spec = get_settings().embed_models[0]
    respx.post(URL).mock(return_value=vectors(1, dim=3))
    with pytest.raises(EmbeddingError, match="dim 3"):
        await embed_raw(spec, ["x"])


@respx.mock
async def test_cache_avoids_second_call(tmp_path):
    route = respx.post(URL).mock(return_value=vectors(1))
    chain = make_chain(tmp_path)
    await chain.embed_with(chain.specs[0], ["same text"])
    await chain.embed_with(chain.specs[0], ["same text"])
    assert route.call_count == 1


@respx.mock
async def test_model_specific_prefixes_and_dimensions_param():
    route = respx.post(URL).mock(return_value=vectors(1, dim=16))
    await embed_raw(EmbedModelSpec("openrouter", "intfloat/multilingual-e5-large", 16), ["hello"], "query")
    assert json.loads(route.calls[0].request.content)["input"] == ["query: hello"]
    await embed_raw(EmbedModelSpec("openrouter", "qwen/qwen3-embedding-8b", 16), ["hello"], "passage")
    assert json.loads(route.calls[1].request.content)["dimensions"] == 16
