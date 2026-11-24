"""Saved conversations: list, open, ask (answer runs in the background), follow, stop."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app import chat

router = APIRouter(prefix="/api/conversations")


class NewConversation(BaseModel):
    kb_id: str | None = None
    title: str = "New chat"


class ConversationPatch(BaseModel):
    title: str | None = None
    kb_id: str | None = None
    set_kb: bool = False


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    web: str = Field("auto", pattern="^(auto|always|never)$")
    library: bool = True
    private: bool = False
    depth: str = Field("answer", pattern="^(answer|quick|standard|deep)$")


@router.get("")
async def list_all() -> list[dict]:
    return chat.list_conversations()


@router.post("", status_code=201)
async def create(body: NewConversation) -> dict:
    return chat.create(body.kb_id, body.title)


@router.get("/running")
async def running() -> list[str]:
    return chat.running()


@router.get("/{conv_id}")
async def get(conv_id: str) -> dict:
    conv = chat.get(conv_id)
    if conv is None:
        raise HTTPException(404, "Conversation not found")
    return conv


@router.patch("/{conv_id}")
async def patch(conv_id: str, body: ConversationPatch) -> dict:
    conv = chat.update(conv_id, title=body.title, kb_id=body.kb_id if body.set_kb else False)
    if conv is None:
        raise HTTPException(404, "Conversation not found")
    return conv


@router.delete("/{conv_id}", status_code=204)
async def delete(conv_id: str) -> None:
    if not chat.delete(conv_id):
        raise HTTPException(404, "Conversation not found")


@router.post("/{conv_id}/ask", status_code=202)
async def ask(conv_id: str, body: AskBody) -> dict:
    try:
        return chat.ask(conv_id, body.question, web=body.web, library=body.library, private=body.private, depth=body.depth)
    except KeyError:
        raise HTTPException(404, "Conversation not found") from None


@router.get("/messages/{msg_id}/events")
async def events(msg_id: str, after: int = 0):
    async def stream():
        async for event in chat.follow(msg_id, after):
            yield {"event": event["type"], "data": json.dumps(event)}

    return EventSourceResponse(stream())


@router.post("/messages/{msg_id}/stop")
async def stop(msg_id: str) -> dict:
    return {"stopped": chat.stop(msg_id)}


@router.post("/messages/{msg_id}/to-sheet", status_code=201)
async def message_to_sheet(msg_id: str) -> dict:
    """Put the sources behind an answer into a sheet: one row per source, columns chosen for the question."""
    from sqlalchemy import select

    from app.catalog.models import Conversation, ConversationMessage
    from app.db import session_scope
    from app.keiro.client import WebResult
    from app.sheets import from_sources

    with session_scope() as s:
        msg = s.get(ConversationMessage, msg_id)
        if msg is None or msg.role != "assistant":
            raise HTTPException(404, "Answer not found")
        meta = msg.meta or {}
        asked = s.scalar(select(ConversationMessage).where(
            ConversationMessage.conversation_id == msg.conversation_id, ConversationMessage.role == "user",
            ConversationMessage.created_at <= msg.created_at).order_by(ConversationMessage.created_at.desc()))
        question = asked.content if asked else "What do these sources say?"
        title = s.get(Conversation, msg.conversation_id).title
    urls = [r["url"] for r in (meta.get("research") or []) if r.get("url")] + [w["url"] for w in (meta.get("web") or []) if w.get("url")]
    pages = from_sources.pages_from_evidence(urls)
    pages += [WebResult(h.get("url") or "", h.get("title", ""), content=h.get("text", "")) for h in (meta.get("local") or [])[:8]]
    pages += [WebResult("", w.get("title", ""), content=w.get("content") or w.get("snippet", "")) for w in (meta.get("web") or []) if not w.get("url")]
    if not pages:
        raise HTTPException(400, "This answer has no sources to read")
    try:
        return await from_sources.build_sheet(question, pages, title)
    except from_sources.FromSourcesError as exc:
        raise HTTPException(400, str(exc)) from exc
