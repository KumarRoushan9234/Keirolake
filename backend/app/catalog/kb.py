"""Knowledge base lifecycle: create, delete, re-embed (blue/green via alias)."""
from __future__ import annotations

from sqlalchemy import select

from app.catalog.models import Chunk, KBModelStat, KnowledgeBase
from app.config import EmbedModelSpec, get_settings
from app.db import session_scope
from app.embeddings.chain import get_chain
from app.lake.zones import slugify
from app.vector.qdrant_store import get_store


def spec_to_dict(spec: EmbedModelSpec) -> dict:
    return {"provider": spec.provider, "model": spec.model, "dim": spec.dim, "vector_name": spec.vector_name}


def kb_specs(kb: KnowledgeBase) -> list[EmbedModelSpec]:
    return [EmbedModelSpec(v["provider"], v["model"], v["dim"]) for v in kb.vector_config]


class KBExists(Exception):
    pass


async def create_kb(name: str, description: str = "", chunk_size: int | None = None, chunk_overlap: int | None = None) -> KnowledgeBase:
    settings = get_settings()
    slug = slugify(name, 40)
    with session_scope() as s:
        if s.scalar(select(KnowledgeBase).where((KnowledgeBase.slug == slug) | (KnowledgeBase.name == name))):
            raise KBExists(f"Knowledge base {name!r} already exists")
    specs = get_chain().specs
    alias = f"kb_{slug}"
    collection = f"{alias}_v1"
    store = get_store()
    if await store.client.collection_exists(collection):
        await store.client.delete_collection(collection)
    await store.create_collection(collection, specs)
    await store.point_alias(alias, collection)
    with session_scope() as s:
        kb = KnowledgeBase(
            name=name, slug=slug, description=description, collection_alias=alias, collection_name=collection,
            vector_config=[spec_to_dict(sp) for sp in specs],
            chunk_size=chunk_size or settings.chunk_size, chunk_overlap=chunk_overlap or settings.chunk_overlap,
        )
        s.add(kb)
    return kb


async def delete_kb(kb_id: str) -> None:
    with session_scope() as s:
        kb = s.get(KnowledgeBase, kb_id)
        if kb is None:
            return
        alias, collection = kb.collection_alias, kb.collection_name
        s.delete(kb)
    await get_store().drop(alias, collection)


def chunk_payload(chunk: Chunk) -> dict:
    """Qdrant payload for a chunk: enough to cite and filter without a DB hit."""
    doc = chunk.document
    return {"chunk_id": chunk.id, "doc_id": doc.id, "kb_id": chunk.kb_id, "ordinal": chunk.ordinal,
            "title": doc.title, "url": doc.url, "source_id": doc.source_id, "run_id": doc.run_id,
            "mime": doc.mime, "text": chunk.text, "embed_model": chunk.embed_model}


def bump_model_stat(session, kb_id: str, spec: EmbedModelSpec, delta: int) -> None:
    stat = session.scalar(select(KBModelStat).where(KBModelStat.kb_id == kb_id, KBModelStat.vector_name == spec.vector_name))
    if stat is None:
        stat = KBModelStat(kb_id=kb_id, vector_name=spec.vector_name, model=spec.label, points=0)
        session.add(stat)
    stat.points = max(0, stat.points + delta)


async def reembed_kb(kb_id: str, progress=None) -> dict:
    """Rebuild the KB's collection with the *current* embedding chain.

    Chunk text lives in SQLite, so a new collection version is built from it,
    then the alias is switched and the old collection dropped. Queries keep
    hitting the old version until the switch.
    """
    chain = get_chain()
    store = get_store()
    with session_scope() as s:
        kb = s.get(KnowledgeBase, kb_id)
        if kb is None:
            raise ValueError("knowledge base not found")
        alias, old_collection, version = kb.collection_alias, kb.collection_name, kb.collection_version + 1
        rows = list(s.scalars(select(Chunk).where(Chunk.kb_id == kb_id).order_by(Chunk.pk)))
        payloads = {ch.id: chunk_payload(ch) for ch in rows}
    new_collection = f"{alias}_v{version}"
    if await store.client.collection_exists(new_collection):
        await store.client.delete_collection(new_collection)
    await store.create_collection(new_collection, chain.specs)

    per_model: dict[str, int] = {}
    used_specs: dict[str, EmbedModelSpec] = {}
    batch = 64
    for start in range(0, len(rows), batch):
        part = rows[start : start + batch]
        result = await chain.embed([r.text for r in part])
        ids = [r.id for r in part]
        pls = [{**payloads[r.id], "embed_model": result.spec.label} for r in part]
        await store.upsert(new_collection, result.spec.vector_name, ids, result.vectors, pls)
        per_model[result.spec.vector_name] = per_model.get(result.spec.vector_name, 0) + len(part)
        used_specs[result.spec.vector_name] = result.spec
        with session_scope() as s:
            for r in part:
                chunk = s.scalar(select(Chunk).where(Chunk.id == r.id))
                chunk.embed_model, chunk.vector_name = result.spec.label, result.spec.vector_name
        if progress:
            await progress(min(0.99, (start + len(part)) / max(1, len(rows))), f"Re-embedded {start + len(part)}/{len(rows)} chunks")

    await store.point_alias(alias, new_collection)
    if old_collection != new_collection and await store.client.collection_exists(old_collection):
        await store.client.delete_collection(old_collection)
    with session_scope() as s:
        kb = s.get(KnowledgeBase, kb_id)
        kb.collection_name, kb.collection_version = new_collection, version
        kb.vector_config = [spec_to_dict(sp) for sp in chain.specs]
        for stat in list(kb.model_stats):
            s.delete(stat)
        s.flush()
        for name, count in per_model.items():
            s.add(KBModelStat(kb_id=kb_id, vector_name=name, model=used_specs[name].label, points=count))
    return {"collection": new_collection, "chunks": len(rows), "per_model": per_model}
