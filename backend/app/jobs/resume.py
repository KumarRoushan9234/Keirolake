"""Rebuild long jobs after a restart or update, so they continue instead of being lost.

A job submitted with `resume={...}` stores that description; at the next start the
runner calls `build()` and queues the job again. Downloads continue from their
partial file and indexing from its saved cursor.
"""
from __future__ import annotations

from datetime import datetime, timezone

MAX_TRIES = 3


def build(resume: dict, interrupted_at: datetime | None = None):
    """(kind, title, job function, lane) for a stored resume description, or None if it can't be resumed."""
    kind = resume.get("type")
    if kind == "pipeline":
        from app import pipelines

        p = pipelines.get_pipeline(resume["id"])
        if p is None:
            return None
        dry = bool(resume.get("dry"))
        return "pipeline", f"{'Dry run' if dry else 'Run'}: {p['name']}", pipelines.run_job(resume["id"], dry), "background"
    if kind == "model":
        from app.local import ollama

        return "model", f"Download {resume['model']}", ollama.pull_job(resume["model"]), "background"
    if kind == "library":
        from app.library import service as library

        entry = resume["entry"]
        _, job = library.start_download(entry)
        return "library", f"Download {entry.get('title') or entry['name']}", job, "background"
    if kind == "index":
        from app.library import service as library

        lib = library.get(resume["library_id"])
        if lib is None:
            return None
        title = f"Index {'intros' if resume['tier'] == 1 else 'passages'}: {lib.title or lib.name}"
        return "index", title, library.index_job(resume["library_id"], resume["tier"]), "background"
    return None
