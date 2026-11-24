"""Offline libraries: register, download, index and retrieve from ZIM files.

Retrieval works in tiers so a library is useful the moment it is downloaded:
  0  the ZIM's own full-text/title index picks articles; passages are ranked on the fly
  1  + a vector index of every article intro (semantic "which article")
  2  + a vector index of every passage
Passages are re-ranked with the local embedder when it is available, and by
keyword overlap otherwise, so this works with no internet and no GPU.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from qdrant_client import models
from sqlalchemy import select

from app.catalog.models import Library
from app.config import get_settings
from app.db import session_scope
from app.ingest.chunking import chunk_text
from app.library import zim
from app.library.download import fetch_resumable, published_sha256
from app.local import ollama
from app.vector.qdrant_store import COMPACT, get_store

log = logging.getLogger(__name__)
_pause: set[str] = set()
MIN_COSINE = 0.40  # below this a semantic match is noise (unrelated pages score ~0.2-0.38)
MIN_KEYWORD = 1.0
STOPWORDS = set("""a an and are as at be been but by can could do does for from had has have how i if in into is it its
me my no not of on or our should so than that the their them then there these they this to too us was we what when
where which who why will with would you your about after before during between best way ways tell explain""".split())


def library_dir() -> Path:
    path = Path(os.getenv("LIBRARY_DIR") or get_settings().data_dir / "library" / "zim")
    path.mkdir(parents=True, exist_ok=True)
    return path


def to_dict(lib: Library) -> dict:
    return {"id": lib.id, "name": lib.name, "title": lib.title, "summary": lib.summary, "filename": lib.filename,
            "path": lib.path, "size_bytes": lib.size_bytes, "articles": lib.article_count, "language": lib.language,
            "issued": lib.issued, "status": lib.status, "message": lib.message, "enabled": bool(lib.enabled),
            "index_tier": lib.index_tier, "indexed": lib.indexed, "index_model": lib.index_model,
            "url": lib.url}


def list_libraries() -> list[dict]:
    with session_scope() as s:
        return [to_dict(lib) for lib in s.scalars(select(Library).order_by(Library.created_at))]


def ready_libraries() -> list[Library]:
    with session_scope() as s:
        return list(s.scalars(select(Library).where(Library.status == "ready", Library.enabled == 1)))


def get(lib_id: str) -> Library | None:
    with session_scope() as s:
        return s.get(Library, lib_id)


def _base_name(filename: str) -> str:
    return re.sub(r"_\d{4}-\d{2}$", "", Path(filename).stem)


# ---------- registering ----------
def register_file(path: str) -> dict:
    """Add a ZIM that is already on disk (e.g. downloaded with a torrent)."""
    file = Path(path).expanduser()
    if file.suffix != ".zim" or not file.is_file():
        raise ValueError("Pick an existing .zim file")
    meta = zim.metadata(str(file))
    name = _base_name(file.name) or meta["name"]
    with session_scope() as s:
        lib = s.scalar(select(Library).where(Library.name == name)) or Library(name=name)
        lib.title, lib.summary, lib.filename, lib.path = meta["title"] or name, meta["description"], file.name, str(file)
        lib.size_bytes, lib.article_count, lib.language, lib.issued = file.stat().st_size, meta["articles"], meta["language"], meta["date"]
        lib.status, lib.message = "ready", ""
        s.add(lib)
        s.flush()
        return to_dict(lib)


def start_download(entry: dict) -> tuple[str, callable]:
    """Create (or reuse) the library row and return the download job."""
    url = entry["url"]
    if not url.startswith("https://") or not url.endswith(".zim"):
        raise ValueError("Library downloads must be https:// links to .zim files")
    filename = url.rsplit("/", 1)[-1]
    with session_scope() as s:
        lib = s.scalar(select(Library).where(Library.name == entry["name"])) or Library(name=entry["name"])
        lib.title, lib.summary, lib.url, lib.filename = entry.get("title", ""), entry.get("summary", ""), url, filename
        lib.size_bytes, lib.article_count, lib.issued = entry.get("size_bytes", 0), entry.get("articles", 0), entry.get("issued", "")
        lib.language = entry.get("language", "")
        if lib.status == "ready" and lib.path and Path(lib.path).exists():
            lib.message = "Updating in the background; the current version stays usable"
        else:
            lib.status, lib.message = "downloading", ""
        s.add(lib)
        s.flush()
        lib_id = lib.id

    async def job(ctx) -> dict:
        target = library_dir() / filename
        try:
            sha = await published_sha256(url)
            await fetch_resumable(url, target, ctx, sha)
            await ctx.progress(1.0, "Checking the file")
            meta = await asyncio.to_thread(zim.metadata, str(target))
        except Exception as exc:
            _set(lib_id, status="failed", message=str(exc)[:500])
            raise
        old_path = None
        with session_scope() as s:
            lib = s.get(Library, lib_id)
            old_path = lib.path if lib.path and lib.path != str(target) else None
            lib.path, lib.status, lib.message = str(target), "ready", "" if sha else "No published checksum; not verified"
            lib.article_count, lib.size_bytes = meta["articles"], target.stat().st_size
            lib.title = lib.title or meta["title"]
        if old_path and Path(old_path).exists() and Path(old_path).parent == library_dir():
            zim.open_archive.cache_clear()
            zim.read_article.cache_clear()
            Path(old_path).unlink(missing_ok=True)  # an older snapshot of the same library
        return {"library": lib_id, "path": str(target), "verified": bool(sha)}

    return lib_id, job


def updates(entries: list[dict]) -> list[dict]:
    """Installed libraries that have a newer snapshot in the catalog."""
    by_name = {e["name"]: e for e in entries}
    out = []
    for lib in list_libraries():
        entry = by_name.get(lib["name"])
        if lib["status"] == "ready" and entry and entry["issued"] and lib["issued"] and entry["issued"][:10] > lib["issued"][:10]:
            out.append({"id": lib["id"], "name": lib["name"], "title": lib["title"], "current": lib["issued"][:10],
                        "available": entry["issued"][:10], "size_bytes": entry["size_bytes"]})
    return out


def _set(lib_id: str, **fields) -> None:
    with session_scope() as s:
        lib = s.get(Library, lib_id)
        for key, value in fields.items():
            setattr(lib, key, value)


async def remove(lib_id: str, delete_file: bool) -> None:
    lib = get(lib_id)
    if lib is None:
        return
    store = get_store()
    for suffix in ("intro", "passages"):
        name = f"lib_{lib_id}_{suffix}"
        if await store.client.collection_exists(name):
            await store.client.delete_collection(name)
    zim.open_archive.cache_clear()
    zim.read_article.cache_clear()
    if delete_file and lib.path and Path(lib.path).parent == library_dir():
        Path(lib.path).unlink(missing_ok=True)
        Path(lib.path + ".part").unlink(missing_ok=True)
    with session_scope() as s:
        s.delete(s.get(Library, lib_id))


# ---------- indexing (tiers 1 and 2) ----------
def collection_for(lib_id: str, tier: int) -> str:
    return f"lib_{lib_id}_{'intro' if tier == 1 else 'passages'}"


async def _ensure_collection(name: str, dim: int) -> None:
    client = get_store().client
    if await client.collection_exists(name):
        return
    await client.create_collection(
        collection_name=name,
        vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE, on_disk=True),
        quantization_config=models.ScalarQuantization(
            scalar=models.ScalarQuantizationConfig(type=models.ScalarType.INT8, always_ram=True)),
        on_disk_payload=True,
        **COMPACT,
    )


def _point_id(lib_id: str, path: str, ordinal: int = 0) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{lib_id}/{path}#{ordinal}"))


def pause_index(lib_id: str) -> None:
    _pause.add(lib_id)


def index_job(lib_id: str, tier: int, batch: int = 32):
    """Resumable indexing job. Re-running continues from the saved cursor."""
    if tier not in (1, 2):
        raise ValueError("tier must be 1 (intros) or 2 (passages)")

    async def job(ctx) -> dict:
        settings = get_settings()
        model, dim = settings.wiki_embed_model, settings.wiki_embed_dim
        lib = get(lib_id)
        if lib is None or lib.status != "ready":
            raise RuntimeError("Library is not ready")
        if not await ollama.has_model(model):
            raise RuntimeError(f"Embedding model {model} is not installed; install it on the Models page")
        if lib.index_model and lib.index_model != f"{model}@{dim}" and lib.index_tier >= tier:
            raise RuntimeError("This library was indexed with another embedding model; reset its index first")
        collection = collection_for(lib_id, tier)
        await _ensure_collection(collection, dim)
        archive_total = (await asyncio.to_thread(zim.open_archive, lib.path)).entry_count
        cursor = lib.index_cursor if lib.index_tier == tier else 0
        indexed = lib.indexed if lib.index_tier == tier else 0
        _set(lib_id, index_tier=tier, index_model=f"{model}@{dim}", message="Indexing")
        _pause.discard(lib_id)
        articles = zim.iter_articles(lib.path, cursor)
        while True:
            if lib_id in _pause:
                _pause.discard(lib_id)
                _set(lib_id, index_cursor=cursor, indexed=indexed, message="Paused")
                return {"paused": True, "indexed": indexed}
            group = await asyncio.to_thread(lambda: [a for _, a in zip(range(batch), articles)])
            if not group:
                break
            texts, ids, payloads = [], [], []
            for entry_id, path, title in group:
                art = await asyncio.to_thread(zim.read_article, lib.path, path)
                cursor = entry_id + 1
                if art is None or len(art.text) < 200:
                    continue
                pieces = [zim.intro(art.text)] if tier == 1 else chunk_text(art.text, 1200, 150)[:60]
                for n, piece in enumerate(pieces):
                    texts.append(f"{title}\n{piece}")
                    ids.append(_point_id(lib_id, path, n))
                    payloads.append({"path": path, "title": title, "text": piece[:1500] if tier == 2 else piece[:600]})
            if texts:
                vectors = []
                for i in range(0, len(texts), 64):
                    vectors += await ollama.embed(model, texts[i:i + 64], dim)
                await get_store().client.upsert(collection, points=[
                    models.PointStruct(id=pid, vector=vec, payload=pl) for pid, vec, pl in zip(ids, vectors, payloads)])
                indexed += len(texts)
            _set(lib_id, index_cursor=cursor, indexed=indexed)
            await ctx.progress(cursor / max(archive_total, 1), f"{indexed:,} {'intros' if tier == 1 else 'passages'} indexed")
        _set(lib_id, index_cursor=cursor, indexed=indexed, message="Index complete")
        return {"indexed": indexed}

    return job


async def reset_index(lib_id: str) -> None:
    store = get_store()
    for tier in (1, 2):
        name = collection_for(lib_id, tier)
        if await store.client.collection_exists(name):
            await store.client.delete_collection(name)
    _set(lib_id, index_tier=0, index_cursor=0, indexed=0, index_model="", message="")


# ---------- retrieval ----------
@dataclass
class LibraryHit:
    library_id: str
    library: str
    path: str
    title: str
    text: str
    score: float
    semantic: bool = False  # score is a cosine similarity (else a keyword score)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def is_strong(hits: list[LibraryHit], threshold: float) -> bool:
    """Do the library passages plausibly answer the question? Cosine when semantic, keyword score otherwise."""
    if not hits:
        return False
    best = max(h.score for h in hits)
    return best >= threshold if hits[0].semantic else best >= 2.5


def keywords(question: str) -> list[str]:
    words = re.findall(r"[\w'-]+", question.lower())
    return [w for w in words if w not in STOPWORDS and len(w) > 1][:8]


def _lexical(text: str, terms: list[str]) -> float:
    low = text.lower()
    return sum(math.log(1 + low.count(t)) * (1 + len(t) / 10) for t in terms)


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _search_paths(lib: Library, terms: list[str], n: int) -> list[str]:
    if not terms:
        return []
    paths = zim.search(lib.path, " ".join(terms), n)
    if len(paths) < n // 2 and len(terms) > 2:
        paths += [p for p in zim.search(lib.path, " ".join(sorted(terms, key=len, reverse=True)[:2]), n) if p not in paths]
    if not paths:
        for term in sorted(terms, key=len, reverse=True)[:2]:
            paths += [p for p in zim.search(lib.path, term, n // 2 or 1) if p not in paths]
    return paths[:n]


async def retrieve(question: str, limit: int = 6, libraries: list[Library] | None = None) -> tuple[list[LibraryHit], list[str]]:
    """Best passages from the offline libraries for `question`."""
    libs = libraries if libraries is not None else ready_libraries()
    if not libs:
        return [], []
    settings = get_settings()
    terms = keywords(question)
    warnings: list[str] = []
    qvec = None
    try:
        if await ollama.has_model(settings.wiki_embed_model):
            qvec = (await ollama.embed(settings.wiki_embed_model, [question], settings.wiki_embed_dim))[0]
    except Exception as exc:  # noqa: BLE001 - lexical ranking still works
        warnings.append(f"Local embedder unavailable ({exc}); ranking library passages by keywords")

    candidates: list[tuple[Library, str, str]] = []  # (lib, path, passage)
    seen: set[tuple[str, str]] = set()
    store = get_store()
    for lib in libs:
        paths = await asyncio.to_thread(_search_paths, lib, terms, 5)
        if qvec is not None and lib.index_tier >= 1 and lib.index_model == f"{settings.wiki_embed_model}@{settings.wiki_embed_dim}":
            for tier in (1, 2) if lib.index_tier == 2 else (1,):
                name = collection_for(lib.id, tier)
                if not await store.client.collection_exists(name):
                    continue
                res = await store.client.query_points(name, query=qvec, limit=6 if tier == 1 else 8, with_payload=True)
                for point in res.points:
                    if tier == 1:
                        if point.payload["path"] not in paths:
                            paths.append(point.payload["path"])
                    elif (lib.id, point.payload["path"] + point.payload["text"][:40]) not in seen:
                        seen.add((lib.id, point.payload["path"] + point.payload["text"][:40]))
                        candidates.append((lib, point.payload["path"], f"{point.payload['title']}\n{point.payload['text']}"))
        arts = await asyncio.gather(*(asyncio.to_thread(zim.read_article, lib.path, p) for p in paths[:8]))
        for art in arts:
            if art is None or not art.text.strip():
                continue
            for piece in chunk_text(art.text, 1200, 150)[:40]:
                candidates.append((lib, art.path, f"{art.title}\n{piece}"))

    if not candidates:
        return [], warnings
    # Cheap keyword pre-filter, then semantic re-rank of the best few dozen.
    scored = sorted(candidates, key=lambda c: _lexical(c[2], terms), reverse=True)[:48]
    lexical_only = qvec is None
    if qvec is not None:
        try:
            vectors = await ollama.embed(settings.wiki_embed_model, [c[2] for c in scored], settings.wiki_embed_dim)
            ranked = sorted(zip(scored, (_cosine(qvec, v) for v in vectors)), key=lambda x: x[1], reverse=True)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Semantic re-rank failed ({exc}); using keyword order")
            lexical_only = True
            ranked = [(c, _lexical(c[2], terms)) for c in scored]
    else:
        ranked = [(c, _lexical(c[2], terms)) for c in scored]

    hits, per_article = [], {}
    for (lib, path, passage), score in ranked:
        if score < (MIN_KEYWORD if lexical_only else MIN_COSINE):
            continue  # unrelated passages would mislead the model and the user
        key = (lib.id, path)
        if per_article.get(key, 0) >= 2:
            continue
        per_article[key] = per_article.get(key, 0) + 1
        title, _, text = passage.partition("\n")
        hits.append(LibraryHit(lib.id, lib.title or lib.name, path, title, text, round(float(score), 4), qvec is not None and not lexical_only))
        if len(hits) >= limit:
            break
    return hits, warnings
