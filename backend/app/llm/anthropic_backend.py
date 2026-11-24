"""Claude via the official Anthropic SDK."""
from __future__ import annotations

from typing import AsyncIterator

import anthropic

from app.config import ProviderConfig
from app.llm.base import ChatMessage, LLMError


class AnthropicBackend:
    provider = "anthropic"

    def __init__(self, cfg: ProviderConfig):
        self.client = anthropic.AsyncAnthropic(api_key=cfg.api_key, max_retries=1)

    @staticmethod
    def _split(messages: list[ChatMessage]) -> tuple[str, list[dict]]:
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        rest = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
        return system, rest

    async def complete(self, model: str, messages: list[ChatMessage], *, temperature: float = 0.2, max_tokens: int = 1024) -> str:
        system, rest = self._split(messages)
        try:
            resp = await self.client.messages.create(
                model=model, max_tokens=max_tokens, temperature=temperature, system=system or anthropic.NOT_GIVEN, messages=rest
            )
        except anthropic.APIError as exc:
            raise LLMError(str(exc)[:300]) from exc
        text = "".join(block.text for block in resp.content if block.type == "text")
        if not text.strip():
            raise LLMError("Empty completion")
        return text

    async def stream(
        self, model: str, messages: list[ChatMessage], *, temperature: float = 0.2, max_tokens: int = 1024
    ) -> AsyncIterator[str]:
        system, rest = self._split(messages)
        try:
            async with self.client.messages.stream(
                model=model, max_tokens=max_tokens, temperature=temperature, system=system or anthropic.NOT_GIVEN, messages=rest
            ) as stream:
                async for text in stream.text_stream:
                    yield text
        except anthropic.APIError as exc:
            raise LLMError(str(exc)[:300]) from exc
