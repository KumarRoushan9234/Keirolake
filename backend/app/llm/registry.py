"""LLM routing with ordered fallback.

Attempt order = for each provider in LLM_FALLBACK_ORDER, each model in
<PROVIDER>_MODELS. A model that fails is put on a short cooldown so the next
request skips straight to a healthy one.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator

from app import usage
from app.config import LOCAL_PROVIDERS, get_settings, provider_config
from app.llm.anthropic_backend import AnthropicBackend
from app.llm.base import ChatBackend, ChatMessage, LLMError
from app.llm.openai_compat import OpenAICompatBackend

log = logging.getLogger(__name__)
COOLDOWN_SECONDS = 60.0


@dataclass
class Attempt:
    provider: str
    model: str
    backend: ChatBackend


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    errors: list[str]


class AllModelsFailed(LLMError):
    def __init__(self, errors: list[str]):
        super().__init__("All LLM providers failed:\n" + "\n".join(errors) if errors else "No LLM provider is configured")
        self.errors = errors


def _backend_for(name: str) -> ChatBackend | None:
    cfg = provider_config(name)
    if not cfg.configured:
        return None
    return AnthropicBackend(cfg) if name == "anthropic" else OpenAICompatBackend(cfg)


def effective_order(base: list[str], mode: str, online: bool) -> list[str]:
    """Which providers to try, given the model mode and the network.

    local-first (default): models on this machine first, cloud only when they fail.
    local: only models running on this machine. cloud: only remote providers.
    auto: remote providers first while online, then local ones; local only when offline.
    """
    local = [p for p in base if p in LOCAL_PROVIDERS] + [p for p in LOCAL_PROVIDERS if p not in base]
    cloud = [p for p in base if p not in LOCAL_PROVIDERS]
    if mode == "local":
        return local
    if mode == "cloud":
        return cloud
    if mode == "auto":
        return cloud + local if online else local
    return local + cloud if online else local


class LLMRouter:
    def __init__(self, order: list[str] | None = None):
        self.base_order = order or get_settings().llm_fallback_order
        self._cooldown: dict[tuple[str, str], float] = {}

    @property
    def order(self) -> list[str]:
        from app.net import connectivity

        return effective_order(self.base_order, get_settings().llm_mode, connectivity.is_online)

    def attempts(self, include_cooling: bool = False, local_only: bool = False) -> list[Attempt]:
        out: list[Attempt] = []
        now = time.monotonic()
        order = effective_order(self.base_order, "local", True) if local_only else self.order
        if not usage.cloud_allowed():
            order = [p for p in order if p in LOCAL_PROVIDERS]
        for name in order:
            backend = _backend_for(name)
            if backend is None:
                continue
            for model in provider_config(name).models:
                if not include_cooling and self._cooldown.get((name, model), 0) > now:
                    continue
                out.append(Attempt(name, model, backend))
        # If everything is cooling down, try them all anyway rather than fail.
        return out or ([] if include_cooling else self.attempts(include_cooling=True, local_only=local_only))

    @staticmethod
    def _count(attempt: Attempt) -> None:
        usage.add("local_calls" if attempt.provider in LOCAL_PROVIDERS else "cloud_calls")

    def _fail(self, attempt: Attempt, exc: Exception, errors: list[str]) -> None:
        msg = f"{attempt.provider}:{attempt.model} -> {exc}"
        log.warning("LLM fallback: %s", msg)
        errors.append(msg)
        self._cooldown[(attempt.provider, attempt.model)] = time.monotonic() + COOLDOWN_SECONDS

    async def complete(self, messages: list[ChatMessage], *, temperature: float = 0.2, max_tokens: int = 1024,
                       local_only: bool = False) -> LLMResult:
        errors: list[str] = []
        for attempt in self.attempts(local_only=local_only):
            try:
                self._count(attempt)
                text = await attempt.backend.complete(attempt.model, messages, temperature=temperature, max_tokens=max_tokens)
                return LLMResult(text, attempt.provider, attempt.model, errors)
            except LLMError as exc:
                self._fail(attempt, exc, errors)
        raise AllModelsFailed(errors or [self._nothing_to_try(local_only)])

    def _nothing_to_try(self, local_only: bool = False) -> str:
        if local_only:
            return "Private notes are only sent to models on this computer, and none is ready. Pick and start one on the Models page."
        if not usage.cloud_allowed():
            return ("Today's cloud limit is used up and no local model answered. Start a local model on the Models page, "
                    "or raise the daily cloud limit in Settings.")
        mode = get_settings().llm_mode
        if mode == "local" or self.order == [p for p in self.order if p in LOCAL_PROVIDERS]:
            return "No local model is set up. Pick one on the Models page (and start Ollama)."
        return "No model provider is configured for this mode."

    async def stream(
        self, messages: list[ChatMessage], *, temperature: float = 0.2, max_tokens: int = 1024, local_only: bool = False
    ) -> AsyncIterator[tuple[str, str] | str]:
        """Yield ("meta", "provider:model") once, then text deltas.

        Fallback only happens before the first token; a mid-stream failure is
        surfaced to the caller because partial output was already sent.
        """
        errors: list[str] = []
        for attempt in self.attempts(local_only=local_only):
            started = False
            try:
                self._count(attempt)
                async for delta in attempt.backend.stream(attempt.model, messages, temperature=temperature, max_tokens=max_tokens):
                    if not started:
                        started = True
                        yield ("meta", f"{attempt.provider}:{attempt.model}")
                    yield delta
                if started:
                    return
                raise LLMError("Empty stream")
            except LLMError as exc:
                if started:
                    raise
                self._fail(attempt, exc, errors)
        raise AllModelsFailed(errors or [self._nothing_to_try(local_only)])

    async def probe(self) -> list[dict]:
        """Send a tiny prompt to every configured model."""
        results = []
        ping = [ChatMessage("user", "Reply with the single word: ok")]
        for attempt in self.attempts(include_cooling=True):
            started = time.perf_counter()
            try:
                await attempt.backend.complete(attempt.model, ping, temperature=0, max_tokens=256)
                results.append({"provider": attempt.provider, "model": attempt.model, "ok": True,
                                "latency_ms": round((time.perf_counter() - started) * 1000)})
            except LLMError as exc:
                results.append({"provider": attempt.provider, "model": attempt.model, "ok": False, "error": str(exc)})
        return results

    def describe(self) -> list[dict]:
        rows = []
        for name in self.order:
            cfg = provider_config(name)
            rows.append({"provider": name, "configured": cfg.configured, "has_key": bool(cfg.api_key), "models": cfg.models})
        return rows


_router: LLMRouter | None = None


def get_router() -> LLMRouter:
    global _router
    if _router is None:
        _router = LLMRouter()
    return _router
