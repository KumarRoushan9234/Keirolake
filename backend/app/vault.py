"""Encrypted private vault for personal notes (IDs, medical info, contacts, home details).

Everything in the vault file is encrypted with AES-256-GCM using a key derived
from your passphrase (scrypt). Without the passphrase the file is unreadable,
and the key lives only in memory while the vault is unlocked (it locks itself
after 15 idle minutes). When an unlocked vault is used in Ask, its notes are
only ever sent to models running on this computer, never to cloud providers.
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from app.config import get_settings

IDLE_SECONDS = 15 * 60
MIN_PASSPHRASE = 8
MAX_NOTE_CHARS = 20_000
_CHECK = b"keirolake-vault-v1"
_lock = threading.Lock()
_key: bytes | None = None
_last_used = 0.0


class VaultError(ValueError):
    pass


@dataclass
class PrivateHit:
    id: str
    title: str
    text: str
    score: float

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "text": self.text, "score": self.score}


def _path() -> Path:
    return get_settings().data_dir / "vault.json"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _unb64(text: str) -> bytes:
    return base64.b64decode(text)


def _derive(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase.encode("utf-8"))


def _read() -> dict | None:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def _write(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _touch() -> bytes:
    """The key, or an error if locked. Locks the vault when it has been idle too long."""
    global _key, _last_used
    with _lock:
        if _key is not None and time.monotonic() - _last_used > IDLE_SECONDS:
            _key = None
        if _key is None:
            raise VaultError("The vault is locked")
        _last_used = time.monotonic()
        return _key


def _encrypt(key: bytes, plain: bytes, aad: bytes) -> dict:
    nonce = os.urandom(12)
    return {"nonce": _b64(nonce), "ct": _b64(AESGCM(key).encrypt(nonce, plain, aad))}


def _decrypt(key: bytes, blob: dict, aad: bytes) -> bytes:
    try:
        return AESGCM(key).decrypt(_unb64(blob["nonce"]), _unb64(blob["ct"]), aad)
    except InvalidTag as exc:
        raise VaultError("Wrong passphrase, or the vault file was changed") from exc


def status() -> dict:
    data = _read()
    with _lock:
        unlocked = _key is not None and time.monotonic() - _last_used <= IDLE_SECONDS
    return {"exists": data is not None, "unlocked": unlocked, "items": len(data["items"]) if data else 0,
            "idle_minutes": IDLE_SECONDS // 60}


def init(passphrase: str) -> None:
    global _key, _last_used
    if _read() is not None:
        raise VaultError("A vault already exists")
    if len(passphrase) < MIN_PASSPHRASE:
        raise VaultError(f"Use a passphrase of at least {MIN_PASSPHRASE} characters")
    salt = os.urandom(16)
    key = _derive(passphrase, salt)
    _write({"version": 1, "salt": _b64(salt), "check": _encrypt(key, _CHECK, b"check"), "items": []})
    with _lock:
        _key, _last_used = key, time.monotonic()


def unlock(passphrase: str) -> None:
    global _key, _last_used
    data = _read()
    if data is None:
        raise VaultError("No vault yet")
    key = _derive(passphrase, _unb64(data["salt"]))
    if _decrypt(key, data["check"], b"check") != _CHECK:
        raise VaultError("Wrong passphrase")
    with _lock:
        _key, _last_used = key, time.monotonic()


def lock() -> None:
    global _key
    with _lock:
        _key = None


def _items(key: bytes) -> list[dict]:
    data = _read() or {"items": []}
    out = []
    for item in data["items"]:
        body = json.loads(_decrypt(key, item, item["id"].encode()))
        out.append({"id": item["id"], "title": body["title"], "text": body["text"], "created_at": item["created_at"]})
    return out


def list_items() -> list[dict]:
    key = _touch()
    return [{"id": i["id"], "title": i["title"], "chars": len(i["text"]), "created_at": i["created_at"]} for i in _items(key)]


def get_item(item_id: str) -> dict:
    key = _touch()
    item = next((i for i in _items(key) if i["id"] == item_id), None)
    if item is None:
        raise VaultError("Note not found")
    return item


def save_item(title: str, text: str, item_id: str | None = None) -> str:
    key = _touch()
    title, text = title.strip(), text.strip()
    if not title or not text:
        raise VaultError("A note needs a title and some text")
    if len(text) > MAX_NOTE_CHARS:
        raise VaultError(f"Notes are limited to {MAX_NOTE_CHARS:,} characters")
    data = _read()
    item_id = item_id or uuid.uuid4().hex
    created = next((i["created_at"] for i in data["items"] if i["id"] == item_id), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    blob = {"id": item_id, "created_at": created,
            **_encrypt(key, json.dumps({"title": title, "text": text}).encode(), item_id.encode())}
    data["items"] = [i for i in data["items"] if i["id"] != item_id] + [blob]
    _write(data)
    return item_id


def delete_item(item_id: str) -> None:
    _touch()
    data = _read()
    data["items"] = [i for i in data["items"] if i["id"] != item_id]
    _write(data)


def _terms(question: str) -> list[str]:
    from app.library.service import keywords

    return keywords(question)


def search(question: str, limit: int = 3) -> list[PrivateHit]:
    """Notes that match the question by keywords. Empty while the vault is locked."""
    try:
        key = _touch()
    except VaultError:
        return []
    terms = _terms(question)
    if not terms:
        return []
    scored = []
    for item in _items(key):
        hay = f"{item['title']}\n{item['text']}".lower()
        score = sum(len(re.findall(re.escape(t), hay)) * (1 + len(t) / 10) for t in terms)
        if score > 0:
            scored.append(PrivateHit(item["id"], item["title"], item["text"][:2400], round(score, 3)))
    return sorted(scored, key=lambda h: h.score, reverse=True)[:limit]
