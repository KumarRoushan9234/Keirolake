"""Settings changed from the app UI.

Plain values live in `<DATA_DIR>/prefs.json`; API keys go to the OS keychain
(Windows Credential Manager, macOS Keychain, Secret Service on Linux) and fall
back to the same file only when no keychain is available. Values are applied
as environment variables, so the rest of the code keeps reading one source.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)
SERVICE = "keirolake"

_PLAIN = {
    "LLM_MODE", "LOCAL_LLM_MODEL", "LLM_FALLBACK_ORDER", "EMBED_MODELS", "WIKI_EMBED_MODEL", "WIKI_EMBED_DIM",
    "RAG_WEB_FALLBACK", "SETUP_DONE", "AUTO_BACKUP_HOURS", "KEEP_BACKUPS", "OLLAMA_BASE_URL", "LOCAL_AUTOSTART", "LLAMACPP_SERVER", "LLAMACPP_MODEL_PATH", "LIBRARY_DIR",
    "CLOUD_DAILY_LIMIT", "KEIRO_ZDR", "KEIRO_CACHE", "LOCAL_EMBED_MODEL", "LOCAL_EMBED_DIM", "EMERGENCY_MODE", "OFFLINE_MODE", "LAN_ACCESS", "LAN_PORT", "LAN_BIND", "LAN_ALLOW", "LAN_CERT", "LAN_KEY", "LAN_UPNP",
    "BACKUP_TARGET", "BACKUP_INCLUDE_LAKE",
    "OLLAMA_CONTEXT_LENGTH", "OLLAMA_KV_CACHE_TYPE", "OLLAMA_FLASH_ATTENTION", "OLLAMA_KEEP_ALIVE", "OLLAMA_MAX_LOADED_MODELS", "OLLAMA_NUM_PARALLEL",
    "MODEL_SLOT_EVERYDAY", "MODEL_SLOT_HARD", "MODEL_SLOT_RESEARCH", "MODEL_SLOT_CODING", "MARKET_PROVIDER", "MARKET_WATCHLIST", "RELIEFWEB_APPNAME",
    "HERETIC_DIR", "HERETIC_PYTHON", "ONBOARDED", "RENDERDATA_BASE_URL", "SAMPLES_SEEDED", "TUNNEL_ON", "TUNNEL_PORT", "TUNNEL_HOSTNAME", "KEEP_AWAKE", "KEEP_AWAKE_LID",
}
_MODELS = re.compile(r"^[A-Z][A-Z0-9]{1,20}_MODELS$")
_SECRET = re.compile(r"^([A-Z][A-Z0-9_]{1,40}_API_KEY|BACKUP_PASSPHRASE|CLOUDFLARE_TUNNEL_TOKEN)$")


class PrefsError(ValueError):
    pass


def is_secret(key: str) -> bool:
    return bool(_SECRET.match(key))


def _allowed(key: str) -> bool:
    return key in _PLAIN or bool(_MODELS.match(key)) or is_secret(key)


def _path() -> Path:
    from app.config import get_settings

    return get_settings().data_dir / "prefs.json"


def _keyring():
    if os.getenv("KEIROLAKE_KEYRING", "1") == "0":
        return None
    try:
        import keyring

        keyring.get_password(SERVICE, "__probe__")
        return keyring
    except Exception:  # noqa: BLE001 - no usable keychain on this system
        return None


def _load() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"values": {}, "secrets": {}, "secret_keys": []}


def _save(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def apply_saved() -> None:
    """Copy saved prefs into the environment (called once at startup)."""
    data = _load()
    kr = _keyring()
    for key, value in data.get("values", {}).items():
        if _allowed(key):
            os.environ[key] = str(value)
    for key in data.get("secret_keys", []):
        value = (kr.get_password(SERVICE, key) if kr else None) or data.get("secrets", {}).get(key)
        if value and _allowed(key):
            os.environ[key] = value


def update(changes: dict[str, str | None]) -> None:
    """Set (or, with an empty value, remove) prefs, then reload every cached client."""
    bad = [k for k in changes if not _allowed(k)]
    if bad:
        raise PrefsError(f"Not a setting the app can change: {', '.join(bad)}")
    data = _load()
    kr = _keyring()
    values, secrets, secret_keys = data.setdefault("values", {}), data.setdefault("secrets", {}), set(data.get("secret_keys", []))
    for key, value in changes.items():
        value = (value or "").strip()
        if is_secret(key):
            if value:
                if kr:
                    kr.set_password(SERVICE, key, value)
                    secrets.pop(key, None)
                else:
                    secrets[key] = value
                secret_keys.add(key)
            else:
                if kr:
                    try:
                        kr.delete_password(SERVICE, key)
                    except Exception:  # noqa: BLE001 - nothing stored
                        pass
                secrets.pop(key, None)
                secret_keys.discard(key)
        elif value:
            values[key] = value
        else:
            values.pop(key, None)
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
    data["secret_keys"] = sorted(secret_keys)
    _save(data)
    reload_clients()


def reload_clients() -> None:
    from app.config import get_settings
    from app.embeddings import chain
    from app.keiro import client
    from app.llm import registry

    get_settings.cache_clear()
    registry._router = None
    chain._chain = None
    client._client = None


def describe() -> dict:
    """Current values for the settings screen. Secrets are reported as set/unset only."""
    from app.config import KNOWN_PROVIDERS

    keys = set(_PLAIN) | {f"{p.upper()}_MODELS" for p in KNOWN_PROVIDERS} | {f"{p.upper()}_API_KEY" for p in KNOWN_PROVIDERS}
    keys |= {"KEIROLABS_SEARCH_LITE_API_KEY", "KEIROLABS_API_KEY"}
    out: dict[str, dict] = {}
    for key in sorted(keys):
        value = os.getenv(key, "")
        if is_secret(key):
            out[key] = {"secret": True, "set": bool(value), "hint": f"…{value[-4:]}" if len(value) > 8 else ""}
        else:
            out[key] = {"secret": False, "value": value}
    return out
