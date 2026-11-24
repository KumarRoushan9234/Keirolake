"""Grounded answers with numbered citations, streamed as events.

Sources are cited as [1] (your knowledge bases), [Z1] (offline libraries such as
Wikipedia), [P1] (your private vault, local models only) and [W1] (live web via
KeiroLabs). Web search kicks in when the user asks for it, or when local
retrieval is weak, and is skipped while offline.
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from app import safety, vault
from app.config import get_settings, offline_mode
from app.keiro.client import KeiroError, WebResult, get_keiro
from app.llm.base import ChatMessage
from app.llm.registry import AllModelsFailed, get_router
from app.library import service as library
from app.library.service import LibraryHit
from app.net import connectivity
from app.rag.intent import classify, needs_web
from app.rag.retrieve import Hit, retrieve, retrieve_all

log = logging.getLogger(__name__)
MAX_CONTEXT_CHARS = 2400  # per source
CONTENT_GRACE_SECONDS = 10.0  # extra wait for full pages after the quick snippets arrive

SYSTEM_PROMPT = """You are KeiroLake, a research assistant that answers strictly from the provided sources.
Rules:
- Cite every factual claim with the source tag in square brackets, e.g. [1], [Z1], [P1] or [W2]. Use only tags that appear in the sources.
- If the sources do not contain the answer, say so plainly and suggest what data to add. Do not invent facts.
- Anything you add from general knowledge without a source must start with "(From the model, not your library)".
- Never give exact doses, mixing ratios, temperatures, voltages or other safety-critical numbers unless a source states them; if none does, say the sources don't give them.
- Prefer concise, well-structured Markdown answers (short paragraphs, bullet lists, tables where useful)."""

CHAT_PROMPT = """You are KeiroLake, a private assistant that runs on the user's own computer.
It is their local datalake: sheets, documents and offline libraries kept on their own computer, answers with citations,
and web gathering through KeiroLabs. Reply briefly and plainly in Markdown. Don't invent facts."""

NO_SOURCES_RULE = ("No sources were found for this question. Answer from general knowledge if you can, start with "
                   "\"(From the model, not your library)\", and keep safety-critical numbers out unless you are certain.")


def _format_sources(hits: list[Hit], web: list[WebResult], lib: list[LibraryHit] | None = None,
                    private: list[vault.PrivateHit] | None = None) -> str:
    blocks = []
    for i, h in enumerate(hits, 1):
        where = f" ({h.url})" if h.url else ""
        blocks.append(f"[{i}] {h.title}{where}\n{h.text[:MAX_CONTEXT_CHARS]}")
    for i, z in enumerate(lib or [], 1):
        blocks.append(f"[Z{i}] {z.title} ({z.library})\n{z.text[:MAX_CONTEXT_CHARS]}")
    for i, p in enumerate(private or [], 1):
        blocks.append(f"[P{i}] {p.title} (private note)\n{p.text[:MAX_CONTEXT_CHARS]}")
    for i, w in enumerate(web, 1):
        blocks.append(f"[W{i}] {w.title or w.url} ({w.url})\n{(w.content or w.snippet)[:MAX_CONTEXT_CHARS]}")
    return "\n\n---\n\n".join(blocks) if blocks else "(no sources found)"


async def _web_context(query: str, n: int = 3) -> tuple[list[WebResult], str | None]:
    """Quick snippets first (about 4 s), then up to CONTENT_GRACE_SECONDS more for full page text."""
    keiro = get_keiro()
    if not keiro.configured:
        return [], "KeiroLabs key not configured; web fallback skipped"
    content = asyncio.create_task(keiro.search(query, max_results=n, mode="content"))
    last = "no results"
    quick: list[WebResult] = []
    for mode in ("fast", "lite"):
        try:
            quick = await keiro.search(query, max_results=n, mode=mode)
            break
        except KeiroError as exc:
            log.warning("Keiro %s search failed: %s", mode, exc)
            last = str(exc)
    try:
        full = await asyncio.wait_for(asyncio.shield(content), timeout=CONTENT_GRACE_SECONDS if quick else 60)
        if any((r.content or "").strip() for r in full):
            return full, None
    except asyncio.TimeoutError:
        content.cancel()
    except KeiroError as exc:
        last = str(exc)
    if quick:
        return quick, None
    return [], f"Web search failed: {last}"


def build_messages(question: str, history: list[ChatMessage], sources: str, extra_rules: list[str] | None = None) -> list[ChatMessage]:
    trimmed = history[-6:]  # keep recent turns for follow-up questions
    system = "\n".join([SYSTEM_PROMPT, *(extra_rules or [])])
    return [
        ChatMessage("system", system),
        *trimmed,
        ChatMessage("user", f"Sources:\n\n{sources}\n\n---\n\nQuestion: {question}"),
    ]


