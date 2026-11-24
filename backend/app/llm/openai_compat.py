"""One adapter for every OpenAI-compatible chat API.

Covers OpenRouter, GroqCloud, DeepSeek, OpenAI, Mistral, Together, Gemini's
OpenAI endpoint and Ollama's /v1 endpoint.
"""
from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from app import http

from app.config import ProviderConfig
from app.llm.base import ChatMessage, LLMError


class OpenAICompatBackend:
    def __init__(self, cfg: ProviderConfig, timeout: float = 120.0):
        self.provider = cfg.name
        self.cfg = cfg
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        if self.provider == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/keirolake/keirolake"
            headers["X-Title"] = "KeiroLake"
        return headers

    def _body(self, model: str, messages: list[ChatMessage], temperature: float, max_tokens: int, stream: bool) -> dict:
        body = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        # Reasoning models spend max_tokens on hidden thinking and can return an empty answer.
        if "gpt-oss" in model:
            body["reasoning_effort"] = "low"
        elif self.provider == "openrouter":
            # Grounded answers don't need thinking; flash models otherwise burn the budget on it.
            body["reasoning"] = {"enabled": False}
        return body

    @staticmethod
    def _raise_for(resp: httpx.Response, body_text: str) -> None:
        if resp.status_code >= 400:
            try:
                err = json.loads(body_text).get("error", body_text)
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            except ValueError:
                msg = body_text
            raise LLMError(f"HTTP {resp.status_code}: {msg[:300]}")

    async def complete(self, model: str, messages: list[ChatMessage], *, temperature: float = 0.2, max_tokens: int = 1024) -> str:
        url = f"{self.cfg.base_url}/chat/completions"
        try:
            resp = await http.client("llm").post(url, headers=self._headers(), json=self._body(model, messages, temperature, max_tokens, False),
                                                 timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc
        self._raise_for(resp, resp.text)
        data = resp.json()
        try:
            content = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError) as exc:
            raise LLMError(f"Malformed response: {str(data)[:200]}") from exc
        if not content.strip():
            raise LLMError("Empty completion")
        return content

    async def stream(
        self, model: str, messages: list[ChatMessage], *, temperature: float = 0.2, max_tokens: int = 1024
    ) -> AsyncIterator[str]:
        url = f"{self.cfg.base_url}/chat/completions"
        try:
            async with http.client("llm").stream(
                "POST", url, headers=self._headers(), json=self._body(model, messages, temperature, max_tokens, True), timeout=self.timeout
            ) as resp:
                if resp.status_code >= 400:
                    self._raise_for(resp, (await resp.aread()).decode(errors="replace"))
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except ValueError:
                        continue
                    if "error" in chunk:
                        raise LLMError(str(chunk["error"])[:300])
                    choices = chunk.get("choices") or []
                    delta = choices[0].get("delta", {}).get("content") if choices else None
                    if delta:
                        yield delta
        except httpx.HTTPError as exc:
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc
