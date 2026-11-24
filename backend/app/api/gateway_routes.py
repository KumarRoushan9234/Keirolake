"""An OpenAI-compatible gateway: point any OpenAI SDK or tool at KeiroLake and it becomes a data layer over the models.

    POST /v1/chat/completions   chat, streaming or not, through local models first (cloud only within the daily limit)
    POST /v1/embeddings         vectors from the local-first embedding chain
    GET  /v1/models             what can be asked for

Model names:
    auto                   the app's own choice (local first)
    <name>                 a specific model, e.g. qwen2.5:7b (local) or a configured cloud model
    provider:<name>        the same, pinned to a provider, e.g. ollama:qwen2.5:7b
    lake / lake:<id>       auto, grounded in the lake: passages from every collection the key may read, or from one
                           collection or topic; the sources come back in choices[0].message.sources

Grounding can also be asked for with the header X-KeiroLake-Ground: <collection or topic id>.
Every call needs a key with the "llm" scope, is rate limited and logged like the rest of /v1.
"""
from __future__ import annotations

import json
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app import usage
from app.api.public_routes import allowed_kbs, caller, need
from app.config import LOCAL_PROVIDERS
from app.llm.base import ChatMessage, LLMError
from app.llm.registry import AllModelsFailed, Attempt, _backend_for, get_router

router = APIRouter(prefix="/v1", tags=["LLM gateway"])
MAX_CONTEXT_CHARS = 9000
GROUND_PROMPT = ("Use the passages below from the user's own data when they help. Cite them as [1], [2]. "
                 "If they don't contain the answer, say so plainly rather than guessing.\n\n")


class Message(BaseModel):
    role: str = Field(pattern="^(system|user|assistant|tool|developer)$")
    content: str | list | None = ""


class ChatBody(BaseModel):
    model: str = Field("auto", max_length=200)
    messages: list[Message] = Field(min_length=1, max_length=200)
    stream: bool = False
    temperature: float = Field(0.2, ge=0, le=2)
    max_tokens: int | None = Field(None, ge=1, le=32768)
    max_completion_tokens: int | None = Field(None, ge=1, le=32768)


