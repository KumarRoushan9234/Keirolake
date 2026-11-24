"""Data pipelines that collect only through KeiroLabs.

A pipeline is a small, validated spec:

    steps    KeiroLabs calls: search / extract / sitemap / research (nothing else is allowed)
    fields   what to pull out of each page (a model reads the page text and fills them in)
    outputs  a warehouse table (ds_<name>) and/or a knowledge base

`plan()` turns a plain-English goal into a spec; `validate_spec()` is the only
gate between a spec (from the planner or a person) and execution; `run_job()`
executes it. Every web fetch goes through the KeiroLabs client.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import select

from app.catalog.models import Dataset, KnowledgeBase, Pipeline
from app.db import session_scope, writer_connection
from app.ingest.pipeline import IncomingDoc, finish_run, start_run
from app.keiro.client import KeiroError, WebResult, get_keiro
from app.lake.warehouse import table_columns
from app.lake.zones import raw_run_dir, relative, slugify, write_raw_json
from app.llm.base import ChatMessage

log = logging.getLogger(__name__)

STEP_TYPES = ("search", "extract", "sitemap", "research")
FIELD_TYPES = {"text": "TEXT", "number": "REAL", "date": "TEXT", "boolean": "INTEGER", "url": "TEXT"}
SEARCH_MODES = ("lite", "fast", "content")
MAX_STEPS, MAX_FIELDS, MAX_PAGES, MAX_PAGE_CHARS = 6, 12, 30, 6000
RESERVED_COLUMNS = {"source_url", "title", "collected_at"}
MIN_INTERVAL = 15
MIN_USEFUL_PAGES = 2  # fewer after a run triggers one re-plan of the collection steps


class SpecError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


PLACEHOLDER_HOSTS = re.compile(r"(^|\.)(example\.(com|org|net)|localhost|test|invalid|local)$|^(127\.|10\.|192\.168\.|0\.)")


def _http_url(url: str) -> bool:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower()
    return parsed.scheme in ("http", "https") and bool(host) and "." in host and not PLACEHOLDER_HOSTS.search(host)


def validate_spec(raw: dict) -> dict:
    """A clean spec, or SpecError listing every problem. Anything that isn't a KeiroLabs step is rejected."""
    problems: list[str] = []
    if not isinstance(raw, dict):
        raise SpecError(["The pipeline must be a JSON object"])
    name = slugify(str(raw.get("name", "")), 40)
    if not str(raw.get("name", "")).strip():
        problems.append("name is required")

    steps_in = raw.get("steps")
    steps: list[dict] = []
    if not isinstance(steps_in, list) or not steps_in:
        problems.append("at least one step is required")
    else:
        if len(steps_in) > MAX_STEPS:
            problems.append(f"at most {MAX_STEPS} steps")
        for i, s in enumerate(steps_in[:MAX_STEPS], 1):
            kind = s.get("type") if isinstance(s, dict) else None
            if kind not in STEP_TYPES:
                problems.append(f"step {i}: type must be one of {', '.join(STEP_TYPES)} (only KeiroLabs steps are allowed), got {kind!r}")
                continue
            if kind == "search":
                query, mode = str(s.get("query", "")).strip(), s.get("mode", "content")
                if not query:
                    problems.append(f"step {i}: search needs a query")
                if mode not in SEARCH_MODES:
                    problems.append(f"step {i}: mode must be one of {', '.join(SEARCH_MODES)}")
                steps.append({"type": "search", "query": query[:300], "mode": mode,
                              "max_results": max(1, min(int(s.get("max_results", 5) or 5), 10))})
            elif kind == "extract":
                urls = [str(u).strip() for u in s.get("urls", []) if str(u).strip()] if isinstance(s.get("urls"), list) else []
                bad = [u for u in urls if not _http_url(u)]
                if not urls:
                    problems.append(f"step {i}: extract needs urls")
                if bad:
                    problems.append(f"step {i}: not web addresses: {', '.join(bad[:3])}")
                steps.append({"type": "extract", "urls": urls[:20]})
            elif kind == "sitemap":
                site = str(s.get("site", "")).strip()
                if not site or not _http_url(site):
                    problems.append(f"step {i}: sitemap needs a site such as example.com")
                steps.append({"type": "sitemap", "site": site, "path_prefix": str(s.get("path_prefix", ""))[:200],
                              "limit": max(1, min(int(s.get("limit", 10) or 10), 25))})
            else:
                question = str(s.get("question", "")).strip()
                if not question:
                    problems.append(f"step {i}: research needs a question")
                steps.append({"type": "research", "question": question[:400]})

    fields: list[dict] = []
    seen: set[str] = set()
    for i, f in enumerate((raw.get("fields") or [])[:MAX_FIELDS], 1):
        fname = slugify(str(f.get("name", "")), 40) if isinstance(f, dict) else ""
        ftype = f.get("type", "text") if isinstance(f, dict) else "text"
        if not fname or fname in RESERVED_COLUMNS or fname in seen:
            problems.append(f"field {i}: needs a unique name that isn't {', '.join(sorted(RESERVED_COLUMNS))}")
            continue
        if ftype not in FIELD_TYPES:
            problems.append(f"field {fname}: type must be one of {', '.join(FIELD_TYPES)}")
            continue
        seen.add(fname)
        fields.append({"name": fname, "type": ftype, "description": str(f.get("description", ""))[:200]})
    if len(raw.get("fields") or []) > MAX_FIELDS:
        problems.append(f"at most {MAX_FIELDS} fields")

    out = raw.get("outputs") or {}
    to_table, to_kb = bool(out.get("table", bool(fields))), bool(out.get("knowledge_base", False))
    if to_table and not fields:
        problems.append("a table output needs at least one field")
    if not to_table and not to_kb:
        problems.append("choose at least one output: a table or a knowledge base")

    interval = int(raw.get("interval_minutes", 0) or 0)
    if interval and not MIN_INTERVAL <= interval <= 10080:
        problems.append(f"interval_minutes must be 0 (manual) or between {MIN_INTERVAL} and 10080")

    if problems:
        raise SpecError(problems)
    return {"name": name, "goal": str(raw.get("goal", ""))[:1000], "steps": steps, "fields": fields,
            "outputs": {"table": to_table, "knowledge_base": to_kb}, "interval_minutes": interval,
            "instructions": str(raw.get("instructions", ""))[:600], "source_policy": "keirolabs-only"}


