"""Embedding fallback chain with a circuit breaker and an on-disk cache.

Different models produce incompatible vector spaces, so the chain never mixes
them: every batch is embedded entirely by one model, and the caller stores the
result under that model's Qdrant named vector.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
from array import array
from dataclasses import dataclass
from pathlib import Path

from app.config import EmbedModelSpec, get_settings
from app.embeddings.providers import EmbeddingError, embed_raw

log = logging.getLogger(__name__)
BREAKER_SECONDS = 120.0


class EmbeddingCache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("CREATE TABLE IF NOT EXISTS emb (key TEXT PRIMARY KEY, vec BLOB NOT NULL)")
        self._conn.commit()

    @staticmethod
    def key(spec: EmbedModelSpec, kind: str, text: str) -> str:
        return hashlib.sha256(f"{spec.label}|{spec.dim}|{kind}|{text}".encode()).hexdigest()

    def get_many(self, keys: list[str]) -> dict[str, list[float]]:
        if not keys:
            return {}
        out: dict[str, list[float]] = {}
        with self._lock:
            for i in range(0, len(keys), 500):
                part = keys[i : i + 500]
                rows = self._conn.execute(
                    f"SELECT key, vec FROM emb WHERE key IN ({','.join('?' * len(part))})", part
                ).fetchall()
                for k, blob in rows:
                    out[k] = array("f", blob).tolist()
        return out

    def put_many(self, items: dict[str, list[float]]) -> None:
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO emb (key, vec) VALUES (?, ?)",
                [(k, array("f", v).tobytes()) for k, v in items.items()],
            )
            self._conn.commit()


@dataclass
class EmbedResult:
    spec: EmbedModelSpec
    vectors: list[list[float]]
    errors: list[str]


class EmbeddingChain:
    def __init__(self, specs: list[EmbedModelSpec] | None = None, cache: EmbeddingCache | None = None):
        settings = get_settings()
        self.specs = specs if specs is not None else settings.embed_models
        self.batch_size = settings.embed_batch_size
        self.cache = cache or EmbeddingCache(settings.cache_dir / "embeddings.db")
        self._open_until: dict[str, float] = {}

    def spec_by_vector_name(self, name: str) -> EmbedModelSpec | None:
        return next((s for s in self.specs if s.vector_name == name), None)

    def available(self) -> list[EmbedModelSpec]:
        now = time.monotonic()
        healthy = [s for s in self.specs if self._open_until.get(s.label, 0) <= now]
        return healthy or list(self.specs)

    async def embed_with(self, spec: EmbedModelSpec, texts: list[str], kind: str = "passage") -> list[list[float]]:
        """Embed with one specific model (cache-aware, batched). No fallback."""
        keys = [self.cache.key(spec, kind, t) for t in texts]
        cached = self.cache.get_many(keys)
        missing = [i for i, k in enumerate(keys) if k not in cached]
        fresh: dict[str, list[float]] = {}
        for start in range(0, len(missing), self.batch_size):
            idx = missing[start : start + self.batch_size]
            vectors = await embed_raw(spec, [texts[i] for i in idx], kind)
            for i, vec in zip(idx, vectors):
                fresh[keys[i]] = vec
        if fresh:
            self.cache.put_many(fresh)
        merged = {**cached, **fresh}
        return [merged[k] for k in keys]

    async def embed(self, texts: list[str], kind: str = "passage", allowed: set[str] | None = None) -> EmbedResult:
        """Embed with the first healthy model in the chain, falling back on failure.

        `allowed` limits the choice to these vector names (the ones a collection was created with).
        """
        errors: list[str] = []
        specs = [s for s in self.available() if allowed is None or s.vector_name in allowed]
        if allowed is not None and not specs:
            raise EmbeddingError("None of this knowledge base's embedding models is available now: re-index it to use the current models.")
        for spec in specs:
            try:
                vectors = await self.embed_with(spec, texts, kind)
                self._open_until.pop(spec.label, None)
                return EmbedResult(spec, vectors, errors)
            except EmbeddingError as exc:
                log.warning("Embedding fallback: %s -> %s", spec.label, exc)
                errors.append(f"{spec.label}: {exc}")
                self._open_until[spec.label] = time.monotonic() + BREAKER_SECONDS
        raise EmbeddingError("All embedding models failed:\n" + "\n".join(errors))

    async def probe(self) -> list[dict]:
        results = []
        for spec in self.specs:
            started = time.perf_counter()
            try:
                vec = (await embed_raw(spec, ["ping"], "query"))[0]
                results.append({"model": spec.label, "vector_name": spec.vector_name, "dim": spec.dim, "ok": True,
                                "returned_dim": len(vec), "latency_ms": round((time.perf_counter() - started) * 1000)})
            except EmbeddingError as exc:
                results.append({"model": spec.label, "vector_name": spec.vector_name, "dim": spec.dim, "ok": False,
                                "error": str(exc)})
        return results


_chain: EmbeddingChain | None = None


def get_chain() -> EmbeddingChain:
    global _chain
    if _chain is None:
        _chain = EmbeddingChain()
    return _chain
