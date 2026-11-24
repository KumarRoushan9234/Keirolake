"""Evidence: what research finds, ranked once over the whole pool and kept in the lake for reuse.

Differences from the KeiroLabs web agent this replaces:
- one ranking over the whole pool (reciprocal rank fusion of BM25 and meaning), so scores from
  different search rounds are always comparable;
- if embeddings fail, ranking falls back to keywords and says so, instead of zero vectors;
- source quality comes from domain tiers the user can change (pin / block), not a fixed list;
- the model sees a bounded set of the best passages, never the whole pool;
- everything found is stored in SQLite, so later questions start from it.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlparse

from sqlalchemy import select

from app.catalog.models import EvidenceRow, utcnow
from app.db import session_scope

log = logging.getLogger(__name__)
RRF_K = 60
WORD = re.compile(r"[a-z0-9]{2,}")
STOP = set("the a an and or of to in on for is are was were be been with by as at from that this it its into than then their there these those which who what when where how why can could should would will about over under not no yes".split())


def terms(text: str) -> list[str]:
    return [w for w in WORD.findall(text.lower()) if w not in STOP]


def domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def _env_domains(name: str) -> set[str]:
    return {d.strip().lower().removeprefix("www.") for d in os.getenv(name, "").split(",") if d.strip()}


def tier(domain: str) -> int:
    """2 = pinned or official/academic, 1 = normal, 0 = blocked."""
    if not domain:
        return 1
    if any(domain == d or domain.endswith("." + d) for d in _env_domains("ENGINE_BLOCK_DOMAINS")):
        return 0
    if any(domain == d or domain.endswith("." + d) for d in _env_domains("ENGINE_PIN_DOMAINS")):
        return 2
    if re.search(r"\.(gov|edu|ac|mil)(\.[a-z]{2})?$|\.gov\.|\.nic\.in$|\.int$", domain) or domain.endswith(("who.int", "un.org", "europa.eu")):
        return 2
    return 1


@dataclass
class Passage:
    text: str
    url: str = ""
    title: str = ""
    origin: str = "web"  # web | lake | library
    sid: int = 0  # source number shown as [S1]
    score: float = 0.0
    vector: list[float] | None = None

    @property
    def key(self) -> str:
        return hashlib.sha1(re.sub(r"\s+", " ", self.text.strip().lower()).encode()).hexdigest()

    @property
    def domain(self) -> str:
        return domain_of(self.url)


@dataclass
class Pool:
    """De-duplicated passages with stable source numbers (one number per URL or document)."""

    max_passages: int = 400
    passages: list[Passage] = field(default_factory=list)
    _seen: set[str] = field(default_factory=set)
    _sources: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def add(self, items: list[Passage]) -> int:
        added = 0
        for p in items:
            if not p.text.strip() or p.key in self._seen or tier(p.domain) == 0:
                continue
            source = p.url or p.title
            if source not in self._sources:
                self._sources[source] = len(self._sources) + 1
            p.sid = self._sources[source]
            self._seen.add(p.key)
            self.passages.append(p)
            added += 1
        del self.passages[self.max_passages:]
        return added

    def sources(self) -> list[dict]:
        out = {}
        for p in self.passages:
            out.setdefault(p.sid, {"id": p.sid, "url": p.url, "title": p.title or p.url, "origin": p.origin, "domain": p.domain})
        return [out[k] for k in sorted(out)]

    async def rank(self, query: str) -> list[Passage]:
        """Rank the whole pool for `query`: BM25 and meaning fused by rank, nudged by source tier."""
        if not self.passages:
            return []
        lists = [_bm25_order(query, self.passages)]
        vec_order = await self._vector_order(query)
        if vec_order is not None:
            lists.append(vec_order)
        fused: dict[int, float] = Counter()
        for order in lists:
            for rank, idx in enumerate(order):
                fused[idx] += 1.0 / (RRF_K + rank + 1)
        for idx, p in enumerate(self.passages):
            p.score = fused.get(idx, 0.0) * (1.15 if tier(p.domain) == 2 else 1.0)
        return sorted(self.passages, key=lambda p: p.score, reverse=True)

    async def _vector_order(self, query: str) -> list[int] | None:
        from app.embeddings.chain import get_chain
        from app.embeddings.providers import EmbeddingError

        chain = get_chain()
        spec = chain.available()[0] if chain.specs else None
        if spec is None:
            return None
        try:
            missing = [p for p in self.passages if p.vector is None]
            if missing:
                vecs = await chain.embed_with(spec, [p.text[:2000] for p in missing], "passage")
                for p, v in zip(missing, vecs):
                    p.vector = v
            qvec = (await chain.embed_with(spec, [query], "query"))[0]
        except (EmbeddingError, Exception) as exc:  # noqa: BLE001 - keyword ranking still works
            msg = f"Ranking by meaning was unavailable ({type(exc).__name__}); ranked by keywords only"
            if msg not in self.warnings:
                self.warnings.append(msg)
            log.info("Evidence embedding failed: %s", exc)
            return None
        sims = [(_cos(qvec, p.vector), i) for i, p in enumerate(self.passages)]
        return [i for _, i in sorted(sims, reverse=True)]


def _cos(a: list[float], b: list[float] | None) -> float:
    if not b or len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def _bm25_order(query: str, passages: list[Passage], k1: float = 1.4, b: float = 0.75) -> list[int]:
    docs = [terms(p.text) for p in passages]
    q = set(terms(query))
    if not q:
        return list(range(len(passages)))
    n = len(docs)
    avg = sum(len(d) for d in docs) / max(1, n)
    df = Counter(t for d in docs for t in set(d) if t in q)
    scores = []
    for i, d in enumerate(docs):
        tf = Counter(t for t in d if t in q)
        s = 0.0
        for t, f in tf.items():
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * len(d) / max(1.0, avg)))
        scores.append((s, i))
    return [i for _, i in sorted(scores, reverse=True)]


def context(ranked: list[Passage], max_chars: int = 14000, per_source: int = 3) -> str:
    """The best passages for the model, bounded in size and per source."""
    out, used, count = [], 0, Counter()
    for p in ranked:
        if count[p.sid] >= per_source:
            continue
        block = f"[S{p.sid}] {p.title or p.url} ({p.url or p.origin})\n{p.text[:1800]}"
        if used + len(block) > max_chars:
            break
        out.append(block)
        used += len(block)
        count[p.sid] += 1
    return "\n\n---\n\n".join(out) or "(no evidence)"


# ---------- kept in the lake ----------
def store(task: str, query: str, passages: list[Passage]) -> int:
    """Keep web evidence so later research can start from it."""
    rows = 0
    with session_scope() as s:
        have = {h for (h,) in s.execute(select(EvidenceRow.text_hash).where(EvidenceRow.text_hash.in_([p.key for p in passages])))}
        for p in passages:
            if p.origin != "web" or p.key in have:
                continue
            s.add(EvidenceRow(task=task, query=query[:500], url=p.url, title=p.title[:300], domain=p.domain, text=p.text[:4000],
                              text_hash=p.key, fetched_at=utcnow()))
            have.add(p.key)
            rows += 1
    return rows


def recall(query: str, limit: int = 40) -> list[Passage]:
    """Stored evidence that shares words with the query (best BM25 first)."""
    q = terms(query)
    if not q:
        return []
    with session_scope() as s:
        cond = None
        for t in sorted(set(q), key=len, reverse=True)[:6]:
            c = EvidenceRow.text.like(f"%{t}%")
            cond = c if cond is None else cond | c
        rows = list(s.scalars(select(EvidenceRow).where(cond).order_by(EvidenceRow.fetched_at.desc()).limit(500)))
        found = [Passage(r.text, r.url, r.title, "web") for r in rows]
    order = _bm25_order(query, found)
    return [found[i] for i in order[:limit]]
