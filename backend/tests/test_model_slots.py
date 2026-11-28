"""Hard tasks go to the model the user picked; the live catalogue says what each model is good for."""
import asyncio
import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient

from app import usage
from app.llm import catalog, registry, tasks
from app.main import app


@pytest.fixture(autouse=True)
def clean_slots(monkeypatch):
    for slot in tasks.SLOTS:
        monkeypatch.delenv(tasks.ENV[slot], raising=False)


# ---------- which slot ----------
@pytest.mark.parametrize("question,slot", [
    ("hi there", "everyday"),
    ("what is the capital of France", "everyday"),
    ("Compare the pros and cons of solar and wind for a small farm", "hard"),
    ("Analyse the quarterly results of the top Indian banks in detail", "hard"),
    ("write a python function that parses a csv file", "coding"),
    ("Fix this bug:\n```py\nprint(x\n```", "coding"),
    ("give me a market outlook report for Indian IT stocks", "research"),
])
def test_task_rules(question, slot):
    assert tasks.classify(question).slot == slot


def test_research_depth_and_user_choice_win():
    assert tasks.classify("hello", depth="deep").slot == "research"
    assert tasks.classify("hello", override="hard").slot == "hard"
    assert tasks.classify("compare a and b in detail", override="everyday").slot == "everyday"


def test_parse_keeps_ollama_tags():
    assert tasks.parse("ollama:qwen2.5:7b") == ("ollama", "qwen2.5:7b")
    assert tasks.parse("nonsense") is None


# ---------- router ----------
class FakeBackend:
    def __init__(self, name):
        self.name = name

    async def complete(self, model, messages, temperature=0.2, max_tokens=1024):
        return f"{self.name}:{model}"

    async def stream(self, model, messages, temperature=0.2, max_tokens=1024):
        yield f"{self.name}:{model}"


@pytest.fixture
def online(monkeypatch):
    from app.net import connectivity

    monkeypatch.setattr(type(connectivity), "is_online", property(lambda self: True))
    monkeypatch.setattr(usage, "cloud_allowed", lambda: True)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key-not-real")
    monkeypatch.setenv("LLM_MODE", "local-first")
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_a_chosen_hard_model_goes_first(online, monkeypatch):
    monkeypatch.setenv("MODEL_SLOT_HARD", "openai:gpt-test")
    router = registry.LLMRouter()
    first = router.attempts(task="hard")[0]
    assert (first.provider, first.model) == ("openai", "gpt-test")
    assert (router.attempts(task="everyday")[0].provider, router.attempts(task="everyday")[0].model) != ("openai", "gpt-test")


def test_empty_slots_change_nothing(online):
    router = registry.LLMRouter()
    assert [(a.provider, a.model) for a in router.attempts(task="hard")] == [(a.provider, a.model) for a in router.attempts()]


def test_private_notes_never_use_a_cloud_slot(online, monkeypatch):
    monkeypatch.setenv("MODEL_SLOT_HARD", "openai:gpt-test")
    router = registry.LLMRouter()
    assert all(a.provider in ("ollama", "llamacpp") for a in router.attempts(local_only=True, task="hard"))


def test_cloud_slot_is_skipped_offline_over_limit_or_in_local_mode(online, monkeypatch):
    monkeypatch.setenv("MODEL_SLOT_HARD", "openai:gpt-test")
    router = registry.LLMRouter()
    monkeypatch.setattr(usage, "cloud_allowed", lambda: False)
    assert router.slot_attempt("hard") is None
    monkeypatch.setattr(usage, "cloud_allowed", lambda: True)
    monkeypatch.setenv("LLM_MODE", "local")
    from app.config import get_settings

    get_settings.cache_clear()
    assert router.slot_attempt("hard") is None


