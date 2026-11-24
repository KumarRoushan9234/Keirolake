"""In-process background jobs.

A single worker drains the main queue, which also keeps ingestion to one
writer at a time. Long jobs (library downloads, model pulls, library indexing)
run on a separate background lane so they never block ingestion. Job state is persisted so the UI can poll it. A scheduler loop runs
due Keiro topic feeds.
"""
from __future__ import annotations

import asyncio
import logging
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select

from app.catalog.models import Feed, Job
from app.db import session_scope

log = logging.getLogger(__name__)
BACKGROUND_WORKERS = 4  # downloads, model pulls and indexing run side by side
JobFn = Callable[["JobContext"], Awaitable[dict | None]]


class JobContext:
    def __init__(self, job_id: str):
        self.job_id = job_id

    async def progress(self, fraction: float, message: str = "") -> None:
        with session_scope() as s:
            job = s.get(Job, self.job_id)
            if fraction >= 0:  # a negative fraction updates only the message
                job.progress = round(min(1.0, fraction), 3)
            if message:
                job.message = message[:500]


class JobRunner:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[tuple[str, JobFn]] = asyncio.Queue()
        self.background: asyncio.Queue[tuple[str, JobFn]] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []

    def submit(self, kind: str, title: str, fn: JobFn, lane: str = "main", resume: dict | None = None) -> str:
        """Queue a job. With `resume`, a restart or update re-queues it instead of losing it (see jobs/resume.py)."""
        with session_scope() as s:
            job = Job(kind=kind, title=title[:200], result={"_resume": resume} if resume else {})
            s.add(job)
            s.flush()
            job_id = job.id
        (self.background if lane == "background" else self.queue).put_nowait((job_id, fn))
        return job_id

    async def _worker(self, queue: asyncio.Queue) -> None:
        while True:
            job_id, fn = await queue.get()
            with session_scope() as s:
                s.get(Job, job_id).status = "running"
            try:
                result = await fn(JobContext(job_id)) or {}
                status, message = "done", "Completed"
            except Exception as exc:  # noqa: BLE001 - recorded on the job
                log.error("Job %s failed: %s", job_id, traceback.format_exc())
                result, status, message = {"error": str(exc)}, "failed", str(exc)
            with session_scope() as s:
                job = s.get(Job, job_id)
                job.status, job.result = status, _jsonable(result)
                job.message = message[:2000] if status == "failed" else (job.message or message)
                job.progress = 1.0 if status == "done" else job.progress
                job.finished_at = datetime.now(timezone.utc)
            queue.task_done()

    async def _scheduler(self) -> None:
        from app.ingest.web import feed_job  # local import: avoids an import cycle

        while True:
            await asyncio.sleep(60)
            try:
                now = datetime.now(timezone.utc)
                with session_scope() as s:
                    feeds = list(s.scalars(select(Feed).where(Feed.enabled == 1)))
                    due = []
                    for feed in feeds:
                        last = feed.last_run_at.replace(tzinfo=timezone.utc) if feed.last_run_at else None
                        if last is None or now - last >= timedelta(minutes=feed.interval_minutes):
                            feed.last_run_at = now
                            due.append((feed.id, feed.query))
                for feed_id, query in due:
                    self.submit("feed", f"Feed: {query}", feed_job(feed_id))
                from app import connectors  # local import: avoids an import cycle

                for cid in connectors.due(now):
                    c = connectors.get(cid)
                    connectors._finish(cid, c["last_result"], None)  # mark as started so a slow run isn't queued twice
                    self.submit("connector", f"Connector: {c['name']}", connectors.run_job(cid), lane="background")
            except Exception:  # noqa: BLE001 - keep the scheduler alive
                log.exception("Feed scheduler tick failed")

    def start(self) -> None:
        # Jobs cut off by a restart or update: resume the ones that know how, mark the rest failed.
        from app.jobs import resume as resumer

        again: list[tuple[dict, datetime | None]] = []
        with session_scope() as s:
            for job in s.scalars(select(Job).where(Job.status.in_(["queued", "running"]))):
                spec = (job.result or {}).get("_resume")
                tries = (spec or {}).get("tries", 0)
                if spec and tries < resumer.MAX_TRIES:
                    job.status, job.message = "failed", "Interrupted by a restart; continued automatically"
                    again.append(({**spec, "tries": tries + 1}, job.created_at))
                else:
                    job.status, job.message = "failed", "Interrupted by server restart"
        self._tasks = [asyncio.create_task(self._worker(self.queue)), *[asyncio.create_task(self._worker(self.background)) for _ in range(BACKGROUND_WORKERS)],
                       asyncio.create_task(self._scheduler())]
        for spec, created in again:
            try:
                built = resumer.build(spec, created.replace(tzinfo=timezone.utc) if created and created.tzinfo is None else created)
            except Exception:  # noqa: BLE001 - a job that can't be rebuilt stays failed
                log.exception("Could not resume %s", spec)
                continue
            if built:
                kind, title, fn, lane = built
                self.submit(kind, title, fn, lane=lane, resume=spec)
                log.info("Resumed after restart: %s", title)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


runner = JobRunner()
