"""Build-with-AI sessions kept by the engine, so a conversation and its work go on when you switch pages, close the
window or restart the app.

A session holds the chat, the agreed draft (name, columns or sections), the options and what was built. Every model
step (a reply, a fill) runs as a background job that writes its result back into the session; the page only shows
the session and polls it. Sessions are small JSON files in <data>/builder/.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from app import builder
from app.config import get_settings

_lock = threading.Lock()
KINDS = ("table", "collection")
MAX_SESSIONS = 200
DEFAULT_OPTIONS = {"lake": True, "libraries": True, "web": False, "local_only": True}


class SessionError(ValueError):
    pass


def _dir() -> Path:
    d = get_settings().data_dir / "builder"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(sid: str) -> Path:
    if not sid or not all(c in "0123456789abcdef" for c in sid) or len(sid) != 32:
        raise SessionError("Unknown session")
    return _dir() / f"{sid}.json"


def _read(sid: str) -> dict:
    p = _path(sid)
    if not p.exists():
        raise SessionError("Unknown session")
    return json.loads(p.read_text(encoding="utf-8"))


def _write(s: dict) -> dict:
    s["updated"] = time.time()
    tmp = _path(s["id"]).with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_path(s["id"]))
    return s


def _change(sid: str, fn) -> dict:
    """Read, change and save one session under the lock (jobs and the page both write)."""
    with _lock:
        s = _read(sid)
        fn(s)
        return _write(s)


def create(kind: str, sheet: dict | None = None) -> dict:
    """A new conversation. With `sheet` (an existing table), it starts from that table's columns to add to it."""
    if kind not in KINDS:
        raise SessionError("Build a table or a library")
    s = {"id": uuid.uuid4().hex, "kind": kind, "title": "", "messages": [], "draft": None, "ready": False,
         "built": None, "options": dict(DEFAULT_OPTIONS), "busy": "", "error": "", "created": time.time()}
    if sheet:
        cols = [{"name": c["name"], "type": c.get("type", "text"), "description": ""} for c in sheet["columns"] if not c["name"].startswith("_")]
        s.update(title=sheet["name"], draft={"name": sheet["name"], "columns": cols, "sections": [], "rows": 20, "kind": "table"},
                 ready=bool(cols), built={"id": sheet["id"], "kind": "table", "added": 0},
                 messages=[{"role": "assistant", "content": f"\"{sheet['name']}\" has {len(cols)} columns. "
                            "Tell me what rows to add, or ask for a new column."}])
    with _lock:
        _write(s)
        _prune()
    return s


def _prune() -> None:
    files = sorted(_dir().glob("*.json"), key=lambda p: p.stat().st_mtime)
    for p in files[:-MAX_SESSIONS]:
        p.unlink(missing_ok=True)


def list_sessions() -> list[dict]:
    out = []
    for p in sorted(_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            s = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"id": s["id"], "kind": s["kind"], "title": s.get("title") or (s.get("draft") or {}).get("name") or _first_words(s),
                    "busy": s.get("busy", ""), "built": s.get("built"), "updated": s.get("updated", 0)})
    return out


def _first_words(s: dict) -> str:
    first = next((m["content"] for m in s.get("messages", []) if m["role"] == "user"), "")
    return (first[:48] + "…") if len(first) > 48 else (first or "New build")


def get(sid: str) -> dict:
    return _read(sid)


def delete(sid: str) -> None:
    with _lock:
        _path(sid).unlink(missing_ok=True)


def set_options(sid: str, options: dict) -> dict:
    clean = {k: bool(v) for k, v in options.items() if k in DEFAULT_OPTIONS}
    return _change(sid, lambda s: s["options"].update(clean))


def set_draft(sid: str, draft: dict) -> dict:
    """The person edited the plan by hand (renamed a column, added one, removed a section...)."""
    def apply(s):
        if s.get("busy"):
            raise SessionError("Wait for the current step to finish")
        s["draft"] = builder._clean_draft(draft, s["kind"])
        s["title"] = s["draft"]["name"] or s["title"]
        s["ready"] = bool(s["draft"]["name"]) and bool(s["draft"]["columns"] if s["kind"] == "table" else s["draft"]["sections"])
    return _change(sid, apply)


def _brief(s: dict) -> str:
    return "\n".join(m["content"] for m in s["messages"] if m["role"] == "user")[-4000:]


