from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol


@dataclass
class ChatMessage:
    role: str  # "system" | "user" | "assistant"
    content: str


class LLMError(Exception):
    """A provider/model failed in a way that should trigger fallback."""


class ChatBackend(Protocol):
    provider: str

    async def complete(self, model: str, messages: list[ChatMessage], *, temperature: float, max_tokens: int) -> str: ...

    def stream(
        self, model: str, messages: list[ChatMessage], *, temperature: float, max_tokens: int
    ) -> AsyncIterator[str]: ...