def test_a_slot_without_its_key_is_skipped(online, monkeypatch):
    monkeypatch.setenv("MODEL_SLOT_HARD", "anthropic:claude-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert registry.LLMRouter().slot_attempt("hard") is None


def test_the_slot_model_answers_and_failure_falls_back(online, monkeypatch):
    monkeypatch.setenv("MODEL_SLOT_HARD", "openai:gpt-test")
    router = registry.LLMRouter()
    seen = []

    class Boom:
        async def complete(self, model, messages, **kw):
            seen.append(model)
            raise registry.LLMError("down")

    class Fine:
        async def complete(self, model, messages, **kw):
            seen.append(model)
            return "ok"

    attempts = router.attempts(task="hard")
    attempts[0].backend = Boom()
    for a in attempts[1:]:
        a.backend = Fine()
    monkeypatch.setattr(router, "attempts", lambda **kw: attempts)
    result = asyncio.run(router.complete([], task="hard"))
    assert result.text == "ok" and seen[0] == "gpt-test" and len(seen) >= 2


# ---------- catalogue ----------
MODELS_DEV = {
    "anthropic": {"models": {
        "claude-big": {"name": "Claude Big", "reasoning": True, "tool_call": True, "release_date": dt.date.today().isoformat(),
                       "modalities": {"input": ["text", "image"], "output": ["text"]}, "limit": {"context": 1000000, "output": 64000},
                       "cost": {"input": 8, "output": 40}},
        "claude-small": {"name": "Claude Small", "reasoning": False, "tool_call": True, "release_date": "2025-01-01",
                         "modalities": {"input": ["text"], "output": ["text"]}, "limit": {"context": 200000, "output": 8000},
                         "cost": {"input": 0.25, "output": 1.25}}}},
    "openai": {"models": {"gpt-coder": {"name": "GPT Coder", "reasoning": True, "tool_call": True, "release_date": "2026-09-01",
                                         "modalities": {"input": ["text"], "output": ["text"]}, "limit": {"context": 400000, "output": 32000},
                                         "cost": {"input": 1.5, "output": 6}}}},
    "unrelated": {"models": {"x": {"name": "X"}}},
}
OPENROUTER = {"data": [{"id": "vendor/free-vl", "name": "Free VL", "created": 1795705284, "context_length": 131072,
                        "architecture": {"input_modalities": ["text", "image"]}, "pricing": {"prompt": "0", "completion": "0"},
                        "supported_parameters": ["tools", "reasoning"]}]}


def test_models_dev_is_reduced_to_our_providers():
    rows = catalog.from_models_dev(MODELS_DEV)
    assert {r["provider"] for r in rows} == {"anthropic", "openai"}
    big = next(r for r in rows if r["model"] == "claude-big")
    assert big["vision"] and big["reasoning"] and big["context"] == 1000000 and big["price_out"] == 40


def test_openrouter_prices_become_per_million():
    row = catalog.from_openrouter(OPENROUTER)[0]
    assert row["id"] == "openrouter:vendor/free-vl" and row["price_in"] == 0 and row["vision"] and row["reasoning"]


def test_use_cases_come_from_the_facts():
    rows = {r["model"]: r for r in catalog.from_models_dev(MODELS_DEV)}
    big, small, coder = (catalog.use_cases(rows[k]) for k in ("claude-big", "claude-small", "gpt-coder"))
    assert "Hard reasoning" in big and "Very long documents" in big and "Reads images" in big
    assert "Cheap and fast" in small and "Hard reasoning" not in small
    assert "Coding" in coder
    assert "Free" in catalog.use_cases(catalog.from_openrouter(OPENROUTER)[0])


def test_suggestions_only_use_models_the_user_can_run():
    rows = catalog.from_models_dev(MODELS_DEV)
    for r in rows:
        r["usable"] = r["provider"] == "anthropic"
    best = catalog.suggest("hard", rows)
    assert best[0]["model"] == "claude-big" and all(r["provider"] == "anthropic" for r in best)
    assert catalog.suggest("coding", rows)[0]["provider"] == "anthropic"


def test_refresh_caches_and_falls_back_to_the_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(catalog, "_path", lambda: tmp_path / "models-catalog.json")
    calls = []

    class Resp:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            return self

        def json(self):
            return self.data

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            calls.append(url)
            return Resp(MODELS_DEV if "models.dev" in url else OPENROUTER)

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    data = asyncio.run(catalog.refresh(force=True))
    assert {m["provider"] for m in data["models"]} == {"anthropic", "openai", "openrouter"}
    assert json.loads((tmp_path / "models-catalog.json").read_text())["models"]
    asyncio.run(catalog.refresh())  # fresh: no new fetch
    assert len(calls) == 2

    class Offline(Client):
        async def get(self, url):
            raise OSError("offline")

    monkeypatch.setattr(httpx, "AsyncClient", Offline)
    again = asyncio.run(catalog.refresh(force=True))
    assert again["models"] == data["models"]  # the cached list keeps working


# ---------- API ----------
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_slot_api(client, monkeypatch):
    async def fake_entries():
        rows = catalog.from_models_dev(MODELS_DEV)
        for r in rows:
            r["use_cases"], r["usable"] = catalog.use_cases(r), True
        return {"fetched_at": 1, "error": "", "refreshing": False, "usable_providers": ["anthropic"], "models": rows}

    monkeypatch.setattr(catalog, "entries", fake_entries)
    monkeypatch.setattr(catalog, "usable_providers", lambda: {"anthropic"})
    out = client.get("/api/model-slots").json()
    assert [s["slot"] for s in out["slots"]] == list(tasks.SLOTS) and out["slots"][1]["suggestions"][0]["model"] == "claude-big"
    assert client.put("/api/model-slots/hard", json={"model": "anthropic:claude-big"}).json()["value"] == "anthropic:claude-big"
    assert tasks.slot_value("hard") == "anthropic:claude-big"
    assert client.put("/api/model-slots/hard", json={"model": "openai:gpt-coder"}).status_code == 400  # no key for it
    assert client.put("/api/model-slots/nope", json={"model": ""}).status_code == 404
    assert client.put("/api/model-slots/hard", json={"model": ""}).json()["value"] == ""
    assert client.post("/api/model-slots/classify", json={"model": "compare these two plans in detail"}).json()["slot"] == "hard"
