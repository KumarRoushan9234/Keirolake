"""Plain-language help for the offline libraries: a short cited answer plus the articles to open,
and gathering source text about a topic from the libraries and every collection."""
from __future__ import annotations

import logging

from app.library import service as library

log = logging.getLogger(__name__)
SOURCE_CHARS = 9000


async def gather_source(topic: str) -> tuple[str, str]:
    """Text about a topic from the offline libraries and every knowledge base."""
    from app.rag.retrieve import retrieve_all

    parts: list[str] = []
    titles: list[str] = []
    try:
        hits, _ = await library.retrieve(topic, limit=6)
        for h in hits:
            parts.append(f"## {h.title}\n{h.text}")
            titles.append(h.title)
    except Exception as exc:  # noqa: BLE001 - libraries are optional
        log.info("Library search for cards failed: %s", exc)
    try:
        kb_hits, _ = await retrieve_all(topic, 6)
        for h in kb_hits:
            parts.append(f"## {h.title}\n{h.text}")
            titles.append(h.title)
    except Exception as exc:  # noqa: BLE001 - knowledge bases are optional
        log.info("Knowledge base search for cards failed: %s", exc)
    title = topic.title() + (f" ({', '.join(dict.fromkeys(titles))[:120]})" if titles else "")
    return title[:200], "\n\n".join(parts)[:SOURCE_CHARS]


async def find_in_libraries(question: str) -> dict:
    """A short cited answer from the offline libraries, plus matching articles to open."""
    from app.rag.answer import answer_once

    if not library.ready_libraries():
        return {"answer": "", "articles": [], "message": "No offline library is ready yet."}
    hits, _ = await library.retrieve(question, limit=8)
    articles, seen = [], set()
    for h in hits:
        key = (h.library_id, h.path)
        if key not in seen:
            seen.add(key)
            articles.append({"library_id": h.library_id, "library": h.library, "path": h.path, "title": h.title,
                             "snippet": h.text[:220]})
    try:
        out = await answer_once(question, use_web="never")
        answer, model = out["markdown"], out["model"]
    except Exception as exc:  # noqa: BLE001 - the article list is still useful
        log.info("Library answer skipped: %s", exc)
        answer, model = "", ""
    return {"answer": answer, "model": model, "articles": articles}
