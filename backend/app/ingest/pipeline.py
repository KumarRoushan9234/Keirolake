"""raw -> clean -> chunks -> SQLite + Qdrant, with lineage ids at every step."""
from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from sqlalchemy import select

from app.catalog.kb import bump_model_stat
from app.catalog.models import Chunk, Document, IngestionRun, KnowledgeBase, Source
from app.config import get_settings
from app.db import session_scope
from app.embeddings.chain import get_chain
from app.ingest.chunking import chunk_text
from app.lake.zones import clean_dir, relative, slugify
from app.vector.qdrant_store import get_store

log = logging.getLogger(__name__)
Progress = Callable[[float, str], Awaitable[None]]


@dataclass
class IncomingDoc:
    title: str
    text: str
    raw_path: Path
    mime: str = "text/plain"
    url: str | None = None
    meta: dict = field(default_factory=dict)


def start_run(kind: str, name: str, kb_id: str | None, config: dict | None = None) -> tuple[str, str]:
    """Register a source + ingestion run. Returns (source_id, run_id)."""
    with session_scope() as s:
        source = Source(kind=kind, name=name[:200], config=config or {})
        s.add(source)
        s.flush()
        run = IngestionRun(source_id=source.id, kb_id=kb_id)
        s.add(run)
        s.flush()
        return source.id, run.id


def finish_run(run_id: str, *, raw_path: str = "", stats: dict | None = None, error: str | None = None) -> None:
    with session_scope() as s:
        run = s.get(IngestionRun, run_id)
        run.status = "failed" if error else "done"
        run.error = error
        run.raw_path = raw_path or run.raw_path
        run.stats = stats or {}
        run.finished_at = datetime.now(timezone.utc)
        kb_id, source_id = run.kb_id, run.source_id
    if not error:
        from app import access

        access.emit("ingest.finished", {"run_id": run_id, "kb_id": kb_id, "source_id": source_id,
                                        "stats": {k: v for k, v in (stats or {}).items() if isinstance(v, (int, float, str))}})


def _chunk_id(doc_id: str, ordinal: int) -> str:
    # Deterministic UUID: re-ingesting the same document yields the same point ids.
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"keirolake:{doc_id}:{ordinal}"))


async def ingest_docs(kb_id: str, source_id: str, run_id: str, docs: list[IncomingDoc],
                      progress: Progress | None = None) -> dict:
    settings = get_settings()
    chain, store = get_chain(), get_store()
    with session_scope() as s:
        kb = s.get(KnowledgeBase, kb_id)
        if kb is None:
            raise ValueError("knowledge base not found")
        alias, slug, size, overlap = kb.collection_alias, kb.slug, kb.chunk_size, kb.chunk_overlap
        kb_vector_names = {v["vector_name"] for v in kb.vector_config}

    stats = {"documents": 0, "skipped_duplicates": 0, "empty": 0, "chunks": 0, "models": {}, "fallback_errors": []}
    out_dir = clean_dir(slug)
    for index, doc in enumerate(docs):
        text = doc.text.strip()
        if not text:
            stats["empty"] += 1
            continue
        content_hash = hashlib.sha256(text.encode()).hexdigest()
        with session_scope() as s:
            if s.scalar(select(Document.id).where(Document.kb_id == kb_id, Document.content_hash == content_hash)):
                stats["skipped_duplicates"] += 1
                continue
        doc_id = uuid.uuid4().hex
        clean_path = out_dir / f"{doc_id}_{slugify(doc.title, 40)}.md"
        clean_path.write_text(f"# {doc.title}\n\n{text}\n", encoding="utf-8")

        pieces = chunk_text(text, size, overlap)
        result = await chain.embed(pieces, allowed=kb_vector_names)
        stats["fallback_errors"].extend(result.errors)
        extra = None
        if settings.embed_dual_write:
            backup = next((sp for sp in chain.available() if sp != result.spec and sp.vector_name in kb_vector_names), None)
            if backup is not None:
                try:
                    extra = {backup.vector_name: await chain.embed_with(backup, pieces)}
                except Exception as exc:  # noqa: BLE001 - dual write is best effort
                    stats["fallback_errors"].append(f"dual-write {backup.label}: {exc}")

        chunk_ids = [_chunk_id(doc_id, i) for i in range(len(pieces))]
        base_payload = {"doc_id": doc_id, "kb_id": kb_id, "title": doc.title, "url": doc.url, "source_id": source_id,
                        "run_id": run_id, "mime": doc.mime, "embed_model": result.spec.label}
        payloads = [{**base_payload, "chunk_id": cid, "ordinal": i, "text": piece}
                    for i, (cid, piece) in enumerate(zip(chunk_ids, pieces))]
        await store.upsert(alias, result.spec.vector_name, chunk_ids, result.vectors, payloads, extra)

        with session_scope() as s:
            s.add(Document(
                id=doc_id, kb_id=kb_id, run_id=run_id, source_id=source_id, title=doc.title[:300], url=doc.url,
                mime=doc.mime, raw_path=relative(doc.raw_path), clean_path=relative(clean_path),
                content_hash=content_hash, chars=len(text), chunk_count=len(pieces), meta=doc.meta,
            ))
            s.flush()
            s.add_all([
                Chunk(id=cid, document_id=doc_id, kb_id=kb_id, ordinal=i, text=piece,
                      embed_model=result.spec.label, vector_name=result.spec.vector_name)
                for i, (cid, piece) in enumerate(zip(chunk_ids, pieces))
            ])
            bump_model_stat(s, kb_id, result.spec, len(pieces))
        stats["documents"] += 1
        stats["chunks"] += len(pieces)
        stats["models"][result.spec.label] = stats["models"].get(result.spec.label, 0) + len(pieces)
        if progress:
            await progress((index + 1) / len(docs), f"Indexed {doc.title[:60]} ({len(pieces)} chunks via {result.spec.label})")
    return stats


async def delete_document(doc_id: str) -> None:
    with session_scope() as s:
        doc = s.get(Document, doc_id)
        if doc is None:
            return
        kb = doc.kb
        alias = kb.collection_alias
        counts: dict[str, int] = {}
        for chunk in doc.chunks:
            counts[chunk.vector_name] = counts.get(chunk.vector_name, 0) + 1
        for stat in kb.model_stats:
            stat.points = max(0, stat.points - counts.get(stat.vector_name, 0))
        s.delete(doc)
    await get_store().delete_by_doc(alias, doc_id)
