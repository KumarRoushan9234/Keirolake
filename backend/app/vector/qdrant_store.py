"""Qdrant access: one collection per knowledge base, always behind an alias.

Each collection declares one named vector per model in the embedding chain.
A point only carries the vector of the model that embedded it; queries fan
out over every named vector that has points and the results are fused.
"""
from __future__ import annotations

from dataclasses import dataclass

from qdrant_client import AsyncQdrantClient, models

from app.config import EmbedModelSpec, get_settings


@dataclass
class VectorHit:
    point_id: str
    score: float
    vector_name: str
    payload: dict


# Qdrant's defaults (32 MB write-ahead log, one segment per CPU core) made a one-document knowledge base take
# ~700 MB on a 32-thread PC. These keep small collections small; Qdrant still grows segments as data grows.
COMPACT = {"wal_config": models.WalConfigDiff(wal_capacity_mb=4), "optimizers_config": models.OptimizersConfigDiff(default_segment_number=2)}


class QdrantStore:
    def __init__(self, client: AsyncQdrantClient | None = None):
        settings = get_settings()
        if client is not None:
            self.client = client
        elif settings.qdrant_path:  # embedded mode: no server, vectors stored in this folder
            self.client = AsyncQdrantClient(path=settings.qdrant_path)
        else:
            self.client = AsyncQdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key, timeout=60)

    async def healthy(self) -> dict:
        try:
            collections = await self.client.get_collections()
            return {"ok": True, "collections": len(collections.collections)}
        except Exception as exc:  # noqa: BLE001 - surfaced to the health endpoint
            return {"ok": False, "error": str(exc)}

    async def create_collection(self, name: str, specs: list[EmbedModelSpec]) -> None:
        await self.client.create_collection(
            collection_name=name,
            vectors_config={s.vector_name: models.VectorParams(size=s.dim, distance=models.Distance.COSINE) for s in specs},
            on_disk_payload=True,
            **COMPACT,
        )
        for field_name, schema in (("doc_id", models.PayloadSchemaType.KEYWORD),
                                   ("source_id", models.PayloadSchemaType.KEYWORD),
                                   ("run_id", models.PayloadSchemaType.KEYWORD),
                                   ("embed_model", models.PayloadSchemaType.KEYWORD),
                                   ("mime", models.PayloadSchemaType.KEYWORD)):
            await self.client.create_payload_index(name, field_name=field_name, field_schema=schema)

    async def point_alias(self, alias: str, collection: str) -> None:
        """Atomically (re)point an alias at a collection."""
        existing = await self.client.get_aliases()
        ops: list = []
        if any(a.alias_name == alias for a in existing.aliases):
            ops.append(models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=alias)))
        ops.append(models.CreateAliasOperation(create_alias=models.CreateAlias(collection_name=collection, alias_name=alias)))
        await self.client.update_collection_aliases(change_aliases_operations=ops)

    async def drop(self, alias: str, collection: str) -> None:
        try:
            await self.client.update_collection_aliases(
                change_aliases_operations=[models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=alias))]
            )
        except Exception:  # noqa: BLE001 - alias may already be gone
            pass
        if await self.client.collection_exists(collection):
            await self.client.delete_collection(collection)

    async def upsert(self, collection: str, vector_name: str, ids: list[str], vectors: list[list[float]],
                     payloads: list[dict], extra: dict[str, list[list[float]]] | None = None) -> None:
        points = []
        for i, (pid, vec, payload) in enumerate(zip(ids, vectors, payloads)):
            named = {vector_name: vec}
            for other_name, other_vectors in (extra or {}).items():
                named[other_name] = other_vectors[i]
            points.append(models.PointStruct(id=pid, vector=named, payload=payload))
        for start in range(0, len(points), 256):
            await self.client.upsert(collection_name=collection, points=points[start : start + 256], wait=True)

    async def delete_by_doc(self, collection: str, doc_id: str) -> None:
        await self.client.delete(
            collection_name=collection,
            points_selector=models.FilterSelector(filter=models.Filter(
                must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))]
            )),
        )

    async def search(self, collection: str, vector_name: str, vector: list[float], limit: int,
                     doc_ids: list[str] | None = None) -> list[VectorHit]:
        query_filter = None
        if doc_ids:
            query_filter = models.Filter(must=[models.FieldCondition(key="doc_id", match=models.MatchAny(any=doc_ids))])
        resp = await self.client.query_points(
            collection_name=collection, query=vector, using=vector_name, limit=limit,
            query_filter=query_filter, with_payload=True,
        )
        return [VectorHit(str(p.id), float(p.score), vector_name, p.payload or {}) for p in resp.points]

    async def count(self, collection: str) -> int:
        return (await self.client.count(collection_name=collection, exact=True)).count


_store: QdrantStore | None = None


def get_store() -> QdrantStore:
    global _store
    if _store is None:
        _store = QdrantStore()
    return _store
