"""Decide what a message needs before searching anything.

Greetings and small talk get a direct reply (no search). Questions about recent
or changing facts go to the web. Everything else is answered from your data and
libraries; the web is only added when those come up empty AND the model agrees
the question needs outside facts.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from app.llm.base import ChatMessage

log = logging.getLogger(__name__)
DECIDE_TIMEOUT = 8.0

_CHAT = re.compile(
    r"^\s*(hi+|hey+|hello+|hola|namaste|yo|sup|good (morning|afternoon|evening|night)|thanks?( you)?|thank u|thx|ty|"
    r"ok(ay)?|cool|nice|great|awesome|bye|goodbye|see you|how are you( doing)?|how's it going|what'?s up|"
    r"who are you|what are you|what can you do|help|test(ing)?)\b[\s!.?,]*(there|again|keirolake|buddy)?[\s!.?]*$",
    re.I)
_FRESH = re.compile(
    r"\b(latest|today|tonight|yesterday|tomorrow|this (week|month|year)|right now|currently|current|news|breaking|"
    r"live|price of|stock price|weather|forecast|score|election results?|recent(ly)?|just (released|announced)|"
    r"search (the )?(web|internet|online)|look (it )?up online|google|on the web|20(2[4-9]|3\d))\b", re.I)


@dataclass
class Intent:
    kind: str  # chat | fresh | question
    reason: str


def classify(question: str) -> Intent:
    q = question.strip()
    if _CHAT.match(q) or (len(q) <= 3 and not q.isdigit()):
        return Intent("chat", "Small talk: answered directly, nothing searched")
    if _FRESH.search(q):
        return Intent("fresh", "Asks about recent or changing facts: checking the live web")
    return Intent("question", "Searching your data and libraries")


async def needs_web(question: str) -> tuple[bool, str]:
    """Ask the model (local first) whether a question that found nothing locally needs the web."""
    from app.llm.registry import get_router

    prompt = [
        ChatMessage("system", "Reply with exactly one word: WEB or KNOWN."),
        ChatMessage("user", "Nothing about this was found in the user's own files or offline libraries.\n"
                            "Reply WEB if a good answer needs specific facts from the internet (names, numbers, events, products, "
                            "places, documentation). Reply KNOWN if general knowledge, reasoning, writing or maths is enough.\n\n"
                            f"Message: {question[:600]}"),
    ]
    try:
        result = await asyncio.wait_for(get_router().complete(prompt, temperature=0, max_tokens=4), DECIDE_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - undecided means: try the web, as before
        log.info("Web decision skipped: %s", exc)
        return True, "Nothing found in your data: checking the live web"
    if "WEB" in result.text.upper():
        return True, "Nothing found in your data: checking the live web"
    return False, "Nothing found in your data; answered from the model's general knowledge"
