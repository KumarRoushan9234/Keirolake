"""Turn the latest message (and the chat so far) into short, standalone search queries.

A follow-up such as "no pull the full report fo these top performing indian stocks" names nothing on its own, and
search engines do badly with long, chatty text. The model rewrites it into one or two short queries ("top gaining NSE
stocks last 30 days"); if that is slow or fails, a rule-based rewrite still drops filler words and borrows the topic
from the previous question.
"""
from __future__ import annotations

import asyncio
import logging
import re

from app.llm.base import ChatMessage

log = logging.getLogger(__name__)
TIMEOUT = 8.0
MAX_WORDS = 10

PROMPT = """You turn a chat message into web search queries.
Reply with ONLY JSON: {"queries": ["...", "..."]}
- One or two queries, each under 10 words, that stand on their own (replace "these", "it", "them" with what they mean
  from the conversation).
- Fix spelling. Keep names, tickers, places, dates and time ranges ("last 30 days", "Q2 2026").
- No filler ("please", "pull", "create a report"). Today is {today}."""

_FILLER = re.compile(
    r"\b(no|yes|ok(ay)?|please|pls|can you|could you|i want|i need|give me|show me|tell me|pull|get|fetch|find|"
    r"create|make|write|generate|prepare|full|detailed|complete|proper|a|an|the|for me|me|report|reports|of|fo|on|"
    r"about|and|also|now|just|then)\b", re.I)
_PRONOUN = re.compile(r"\b(these|those|them|they|it|its|that|this|same|above|previous)\b", re.I)
_WORDS = re.compile(r"[\w.&-]+", re.U)


def _squash(text: str) -> str:
    return " ".join(text.split())


def rule_queries(question: str, history: list[ChatMessage] | None = None) -> list[str]:
    """No model needed: strip filler, and borrow the previous question's topic for a follow-up."""
    core = _squash(_FILLER.sub(" ", question))
    core = _squash(_PRONOUN.sub(" ", core)) if _PRONOUN.search(core) else core
    earlier = next((m.content for m in reversed(history or []) if m.role == "user" and m.content.strip() != question.strip()), "")
    if earlier and (_PRONOUN.search(question) or len(core.split()) < 3):
        topic = _squash(_FILLER.sub(" ", earlier))
        merged, seen = [], set()
        for w in (core + " " + topic).split():
            if w.lower() not in seen:
                seen.add(w.lower())
                merged.append(w)
        core = " ".join(merged)
    words = _WORDS.findall(core) or _WORDS.findall(question)
    return [" ".join(words[:MAX_WORDS])] if words else [question.strip()[:120]]


def _clean(queries: object) -> list[str]:
    out = []
    for q in queries if isinstance(queries, list) else []:
        words = _WORDS.findall(str(q))
        if words:
            out.append(" ".join(words[:MAX_WORDS + 2]))
    return out[:2]


async def search_queries(question: str, history: list[ChatMessage] | None = None) -> list[str]:
    """One or two short standalone queries for this message. Never raises."""
    words = question.split()
    if not history and len(words) <= MAX_WORDS and not _PRONOUN.search(question):
        return [_squash(question)]
    import datetime as dt

    from app.llm.jsonfix import loads_lenient
    from app.llm.registry import get_router

    recent = [m for m in (history or []) if m.role in ("user", "assistant")][-4:]
    convo = "\n".join(f"{m.role}: {m.content[:400]}" for m in recent)
    try:
        out = await asyncio.wait_for(get_router().complete(
            [ChatMessage("system", PROMPT.replace("{today}", dt.date.today().isoformat())),
             ChatMessage("user", f"CONVERSATION:\n{convo or '(none)'}\n\nMESSAGE: {question}")],
            temperature=0, max_tokens=120), TIMEOUT)
        queries = _clean((loads_lenient(out.text) or {}).get("queries"))
        if queries:
            return queries
    except Exception as exc:  # noqa: BLE001 - the rule-based rewrite is the fallback
        log.info("Query rewrite fell back to rules: %s", exc)
    return rule_queries(question, history)