def estimate(spec: dict) -> dict:
    """How many KeiroLabs calls one run makes. Credit prices vary by plan, so only the counts are stated."""
    calls = {"search": 0, "extract": 0, "sitemap": 0, "research": 0}
    for s in spec["steps"]:
        calls[s["type"]] += 1
    needs_pages = bool(spec["fields"]) or spec["outputs"]["knowledge_base"]
    if needs_pages and any(s["type"] == "search" and s["mode"] != "content" for s in spec["steps"]):
        calls["extract"] += 1  # snippets aren't enough to extract from; the top pages are fetched
    if any(s["type"] == "sitemap" for s in spec["steps"]):
        calls["extract"] += 1
    return {"keiro_calls": calls, "total_calls": sum(calls.values()),
            "note": "Research calls cost 2 credits each; other calls cost fractions of a credit."}


# ---------- planning ----------
PLANNER_PROMPT = """You design data pipelines for KeiroLake. Collection may use ONLY these KeiroLabs steps:
- search: {"type":"search","query":"...","mode":"content|fast|lite","max_results":1-10}  (content returns full page text)
- extract: {"type":"extract","urls":["https://..."]}  (read specific pages)
- sitemap: {"type":"sitemap","site":"example.com","path_prefix":"/blog","limit":1-25}  (list a site's newest pages; they are then read)
- research: {"type":"research","question":"..."}  (deep research across sources; slow, 2 credits)
Never use any other way to fetch data. A model then reads each page and fills "fields".
Return ONLY one JSON object (no prose, no code fence) with these keys:
{"name":"short_snake_case","goal":"...","steps":[...],"fields":[{"name":"snake_case","type":"text|number|date|boolean|url","description":"what to extract"}],
 "outputs":{"table":true,"knowledge_base":false},"interval_minutes":0 or 15-10080,"instructions":"extra extraction guidance",
 "summary":"one or two plain sentences telling the user what this pipeline will do and why these steps"}
Never invent or guess URLs or paths. extract may only use full URLs that appear word for word in the goal. For a site the user names without a full URL, use sitemap (optionally with path_prefix) or a search such as "notion pricing plans".
Rules: prefer the fewest steps; use "content" mode when fields are needed; choose 3-8 useful fields;
set outputs.knowledge_base true when the user wants to ask questions over the pages; interval_minutes 0 unless they want it repeated."""


