"""Check a file against a Tauri updater signature (minisign format, Ed25519), with no external tools.

Releases are signed with the project's updater key; the public half is in the app (UPDATER_PUBKEY, the same value as
desktop/src-tauri/tauri.conf.json). Both the key and the .sig file are base64 of the minisign text formats:

    key:  "untrusted comment: …" / base64("Ed" + key id (8 bytes) + public key (32 bytes))
    sig:  "untrusted comment: …" / base64(alg (2) + key id (8) + signature (64)) / "trusted comment: …" / base64(global sig)

alg "ED" signs the BLAKE2b-512 hash of the file (minisign's default, which Tauri uses); "Ed" signs the file itself.
The global signature covers the signature plus the trusted comment, so that comment can't be altered either.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

UPDATER_PUBKEY = ("dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IDBCRjdBQjE5RjY4MUFERTMKUldUanJZSDJHYXYzQ3p0RThNUU1BSFVYTlluVi85RkFFRWFaWkdpOEphTEVaWllEVmg4MkNIOEcK")


class SignatureError(ValueError):
    pass


def _lines(b64: str) -> list[str]:
    try:
        text = base64.b64decode(b64.strip()).decode()
    except Exception as exc:  # noqa: BLE001
        raise SignatureError("Not a valid signature or key") from exc
    return [line for line in text.splitlines() if line.strip()]


def _public_key(pubkey_b64: str) -> tuple[bytes, Ed25519PublicKey]:
    lines = _lines(pubkey_b64)
    raw = base64.b64decode(lines[1])
    if len(raw) != 42 or raw[:2] != b"Ed":
        raise SignatureError("Not a minisign public key")
    return raw[2:10], Ed25519PublicKey.from_public_bytes(raw[10:])


def verify(data: bytes, signature_b64: str, pubkey_b64: str = UPDATER_PUBKEY) -> str:
    """Raise SignatureError unless `data` was signed by the key. Returns the signature's trusted comment."""
    key_id, key = _public_key(pubkey_b64)
    lines = _lines(signature_b64)
    if len(lines) < 4 or not lines[2].startswith("trusted comment: "):
        raise SignatureError("Not a minisign signature")
    sig = base64.b64decode(lines[1])
    if len(sig) != 74:
        raise SignatureError("Not a minisign signature")
    alg, sig_key_id, signature = sig[:2], sig[2:10], sig[10:]
    if sig_key_id != key_id:
        raise SignatureError("Signed with a different key")
    message = hashlib.blake2b(data, digest_size=64).digest() if alg == b"ED" else data if alg == b"Ed" else None
    if message is None:
        raise SignatureError("Unknown signature type")
    trusted = lines[2][len("trusted comment: "):]
    try:
        key.verify(signature, message)
        key.verify(base64.b64decode(lines[3]), signature + trusted.encode())
    except InvalidSignature as exc:
        raise SignatureError("The signature doesn't match: the file was changed or isn't from this project") from exc
    return trusted
