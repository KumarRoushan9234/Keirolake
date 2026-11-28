"""API keys, the access log and webhooks: how the user's own apps use their data safely.

Keys are random, shown once, and stored only as a SHA-256 hash. Each key has
scopes (read, ask, write, sql), may be limited to some knowledge bases, can
expire, and can be revoked. Every request made with a key is logged.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import delete, select

from app.catalog.models import AccessLog, ApiKey, Webhook, utcnow
from app.db import session_scope

log = logging.getLogger(__name__)
KEY_PREFIX = "klk_"
SCOPES = {
    "read": "List and search knowledge bases, read datasets",
    "ask": "Ask questions and get cited answers",
    "write": "Add documents to knowledge bases",
    "sql": "Run read-only SQL on the warehouse",
    "llm": "Use the models through the OpenAI-compatible gateway",
}
EVENTS = {
    "ingest.finished": "Documents were added (upload, folder, web, pipeline)",
    "pipeline.finished": "A pipeline run finished",
    "dataset.created": "A table was added to the warehouse",
    "monitor.alert": "A monitor alert was raised (a disaster, a critical vulnerability, a big price move, a word you watch)",
    "monitor.brief": "The daily brief was written",
}
RATE_PER_MINUTE = 120
KEEP_LOG_ROWS = 5000


class AccessError(ValueError):
    pass


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def _key_dict(k: ApiKey) -> dict:
    expires = _aware(k.expires_at)
    return {"id": k.id, "name": k.name, "prefix": k.prefix, "scopes": k.scopes, "kb_ids": k.kb_ids,
            "expires_at": expires.isoformat() if expires else None,
            "last_used_at": _aware(k.last_used_at).isoformat() if k.last_used_at else None,
            "revoked": bool(k.revoked), "expired": bool(expires and expires < datetime.now(timezone.utc)),
            "created_at": _aware(k.created_at).isoformat()}


def create_key(name: str, scopes: list[str], kb_ids: list[str] | None = None, expires_days: int | None = None) -> tuple[dict, str]:
    """A new key. The raw key is returned once and never stored."""
    name = name.strip()[:60]
    bad = [s for s in scopes if s not in SCOPES]
    if not name:
        raise AccessError("Give the key a name, such as the app that will use it")
    if not scopes or bad:
        raise AccessError(f"Pick at least one permission: {', '.join(SCOPES)}")
    raw = KEY_PREFIX + secrets.token_urlsafe(32)
    with session_scope() as s:
        key = ApiKey(name=name, prefix=raw[:10], key_hash=_hash(raw), scopes=sorted(set(scopes)), kb_ids=list(kb_ids or []),
                     expires_at=utcnow() + timedelta(days=expires_days) if expires_days else None)
        s.add(key)
        s.flush()
        return _key_dict(key), raw


def list_keys() -> list[dict]:
    with session_scope() as s:
        return [_key_dict(k) for k in s.scalars(select(ApiKey).order_by(ApiKey.created_at.desc()))]


def revoke(key_id: str) -> bool:
    with session_scope() as s:
        key = s.get(ApiKey, key_id)
        if key is None:
            return False
        key.revoked = 1
        return True


def delete_key(key_id: str) -> bool:
    with session_scope() as s:
        key = s.get(ApiKey, key_id)
        if key is None:
            return False
        s.delete(key)
        return True


def verify(raw: str | None) -> dict | None:
    """The key's record if it is valid, active and unexpired."""
    if not raw or not raw.startswith(KEY_PREFIX) or len(raw) > 100:
        return None
    digest = _hash(raw)
    with session_scope() as s:
        key = s.scalar(select(ApiKey).where(ApiKey.key_hash == digest))
        if key is None or not hmac.compare_digest(key.key_hash, digest) or key.revoked:
            return None
        expires = _aware(key.expires_at)
        if expires and expires < datetime.now(timezone.utc):
            return None
        key.last_used_at = utcnow()
        return _key_dict(key)


_hits: dict[str, deque] = defaultdict(deque)


