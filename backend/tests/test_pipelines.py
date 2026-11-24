"""KeiroLabs-only data pipelines: validation, planning, collection, extraction, storage, running."""
import inspect
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import pipelines
from app.keiro.client import KeiroError, WebResult
from app.llm.registry import LLMResult


def good_spec(**over):
    spec = {
        "name": "EV News", "goal": "Track EV announcements",
        "steps": [{"type": "search", "query": "electric vehicle launch", "mode": "content", "max_results": 4}],
        "fields": [{"name": "company", "type": "text", "description": "Company name"},
                   {"name": "price_usd", "type": "number", "description": "Price in USD"},
                   {"name": "launch_date", "type": "date"}],
        "outputs": {"table": True, "knowledge_base": False}, "interval_minutes": 0,
    }
    spec.update(over)
    return spec


# ---------- the KeiroLabs-only guarantee ----------
def test_pipeline_module_cannot_fetch_the_web_itself():
    source = inspect.getsource(pipelines)
    assert not re.search(r"^\s*(import|from)\s+(httpx|requests|urllib\.request|aiohttp|trafilatura|selenium|playwright)", source, re.M)
    assert "get_keiro" in source


@pytest.mark.parametrize("bad_type", ["http_get", "scrape", "browser", "shell", "sql", ""])
def test_any_non_keirolabs_step_is_rejected(bad_type):
    with pytest.raises(pipelines.SpecError) as err:
        pipelines.validate_spec(good_spec(steps=[{"type": bad_type, "url": "https://x.example"}]))
    assert "only KeiroLabs steps are allowed" in str(err.value)


# ---------- validation ----------
def test_valid_spec_is_normalised():
    spec = pipelines.validate_spec(good_spec())
    assert spec["name"] == "ev_news" and spec["source_policy"] == "keirolabs-only"
    assert [f["name"] for f in spec["fields"]] == ["company", "price_usd", "launch_date"]
    assert spec["steps"][0]["max_results"] == 4 and spec["outputs"] == {"table": True, "knowledge_base": False}


@pytest.mark.parametrize("change, message", [
    ({"name": ""}, "name is required"),
    ({"steps": []}, "at least one step"),
    ({"steps": [{"type": "search", "query": "", "mode": "content"}]}, "needs a query"),
    ({"steps": [{"type": "search", "query": "x", "mode": "turbo"}]}, "mode must be"),
    ({"steps": [{"type": "extract", "urls": ["file:///etc/passwd"]}]}, "not web addresses"),
    ({"steps": [{"type": "extract", "urls": []}]}, "needs urls"),
    ({"steps": [{"type": "sitemap", "site": ""}]}, "needs a site"),
    ({"steps": [{"type": "research", "question": ""}]}, "needs a question"),
    ({"fields": [{"name": "source_url", "type": "text"}]}, "unique name"),
    ({"fields": [{"name": "a", "type": "text"}, {"name": "a", "type": "text"}]}, "unique name"),
    ({"fields": [{"name": "a", "type": "blob"}]}, "type must be"),
    ({"fields": [], "outputs": {"table": True}}, "needs at least one field"),
    ({"outputs": {"table": False, "knowledge_base": False}}, "at least one output"),
    ({"interval_minutes": 5}, "interval_minutes"),
])
def test_invalid_specs_explain_themselves(change, message):
    with pytest.raises(pipelines.SpecError, match=message):
        pipelines.validate_spec(good_spec(**change))


def test_limits_are_enforced_not_trusted():
    steps = [{"type": "search", "query": f"q{i}", "mode": "lite", "max_results": 500} for i in range(8)]
    with pytest.raises(pipelines.SpecError, match="at most 6 steps"):
        pipelines.validate_spec(good_spec(steps=steps))
    spec = pipelines.validate_spec(good_spec(steps=[{"type": "search", "query": "q", "mode": "lite", "max_results": 500},
                                                    {"type": "sitemap", "site": "qdrant.tech", "limit": 9999}]))
    assert spec["steps"][0]["max_results"] == 10 and spec["steps"][1]["limit"] == 25


@pytest.mark.parametrize("url", ["https://example.com/article1", "http://www.example.org/x", "https://localhost/a", "http://192.168.1.4/x", "https://intranet"])
def test_placeholder_and_local_addresses_are_refused(url):
    with pytest.raises(pipelines.SpecError, match="not web addresses"):
        pipelines.validate_spec(good_spec(steps=[{"type": "extract", "urls": [url]}]))


def test_planner_is_told_not_to_invent_urls():
    assert "Never invent or guess URLs" in pipelines.PLANNER_PROMPT


def test_planned_extract_urls_must_come_from_the_goal():
    spec = pipelines.validate_spec(good_spec(steps=[{"type": "extract", "urls": ["https://www.notion.so/plans"]}]))
    with pytest.raises(pipelines.SpecError, match="didn't give"):
        pipelines._check_plan(spec, "Read the pricing pages of notion.so")
    ok = pipelines.validate_spec(good_spec(steps=[{"type": "extract", "urls": ["https://www.notion.so/pricing"]}]))
    assert pipelines._check_plan(ok, "Read https://www.notion.so/pricing and record the plans") is ok


async def test_plan_repairs_guessed_urls(monkeypatch):
    from app.llm import registry

    guessed = good_spec(steps=[{"type": "extract", "urls": ["https://www.notion.so/plans"]}])
    fixed = good_spec(steps=[{"type": "sitemap", "site": "notion.so", "path_prefix": "/pricing", "limit": 3}])
    router = ScriptedRouter(json.dumps(guessed), json.dumps(fixed))
    monkeypatch.setattr(registry, "get_router", lambda: router)
    out = await pipelines.plan("Record the plan prices on notion.so")
    assert out["spec"]["steps"][0]["type"] == "sitemap" and "didn't give" in router.calls[1][-1].content


def test_estimate_counts_calls():
    spec = pipelines.validate_spec(good_spec(steps=[{"type": "search", "query": "q", "mode": "fast"},
                                                    {"type": "research", "question": "why?"}]))
    est = pipelines.estimate(spec)
    assert est["keiro_calls"] == {"search": 1, "extract": 1, "sitemap": 0, "research": 1} and est["total_calls"] == 3


# ---------- planning ----------
class ScriptedRouter:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    async def complete(self, messages, **kw):
        self.calls.append(messages)
        return LLMResult(self.replies.pop(0), "ollama", "test", [])


async def test_plan_returns_a_validated_spec(monkeypatch):
    from app.llm import registry

    reply = "Here you go:\n```json\n" + json.dumps(good_spec()) + "\n```"
    router = ScriptedRouter(reply)
    monkeypatch.setattr(registry, "get_router", lambda: router)
    out = await pipelines.plan("Track new electric vehicle announcements every day")
    assert out["spec"]["name"] == "ev_news" and out["estimate"]["total_calls"] == 1
    assert "ONLY these KeiroLabs steps" in router.calls[0][0].content


async def test_plan_repairs_an_invalid_first_answer_once(monkeypatch):
    from app.llm import registry

    broken = good_spec(steps=[{"type": "scrape", "url": "https://x.example"}])
    router = ScriptedRouter(json.dumps(broken), json.dumps(good_spec()))
    monkeypatch.setattr(registry, "get_router", lambda: router)
    out = await pipelines.plan("Collect product pages from a shop site")
    assert out["spec"]["steps"][0]["type"] == "search"
    assert "only KeiroLabs steps are allowed" in router.calls[1][-1].content  # the repair prompt names the problem

    router = ScriptedRouter(json.dumps(broken), json.dumps(broken))
    monkeypatch.setattr(registry, "get_router", lambda: router)
    with pytest.raises(pipelines.SpecError):
        await pipelines.plan("Collect product pages from a shop site")
    with pytest.raises(pipelines.SpecError, match="Describe"):
        await pipelines.plan("hi")


