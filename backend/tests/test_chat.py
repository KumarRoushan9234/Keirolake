"""Intent routing (no web for small talk) and background conversations."""
import asyncio

import pytest

from app.rag.intent import classify

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("text", ["hello", "Hi!", "hey there", "thanks", "good morning", "who are you?", "ok"])
async def test_small_talk_is_chat(text):
    assert classify(text).kind == "chat"


@pytest.mark.parametrize("text", ["latest news on qdrant", "weather today in Delhi", "search the web for ollama releases"])
async def test_recent_facts_go_to_web(text):
    assert classify(text).kind == "fresh"


@pytest.mark.parametrize("text", ["hello world program in rust", "how do I purify water", "help me plan a budget"])
async def test_real_questions_are_questions(text):
    assert classify(text).kind == "question"


class Router:
    def __init__(self, decide="KNOWN", delay=0.0):
        self.decide, self.delay, self.prompts = decide, delay, []

    async def stream(self, messages, **kwargs):
        self.prompts.append(messages)
        yield ("meta", "ollama:test")
        for word in ["Hello", " there"]:
            await asyncio.sleep(self.delay)
            yield word

    async def complete(self, messages, **kwargs):
        from app.llm.registry import LLMResult

        return LLMResult(self.decide, "ollama", "test", [])


async def test_hello_never_searches(monkeypatch):
    from app.rag import answer

    called = []
    monkeypatch.setattr(answer, "_web_context", lambda *a, **k: called.append(1))
    monkeypatch.setattr(answer, "get_router", lambda: Router())
    events = [e async for e in answer.answer_stream(None, "hello", use_web="auto")]
    route = next(e for e in events if e["type"] == "route")
    assert route["route"] == "chat" and not called
    assert "".join(e["text"] for e in events if e["type"] == "token") == "Hello there"


async def test_weak_question_asks_model_before_web(monkeypatch):
    from app.llm import registry
    from app.net import connectivity
    from app.rag import answer

    called = []

    async def web(q, n=3):
        called.append(q)
        return [], None

    router = Router(decide="KNOWN")
    monkeypatch.setattr(answer, "_web_context", web)
    monkeypatch.setattr(answer, "get_router", lambda: router)
    monkeypatch.setattr(registry, "get_router", lambda: router)
    monkeypatch.setattr(type(connectivity), "is_online", property(lambda self: True))
    events = [e async for e in answer.answer_stream(None, "write a haiku about rivers", use_web="auto", use_library=False)]
    assert next(e for e in events if e["type"] == "route")["route"] == "general" and not called
    router.decide = "WEB"
    events = [e async for e in answer.answer_stream(None, "what does the qdrant 1.20 changelog say", use_web="auto", use_library=False)]
    assert next(e for e in events if e["type"] == "route")["route"] == "web" and called


async def test_conversation_answer_survives_leaving_the_page(monkeypatch):
    from app import chat
    from app.rag import answer

    monkeypatch.setattr(answer, "get_router", lambda: Router(delay=0.05))
    conv = chat.create()
    ids = chat.ask(conv["id"], "hello")
    # A page that "comes back" later replays everything from the start.
    await asyncio.sleep(0.02)
    events = [e async for e in chat.follow(ids["message_id"])]
    assert events[-1] == {"type": "end", "status": "done"}
    saved = chat.get(conv["id"])
    assert saved["title"] == "hello"
    assert [m["role"] for m in saved["messages"]] == ["user", "assistant"]
    assert saved["messages"][1]["content"] == "Hello there" and saved["messages"][1]["status"] == "done"
    assert saved["messages"][1]["meta"]["route"]["route"] == "chat"
    # Follow-ups get the earlier turns as history.
    ids2 = chat.ask(conv["id"], "thanks")
    _ = [e async for e in chat.follow(ids2["message_id"])]
    assert len(chat.get(conv["id"])["messages"]) == 4


async def test_stop_keeps_partial_answer(monkeypatch):
    from app import chat
    from app.rag import answer

    monkeypatch.setattr(answer, "get_router", lambda: Router(delay=0.3))
    conv = chat.create()
    ids = chat.ask(conv["id"], "hi")
    await asyncio.sleep(0.4)
    assert chat.stop(ids["message_id"])
    _ = [e async for e in chat.follow(ids["message_id"])]
    msg = chat.get(conv["id"])["messages"][1]
    assert msg["status"] == "stopped" and msg["content"].startswith("Hello")
    chat.delete(conv["id"])
    assert chat.get(conv["id"]) is None