def _json_object(text: str) -> dict:
    """The planner's JSON, forgiving small-model slips (trailing commas, single quotes, a cut-off ending)."""
    from app.llm.jsonfix import loads_lenient

    data = loads_lenient(text)
    if data is None:
        raise SpecError(["The model's answer wasn't a pipeline plan I could read"])
    return data


class UnreadableAnswer(ValueError):
    """The model answered, but not with JSON that could be recovered."""


def summarize_problems(problems: list[str], limit: int = 6) -> list[str]:
    """Group repeated problems ("6 pages: ...") so a run's warnings stay short and readable."""
    unreadable = [p for p in problems if "couldn't be read" in p or "wasn't JSON" in p]
    others = [p for p in problems if p not in unreadable]
    out = []
    if unreadable:
        out.append(f"{len(unreadable)} page{'s' if len(unreadable) != 1 else ''}: the model's answer couldn't be read "
                   f"(e.g. {unreadable[0].split(': ', 1)[0]})")
    return (out + others)[:limit]


def _check_plan(spec: dict, goal: str) -> dict:
    """Planner output only: an extract step may use only URLs the user wrote. Guessed pages usually don't exist."""
    said = goal.lower()
    for step in spec["steps"]:
        if step["type"] == "extract":
            guessed = [u for u in step["urls"] if u.lower().rstrip("/") not in said and u.lower().split("://", 1)[-1].rstrip("/") not in said]
            if guessed:
                raise SpecError([f"extract uses URLs the user didn't give ({', '.join(guessed[:3])}); use sitemap or search for those sites instead"])
    return spec


CLARIFY_PROMPT = """You help a user plan a web data pipeline. Decide if their goal says enough to plan well:
what to collect, from which kind of sources or sites, which details (columns) they want, any time range or region, and how often.
Return ONLY one JSON object: {"ready": true or false, "questions": [{"question": "...", "options": ["...", "..."]}]}
Ask at most 3 short questions, only about things that would change the plan. Give each 2-4 short suggested answers.
If the goal already says enough, return {"ready": true, "questions": []}."""

MAX_QUESTIONS = 3


def _context(goal: str, answers: list[dict] | None) -> str:
    lines = [f"Goal: {goal}"]
    for a in answers or []:
        q, ans = str(a.get("question", "")).strip()[:300], str(a.get("answer", "")).strip()[:300]
        if q and ans:
            lines.append(f"Q: {q}\nA: {ans}")
    return "\n".join(lines)


_SOURCE_HINT = re.compile(r"https?://|\b[\w-]+\.(com|org|net|io|gov|in|co|so|tech|edu|info)\b|\b(site|website|blog|news|articles?|posts?|"
                          r"official|government|reddit|wikipedia|papers?|journals?|sources?)\b", re.I)
_FIELDS_HINT = re.compile(r"\b(for each|columns?|fields?|with (their|the)|including|record|such as|like)\b|:|,.*,", re.I)
_REPEAT_HINT = re.compile(r"\b(once|one[- ]off|daily|every|each (day|week|month|hour)|weekly|monthly|hourly|keep (it )?updated|track)\b", re.I)


def _gaps(goal: str) -> list[dict]:
    """Questions for what a goal plainly leaves out, so a small local model can't skip them."""
    out = []
    if not _SOURCE_HINT.search(goal):
        out.append({"question": "Where should it look?",
                    "options": ["Any reliable websites", "News articles", "Official or government sources", "Specific sites (type them)"]})
    if not _FIELDS_HINT.search(goal) and not re.search(r"knowledge base|ask questions", goal, re.I):
        out.append({"question": "What details should each row have?",
                    "options": ["Name, price and link", "Title, date and summary", "Let the planner choose"]})
    if not _REPEAT_HINT.search(goal):
        out.append({"question": "How often should it run?", "options": ["Just once", "Daily", "Weekly"]})
    return out


