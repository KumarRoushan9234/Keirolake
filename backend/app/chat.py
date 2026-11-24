"""Saved conversations whose answers run in the background.

An answer is produced by a server task, not by the page that asked, so leaving
the page (or closing the window) does not stop it. Events are buffered while the
task runs; a page that comes back replays them from the start and then follows
live. The finished answer is stored with its sources.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import select

from app.catalog.models import Conversation, ConversationMessage, utcnow
from app.db import session_scope
from app.llm.base import ChatMessage
from app.rag.answer import answer_stream

log = logging.getLogger(__name__)
HISTORY_TURNS = 6
KEEP_FINISHED_SECONDS = 300
SAVE_EVERY_SECONDS = 2.0


@dataclass
class Live:
    events: list[dict] = field(default_factory=list)
    done: bool = False
    finished_at: float = 0.0
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None

    def push(self, event: dict) -> None:
        self.events.append(event)
        self.changed.set()


_live: dict[str, Live] = {}


def _conv_dict(c: Conversation) -> dict:
    return {"id": c.id, "title": c.title, "kb_id": c.kb_id, "created_at": c.created_at.isoformat(),
            "updated_at": c.updated_at.isoformat()}


def _msg_dict(m: ConversationMessage) -> dict:
    return {"id": m.id, "role": m.role, "content": m.content, "status": m.status, "meta": m.meta or {},
            "created_at": m.created_at.isoformat()}


def list_conversations(limit: int = 100) -> list[dict]:
    with session_scope() as s:
        rows = s.scalars(select(Conversation).order_by(Conversation.updated_at.desc()).limit(limit))
        return [_conv_dict(c) for c in rows]


def create(kb_id: str | None = None, title: str = "New chat") -> dict:
    with session_scope() as s:
        c = Conversation(title=title[:80] or "New chat", kb_id=kb_id)
        s.add(c)
        s.flush()
        return _conv_dict(c)


def get(conv_id: str) -> dict | None:
    with session_scope() as s:
        c = s.get(Conversation, conv_id)
        if c is None:
            return None
        return {**_conv_dict(c), "messages": [_msg_dict(m) for m in c.messages]}


def update(conv_id: str, *, title: str | None = None, kb_id: str | None | bool = False) -> dict | None:
    with session_scope() as s:
        c = s.get(Conversation, conv_id)
        if c is None:
            return None
        if title is not None:
            c.title = title.strip()[:80] or c.title
        if kb_id is not False:
            c.kb_id = kb_id or None
        return _conv_dict(c)


def delete(conv_id: str) -> bool:
    with session_scope() as s:
        c = s.get(Conversation, conv_id)
        if c is None:
            return False
        for m in c.messages:
            live = _live.pop(m.id, None)
            if live and live.task and not live.task.done():
                live.task.cancel()
        s.delete(c)
        return True


def _history(conv_id: str) -> list[ChatMessage]:
    with session_scope() as s:
        rows = list(s.scalars(select(ConversationMessage).where(
            ConversationMessage.conversation_id == conv_id, ConversationMessage.status == "done")
            .order_by(ConversationMessage.created_at.desc()).limit(HISTORY_TURNS)))
    return [ChatMessage(m.role, m.content) for m in reversed(rows) if m.content]


def ask(conv_id: str, question: str, *, web: str = "auto", library: bool = True, private: bool = False,
        depth: str = "answer") -> dict:
    """Store the question, start the answer in the background and return both message ids."""
    with session_scope() as s:
        c = s.get(Conversation, conv_id)
        if c is None:
            raise KeyError(conv_id)
        kb_id = c.kb_id
    history = _history(conv_id)  # read before the new rows exist
    with session_scope() as s:
        c = s.get(Conversation, conv_id)
        if c.title == "New chat":
            c.title = question.strip().splitlines()[0][:80]
        c.updated_at = utcnow()
        user = ConversationMessage(conversation_id=conv_id, role="user", content=question)
        s.add(user)
        s.flush()
        reply = ConversationMessage(conversation_id=conv_id, role="assistant", content="", status="running", meta={})
        s.add(reply)
        s.flush()
        user_id, reply_id = user.id, reply.id
    live = Live()
    _live[reply_id] = live
    live.task = asyncio.create_task(_run(reply_id, kb_id, question, history, web, library, private, live, depth))
    _prune()
    return {"user_id": user_id, "message_id": reply_id}


async def _run(msg_id: str, kb_id: str | None, question: str, history: list[ChatMessage], web: str, library: bool,
               private: bool, live: Live, depth: str = "answer") -> None:
    content, meta, status = "", {}, "done"
    last_save = time.monotonic()
    try:
        if depth in ("quick", "standard", "deep") and not private:
            from app.engine.research import research_stream

            stream = research_stream(question, depth=depth, kb_id=kb_id, history=history, use_library=library, allow_web=web != "never")
        else:
            stream = answer_stream(kb_id, question, history, None, web, library, private)
        async for event in stream:
            live.push(event)
            kind = event["type"]
            if kind == "token":
                content += event["text"]
            elif kind == "meta":
                meta["model"] = event["model"]
            elif kind == "sources":
                meta.update({k: event.get(k) or [] for k in ("local", "web", "library", "private", "research")})
            elif kind == "verify":
                meta["verify"] = {k: event[k] for k in ("checked", "supported", "unsupported")}
            elif kind == "safety":
                meta["safety"] = event["message"]
            elif kind == "route":
                meta["route"] = {"route": event["route"], "reason": event["reason"]}
            elif kind == "error":
                meta["error"], status = event["message"], "failed"
            if time.monotonic() - last_save > SAVE_EVERY_SECONDS:
                _save(msg_id, content, meta, "running")
                last_save = time.monotonic()
    except asyncio.CancelledError:
        status = "stopped"
        live.push({"type": "stopped"})
    except Exception as exc:  # noqa: BLE001 - shown in the chat
        log.exception("Answer failed")
        meta["error"], status = f"Answer failed: {exc}", "failed"
        live.push({"type": "error", "message": meta["error"]})
    finally:
        _save(msg_id, content, meta, status if not (status == "failed" and content) else "done")
        live.done, live.finished_at = True, time.monotonic()
        live.push({"type": "end", "status": status})


def _save(msg_id: str, content: str, meta: dict, status: str) -> None:
    with session_scope() as s:
        m = s.get(ConversationMessage, msg_id)
        if m is not None:
            m.content, m.meta, m.status = content, dict(meta), status


def stop(msg_id: str) -> bool:
    live = _live.get(msg_id)
    if live and live.task and not live.task.done():
        live.task.cancel()
        return True
    return False


def running() -> list[str]:
    return [mid for mid, live in _live.items() if not live.done]


def _prune() -> None:
    now = time.monotonic()
    for mid in [m for m, live in _live.items() if live.done and now - live.finished_at > KEEP_FINISHED_SECONDS]:
        _live.pop(mid, None)


async def follow(msg_id: str, after: int = 0):
    """Yield buffered then live events for an answer; a finished answer that left the buffer yields one 'final' event."""
    live = _live.get(msg_id)
    if live is None:
        with session_scope() as s:
            m = s.get(ConversationMessage, msg_id)
            if m is None:
                return
            yield {"type": "final", "message": _msg_dict(m)}
        return
    i = after
    while True:
        while i < len(live.events):
            yield live.events[i]
            i += 1
        if live.done:
            return
        live.changed.clear()
        if i < len(live.events):
            continue
        try:
            await asyncio.wait_for(live.changed.wait(), 15)
        except asyncio.TimeoutError:
            yield {"type": "ping"}


def mark_interrupted() -> int:
    """At start: answers that were running when the engine stopped keep their partial text."""
    with session_scope() as s:
        rows = list(s.scalars(select(ConversationMessage).where(ConversationMessage.status == "running")))
        for m in rows:
            m.status = "interrupted"
        return len(rows)