async def answer_stream(kb_id: str | None, question: str, history: list[ChatMessage] | None = None,
                        top_k: int | None = None, use_web: str = "auto", use_library: bool = True,
                        use_private: bool = False, style: str = "", use_all_kbs: bool = True) -> AsyncIterator[dict]:
    """Yields events: {type: safety|sources|status|meta|token|done|error}.

    use_web: "auto" (fallback when weak), "always", or "never".
    use_library: also search the offline libraries.
    use_private: also search the unlocked vault; the answer is then generated by a local model only.
    """
    settings = get_settings()
    top_k = top_k or settings.rag_top_k
    if offline_mode():
        use_web = "never"
    category = safety.classify(question)
    if category:
        yield {"type": "safety", "category": category, "message": safety.notice(category)}

    intent = classify(question)
    if intent.kind == "chat" and not category and use_web != "always" and not use_private:
        yield {"type": "route", "route": "chat", "reason": intent.reason}
        yield {"type": "sources", "local": [], "web": [], "library": [], "private": [], "best_cosine": None, "web_used": False}
        chat = [ChatMessage("system", CHAT_PROMPT), *(history or [])[-6:], ChatMessage("user", question)]
        async for event in _generate(chat, max_tokens=400):
            yield event
        return

    hits: list[Hit] = []
    best = None
    if kb_id:
        result = await retrieve(kb_id, question, top_k)
        hits, best = result.hits, result.best_cosine
        for w in result.warnings:
            yield {"type": "status", "message": w}
    elif use_all_kbs:
        hits, best = await retrieve_all(question, top_k)

    lib_hits: list[LibraryHit] = []
    if use_library and library.ready_libraries():
        yield {"type": "status", "message": "Searching your offline libraries..."}
        try:
            lib_hits, lib_warnings = await library.retrieve(question)
            for w in lib_warnings:
                yield {"type": "status", "message": w}
        except Exception as exc:  # noqa: BLE001 - libraries are optional context
            log.warning("Library retrieval failed: %s", exc)
            yield {"type": "status", "message": f"Library search failed: {exc}"}

    private_hits = vault.search(question) if use_private else []
    if private_hits:
        yield {"type": "status", "message": "Using your private notes: only a model on this computer will see them"}

    weak = ((not hits or best is None or best < settings.rag_web_threshold)
            and not library.is_strong(lib_hits, settings.rag_web_threshold) and not private_hits)
    want_web, reason = False, "Answered from your data and libraries"
    if use_web == "always":
        want_web, reason = True, "Web search is set to always"
    elif use_web == "auto" and settings.rag_web_fallback and not private_hits:
        if intent.kind == "fresh":
            want_web, reason = True, intent.reason
        elif weak and connectivity.is_online:
            want_web, reason = await needs_web(question)
        elif weak:
            reason = "Nothing found in your data; offline, so answered from the model's general knowledge"
    web: list[WebResult] = []
    if want_web and not connectivity.is_online:
        yield {"type": "status", "message": "Offline: answering from your data and libraries only"}
        want_web, reason = False, "Offline: answered from your data and libraries"
    yield {"type": "route", "route": "web" if want_web else ("local" if not weak else "general"), "reason": reason}
    if want_web:
        yield {"type": "status", "message": "Searching the live web with KeiroLabs..."}
        web, err = await _web_context(question)
        if err:
            yield {"type": "status", "message": err}

    yield {"type": "sources", "local": [h.to_dict() for h in hits], "web": [w.to_dict() for w in web],
           "library": [z.to_dict() for z in lib_hits], "private": [p.to_dict() for p in private_hits],
           "best_cosine": best, "web_used": bool(web)}

    rules = []
    if category:
        rules.append(safety.model_rule(category))
    if not (hits or web or lib_hits or private_hits):
        rules.append(NO_SOURCES_RULE)
    messages = build_messages(question, history or [], _format_sources(hits, web, lib_hits, private_hits), rules)
    async for event in _generate(messages, local_only=bool(private_hits)):
        yield event


async def _generate(messages: list[ChatMessage], *, max_tokens: int = 1500, local_only: bool = False) -> AsyncIterator[dict]:
    try:
        async for item in get_router().stream(messages, temperature=0.2, max_tokens=max_tokens, local_only=local_only):
            if isinstance(item, tuple):
                yield {"type": "meta", "model": item[1]}
            else:
                yield {"type": "token", "text": item}
    except AllModelsFailed as exc:
        yield {"type": "error", "message": str(exc)}
        return
    except Exception as exc:  # noqa: BLE001 - stream errors go to the client
        yield {"type": "error", "message": f"Generation failed: {exc}"}
        return
    yield {"type": "done"}


async def answer_once(question: str, kb_id: str | None = None, style: str = "", use_web: str = "never") -> dict:
    """Run answer_stream to completion (library answers, API)."""
    text, meta, sources, category, error = "", "", {}, None, None
    async for event in answer_stream(kb_id, question, None, None, use_web, True, False, style, use_all_kbs=False):
        if event["type"] == "token":
            text += event["text"]
        elif event["type"] == "meta":
            meta = event["model"]
        elif event["type"] == "sources":
            sources = {"library": event["library"], "local": event["local"], "web": event["web"]}
        elif event["type"] == "safety":
            category = event["category"]
        elif event["type"] == "error":
            error = event["message"]
    if error and not text:
        raise RuntimeError(error)
    return {"markdown": text.strip(), "model": meta, "sources": sources, "safety": category}