async def clarify(goal: str) -> dict:
    """Questions worth asking before planning: the model's own, plus any basic detail the goal leaves out."""
    from app.llm.registry import get_router

    goal = goal.strip()
    if len(goal) < 10:
        raise SpecError(["Describe what you want to collect in a sentence or two"])
    reply = await get_router().complete([ChatMessage("system", CLARIFY_PROMPT), ChatMessage("user", f"Goal: {goal}")],
                                        temperature=0.2, max_tokens=600)
    try:
        asked = _json_object(reply.text).get("questions") or []
    except SpecError:
        asked = []
    questions: list[dict] = []
    seen: set[str] = set()
    for q in [*asked, *_gaps(goal)]:
        if len(questions) >= MAX_QUESTIONS or not isinstance(q, dict) or not str(q.get("question", "")).strip():
            continue
        text = str(q["question"]).strip()[:200]
        topic = next((w for w in ("where", "detail", "column", "field", "often", "frequen", "site", "source") if w in text.lower()), text)
        if topic in seen:
            continue
        seen.add(topic)
        options = [str(o).strip()[:80] for o in (q.get("options") or []) if str(o).strip()][:4]
        questions.append({"id": f"q{len(questions) + 1}", "question": text, "options": options})
    return {"ready": not questions, "questions": questions, "model": f"{reply.provider}:{reply.model}"}


async def plan(goal: str, answers: list[dict] | None = None) -> dict:
    """Turn a goal (and answers to the clarifying questions) into a validated spec. One repair round."""
    from app.llm.registry import get_router

    goal = goal.strip()
    if len(goal) < 10:
        raise SpecError(["Describe what you want to collect in a sentence or two"])
    context = _context(goal, answers)
    router = get_router()
    messages = [ChatMessage("system", PLANNER_PROMPT), ChatMessage("user", context)]
    reply = await router.complete(messages, temperature=0.1, max_tokens=1800)

    def read(text: str) -> tuple[dict, str]:
        raw = _json_object(text)
        summary = str(raw.pop("summary", "") or "")[:500]
        return _check_plan(validate_spec(raw), context), summary

    try:
        spec, summary = read(reply.text)
    except SpecError as first:
        messages += [ChatMessage("assistant", reply.text),
                     ChatMessage("user", "That pipeline is invalid: " + "; ".join(first.problems) + ". Return the corrected JSON only.")]
        reply = await router.complete(messages, temperature=0.0, max_tokens=1800)
        spec, summary = read(reply.text)
    spec["goal"] = spec["goal"] or goal
    told = "; ".join(f"{a.get('question')}: {a.get('answer')}" for a in answers or [] if a.get("answer"))
    if told:
        spec["instructions"] = f"{spec.get('instructions', '')} {told}".strip()[:600]
    return {"spec": spec, "estimate": estimate(spec), "model": f"{reply.provider}:{reply.model}", "summary": summary}


REPAIR_PROMPT = """A data pipeline run collected too little. Propose better KeiroLabs steps for the same goal.
Use the same step formats as before (search, extract, sitemap, research). Never invent URLs: extract may only use URLs
listed below as already found or written in the goal. Return ONLY JSON: {"steps": [...], "why": "one short sentence"}"""

REVIEW_PROMPT = """You review the result of a data pipeline run for the user. In at most 4 short bullet points (Markdown):
what was found, what is missing or looks wrong, and one concrete suggestion for the next run. No preamble."""


async def repair_steps(spec: dict, pages: list[WebResult], warnings: list[str]) -> tuple[list[dict], str] | None:
    """Ask the model for replacement steps after a thin run. None if it can't produce valid, different ones."""
    from app.llm.registry import get_router

    found = "\n".join(f"- {p.title or ''} {p.url} ({len(p.content or '')} chars)" for p in pages[:10]) or "(nothing)"
    ask = (f"Goal: {spec['goal']}\nFields wanted: {', '.join(f['name'] for f in spec['fields']) or 'none'}\n"
           f"Steps used: {json.dumps(spec['steps'])}\nProblems: {'; '.join(warnings[:5]) or 'too few useful pages'}\nFound:\n{found}")
    try:
        reply = await get_router().complete([ChatMessage("system", REPAIR_PROMPT), ChatMessage("user", ask)], temperature=0.2, max_tokens=900)
        raw = _json_object(reply.text)
        fixed = _check_plan(validate_spec({**spec, "steps": raw.get("steps")}), spec["goal"] + " " + " ".join(p.url for p in pages))
    except Exception as exc:  # noqa: BLE001 - repair is best effort
        log.info("Pipeline repair skipped: %s", exc)
        return None
    if fixed["steps"] == spec["steps"]:
        return None
    return fixed["steps"], str(raw.get("why", ""))[:200]


