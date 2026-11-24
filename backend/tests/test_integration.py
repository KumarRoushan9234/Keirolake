"""End-to-end against a real Qdrant (docker compose up -d qdrant).

Embeddings are replaced by a deterministic bag-of-words hash so retrieval
quality is testable without network access or API keys.
"""
import hashlib
import uuid

import httpx
import pytest

from app.catalog import kb as kb_service
from app.config import get_settings
from app.embeddings import chain as chain_module
from app.embeddings.providers import EmbeddingError
from app.ingest.pipeline import IncomingDoc, delete_document, finish_run, ingest_docs, start_run
from app.lake.zones import raw_run_dir, write_raw_bytes
from app.rag.retrieve import retrieve
from app.vector.qdrant_store import QdrantStore

pytestmark = pytest.mark.integration


def _qdrant_up() -> bool:
    try:
        return httpx.get(get_settings().qdrant_url + "/healthz", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture(autouse=True)
def _require_qdrant():
    if not _qdrant_up():
        pytest.skip("Qdrant is not running on " + get_settings().qdrant_url)


def _vec(text: str, dim: int, salt: str) -> list[float]:
    v = [0.0] * dim
    for word in text.lower().split():
        h = int(hashlib.md5(f"{salt}{word}".encode()).hexdigest(), 16)
        v[h % dim] += 1.0
    norm = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / norm for x in v]


@pytest.fixture
def fake_embedder(monkeypatch, tmp_path):
    failing: set[str] = set()

    async def fake(spec, texts, kind="passage", timeout=0):
        if spec.model in failing:
            raise EmbeddingError(f"{spec.model} is down")
        return [_vec(t, spec.dim, spec.model) for t in texts]

    monkeypatch.setattr(chain_module, "embed_raw", fake)
    # Fresh chain per test: no breaker state and no embedding-cache hits from other tests.
    monkeypatch.setattr(chain_module, "_chain", chain_module.EmbeddingChain(cache=chain_module.EmbeddingCache(tmp_path / "c.db")))
    return failing


async def _make_kb():
    # Fresh singletons so the store binds to this test's event loop.
    import app.vector.qdrant_store as qs
    qs._store = QdrantStore()
    return await kb_service.create_kb(f"it-{uuid.uuid4().hex[:8]}")


async def _ingest(kb_id, title, text):
    source_id, run_id = start_run("text", title, kb_id)
    raw = write_raw_bytes(raw_run_dir("text", run_id), f"{title}.md", text.encode())
    stats = await ingest_docs(kb_id, source_id, run_id, [IncomingDoc(title, text, raw)])
    finish_run(run_id, stats=stats)
    return stats


DOCS = {
    "Qdrant": "Qdrant is a vector database written in Rust that supports payload filtering and hybrid search.",
    "Pasta": "Boil salted water, cook the spaghetti for nine minutes, then toss with olive oil and garlic.",
}


async def test_ingest_search_and_lineage(fake_embedder):
    kb = await _make_kb()
    try:
        for title, text in DOCS.items():
            stats = await _ingest(kb.id, title, text)
            assert stats["documents"] == 1 and stats["models"] == {"openrouter:baai/bge-m3": 1}

        result = await retrieve(kb.id, "vector database written in Rust", top_k=2)
        assert result.hits[0].title == "Qdrant"
        assert "vector:openrouter:baai/bge-m3" in result.hits[0].matched_by

        # keyword-only match (FTS5) works too
        kw = await retrieve(kb.id, "spaghetti", top_k=1)
        assert kw.hits[0].title == "Pasta"

        # duplicate content is skipped, not re-indexed
        assert (await _ingest(kb.id, "Qdrant again", DOCS["Qdrant"]))["skipped_duplicates"] == 1
    finally:
        await kb_service.delete_kb(kb.id)


async def test_embedding_fallback_stores_under_backup_vector_and_search_spans_both(fake_embedder):
    kb = await _make_kb()
    try:
        await _ingest(kb.id, "Qdrant", DOCS["Qdrant"])  # embedded by the primary

        fake_embedder.add("baai/bge-m3")  # primary goes down mid-flight
        stats = await _ingest(kb.id, "Pasta", DOCS["Pasta"])
        assert stats["models"] == {"openrouter:qwen/qwen3-embedding-8b": 1}
        assert any("bge-m3" in e for e in stats["fallback_errors"])

        # Query embedding for the primary now fails; the backup vector space still answers
        pasta = await retrieve(kb.id, "cook spaghetti with garlic", top_k=2)
        assert pasta.hits[0].title == "Pasta"
        assert any("bge-m3" in w for w in pasta.warnings)

        fake_embedder.clear()  # primary recovers: both vector spaces are queried and fused
        both = await retrieve(kb.id, "Rust vector database", top_k=2)
        assert both.hits[0].title == "Qdrant"
        matched = {m for h in both.hits for m in h.matched_by}
        assert {"vector:openrouter:baai/bge-m3", "vector:openrouter:qwen/qwen3-embedding-8b"} <= matched
    finally:
        await kb_service.delete_kb(kb.id)


async def test_delete_document_removes_points_and_rows(fake_embedder):
    kb = await _make_kb()
    try:
        await _ingest(kb.id, "Qdrant", DOCS["Qdrant"])
        from app.catalog.models import Document
        from app.db import session_scope
        from sqlalchemy import select
        with session_scope() as s:
            doc_id = s.scalar(select(Document.id).where(Document.kb_id == kb.id))
        await delete_document(doc_id)
        assert (await retrieve(kb.id, "vector database", top_k=3)).hits == []
    finally:
        await kb_service.delete_kb(kb.id)


async def test_reembed_swaps_collection_behind_alias(fake_embedder):
    kb = await _make_kb()
    try:
        await _ingest(kb.id, "Qdrant", DOCS["Qdrant"])
        result = await kb_service.reembed_kb(kb.id)
        assert result["collection"].endswith("_v2") and result["chunks"] == 1
        found = await retrieve(kb.id, "vector database Rust", top_k=1)
        assert found.hits[0].title == "Qdrant"
    finally:
        await kb_service.delete_kb(kb.id)
