"""Abstract base class for LLM provider adapters.

Each provider implements this interface so adding a new provider means
adding one file. This is the vendor-lock-in escape hatch.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProviderRequest:
    """Normalized request sent to a provider."""

    model: str
    messages: list[dict[str, Any]]
    temperature: float = 1.0
    max_tokens: int | None = None
    stream: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderResponse:
    """Normalized response from a provider."""

    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str = "stop"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderError(Exception):
    """Normalized error from a provider inheriting from Exception for clean raising."""

    status_code: int
    message: str
    retryable: bool
    raw: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"ProviderError(status_code={self.status_code}, message='{self.message}')"


class BaseProvider(ABC):
    """Interface that every LLM provider adapter must implement."""

    name: str

    @abstractmethod
    async def send(self, request: ProviderRequest) -> ProviderResponse:
        """Send a non-streaming request and return the full response."""
        ...

    @abstractmethod
    def stream(self, request: ProviderRequest) -> AsyncGenerator[str, None]:
        """Send a streaming request and yield SSE data chunks.

        Each yielded string is a complete SSE `data: ...` line.
        """
        ...

    async def close(self) -> None:
        """Close underlying HTTP client connections. Override if needed."""
        return None