async def review(spec: dict, rows: list[dict], pages: list[WebResult], warnings: list[str]) -> str:
    """A short model-written review of a run: found, missing, next step. Empty if no model answers."""
    from app.llm.registry import get_router

    sample = json.dumps(rows[:8], default=str)[:3000]
    ask = (f"Goal: {spec['goal']}\nPages read: {len(pages)}\nRows found: {len(rows)}\nWarnings: {'; '.join(warnings[:5]) or 'none'}\n"
           f"Sample rows: {sample}")
    try:
        reply = await get_router().complete([ChatMessage("system", REVIEW_PROMPT), ChatMessage("user", ask)], temperature=0.2, max_tokens=350)
        return reply.text.strip()[:1500]
    except Exception as exc:  # noqa: BLE001 - review is best effort
        log.info("Pipeline review skipped: %s", exc)
        return ""


def _thin(spec: dict, pages: list[WebResult], rows: list[dict]) -> bool:
    useful = [p for p in pages if len((p.content or "").strip()) >= 200]
    return len(useful) < MIN_USEFUL_PAGES or (bool(spec["fields"]) and spec["outputs"]["table"] and not rows)


# ---------- storage ----------
def _table_for(name: str) -> str:
    return f"ds_{slugify(name, 50)}"


def _dict(p: Pipeline) -> dict:
    return {"id": p.id, "name": p.name, "goal": p.goal, "spec": p.spec, "table": p.table_name, "kb_id": p.kb_id,
            "enabled": bool(p.enabled), "interval_minutes": p.interval_minutes,
            "last_run_at": p.last_run_at.isoformat() if p.last_run_at else None, "last_result": p.last_result,
            "estimate": estimate(p.spec) if p.spec else {}}


def list_pipelines() -> list[dict]:
    with session_scope() as s:
        return [_dict(p) for p in s.scalars(select(Pipeline).order_by(Pipeline.created_at))]


def get_pipeline(pipeline_id: str) -> dict | None:
    with session_scope() as s:
        p = s.get(Pipeline, pipeline_id)
        return _dict(p) if p else None


def save_pipeline(raw_spec: dict, kb_id: str | None = None, pipeline_id: str | None = None) -> dict:
    spec = validate_spec(raw_spec)
    table = _table_for(spec["name"]) if spec["outputs"]["table"] else ""
    with session_scope() as s:
        if spec["outputs"]["knowledge_base"]:
            if not kb_id or s.get(KnowledgeBase, kb_id) is None:
                raise SpecError(["Pick the knowledge base to fill"])
        else:
            kb_id = None
        pipeline = s.get(Pipeline, pipeline_id) if pipeline_id else None
        clash = s.scalar(select(Pipeline).where(Pipeline.name == spec["name"]))
        if clash and (pipeline is None or clash.id != pipeline.id):
            raise SpecError([f"A pipeline named {spec['name']} already exists"])
        if table:
            owned = s.scalar(select(Dataset).where(Dataset.table_name == table))
            if owned and not (pipeline and pipeline.table_name == table) and not (owned.description or "").startswith("Pipeline:"):
                raise SpecError([f"A table named {table} already exists (not from a pipeline); rename this pipeline"])
        if pipeline is None:
            pipeline = Pipeline(name=spec["name"])
            s.add(pipeline)
        pipeline.goal, pipeline.spec, pipeline.table_name, pipeline.kb_id = spec["goal"], spec, table, kb_id
        pipeline.interval_minutes = spec["interval_minutes"]
        s.flush()
        return _dict(pipeline)


def delete_pipeline(pipeline_id: str, drop_table: bool = False) -> None:
    with session_scope() as s:
        p = s.get(Pipeline, pipeline_id)
        if p is None:
            return
        table = p.table_name
        s.delete(p)
    if drop_table and table:
        conn = writer_connection()
        try:
            conn.execute(f'DROP TABLE IF EXISTS "{table}"')
            conn.commit()
        finally:
            conn.close()
        with session_scope() as s:
            ds = s.scalar(select(Dataset).where(Dataset.table_name == table))
            if ds:
                s.delete(ds)


