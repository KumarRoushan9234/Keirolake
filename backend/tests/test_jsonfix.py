"""Lenient JSON reading, and the pipeline errors it fixes."""
import json

import pytest

from app import pipelines
from app.keiro.client import WebResult
from app.llm.jsonfix import loads_lenient
from app.llm.registry import LLMResult


@pytest.mark.parametrize("text,expected", [
    ('{"a": 1, "b": "x"}', {"a": 1, "b": "x"}),
    ('Sure! Here you go:\n```json\n{"a": 1,}\n```\nHope that helps', {"a": 1}),
    ("{'company': 'Ather', 'price': 99}", {"company": "Ather", "price": 99}),
    ('{"ok": True, "none": None, "n": 3}', {"ok": True, "none": None, "n": 3}),
    ('{"a": 1, // note\n "b": 2}', {"a": 1, "b": 2}),
    ('{"company": "Acme", "price": 10, "launch": "2026-0', {"company": "Acme", "price": 10}),   # cut off mid-value
    ('{"rows": [{"a": 1}, {"a": 2', {"rows": [{"a": 1}, {"a": 2}]}),
    ('text {"url": "https://x.org/a//b", "n": 1} text', {"url": "https://x.org/a//b", "n": 1}),
])
def test_forgives_the_usual_model_slips(text, expected):
    assert loads_lenient(text) == expected


def test_hopeless_text_is_none_and_arrays_work():
    assert loads_lenient("I can't help with that") is None and loads_lenient("") is None
    assert loads_lenient('```\n[{"a": 1}, {"a": 2},]\n```', list) == [{"a": 1}, {"a": 2}]


class Ctx:
    async def progress(self, *a):
        pass


def _spec():
    return pipelines.validate_spec({
        "name": "slips", "goal": "x", "steps": [{"type": "search", "query": "q", "mode": "content", "max_results": 3}],
        "fields": [{"name": "company", "type": "text"}, {"name": "price", "type": "number"}],
        "outputs": {"table": True, "knowledge_base": False}, "interval_minutes": 0})


@pytest.mark.asyncio
async def test_extraction_survives_sloppy_json_and_retries_garbage():
    replies = ["{'company': 'Ather', 'price': '99,000',}", "totally not json", '{"company": "Ola", "price": 90000}']
    calls = []

    async def complete(messages, **kw):
        calls.append(messages[-1].content[:30])
        return LLMResult(replies.pop(0), "ollama", "t", [])

    pages = [WebResult("https://a.example.org/1", "A", content="Ather launches a scooter. " * 20),
             WebResult("https://b.example.org/2", "B", content="Ola launches a scooter. " * 20)]
    rows, errors = await pipelines.extract_rows(_spec(), pages, complete)
    assert {r["company"] for r in rows} == {"Ather", "Ola"} and next(r for r in rows if r["company"] == "Ather")["price"] == 99000.0
    assert errors == [] and len(calls) == 3  # one page needed a second try


@pytest.mark.asyncio
async def test_unreadable_pages_are_reported_clearly_and_grouped():
    async def complete(messages, **kw):
        return LLMResult("no json here at all", "ollama", "t", [])

    pages = [WebResult(f"https://s{i}.example.org/p", "T", content="Some long page text. " * 20) for i in range(4)]
    rows, errors = await pipelines.extract_rows(_spec(), pages, complete)
    assert rows == [] and len(errors) == 4
    assert all("planner" not in e for e in errors)  # the old message blamed the planner
    assert pipelines.summarize_problems(errors)[0].startswith("4 pages: the model's answer couldn't be read")


@pytest.mark.asyncio
async def test_placeholder_sites_are_skipped_with_a_plain_reason():
    class Keiro:
        configured = True

        async def sitemap(self, *a, **k):
            raise AssertionError("must not call KeiroLabs for example.com")

        async def search(self, q, n=5, mode="lite"):
            return [WebResult("https://real.example.org/1", "R", content="text " * 100)]

        async def extract(self, urls):
            return []

    spec = _spec()
    spec["steps"] = [{"type": "sitemap", "site": "example.com", "path_prefix": "", "limit": 5},
                     {"type": "search", "query": "q", "mode": "content", "max_results": 3}]
    pages, warnings = await pipelines.collect(spec, Keiro())
    assert len(pages) == 1 and any("example.com" in w and "placeholder" in w for w in warnings)