# ---------- collection ----------
class FakeKeiro:
    configured = True

    def __init__(self):
        self.calls = []

        class S:
            credits_remaining = 100.0

        self.state = S()

    async def search(self, query, max_results=5, mode="lite"):
        self.calls.append(("search", mode))
        return [WebResult("https://a.example/1", "A1", content="A" * 300), WebResult("https://a.example/2", "A2", snippet="short")]

    async def extract(self, urls):
        self.calls.append(("extract", tuple(urls)))
        return [WebResult(u, f"X {u[-1]}", content="Full text of the page. " * 30) for u in urls]

    async def sitemap(self, url, path_prefix="", since="", limit=50):
        self.calls.append(("sitemap", url))
        return [{"url": f"https://s.example/p{i}"} for i in range(3)]

    async def deep(self, question, content=True):
        self.calls.append(("deep", question))

        class D:
            results = [WebResult("https://d.example/1", "D1", content="Deep content. " * 30)]

        return D()


async def test_collect_runs_only_keirolabs_calls_and_dedupes_and_fills_thin_pages():
    keiro = FakeKeiro()
    spec = pipelines.validate_spec(good_spec(steps=[
        {"type": "search", "query": "q", "mode": "fast"}, {"type": "search", "query": "q again", "mode": "fast"},
        {"type": "sitemap", "site": "s.example", "limit": 3}, {"type": "research", "question": "why"}]))
    pages, warnings = await pipelines.collect(spec, keiro)
    urls = [p.url for p in pages]
    assert len(urls) == len(set(urls)) and "https://s.example/p0" in urls and "https://d.example/1" in urls
    assert {c[0] for c in keiro.calls} <= {"search", "extract", "sitemap", "deep"}
    thin = next(p for p in pages if p.url == "https://a.example/2")
    assert len(thin.content) > 200  # the short snippet was replaced by an extracted page
    assert warnings == []


async def test_collect_reports_step_failures_and_missing_key():
    class Failing(FakeKeiro):
        async def search(self, *a, **k):
            raise KeiroError("out of credits")

    spec = pipelines.validate_spec(good_spec(steps=[{"type": "search", "query": "q", "mode": "content"},
                                                    {"type": "extract", "urls": ["https://ok.example/1"]}]))
    pages, warnings = await pipelines.collect(spec, Failing())
    assert [p.url for p in pages] == ["https://ok.example/1"] and warnings == ["search: out of credits"]

    class NoKey(FakeKeiro):
        configured = False

    with pytest.raises(KeiroError, match="isn't configured"):
        await pipelines.collect(spec, NoKey())


# ---------- extraction and storage ----------
async def test_extract_rows_coerces_types_and_drops_empty_and_failed_pages():
    spec = pipelines.validate_spec(good_spec())
    pages = [WebResult("https://x/1", "One", content="Acme launches the Roadster at $42,500 on 2026-03-01. " * 3),
             WebResult("https://x/2", "Two", content="Nothing relevant here at all, just filler words. " * 3),
             WebResult("https://x/3", "Three", content="Broken model output page content words. " * 3),
             WebResult("https://x/4", "Four", content="tiny")]
    answers = iter([json.dumps({"company": "Acme", "price_usd": "42,500", "launch_date": "2026-03-01"}),
                    json.dumps({"company": None, "price_usd": "", "launch_date": None}),
                    "not json at all"])

    async def complete(messages, **kw):
        return LLMResult(next(answers), "ollama", "t", [])

    rows, errors = await pipelines.extract_rows(spec, pages, complete)
    assert len(rows) == 1 and rows[0]["company"] == "Acme" and rows[0]["price_usd"] == 42500.0
    assert rows[0]["source_url"] == "https://x/1" and rows[0]["collected_at"]
    assert len(errors) == 1 and "https://x/3" in errors[0]


def test_write_rows_upserts_by_url_and_adds_new_columns(tmp_path):
    spec = pipelines.validate_spec(good_spec(name="upsert_test"))
    table = "ds_upsert_test"
    row = lambda url, company, price: {"source_url": url, "title": "t", "collected_at": "2026-01-01", "company": company, "price_usd": price, "launch_date": None}  # noqa: E731
    assert pipelines.write_rows(table, spec, [row("u1", "Acme", 1.0), row("u2", "Bolt", 2.0)]) == 2
    assert pipelines.write_rows(table, spec, [row("u1", "Acme Motors", 3.0)]) == 2  # updated, not duplicated
    from app.lake import warehouse

    assert warehouse.run_query(f'SELECT company, price_usd FROM "{table}" ORDER BY source_url')["rows"] == [["Acme Motors", 3.0], ["Bolt", 2.0]]
    wider = pipelines.validate_spec(good_spec(name="upsert_test", fields=spec["fields"] + [{"name": "country", "type": "text"}]))
    pipelines.write_rows(table, wider, [{**row("u3", "Cyan", 4.0), "country": "NL"}])
    cols = [c["name"] for c in next(t for t in warehouse.schema() if t["table"] == table)["columns"]]
    assert "country" in cols


