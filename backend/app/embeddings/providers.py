"""OpenAI-compatible embedding client (OpenRouter, Gemini, Ollama, OpenAI, ...)."""
from __future__ import annotations

import re

import httpx

from app import http
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import EmbedModelSpec, provider_config


class EmbeddingError(Exception):
    pass


class _Retryable(EmbeddingError):
    pass


# Models that need instruction prefixes to embed well. (query_prefix, passage_prefix)
_PREFIXES: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"e5"), "query: ", "passage: "),
    (re.compile(r"bge-(large|base|small)-en"), "Represent this sentence for searching relevant passages: ", ""),
    (re.compile(r"qwen3-embedding"),
     "Instruct: Given a search query, retrieve relevant passages that answer the query\nQuery: ", ""),
]
# Models that accept the `dimensions` parameter (Matryoshka truncation).
_MRL = re.compile(r"qwen3-embedding|gemini-embedding|text-embedding-3")


def _prefix(model: str, kind: str) -> str:
    for pattern, query_prefix, passage_prefix in _PREFIXES:
        if pattern.search(model.lower()):
            return query_prefix if kind == "query" else passage_prefix
    return ""


async def embed_raw(spec: EmbedModelSpec, texts: list[str], kind: str = "passage", timeout: float = 90.0) -> list[list[float]]:
    """Embed with exactly one model. Raises EmbeddingError on any failure."""
    if spec.provider == "ollama":
        return await _embed_ollama(spec, texts, kind, timeout)
    cfg = provider_config(spec.provider)
    if not cfg.api_key:
        raise EmbeddingError(f"{spec.provider.upper()}_API_KEY is not set")
    prefix = _prefix(spec.model, kind)
    body: dict = {"model": spec.model, "input": [prefix + t for t in texts]}
    if _MRL.search(spec.model.lower()):
        body["dimensions"] = spec.dim
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"

    async def _call() -> list[list[float]]:
        try:
            resp = await http.client("embed").post(f"{cfg.base_url}/embeddings", headers=headers, json=body, timeout=timeout)
        except httpx.HTTPError as exc:
            raise _Retryable(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise _Retryable(f"HTTP {resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise EmbeddingError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if "data" not in data:
            raise EmbeddingError(f"Malformed response: {str(data)[:200]}")
        vectors = [row["embedding"] for row in sorted(data["data"], key=lambda r: r.get("index", 0))]
        if len(vectors) != len(texts):
            raise EmbeddingError(f"Expected {len(texts)} vectors, got {len(vectors)}")
        if vectors and len(vectors[0]) != spec.dim:
            raise EmbeddingError(f"{spec.label} returned dim {len(vectors[0])}, EMBED_MODELS says {spec.dim}")
        return vectors

    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=8),
        retry=retry_if_exception(lambda e: isinstance(e, _Retryable)),
        reraise=True,
    ):
        with attempt:
            return await _call()
    raise EmbeddingError("unreachable")


async def _embed_ollama(spec: EmbedModelSpec, texts: list[str], kind: str, timeout: float) -> list[list[float]]:
    """Local embeddings through Ollama's native API (it truncates Matryoshka models to `dim`)."""
    from app.local import ollama

    prefix = _prefix(spec.model, kind)
    try:
        vectors = await ollama.embed(spec.model, [prefix + t for t in texts], dim=spec.dim, timeout=timeout)
    except (httpx.HTTPError, RuntimeError) as exc:
        raise EmbeddingError(f"Local embedding with {spec.model} failed: {exc}") from exc
    if vectors and len(vectors[0]) != spec.dim:
        raise EmbeddingError(f"{spec.label} returned dim {len(vectors[0])}, expected {spec.dim}")
    return vectors
