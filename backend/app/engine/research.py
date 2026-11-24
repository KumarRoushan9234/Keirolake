"""Research: gather evidence (lake first, then the web through KeiroLabs with ZDR), write a cited answer, check it.

Loop, per round: rank the whole pool → ask the model whether the best evidence answers the question →
if not, search the gaps (new queries only) → add what comes back. Then write the answer in Markdown
with [S1] tags, check every cited sentence against the passages it cites, and flag what isn't supported.
Budgets (rounds, searches, time) come from the depth preset and are reported as they are spent.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import AsyncIterator

from app.engine.evidence import Passage, Pool, context, recall, store, terms
from app.llm.base import ChatMessage

log = logging.getLogger(__name__)

DEPTHS = {
    "quick": {"rounds": 1, "queries": 3, "results": 4, "seconds": 60, "context": 9000},
    "standard": {"rounds": 2, "queries": 4, "results": 5, "seconds": 150, "context": 14000},
    "deep": {"rounds": 4, "queries": 5, "results": 6, "seconds": 360, "context": 20000},
}
SUPPORT_THRESHOLD = 0.34

EVALUATE = """You decide whether research is finished. You see a QUESTION and the best EVIDENCE found so far.
Reply with ONLY JSON: {"sufficient": true|false, "missing": "what is still unknown, one line", "queries": ["web search", ...]}
- sufficient = true only if the evidence answers every part of the question.
- queries: up to {n} short web searches (under 10 words) for what is missing; none that were already run: {done}.
- For comparisons, search each side separately. For multi-step questions, search the next step."""

WRITE = """You write a research answer from numbered EVIDENCE.
- Cite every factual sentence with source tags like [S1] or [S2][S5], using only numbers that appear in the evidence.
- Use clear Markdown: a short direct answer first, then details, tables when comparing.
- If the evidence doesn't answer something, say so plainly. Never invent facts or sources.
- Note disagreements between sources."""

CHECK = """Does the SOURCE text support the CLAIM? Reply with one word: YES or NO."""


def _json(text: str) -> dict:
    from app.llm.jsonfix import loads_lenient

    return loads_lenient(text) or {}


def _norm(q: str) -> str:
    return " ".join(sorted(set(terms(q))))


def _chunks(text: str, size: int = 1200, overlap: int = 150, limit: int = 4) -> list[str]:
    from app.ingest.chunking import chunk_text

    return chunk_text(text, size, overlap)[:limit] if text else []


async def _lake(question: str, kb_id: str | None, use_library: bool) -> list[Passage]:
    from app.library import service as library
    from app.rag.retrieve import retrieve, retrieve_all

    out: list[Passage] = []
    try:
        hits = (await retrieve(kb_id, question, 8)).hits if kb_id else (await retrieve_all(question, 8))[0]
        out += [Passage(h.text, h.url or "", h.title, "lake") for h in hits]
    except Exception as exc:  # noqa: BLE001 - research continues without it
        log.info("Lake search failed: %s", exc)
    if use_library:
        try:
            lib, _ = await library.retrieve(question, limit=6)
            out += [Passage(z.text, "", f"{z.title} ({z.library})", "library") for z in lib]
        except Exception as exc:  # noqa: BLE001
            log.info("Library search failed: %s", exc)
    return out + recall(question, 20)


def verify_claims(answer: str, pool: Pool) -> tuple[list[str], list[str], list[str]]:
    """(supported, borderline, unsupported) cited sentences, by word overlap with the passages they cite."""
    by_sid: dict[int, str] = {}
    for p in pool.passages:
        by_sid[p.sid] = by_sid.get(p.sid, "") + " " + p.text
    supported, borderline, unsupported = [], [], []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", answer):
        sids = [int(x) for x in re.findall(r"\[S(\d+)\]", sentence)]
        claim = re.sub(r"\[S\d+\]", "", sentence).strip(" -*#|")
        words = set(terms(claim))
        if not sids or len(words) < 4:
            continue
        source = set(terms(" ".join(by_sid.get(s, "") for s in sids)))
        if not source:
            unsupported.append(claim)
            continue
        ratio = len(words & source) / len(words)
        (supported if ratio >= SUPPORT_THRESHOLD else borderline if ratio >= 0.15 else unsupported).append(claim)
    return supported, borderline, unsupported


async def research_stream(question: str, *, depth: str = "standard", kb_id: str | None = None,
                          history: list[ChatMessage] | None = None, use_library: bool = True,
                          allow_web: bool = True) -> AsyncIterator[dict]:
    from app.config import offline_mode
    from app.keiro.client import KeiroError, get_keiro
    from app.llm.registry import AllModelsFailed, get_router
    from app.net import connectivity

    preset = DEPTHS.get(depth, DEPTHS["standard"])
    started = time.monotonic()
    router = get_router()
    pool = Pool()
    yield {"type": "status", "message": "Searching your lake"}
    pool.add(await _lake(question, kb_id, use_library))

    keiro = get_keiro()
    web_ok = allow_web and keiro.configured and connectivity.is_online and not offline_mode()
    yield {"type": "route", "route": "research", "reason": f"{depth.capitalize()} research: your lake first"
           + (", then the web through KeiroLabs (zero data retention)" if web_ok else ", web not used")}
    done_queries: list[str] = []
    searches = 0
    for round_no in range(preset["rounds"] if web_ok else 0):
        if time.monotonic() - started > preset["seconds"]:
            yield {"type": "status", "message": "Time budget reached; writing with what was found"}
            break
        ranked = await pool.rank(question)
        prompt = EVALUATE.replace("{n}", str(preset["queries"])).replace("{done}", json.dumps(done_queries) or "none")
        try:
            judged = await router.complete([ChatMessage("system", prompt), ChatMessage("user", f"QUESTION: {question}\n\nEVIDENCE:\n{context(ranked, 8000)}")],
                                           temperature=0.1, max_tokens=300)
            plan = _json(judged.text)
        except AllModelsFailed as exc:
            yield {"type": "error", "message": str(exc)}
            return
        if plan.get("sufficient") and pool.passages:
            yield {"type": "status", "message": "The evidence answers the question"}
            break
        seen = {_norm(q) for q in done_queries}
        queries = []
        for q in plan.get("queries") or ([question] if round_no == 0 else []):
            q = str(q).strip()[:200]
            if q and _norm(q) not in seen:
                seen.add(_norm(q))
                queries.append(q)
        queries = queries[:preset["queries"]]
        if not queries:
            break
        done_queries += queries
        yield {"type": "status", "message": f"Round {round_no + 1}: searching {', '.join(queries)}"}

        async def one(q: str) -> list[Passage]:
            try:
                results = await keiro.search(q, preset["results"], "content")
            except KeiroError as exc:
                pool.warnings.append(f"Search failed for “{q}”: {exc}")
                return []
            found = []
            for r in results:
                for chunk in _chunks(r.content or r.snippet):
                    found.append(Passage(chunk, r.url, r.title, "web"))
            return found

        batches = await asyncio.gather(*(one(q) for q in queries))
        searches += len(queries)
        added = pool.add([p for b in batches for p in b])
        yield {"type": "status", "message": f"Round {round_no + 1}: {added} new passages from {sum(1 for b in batches if b)} searches"}

    ranked = await pool.rank(question)
    used = ranked[:60]
    sources = pool.sources()
    yield {"type": "sources", "local": [], "library": [], "private": [], "web": [], "research": sources,
           "best_cosine": None, "web_used": searches > 0}
    for w in pool.warnings[:3]:
        yield {"type": "status", "message": w}

    messages = [ChatMessage("system", WRITE), *(history or [])[-4:],
                ChatMessage("user", f"EVIDENCE:\n{context(used, preset['context'])}\n\n---\n\nQUESTION: {question}")]
    answer = ""
    try:
        async for item in router.stream(messages, temperature=0.2, max_tokens=1800):
            if isinstance(item, tuple):
                yield {"type": "meta", "model": item[1]}
            else:
                answer += item
                yield {"type": "token", "text": item}
    except AllModelsFailed as exc:
        yield {"type": "error", "message": str(exc)}
        return

    yield {"type": "status", "message": "Checking every cited sentence against its sources"}
    supported, borderline, unsupported = verify_claims(answer, pool)
    by_sid = {}
    for p in pool.passages:
        by_sid.setdefault(p.sid, []).append(p.text)
    for claim in borderline[:6]:  # let the model judge the close calls
        sids = []
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", answer):
            if claim[:40] in sentence:
                sids = [int(x) for x in re.findall(r"\[S(\d+)\]", sentence)]
                break
        source = " ".join(" ".join(by_sid.get(s, [])) for s in sids)[:4000]
        try:
            verdict = await router.complete([ChatMessage("system", CHECK), ChatMessage("user", f"CLAIM: {claim}\n\nSOURCE: {source}")],
                                            temperature=0, max_tokens=3)
            (supported if "YES" in verdict.text.upper() else unsupported).append(claim)
        except Exception:  # noqa: BLE001 - leave it unchecked
            unsupported.append(claim)
    unsupported += borderline[6:]
    if unsupported:
        note = "\n\n> **Not confirmed by the cited sources:** " + " · ".join(c[:160] for c in unsupported[:5])
        answer += note
        yield {"type": "token", "text": note}
    yield {"type": "verify", "checked": len(supported) + len(unsupported), "supported": len(supported), "unsupported": len(unsupported)}
    try:
        kept = await asyncio.to_thread(store, "research", question, [p for p in used if p.origin == "web"])
        if kept:
            yield {"type": "status", "message": f"Kept {kept} new passages in the lake for next time"}
    except Exception as exc:  # noqa: BLE001 - the answer is already written
        log.warning("Storing evidence failed: %s", exc)
    yield {"type": "done", "searches": searches, "seconds": round(time.monotonic() - started, 1)}