def test_save_pipeline_rules():
    from app.catalog.models import KnowledgeBase
    from app.db import session_scope

    with pytest.raises(pipelines.SpecError, match="Pick the knowledge base"):
        pipelines.save_pipeline(good_spec(name="needs_kb", outputs={"table": True, "knowledge_base": True}))
    with session_scope() as s:
        kb = KnowledgeBase(name="pipekb", slug="pipekb", collection_alias="kb_pk", collection_name="kb_pk_v1")
        s.add(kb)
        s.flush()
        kb_id = kb.id
    saved = pipelines.save_pipeline(good_spec(name="with_kb", outputs={"table": True, "knowledge_base": True}), kb_id)
    assert saved["kb_id"] == kb_id and saved["table"] == "ds_with_kb"
    with pytest.raises(pipelines.SpecError, match="already exists"):
        pipelines.save_pipeline(good_spec(name="with_kb"))
    renamed = pipelines.save_pipeline(good_spec(name="with_kb", interval_minutes=60), None, saved["id"])
    assert renamed["interval_minutes"] == 60 and renamed["kb_id"] is None  # kb output was switched off
    pipelines.delete_pipeline(saved["id"], drop_table=True)
    assert pipelines.get_pipeline(saved["id"]) is None


# ---------- running ----------
class Ctx:
    async def progress(self, *_a):
        pass


async def test_run_job_dry_run_writes_nothing_and_real_run_fills_table(monkeypatch):
    from app.lake import warehouse

    keiro = FakeKeiro()
    monkeypatch.setattr(pipelines, "get_keiro", lambda: keiro)

    async def complete(messages, **kw):
        return LLMResult(json.dumps({"company": "Acme", "price_usd": 10, "launch_date": "2026-01-01"}), "ollama", "t", [])

    from app.llm import registry

    fake_router = type("R", (), {"complete": staticmethod(complete)})()
    monkeypatch.setattr(registry, "get_router", lambda: fake_router)
    saved = pipelines.save_pipeline(good_spec(name="runner_test"))

    dry = await pipelines.run_job(saved["id"], dry=True)(Ctx())
    assert dry["dry_run"] is True and dry["rows_found"] >= 1 and dry["sample"][0]["company"] == "Acme"
    assert not any(t["table"] == "ds_runner_test" for t in warehouse.schema())  # nothing written

    real = await pipelines.run_job(saved["id"])(Ctx())
    assert real["table"] == "ds_runner_test" and real["table_rows"] == real["rows_found"] >= 1
    assert pipelines.get_pipeline(saved["id"])["last_result"]["table_rows"] == real["table_rows"]
    raw_files = list((Path(__import__("app.config", fromlist=["x"]).get_settings().lake_dir) / "raw" / "pipeline").rglob("keirolabs_pages.json"))
    assert raw_files  # the untouched Keiro response is kept in the lake


async def test_scheduler_picks_due_pipelines_once(monkeypatch):
    from datetime import datetime, timedelta, timezone

    saved = pipelines.save_pipeline(good_spec(name="sched_test", interval_minutes=60))
    now = datetime.now(timezone.utc)
    assert saved["id"] in pipelines.due_pipelines(now)  # never run: due
    assert saved["id"] not in pipelines.due_pipelines(now)  # claimed
    assert saved["id"] in pipelines.due_pipelines(now + timedelta(minutes=61))
    manual = pipelines.save_pipeline(good_spec(name="manual_only", interval_minutes=0))
    assert manual["id"] not in pipelines.due_pipelines(now + timedelta(days=30))