def _text(content) -> str:
    """OpenAI content may be a string or a list of parts; only text parts are used."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") in ("text", "input_text"))
    return ""


def _attempts(model: str) -> list[Attempt]:
    """Which backends to try for a model name. A named model is tried on its own, so callers get what they asked for."""
    r = get_router()
    if model in ("auto", "lake") or model.startswith("lake:"):
        return r.attempts()
    head, sep, rest = model.partition(":")
    provider, name = (head, rest) if sep and head in (_providers() | set(LOCAL_PROVIDERS)) else ("", model)
    found = [a for a in r.attempts() if a.model == name and (not provider or a.provider == provider)]
    if found:
        return found
    if provider in ("", *LOCAL_PROVIDERS):  # any installed local model, even one the router doesn't list
        local = provider or "ollama"
        backend = _backend_for(local)
        if backend is not None:
            return [Attempt(local, name, backend)]
    if provider and not usage.cloud_allowed():
        raise HTTPException(429, "Today's cloud limit is used up; ask for a local model or 'auto'")
    raise HTTPException(404, f"Model '{model}' isn't available. GET /v1/models lists what is.")


def _providers() -> set[str]:
    return {row["provider"] for row in get_router().describe()}


async def _ground(key: dict, model: str, request: Request, question: str) -> tuple[str, list[dict]]:
    """Passages from the lake for the last user message, and their sources."""
    from app.rag.retrieve import retrieve

    target = request.headers.get("x-keirolake-ground", "").strip() or (model.split(":", 1)[1] if model.startswith("lake:") else "")
    if not target and model != "lake":
        return "", []
    need(key, "read")
    kbs = {k["id"]: k for k in allowed_kbs(key)}
    if target:
        from app import topics

        try:
            target = topics.get(target)["kb_id"] or target  # a topic id means its collection
        except KeyError:
            pass
        if target not in kbs:
            raise HTTPException(404, "That collection or topic isn't available to this key")
        ids = [target]
    else:
        ids = list(kbs)
    hits = []
    for kb_id in ids[:8]:
        hits += [(kb_id, h) for h in (await retrieve(kb_id, question, 4)).hits]
    hits.sort(key=lambda kh: (kh[1].cosine or 0, kh[1].score), reverse=True)
    context, sources, used = [], [], 0
    for kb_id, h in hits[:6]:
        piece = h.text[:1800]
        if used + len(piece) > MAX_CONTEXT_CHARS:
            break
        used += len(piece)
        n = len(sources) + 1
        context.append(f"[{n}] {h.title}\n{piece}")
        sources.append({"n": n, "title": h.title, "url": h.url, "collection": kbs[kb_id]["name"]})
    return (GROUND_PROMPT + "\n\n".join(context)) if context else "", sources


def _messages(body: ChatBody, grounding: str) -> list[ChatMessage]:
    msgs = [ChatMessage("system" if m.role in ("system", "developer") else "assistant" if m.role == "assistant" else "user", _text(m.content))
            for m in body.messages if m.role != "tool"]
    if grounding:
        msgs.insert(0, ChatMessage("system", grounding))
    return msgs


def _usage(prompt: list[ChatMessage], answer: str) -> dict:
    p = sum(len(m.content) for m in prompt) // 4
    c = len(answer) // 4
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


@router.get("/models")
async def models(key: dict = Depends(caller)) -> dict:
    need(key, "llm")
    from app.local import ollama

    now = int(time.time())
    out = [{"id": "auto", "object": "model", "created": now, "owned_by": "keirolake"},
           {"id": "lake", "object": "model", "created": now, "owned_by": "keirolake"}]
    status = await ollama.status()
    out += [{"id": m["name"], "object": "model", "created": now, "owned_by": "ollama"} for m in status["models"] if m.get("local")]
    for row in get_router().describe():
        if row["provider"] not in LOCAL_PROVIDERS and row["configured"]:
            out += [{"id": f"{row['provider']}:{m}", "object": "model", "created": now, "owned_by": row["provider"]} for m in row["models"]]
    out += [{"id": f"lake:{k['id']}", "object": "model", "created": now, "owned_by": "keirolake", "name": k["name"]} for k in allowed_kbs(key)] if "read" in key["scopes"] else []
    return {"object": "list", "data": out}


@router.post("/chat/completions")
async def chat_completions(body: ChatBody, request: Request, key: dict = Depends(caller)):
    need(key, "llm")
    question = next((_text(m.content) for m in reversed(body.messages) if m.role == "user"), "")
    grounding, sources = await _ground(key, body.model, request, question)
    msgs = _messages(body, grounding)
    attempts = _attempts(body.model)
    max_tokens = body.max_completion_tokens or body.max_tokens or 1024
    cid, created = f"chatcmpl-{uuid.uuid4().hex[:24]}", int(time.time())

    if not body.stream:
        errors = []
        for a in attempts:
            try:
                get_router()._count(a)
                text = await a.backend.complete(a.model, msgs, temperature=body.temperature, max_tokens=max_tokens)
                message = {"role": "assistant", "content": text, **({"sources": sources} if sources else {})}
                return {"id": cid, "object": "chat.completion", "created": created, "model": f"{a.provider}:{a.model}",
                        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}], "usage": _usage(msgs, text)}
            except LLMError as exc:
                errors.append(f"{a.provider}:{a.model} -> {exc}")
        raise HTTPException(502, "No model answered: " + "; ".join(errors)[:600])

    async def events():
        for a in attempts:
            started = False
            try:
                get_router()._count(a)
                async for delta in a.backend.stream(a.model, msgs, temperature=body.temperature, max_tokens=max_tokens):
                    if not started:
                        started = True
                        first = {"role": "assistant", "content": "", **({"sources": sources} if sources else {})}
                        yield _chunk(cid, created, a, first)
                    yield _chunk(cid, created, a, {"content": delta})
                yield _chunk(cid, created, a, {}, "stop")
                yield "data: [DONE]\n\n"
                return
            except LLMError as exc:
                if started:  # part of an answer was already sent: say so instead of mixing two models
                    yield f"data: {json.dumps({'error': {'message': str(exc)[:300]}})}\n\n"
                    return
        yield f"data: {json.dumps({'error': {'message': 'No model answered'}})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


def _chunk(cid: str, created: int, a: Attempt, delta: dict, finish: str | None = None) -> str:
    body = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": f"{a.provider}:{a.model}",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(body)}\n\n"


class EmbedBody(BaseModel):
    input: str | list[str] = Field(...)
    model: str = Field("auto", max_length=200)


@router.post("/embeddings")
async def embeddings(body: EmbedBody, key: dict = Depends(caller)) -> dict:
    need(key, "llm")
    from app.embeddings.chain import get_chain

    texts = [body.input] if isinstance(body.input, str) else body.input
    if not texts or len(texts) > 256 or any(len(t) > 32000 for t in texts):
        raise HTTPException(400, "Send 1 to 256 texts of up to 32,000 characters")
    chain = get_chain()
    allowed = None if body.model in ("auto", "") else {s.vector_name for s in chain.specs if body.model in (s.model, s.vector_name, f"{s.provider}:{s.model}")}
    if allowed is not None and not allowed:
        raise HTTPException(404, f"Embedding model '{body.model}' isn't configured")
    try:
        result = await chain.embed(texts, kind="passage", allowed=allowed)
    except (LLMError, AllModelsFailed, RuntimeError) as exc:
        raise HTTPException(502, f"No embedding model answered: {exc}") from exc
    return {"object": "list", "model": f"{result.spec.provider}:{result.spec.model}",
            "data": [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(result.vectors)],
            "usage": {"prompt_tokens": sum(len(t) for t in texts) // 4, "total_tokens": sum(len(t) for t in texts) // 4}}
