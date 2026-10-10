"""OpenRouter provider adapter."""

import logging
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from app.core.config import settings
from app.providers.base import (
    BaseProvider,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
)

logger = logging.getLogger(__name__)


class OpenRouterProvider(BaseProvider):
    """Adapter for OpenRouter API."""

    name = "openrouter"

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self.api_key = api_key or settings.openrouter_api_key
        self.base_url = (base_url or settings.openrouter_base_url).rstrip("/")
        self._client = httpx.AsyncClient(timeout=60.0)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/routewise/gateway",
            "X-Title": "Routewise LLM Gateway",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def send(self, request: ProviderRequest) -> ProviderResponse:
        """Send chat completion request to OpenRouter."""
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": request.messages,
            "temperature": request.temperature,
            "stream": False,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        url = f"{self.base_url}/chat/completions"
        try:
            response = await self._client.post(url, headers=self._headers(), json=payload)
        except httpx.TimeoutException as e:
            raise ProviderError(
                status_code=504,
                message=f"OpenRouter request timed out: {e or 'Timeout'}",
                retryable=True,
            ) from e
        except httpx.TransportError as e:
            raise ProviderError(
                status_code=503,
                message=f"OpenRouter transport error: {e or 'Transport error'}",
                retryable=True,
            ) from e

        if response.status_code >= 400:
            try:
                body = response.json()
            except Exception:
                body = {"message": response.text}
            error = self.normalize_error(response.status_code, body)
            raise error

        data = response.json()
        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        usage = data.get("usage", {})

        return ProviderResponse(
            content=message.get("content", ""),
            model=data.get("model", request.model),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            finish_reason=choice.get("finish_reason", "stop"),
            raw=data,
        )

    async def stream(self, request: ProviderRequest) -> AsyncGenerator[str, None]:
        """Stream SSE chunks from OpenRouter."""
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": request.messages,
            "temperature": request.temperature,
            "stream": True,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        url = f"{self.base_url}/chat/completions"
        try:
            async with self._client.stream(
                "POST", url, headers=self._headers(), json=payload
            ) as response:
                if response.status_code >= 400:
                    text = await response.aread()
                    msg = text.decode("utf-8", errors="ignore")
                    raise ProviderError(
                        status_code=response.status_code,
                        message=f"OpenRouter stream error {response.status_code}: {msg}",
                        retryable=response.status_code in (429, 500, 502, 503, 504),
                        raw={"message": msg},
                    )

                async for line in response.aiter_lines():
                    if line.strip():
                        yield line
        except httpx.TimeoutException as e:
            raise ProviderError(
                status_code=504,
                message=f"OpenRouter streaming timed out: {e or 'Timeout'}",
                retryable=True,
            ) from e
        except httpx.TransportError as e:
            raise ProviderError(
                status_code=503,
                message=f"OpenRouter streaming transport error: {e or 'Transport error'}",
                retryable=True,
            ) from e

    def normalize_error(self, status_code: int, body: dict[str, Any]) -> ProviderError:
        error_info = body.get("error", {})
        message = (
            error_info.get("message")
            if isinstance(error_info, dict)
            else str(body.get("message", "OpenRouter Error"))
        )
        retryable = status_code in (429, 500, 502, 503, 504)
        return ProviderError(
            status_code=status_code,
            message=message or "Unknown OpenRouter error",
            retryable=retryable,
            raw=body,
        )

    async def close(self) -> None:
        await self._client.aclose()