# ---------- running ----------
def _placeholder_in(step: dict) -> str:
    """The placeholder host (example.com, localhost...) a step points at, or ''. Old saved plans may still contain them."""
    targets = [step.get("site", "")] if step["type"] == "sitemap" else list(step.get("urls", [])) if step["type"] == "extract" else []
    for t in targets:
        host = (urlparse(t if "://" in t else f"https://{t}").hostname or "").lower()
        if host and PLACEHOLDER_HOSTS.search(host):
            return host
    return ""


async def collect(spec: dict, keiro=None) -> tuple[list[WebResult], list[str]]:
    """Run the KeiroLabs steps. Returns de-duplicated pages (with text where the step provides it) and warnings."""
    keiro = keiro or get_keiro()
    if not keiro.configured:
        raise KeiroError("KeiroLabs isn't configured: add your key on the Models page")
    pages: dict[str, WebResult] = {}
    warnings: list[str] = []
    need_text = bool(spec["fields"]) or spec["outputs"]["knowledge_base"]

    def add(results: list[WebResult]) -> None:
        for r in results:
            if r.url and r.url not in pages and len(pages) < MAX_PAGES:
                pages[r.url] = r
            elif r.url in pages and len(r.content or "") > len(pages[r.url].content or ""):
                pages[r.url] = r

    for step in spec["steps"]:
        fake = _placeholder_in(step)
        if fake:
            warnings.append(f"{step['type']}: skipped, {fake} is a placeholder, not a real site")
            continue
        try:
            if step["type"] == "search":
                add(await keiro.search(step["query"], step["max_results"], step["mode"]))
            elif step["type"] == "extract":
                add(await keiro.extract(step["urls"]))
            elif step["type"] == "sitemap":
                rows = await keiro.sitemap(step["site"], step["path_prefix"], "", step["limit"])
                urls = [r["url"] for r in rows][:step["limit"]]
                for i in range(0, len(urls), 10):
                    add(await keiro.extract(urls[i:i + 10]))
            else:
                deep = await keiro.deep(step["question"])
                add(deep.results)
        except KeiroError as exc:
            warnings.append(f"{step['type']}: {exc}")
    if need_text:
        thin = [u for u, r in pages.items() if len((r.content or "").strip()) < 200][:10]
        if thin:
            try:
                add(await keiro.extract(thin))
            except KeiroError as exc:
                warnings.append(f"extract: {exc}")
    return list(pages.values()), warnings


EXTRACT_PROMPT = """You extract structured data from one web page.
Return ONLY a JSON object with exactly these keys: {keys}.
Field meanings:
{meanings}
Use null for anything the page doesn't state. Never guess. Numbers are plain numbers (no units or commas); dates are ISO (YYYY-MM-DD) when the page gives a full date.
{extra}"""


def _coerce(value, ftype: str):
    if value is None or value == "":
        return None
    try:
        if ftype == "number":
            return float(str(value).replace(",", "").strip())
        if ftype == "boolean":
            if isinstance(value, bool):
                return int(value)
            return 1 if str(value).strip().lower() in ("true", "yes", "1") else 0 if str(value).strip().lower() in ("false", "no", "0") else None
        return str(value).strip()[:2000]
    except (ValueError, TypeError):
        return None


EXTRACT_PARALLEL = 3


