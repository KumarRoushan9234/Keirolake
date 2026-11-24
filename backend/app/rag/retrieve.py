"""Hybrid retrieval: Qdrant dense search over every named vector that has
points, plus SQLite FTS5 keyword search, fused with Reciprocal Rank Fusion."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import select, text

from app.catalog.models import KBModelStat, KnowledgeBase
from app.db import get_engine, session_scope
from app.embeddings.chain import get_chain
from app.embeddings.providers import EmbeddingError
from app.vector.qdrant_store import get_store

log = logging.getLogger(__name__)
RRF_K = 60


@dataclass
class Hit:
    chunk_id: str
    doc_id: str
    title: str
    url: str | None
    text: str
    score: float = 0.0  # fused RRF score
    cosine: float | None = None  # best dense similarity, if found by vector search
    matched_by: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class RetrievalResult:
    hits: list[Hit]
    best_cosine: float | None
    warnings: list[str]


def _fts_query(query: str) -> str:
    # Quote every term so user input can never be parsed as FTS5 syntax.
    terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 1]
    return " OR ".join(f'"{t}"' for t in terms[:32])


def keyword_search(kb_id: str, query: str, limit: int) -> list[dict]:
    match = _fts_query(query)
    if not match:
        return []
    sql = text("""
        SELECT c.id AS chunk_id, c.document_id AS doc_id, c.text AS text, d.title AS title, d.url AS url,
               bm25(chunks_fts) AS rank
        FROM chunks_fts JOIN chunks c ON c.pk = chunks_fts.rowid JOIN documents d ON d.id = c.document_id
        WHERE chunks_fts MATCH :match AND c.kb_id = :kb
        ORDER BY rank LIMIT :limit""")
    with get_engine().connect() as conn:
        return [dict(r._mapping) for r in conn.execute(sql, {"match": match, "kb": kb_id, "limit": limit})]


async def retrieve(kb_id: str, query: str, top_k: int = 8, doc_ids: list[str] | None = None,
                   use_keyword: bool = True) -> RetrievalResult:
    chain, store = get_chain(), get_store()
    with session_scope() as s:
        kb = s.get(KnowledgeBase, kb_id)
        if kb is None:
            raise ValueError("knowledge base not found")
        alias = kb.collection_alias
        stats = list(s.scalars(select(KBModelStat).where(KBModelStat.kb_id == kb_id, KBModelStat.points > 0)))
        vector_names = [st.vector_name for st in stats]

    ranked_lists: list[tuple[str, list[Hit]]] = []
    warnings: list[str] = []
    best_cosine: float | None = None
    for name in vector_names:
        spec = chain.spec_by_vector_name(name)
        if spec is None:
            warnings.append(f"Some documents were indexed with a cloud model ({name}); keyword search still finds them. "
                            "Re-index this knowledge base to search them by meaning on this PC.")
            continue
        try:
            qvec = (await chain.embed_with(spec, [query], "query"))[0]
        except EmbeddingError as exc:
            warnings.append(f"Query embedding with {spec.label} failed: {exc}")
            continue
        found = await store.search(alias, name, qvec, top_k * 3, doc_ids)
        hits = [Hit(h.payload.get("chunk_id", h.point_id), h.payload.get("doc_id", ""), h.payload.get("title", ""),
                    h.payload.get("url"), h.payload.get("text", ""), cosine=h.score, matched_by=[f"vector:{spec.label}"])
                for h in found]
        if hits:
            best_cosine = max(best_cosine or -1.0, hits[0].cosine or -1.0)
        ranked_lists.append((f"vector:{spec.label}", hits))

    if use_keyword:
        rows = keyword_search(kb_id, query, top_k * 3)
        if doc_ids:
            rows = [r for r in rows if r["doc_id"] in doc_ids]
        ranked_lists.append(("keyword", [Hit(r["chunk_id"], r["doc_id"], r["title"], r["url"], r["text"], matched_by=["keyword"])
                                         for r in rows]))

    fused: dict[str, Hit] = {}
    for label, hits in ranked_lists:
        for rank, hit in enumerate(hits):
            current = fused.get(hit.chunk_id)
            if current is None:
                current = fused[hit.chunk_id] = hit
                current.matched_by = []
            current.score += 1.0 / (RRF_K + rank + 1)
            if hit.cosine is not None:
                current.cosine = max(current.cosine or -1.0, hit.cosine)
            if label not in current.matched_by:
                current.matched_by.append(label)
    ordered = sorted(fused.values(), key=lambda h: h.score, reverse=True)[:top_k]
    for hit in ordered:
        hit.score = round(hit.score, 5)
        hit.cosine = round(hit.cosine, 4) if hit.cosine is not None else None
    return RetrievalResult(ordered, best_cosine, warnings)


async def retrieve_all(query: str, top_k: int = 8) -> tuple[list[Hit], float | None]:
    """Search every knowledge base and keep the best hits overall (ranked by meaning, then keyword rank)."""
    with session_scope() as s:
        kb_ids = [kb.id for kb in s.scalars(select(KnowledgeBase))]
    hits: list[Hit] = []
    best: float | None = None
    for kb_id in kb_ids:
        try:
            result = await retrieve(kb_id, query, top_k)
        except Exception as exc:  # noqa: BLE001 - one broken KB must not stop the others
            log.warning("Search in %s failed: %s", kb_id, exc)
            continue
        hits.extend(result.hits)
        if result.best_cosine is not None:
            best = max(best or -1.0, result.best_cosine)
    hits.sort(key=lambda h: (h.cosine if h.cosine is not None else -1.0, h.score), reverse=True)
    return hits[:top_k], best