def rate_ok(key_id: str, now: float | None = None) -> bool:
    """At most RATE_PER_MINUTE requests per key in any 60 seconds."""
    now = now or time.monotonic()
    window = _hits[key_id]
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= RATE_PER_MINUTE:
        return False
    window.append(now)
    return True


def record(key: dict | None, method: str, path: str, status: int, client: str) -> None:
    with session_scope() as s:
        s.add(AccessLog(key_id=key["id"] if key else None, key_name=key["name"] if key else "", method=method,
                        path=path[:300], status=status, client=client[:60]))
    if secrets.randbelow(50) == 0:  # trim now and then
        prune_log()


def prune_log(keep: int = KEEP_LOG_ROWS) -> None:
    with session_scope() as s:
        cutoff = s.scalar(select(AccessLog.id).order_by(AccessLog.id.desc()).offset(keep).limit(1))
        if cutoff is not None:
            s.execute(delete(AccessLog).where(AccessLog.id <= cutoff))


def access_log(limit: int = 200) -> list[dict]:
    with session_scope() as s:
        rows = s.scalars(select(AccessLog).order_by(AccessLog.id.desc()).limit(limit))
        return [{"key": r.key_name or "(no key)", "method": r.method, "path": r.path, "status": r.status, "client": r.client,
                 "at": _aware(r.at).isoformat()} for r in rows]


# ---------- webhooks ----------
def _hook_dict(h: Webhook) -> dict:
    return {"id": h.id, "url": h.url, "events": h.events, "enabled": bool(h.enabled), "last_status": h.last_status,
            "last_at": _aware(h.last_at).isoformat() if h.last_at else None}


def add_webhook(url: str, events: list[str]) -> tuple[dict, str]:
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")) or len(url) > 500:
        raise AccessError("Use a full http:// or https:// address")
    bad = [e for e in events if e not in EVENTS]
    if not events or bad:
        raise AccessError(f"Pick at least one event: {', '.join(EVENTS)}")
    secret = secrets.token_urlsafe(24)
    with session_scope() as s:
        hook = Webhook(url=url, events=sorted(set(events)), secret=secret)
        s.add(hook)
        s.flush()
        return _hook_dict(hook), secret


def list_webhooks() -> list[dict]:
    with session_scope() as s:
        return [_hook_dict(h) for h in s.scalars(select(Webhook).order_by(Webhook.created_at))]


def delete_webhook(hook_id: str) -> bool:
    with session_scope() as s:
        hook = s.get(Webhook, hook_id)
        if hook is None:
            return False
        s.delete(hook)
        return True


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def emit(event: str, payload: dict) -> None:
    """Tell every webhook that wants this event. Never blocks or fails the caller."""
    try:
        with session_scope() as s:
            hooks = [(h.id, h.url, h.secret) for h in s.scalars(select(Webhook).where(Webhook.enabled == 1)) if event in (h.events or [])]
        if not hooks:
            return
        loop = asyncio.get_running_loop()
    except Exception:  # noqa: BLE001 - no loop (CLI) or no database yet: nothing to deliver
        return
    body = json.dumps({"event": event, "at": utcnow().isoformat(), "data": payload}, default=str).encode()
    for hook_id, url, secret in hooks:
        loop.create_task(_deliver(hook_id, url, secret, event, body))


async def _deliver(hook_id: str, url: str, secret: str, event: str, body: bytes, tries: int = 3) -> None:
    headers = {"Content-Type": "application/json", "X-KeiroLake-Event": event, "X-KeiroLake-Signature": sign(secret, body),
               "User-Agent": "KeiroLake-Webhook"}
    status = ""
    for attempt in range(tries):
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                resp = await client.post(url, content=body, headers=headers)
            status = str(resp.status_code)
            if resp.status_code < 500:
                break
        except httpx.HTTPError as exc:
            status = type(exc).__name__
        await asyncio.sleep(2 ** attempt)
    with session_scope() as s:
        hook = s.get(Webhook, hook_id)
        if hook is not None:
            hook.last_status, hook.last_at = status, utcnow()