async def extract_rows(spec: dict, pages: list[WebResult], complete=None) -> tuple[list[dict], list[str]]:
    """One model call per page (up to EXTRACT_PARALLEL at once). A garbled answer gets one more try. Empty rows are dropped."""
    import asyncio

    from app.llm.jsonfix import loads_lenient

    if not spec["fields"]:
        return [], []
    if complete is None:
        from app.llm.registry import get_router

        complete = get_router().complete
    keys = ", ".join(f'"{f["name"]}"' for f in spec["fields"])
    meanings = "\n".join(f"- {f['name']} ({f['type']}): {f['description'] or f['name']}" for f in spec["fields"])
    system = EXTRACT_PROMPT.format(keys=keys, meanings=meanings, extra=spec.get("instructions", ""))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sem = asyncio.Semaphore(EXTRACT_PARALLEL)

    async def one(page: WebResult) -> tuple[dict | None, str | None]:
        text = (page.content or page.excerpt or "").strip()
        if len(text) < 80:
            return None, None
        user = f"Title: {page.title}\nURL: {page.url}\n\n{text[:MAX_PAGE_CHARS]}"
        data, last = None, ""
        async with sem:
            for attempt in range(2):
                try:
                    reply = await complete([ChatMessage("system", system if attempt == 0 else system + "\nReturn ONLY one valid JSON object, nothing else."),
                                            ChatMessage("user", user)],
                                           temperature=0.0 if attempt == 0 else 0.2, max_tokens=900 if attempt == 0 else 1400)
                except Exception as exc:  # noqa: BLE001 - one bad page shouldn't stop the run
                    return None, f"{page.url}: {str(exc)[:120]}"
                data = loads_lenient(reply.text)
                if data is not None:
                    break
                last = f"{page.url}: the model's answer couldn't be read"
        if data is None:
            return None, last
        row = {f["name"]: _coerce(data.get(f["name"]), f["type"]) for f in spec["fields"]}
        if all(v is None for v in row.values()):
            return None, None
        return {"source_url": page.url, "title": page.title[:300], "collected_at": now, **row}, None

    results = await asyncio.gather(*(one(p) for p in pages))
    return [r for r, _ in results if r], [e for _, e in results if e]


def write_rows(table: str, spec: dict, rows: list[dict]) -> int:
    """Upsert by source_url, so a re-run updates a page's row instead of duplicating it."""
    cols = ["source_url", "title", "collected_at"] + [f["name"] for f in spec["fields"]]
    ddl = ", ".join(['"source_url" TEXT', '"title" TEXT', '"collected_at" TEXT'] +
                    [f'"{f["name"]}" {FIELD_TYPES[f["type"]]}' for f in spec["fields"]])
    conn = writer_connection()
    try:
        conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({ddl})')
        existing = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        for col in cols:
            if col not in existing:  # a field added after the first run
                ftype = next((FIELD_TYPES[f["type"]] for f in spec["fields"] if f["name"] == col), "TEXT")
                conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{col}" {ftype}')
        if rows:
            conn.executemany(f'DELETE FROM "{table}" WHERE source_url = ?', [(r["source_url"],) for r in rows])
            conn.executemany(f'INSERT INTO "{table}" ({", ".join(chr(34) + c + chr(34) for c in cols)}) VALUES ({", ".join("?" * len(cols))})',
                             [tuple(r.get(c) for c in cols) for r in rows])
        conn.commit()
        total = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        columns = table_columns(conn, table)
    finally:
        conn.close()
    with session_scope() as s:
        ds = s.scalar(select(Dataset).where(Dataset.table_name == table))
        if ds is None:
            ds = Dataset(name=table[3:], table_name=table)
            s.add(ds)
        ds.zone, ds.description, ds.row_count, ds.columns = "clean", f"Pipeline: {spec['name']}", total, columns
    return total


