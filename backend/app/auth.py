"""Sign-in for the browser UI and other devices.

The desktop window talks to the engine with its per-launch token and needs no password. Anything that
reaches the engine through the network listener (a browser tab, another computer) must sign in:
a password you set once, stored only as a scrypt hash, gives an HttpOnly SameSite=Strict cookie whose
hash is kept in SQLite. Wrong passwords are rate limited with a lockout. "Open in browser" from this
PC uses a one-time code (valid for 2 minutes) so you never retype the password at home.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.catalog.models import AuthConfig, AuthSession, utcnow
from app.db import session_scope

COOKIE = "keirolake_session"
SESSION_DAYS = 30
MIN_PASSWORD = 10
MAX_FAILS = 5
LOCKOUT_SECONDS = 300
CODE_SECONDS = 120
SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


class AuthError(ValueError):
    pass


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, **{"n": SCRYPT["n"], "r": SCRYPT["r"], "p": SCRYPT["p"], "dklen": SCRYPT["dklen"]}).hex()


def _config(key: str) -> str:
    with session_scope() as s:
        row = s.get(AuthConfig, key)
        return row.value if row else ""


def _set_config(key: str, value: str) -> None:
    with session_scope() as s:
        row = s.get(AuthConfig, key)
        if row is None:
            s.add(AuthConfig(key=key, value=value))
        else:
            row.value = value


def has_password() -> bool:
    return bool(_config("password_hash"))


def set_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"Use a password of at least {MIN_PASSWORD} characters")
    salt = secrets.token_bytes(16)
    _set_config("password_salt", salt.hex())
    _set_config("password_hash", _hash_password(password, salt))
    sign_out_everywhere()  # a new password signs every device out


def clear_password() -> None:
    _set_config("password_hash", "")
    sign_out_everywhere()


def _check_password(password: str) -> bool:
    stored, salt = _config("password_hash"), _config("password_salt")
    if not stored or not salt:
        return False
    return hmac.compare_digest(stored, _hash_password(password, bytes.fromhex(salt)))


_fails: dict[str, list[float]] = {}


def locked(client: str, now: float | None = None) -> int:
    """Seconds left on a lockout (0 when not locked)."""
    now = now or time.monotonic()
    recent = [t for t in _fails.get(client, []) if now - t < LOCKOUT_SECONDS]
    _fails[client] = recent
    return int(LOCKOUT_SECONDS - (now - recent[0])) if len(recent) >= MAX_FAILS else 0


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def login(password: str, device: str, client: str) -> str:
    """A new session token (the cookie value). Raises AuthError on a wrong password or lockout."""
    wait = locked(client)
    if wait:
        raise AuthError(f"Too many wrong passwords. Try again in {wait // 60 + 1} minutes")
    if not has_password():
        raise AuthError("No password has been set. Set one in Settings → Network on the computer running KeiroLake")
    if not _check_password(password):
        _fails.setdefault(client, []).append(time.monotonic())
        raise AuthError("Wrong password")
    _fails.pop(client, None)
    return _new_session(device, client)


def _new_session(device: str, client: str) -> str:
    token = secrets.token_urlsafe(32)
    with session_scope() as s:
        s.add(AuthSession(token_hash=_sha(token), device=device[:120], client=client[:60],
                          expires_at=utcnow() + timedelta(days=SESSION_DAYS)))
    return token


def session(token: str | None) -> dict | None:
    if not token or len(token) > 100:
        return None
    with session_scope() as s:
        row = s.scalar(select(AuthSession).where(AuthSession.token_hash == _sha(token)))
        if row is None:
            return None
        expires = row.expires_at.replace(tzinfo=timezone.utc) if row.expires_at.tzinfo is None else row.expires_at
        if expires < datetime.now(timezone.utc):
            s.delete(row)
            return None
        row.last_seen = utcnow()
        return {"id": row.id, "device": row.device}


def logout(token: str | None) -> None:
    if token:
        with session_scope() as s:
            s.execute(delete(AuthSession).where(AuthSession.token_hash == _sha(token)))


def sign_out_everywhere() -> int:
    with session_scope() as s:
        return s.execute(delete(AuthSession)).rowcount or 0


def sessions() -> list[dict]:
    with session_scope() as s:
        return [{"id": r.id, "device": r.device, "client": r.client, "created_at": r.created_at.isoformat(), "last_seen": r.last_seen.isoformat()}
                for r in s.scalars(select(AuthSession).order_by(AuthSession.last_seen.desc()))]


def revoke(session_id: str) -> bool:
    with session_scope() as s:
        return bool(s.execute(delete(AuthSession).where(AuthSession.id == session_id)).rowcount)


# ---------- one-time codes: "Open in browser" from this PC ----------
_codes: dict[str, float] = {}


def make_code() -> str:
    now = time.monotonic()
    for c in [c for c, t in _codes.items() if now - t > CODE_SECONDS]:
        _codes.pop(c, None)
    code = secrets.token_urlsafe(24)
    _codes[code] = now
    return code


def exchange_code(code: str, device: str, client: str) -> str:
    issued = _codes.pop(code, None)
    if issued is None or time.monotonic() - issued > CODE_SECONDS:
        raise AuthError("That link has expired. Open KeiroLake from the tray again")
    return _new_session(device, client)