def say_job(sid: str, text: str):
    """Add the person's message now; the job writes the model's reply into the session when it is ready."""
    text = text.strip()
    if not text:
        raise SessionError("Say something first")

    def start(s):
        if s.get("busy"):
            raise SessionError("Wait for the current step to finish")
        s["messages"].append({"role": "user", "content": text[:4000]})
        s["busy"], s["error"] = "thinking", ""
    _change(sid, start)

    async def job(ctx) -> dict:
        s = _read(sid)
        try:
            await ctx.progress(0.2, "Thinking")
            out = await builder.turn(s["kind"], s["messages"], s.get("draft"), s["options"].get("local_only", False))
        except Exception as exc:
            _change(sid, lambda x: x.update(busy="", error=_friendly(exc)))
            raise

        def done(x):
            x["messages"].append({"role": "assistant", "content": out["reply"], "model": out.get("model", "")})
            x["draft"], x["ready"], x["busy"] = out["draft"], out["ready"], ""
            x["title"] = out["draft"]["name"] or x["title"]
        _change(sid, done)
        await ctx.progress(1, out["reply"][:160])  # the notice shows the start of the reply
        return {"session": sid, "ready": out["ready"]}

    return job


def fill_job(sid: str, count: int):
    """Create the table or library (or add to it) from the agreed draft, then ask what's next in the chat."""
    def start(s):
        if s.get("busy"):
            raise SessionError("Wait for the current step to finish")
        d = s.get("draft") or {}
        if not d.get("name"):
            raise SessionError("Give it a name first")
        if s["kind"] == "table" and not d.get("columns"):
            raise SessionError("Agree on the columns first")
        if s["kind"] == "collection" and not d.get("sections"):
            raise SessionError("Agree on the sections first")
        s["messages"].append({"role": "user", "content": f"Fill {count} rows." if s["kind"] == "table" else "Write the sections."})
        s["busy"], s["error"] = "filling", ""
    s = _change(sid, start)
    count = max(1, min(int(count), builder.MAX_ROWS))

    async def job(ctx) -> dict:
        s = _read(sid)
        d, o, built = builder._clean_draft(s["draft"], s["kind"]), s["options"], s.get("built")
        try:
            if s["kind"] == "table":
                if built:  # columns agreed after the table was made are added before filling
                    from app.sheets import service
                    have = {c["name"] for c in service.get(built["id"])["columns"]}
                    for c in d["columns"]:
                        if c["name"] not in have:
                            service.add_column(built["id"], c["name"], c["type"])
                result = await builder.fill_table_job(d, _brief(s), count, o["lake"], o["libraries"], built["id"] if built else None,
                                                      o["local_only"], o["web"])(ctx)
                target, n = result["sheet_id"], result["added"]
                reply = f"Done: {n} rows added to \"{d['name']}\".{(' ' + result['note']) if result.get('note') else ''} " \
                        "Want more rows, another column, or is this enough?"
            else:
                result = await builder.fill_collection_job(d, _brief(s), o["lake"], o["libraries"], built["id"] if built else None,
                                                           o["local_only"], o["web"])(ctx)
                target, n = result["kb_id"], result["notes"]
                reply = f"Done: {n} sections written into \"{d['name']}\". Want more sections, or is this enough?"
        except Exception as exc:
            _change(sid, lambda x: x.update(busy="", error=_friendly(exc)))
            raise

        def done(x):
            prev = x.get("built") or {}
            x["built"] = {"id": target, "kind": x["kind"], "added": (prev.get("added", 0) if prev.get("id") == target else 0) + n}
            x["messages"].append({"role": "assistant", "content": reply})
            x["busy"] = ""
        _change(sid, done)
        await ctx.progress(1, f"{n} {'rows added' if s['kind'] == 'table' else 'sections written'}")
        return {"session": sid, "target": target, "added": n, "sheet_id": target if s["kind"] == "table" else None,
                "kb_id": target if s["kind"] == "collection" else None}

    return job, s


def recover() -> None:
    """After a restart, a step that was running is gone: say so instead of showing it as busy forever."""
    for p in _dir().glob("*.json"):
        try:
            s = json.loads(p.read_text(encoding="utf-8"))
            if s.get("busy"):
                s["busy"], s["error"] = "", "The app restarted before this step finished. Send it again."
                _write(s)
        except (OSError, ValueError):
            continue


def _friendly(exc: Exception) -> str:
    from app.llm.registry import AllModelsFailed

    if isinstance(exc, AllModelsFailed):
        return "No model could answer. Choose a local model in Models, or allow cloud models."
    return str(exc)[:300] or exc.__class__.__name__
