"""Runtime configuration.

Everything is read from environment variables (and a `.env` file if present).
Provider settings are discovered dynamically: for a provider named `X` the app
reads `X_API_KEY`, `X_MODELS` (comma separated) and optionally `X_BASE_URL`.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the repo root (one level above backend/) or the CWD.
for _candidate in (Path(__file__).resolve().parents[2] / ".env", Path.cwd() / ".env"):
    if _candidate.exists():
        load_dotenv(_candidate, override=False)
        break

# OpenAI-compatible chat/embedding endpoints. Anthropic is handled natively.
PROVIDER_BASE_URLS: dict[str, str] = {
    "openrouter": "https://openrouter.ai/api/v1",
    "groqcloud": "https://api.groq.com/openai/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "openai": "https://api.openai.com/v1",
    "mistral": "https://api.mistral.ai/v1",
    "together": "https://api.together.xyz/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "ollama": "http://127.0.0.1:11434/v1",
    "llamacpp": "http://127.0.0.1:8081/v1",
    "anthropic": "https://api.anthropic.com",
}
KNOWN_PROVIDERS = tuple(PROVIDER_BASE_URLS)
KEYLESS_PROVIDERS = {"ollama", "llamacpp"}
# Providers that run on this machine and keep working without internet.
LOCAL_PROVIDERS = ("ollama", "llamacpp")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip().strip('"').strip("'")


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _paid_only(models: list[str]) -> list[str]:
    """Drop OpenRouter `:free` variants: they are rate-limited and get withdrawn without notice."""
    kept = [m for m in models if not m.endswith(":free")]
    for m in set(models) - set(kept):
        logging.getLogger(__name__).warning("Ignoring free-tier model %s (paid models only)", m)
    return kept


def _bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    return default if not raw else raw.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    base_url: str
    api_key: str
    models: list[str]

    @property
    def configured(self) -> bool:
        return bool(self.models) and (bool(self.api_key) or self.name in KEYLESS_PROVIDERS)


@dataclass(frozen=True)
class EmbedModelSpec:
    """One link in the embedding fallback chain."""

    provider: str
    model: str
    dim: int

    @property
    def vector_name(self) -> str:
        """Qdrant named-vector key, stable per provider+model."""
        return re.sub(r"[^a-z0-9]+", "_", f"{self.provider}_{self.model}".lower()).strip("_")

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"


def provider_config(name: str) -> ProviderConfig:
    upper = name.upper()
    base = _env(f"{upper}_BASE_URL") or PROVIDER_BASE_URLS.get(name, "")
    if name == "ollama":
        base = (_env("OLLAMA_BASE_URL") or "http://127.0.0.1:11434").rstrip("/")
        base = base if base.endswith("/v1") else base + "/v1"
    return ProviderConfig(
        name=name,
        base_url=base.rstrip("/"),
        api_key=_env(f"{upper}_API_KEY"),
        models=_paid_only(_csv(_env(f"{upper}_MODELS")) or _local_default_models(name)),
    )


_installed_cache: dict = {"at": 0.0, "models": []}


def _installed_chat_model() -> list[str]:
    """With no model picked, use the largest chat model Ollama already has, so local-first still means local.
    Checked at most once a minute, with a one-second limit; any failure just means "none"."""
    import json as _json
    import time as _time
    import urllib.request

    if _env("KEIROLAKE_NO_LOCAL_FALLBACK") == "1":
        return []
    if _time.monotonic() - _installed_cache["at"] < 60:
        return _installed_cache["models"]
    models: list[str] = []
    try:
        base = (_env("OLLAMA_BASE_URL") or "http://127.0.0.1:11434").rstrip("/").removesuffix("/v1")
        with urllib.request.urlopen(base + "/api/tags", timeout=1) as resp:  # noqa: S310 - the local Ollama address
            tags = _json.loads(resp.read()).get("models", [])
        chat = [m for m in tags if "embed" not in m.get("name", "") and "bert" not in m.get("details", {}).get("family", "")]
        chat.sort(key=lambda m: m.get("size", 0), reverse=True)
        models = [chat[0]["name"]] if chat else []
    except Exception:  # noqa: BLE001 - Ollama not running
        models = []
    _installed_cache.update(at=_time.monotonic(), models=models)
    return models


def _local_default_models(name: str) -> list[str]:
    """Local runtimes use the model picked in the app (LOCAL_LLM_MODEL), else an installed Ollama chat model."""
    if name == "ollama":
        return _csv(_env("LOCAL_LLM_MODEL")) or _installed_chat_model()
    if name == "llamacpp" and _env("LLAMACPP_MODEL_PATH"):
        return ["local"]
    return []


def parse_embed_models(raw: str) -> list[EmbedModelSpec]:
    """Parse `[provider:]model:dim` entries.

    The dimension is always the part after the last colon, so OpenRouter ids
    that contain colons still work. `:free` variants are skipped.
    """
    specs: list[EmbedModelSpec] = []
    for entry in _csv(raw):
        head, _, dim = entry.rpartition(":")
        if not _paid_only([head]):
            continue
        if not head or not dim.isdigit():
            raise ValueError(f"EMBED_MODELS entry {entry!r} must end with ':<dimension>'")
        provider, sep, rest = head.partition(":")
        if sep and provider in KNOWN_PROVIDERS:
            specs.append(EmbedModelSpec(provider, rest, int(dim)))
        else:
            specs.append(EmbedModelSpec("openrouter", head, int(dim)))
    return specs


DEFAULT_EMBED_MODELS = (
    "baai/bge-m3:1024,"
    "qwen/qwen3-embedding-8b:1024,"
    "intfloat/multilingual-e5-large:1024,"
    "baai/bge-large-en-v1.5:1024"
)


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    sqlite_path: Path
    qdrant_url: str
    qdrant_path: str | None
    qdrant_api_key: str | None
    llm_fallback_order: list[str]
    embed_models: list[EmbedModelSpec]
    embed_dual_write: bool
    embed_batch_size: int
    keiro_lite_key: str
    keiro_key: str
    keiro_base_url: str
    chunk_size: int
    chunk_overlap: int
    rag_top_k: int
    rag_web_fallback: bool
    rag_web_threshold: float
    max_upload_mb: int
    rag_min_cosine: float = 0.30  # local hits below this similarity are not used as sources
    llm_mode: str = "local-first"  # local-first | local | cloud | auto (cloud first)
    cloud_daily_limit: int = 300  # cloud model calls per day; 0 = no limit
    keiro_zdr: bool = True  # ask KeiroLabs not to retain queries or results
    keiro_cache: bool = True  # keep KeiroLabs answers on this PC for 15 minutes
    local_embed_model: str = "qwen3-embedding:0.6b"
    local_embed_dim: int = 1024
    wiki_embed_model: str = "qwen3-embedding:0.6b"  # Ollama model for offline-library indexes
    wiki_embed_dim: int = 256
    token: str = ""  # per-launch API token set by the desktop shell
    cors_origins: list[str] = field(default_factory=list)

    @property
    def lake_dir(self) -> Path:
        return self.data_dir / "lake"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def keiro_api_key(self) -> str:
        return self.keiro_key or self.keiro_lite_key


LLM_MODES = ("local-first", "local", "cloud", "auto")


def offline_mode() -> bool:
    """Local models only, no web, no scheduled collection. EMERGENCY_MODE is the old name."""
    return _bool("OFFLINE_MODE") or _bool("EMERGENCY_MODE")


def _llm_mode() -> str:
    if offline_mode():
        return "local"
    mode = (_env("LLM_MODE") or "local-first").lower()
    return mode if mode in LLM_MODES else "local-first"


def _build_embed_chain() -> list[EmbedModelSpec]:
    chain = parse_embed_models(_env("EMBED_MODELS") or DEFAULT_EMBED_MODELS)
    mode = _llm_mode()
    if mode in ("local-first", "local"):
        local = EmbedModelSpec("ollama", _env("LOCAL_EMBED_MODEL") or _env("WIKI_EMBED_MODEL") or "qwen3-embedding:0.6b",
                               int(_env("LOCAL_EMBED_DIM") or 1024))
        cloud = [c for c in chain if c.provider not in LOCAL_PROVIDERS and c.label != local.label]
        # Cloud models stay in the chain so existing vectors remain searchable, but local mode never embeds with them.
        return [local] if mode == "local" else [local, *cloud]
    if _bool("EMBED_GEMINI_ENABLED"):
        model = _env("EMBED_GEMINI_MODEL") or "gemini-embedding-001"
        dim = int(_env("EMBED_GEMINI_DIM") or 768)
        if (_env("EMBED_GEMINI_VIA") or "openrouter").lower() == "native":
            gemini = EmbedModelSpec("gemini", model, dim)
        else:
            gemini = EmbedModelSpec("openrouter", f"google/{model}", dim)
        position = _env("EMBED_GEMINI_POSITION") or "last"
        chain = [gemini, *chain] if position == "first" else [*chain, gemini]
    return chain


@lru_cache
def get_settings() -> Settings:
    data_dir = Path(_env("DATA_DIR") or Path(__file__).resolve().parents[2] / "data")
    return Settings(
        data_dir=data_dir,
        sqlite_path=Path(_env("SQLITE_PATH") or data_dir / "keirolake.db"),
        qdrant_url=_env("QDRANT_URL") or "http://localhost:6333",
        qdrant_path=_env("QDRANT_PATH") or None,
        qdrant_api_key=_env("QDRANT_API_KEY") or None,
        llm_fallback_order=_csv(_env("LLM_FALLBACK_ORDER") or "openrouter,groqcloud"),
        embed_models=_build_embed_chain(),
        embed_dual_write=_bool("EMBED_DUAL_WRITE"),
        embed_batch_size=int(_env("EMBED_BATCH_SIZE") or 64),
        keiro_lite_key=_env("KEIROLABS_SEARCH_LITE_API_KEY"),
        keiro_key=_env("KEIROLABS_API_KEY"),
        keiro_base_url=(_env("KEIROLABS_BASE_URL") or "https://api.keirolabs.cloud/api/v2").rstrip("/"),
        chunk_size=int(_env("CHUNK_SIZE") or 1500),
        chunk_overlap=int(_env("CHUNK_OVERLAP") or 200),
        rag_top_k=int(_env("RAG_TOP_K") or 8),
        rag_web_fallback=_bool("RAG_WEB_FALLBACK", True),
        # Best cosine similarity below which local context counts as "weak".
        rag_web_threshold=float(_env("RAG_WEB_THRESHOLD") or 0.45),
        rag_min_cosine=float(_env("RAG_MIN_COSINE") or 0.30),
        max_upload_mb=int(_env("MAX_UPLOAD_MB") or 100),
        llm_mode=_llm_mode(),
        cloud_daily_limit=int(_env("CLOUD_DAILY_LIMIT") or 300),
        keiro_zdr=_bool("KEIRO_ZDR", True),
        keiro_cache=_bool("KEIRO_CACHE", True),
        local_embed_model=_env("LOCAL_EMBED_MODEL") or _env("WIKI_EMBED_MODEL") or "qwen3-embedding:0.6b",
        local_embed_dim=int(_env("LOCAL_EMBED_DIM") or 1024),
        wiki_embed_model=_env("WIKI_EMBED_MODEL") or "qwen3-embedding:0.6b",
        wiki_embed_dim=int(_env("WIKI_EMBED_DIM") or 256),
        token=_env("KEIROLAKE_TOKEN"),
        cors_origins=_csv(_env("CORS_ORIGINS") or "http://localhost:3000,http://localhost:5173")
        + ["tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"],
    )