def run_job(pipeline_id: str, dry: bool = False):
    """Job factory. `dry` collects and extracts but writes nothing, so a plan can be tried first."""

    async def job(ctx) -> dict:
        from app.ingest.web import _run

        with session_scope() as s:
            p = s.get(Pipeline, pipeline_id)
            if p is None:
                raise SpecError(["This pipeline was removed"])
            spec, table, kb_id, name = p.spec, p.table_name, p.kb_id, p.name
        keiro = get_keiro()
        credits_before = keiro.state.credits_remaining
        await ctx.progress(0.05, "Collecting with KeiroLabs")
        pages, warnings = await collect(spec, keiro)
        replanned = None
        if not pages and not any("credits" in w.lower() or "api key" in w.lower() for w in warnings):
            await ctx.progress(0.2, "Nothing found; re-planning the collection steps")
            fix = await repair_steps(spec, pages, warnings)
            if fix:
                pages, more_warnings = await collect({**spec, "steps": fix[0]}, keiro)
                warnings += more_warnings
                replanned = {"steps": fix[0], "why": fix[1], "new_pages": len(pages)}
        if not pages:
            raise KeiroError("KeiroLabs returned no pages" + (f" ({warnings[0]})" if warnings else ""))
        await ctx.progress(0.4, f"Reading {len(pages)} pages")
        rows, errors = await extract_rows(spec, pages)
        if not replanned and _thin(spec, pages, rows):
            await ctx.progress(0.5, "Too little found; re-planning the collection steps")
            fix = await repair_steps(spec, pages, warnings + errors)
            if fix:
                steps, why = fix
                more, more_warnings = await collect({**spec, "steps": steps}, keiro)
                known = {p.url for p in pages}
                extra = [p for p in more if p.url not in known]
                if extra:
                    await ctx.progress(0.6, f"Reading {len(extra)} more pages")
                    extra_rows, extra_errors = await extract_rows(spec, extra)
                    pages, rows, errors = pages + extra, rows + extra_rows, errors + extra_errors
                warnings += more_warnings
                replanned = {"steps": steps, "why": why, "new_pages": len(extra)}
        result: dict = {"pages": len(pages), "rows_found": len(rows), "warnings": summarize_problems(warnings + errors), "dry_run": dry}
        if replanned:
            result["replanned"] = replanned
        await ctx.progress(0.7, "Reviewing the results")
        result["review"] = await review(spec, rows, pages, warnings + errors)
        if dry:
            result["sample"] = rows[:5]
            result["pages_sample"] = [{"title": p.title, "url": p.url, "chars": len(p.content or "")} for p in pages[:5]]
            return result

        source_id, run_id = start_run("pipeline", name, kb_id, {"pipeline_id": pipeline_id})
        raw_dir = raw_run_dir("pipeline", run_id)
        write_raw_json(raw_dir, "keirolabs_pages", [pg.to_dict() for pg in pages])
        try:
            if table:
                await ctx.progress(0.75, "Saving rows")
                result["table"], result["table_rows"] = table, write_rows(table, spec, rows)
            if kb_id:
                await ctx.progress(0.85, "Indexing pages")
                docs = [IncomingDoc(pg.title or pg.url, pg.content, raw_dir / "keirolabs_pages.json", "text/markdown", pg.url,
                                    {"pipeline": name}) for pg in pages if len((pg.content or "").strip()) > 80]
                if docs:
                    stats = await _run(kb_id, source_id, run_id, raw_dir, docs, ctx)
                    result["kb_documents"] = stats.get("documents", 0)
            finish_run(run_id, raw_path=relative(raw_dir), stats={k: v for k, v in result.items() if isinstance(v, (int, str))})
        except Exception as exc:
            finish_run(run_id, raw_path=relative(raw_dir), error=str(exc))
            raise
        after = keiro.state.credits_remaining
        if credits_before is not None and after is not None:
            result["credits_used"] = round(credits_before - after, 3)
        with session_scope() as s:
            p = s.get(Pipeline, pipeline_id)
            if p is not None:
                p.last_run_at, p.last_result = datetime.now(timezone.utc), result
        if table:
            try:  # keep a dated copy of the sheet after every refresh
                from app.sheets.service import snapshot_table

                result["snapshot"] = snapshot_table(table)
            except Exception as exc:  # noqa: BLE001 - the run itself succeeded
                log.warning("Snapshot after run failed: %s", exc)
        from app import access

        access.emit("pipeline.finished", {"pipeline_id": pipeline_id, "name": name,
                                          **{k: v for k, v in result.items() if isinstance(v, (int, float, str)) and k != "review"}})
        return result

    return job


def due_pipelines(now: datetime | None = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    due = []
    with session_scope() as s:
        for p in s.scalars(select(Pipeline).where(Pipeline.enabled == 1, Pipeline.interval_minutes > 0)):
            last = p.last_run_at.replace(tzinfo=timezone.utc) if p.last_run_at else None
            if last is None or now - last >= timedelta(minutes=p.interval_minutes):
                p.last_run_at = now  # claim it; a slow run isn't queued twice
                due.append(p.id)
    return due


async def pipeline_loop() -> None:
    import asyncio

    from app.jobs.runner import runner

    while True:
        await asyncio.sleep(60)
        try:
            from app.config import offline_mode

            if offline_mode():
                continue
            for pid in due_pipelines():
                runner.submit("pipeline", "Pipeline run", run_job(pid), lane="background", resume={"type": "pipeline", "id": pid})
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("Pipeline schedule tick failed")