# ---------- API ----------
def test_pipeline_api(monkeypatch):
    from app.main import app

    with TestClient(app) as client:
        bad = client.post("/api/pipelines/validate", json={"spec": good_spec(steps=[{"type": "scrape"}])})
        assert bad.status_code == 400 and "only KeiroLabs" in bad.json()["detail"]["message"]
        ok = client.post("/api/pipelines/validate", json={"spec": good_spec()}).json()
        assert ok["spec"]["name"] == "ev_news" and ok["estimate"]["total_calls"] == 1
        created = client.post("/api/pipelines", json={"spec": good_spec(name="api_made")})
        assert created.status_code == 201
        pid = created.json()["id"]
        assert client.post("/api/pipelines", json={"spec": good_spec(name="api_made")}).status_code == 400
        assert client.patch(f"/api/pipelines/{pid}", json={"enabled": False}).json()["enabled"] is False
        assert client.get(f"/api/pipelines/{pid}/rows").json()["row_count"] == 0
        assert client.post(f"/api/pipelines/{pid}/run").status_code == 202
        assert client.post("/api/pipelines/nope/run").status_code == 404
        assert client.delete(f"/api/pipelines/{pid}").status_code == 204


# ---------- intelligent planner ----------
async def test_clarify_asks_at_most_three_questions_with_options(monkeypatch):
    from app.llm import registry

    questions = [{"question": f"Q{i}?", "options": ["a", "b", "c", "d", "e"]} for i in range(5)]
    router = ScriptedRouter(json.dumps({"ready": False, "questions": questions}))
    monkeypatch.setattr(registry, "get_router", lambda: router)
    out = await pipelines.clarify("Collect prices of electric cars")
    assert not out["ready"] and len(out["questions"]) == 3 and len(out["questions"][0]["options"]) == 4

    router = ScriptedRouter("not json at all")
    monkeypatch.setattr(registry, "get_router", lambda: router)
    clear = "Every week, collect from qdrant.tech the title, date and summary of each post"
    assert (await pipelines.clarify(clear))["ready"] is True
    router = ScriptedRouter("{}")
    monkeypatch.setattr(registry, "get_router", lambda: router)
    vague = await pipelines.clarify("Collect prices of electric scooters")  # the model asked nothing; the gaps still are
    assert [q["question"] for q in vague["questions"]] == ["Where should it look?", "What details should each row have?", "How often should it run?"]


async def test_plan_uses_answers_and_returns_a_summary(monkeypatch):
    from app.llm import registry

    router = ScriptedRouter(json.dumps({**good_spec(), "summary": "Searches EV news daily and records prices."}))
    monkeypatch.setattr(registry, "get_router", lambda: router)
    out = await pipelines.plan("Track electric vehicle launches", [{"question": "Which region?", "answer": "India"}])
    assert out["summary"].startswith("Searches EV news")
    assert "Which region?" in router.calls[0][-1].content and "India" in out["spec"]["instructions"]


async def test_thin_run_is_replanned_and_reviewed(monkeypatch):
    keiro = FakeKeiro()

    async def empty_search(query, max_results=5, mode="lite"):
        keiro.calls.append(("search", query))
        return [] if query == "nothing here" else [WebResult("https://b.example/1", "B1", content="B" * 400)]

    keiro.search = empty_search
    monkeypatch.setattr(pipelines, "get_keiro", lambda: keiro)
    replies = [json.dumps({"steps": [{"type": "search", "query": "better query", "mode": "content"}], "why": "broader"})]

    async def complete(messages, **kw):
        system = messages[0].content
        if "collected too little" in system:
            return LLMResult(replies.pop(0), "ollama", "t", [])
        if "review the result" in system:
            return LLMResult("- Found 1 row", "ollama", "t", [])
        return LLMResult(json.dumps({"company": "Acme", "price_usd": 1, "launch_date": "2026-01-01"}), "ollama", "t", [])

    from app.llm import registry

    monkeypatch.setattr(registry, "get_router", lambda: type("R", (), {"complete": staticmethod(complete)})())
    saved = pipelines.save_pipeline(good_spec(name="replan_test", steps=[{"type": "search", "query": "nothing here", "mode": "content"}]))
    out = await pipelines.run_job(saved["id"], dry=True)(Ctx())
    assert ("search", "better query") in keiro.calls and out["pages"] == 1 and out["replanned"]["why"] == "broader" and out["review"] == "- Found 1 row"
