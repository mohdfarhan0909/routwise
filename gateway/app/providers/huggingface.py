"""Hugging Face LLM Provider adapter using Hugging Face Router endpoint."""

from collections.abc import AsyncGenerator
from typing import Any

import httpx

from app.core.config import settings
from app.providers.base import BaseProvider, ProviderError, ProviderRequest, ProviderResponse


class HuggingFaceProvider(BaseProvider):
    """Adapter for Hugging Face hosted models via the OpenAI-compatible router endpoint."""

    name = "huggingface"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.api_key = api_key or settings.hf_api_key
        self.base_url = (base_url or settings.hf_base_url).rstrip("/")
        self._client = httpx.AsyncClient(timeout=45.0)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def send(self, request: ProviderRequest) -> ProviderResponse:
        """Send chat completion request to Hugging Face Router."""
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
                message=f"Hugging Face request timed out: {e or 'Timeout'}",
                retryable=True,
            ) from e
        except httpx.TransportError as e:
            raise ProviderError(
                status_code=503,
                message=f"Hugging Face transport error: {e or 'Transport error'}",
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
        """Stream SSE chunks from Hugging Face Router."""
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
                        message=f"Hugging Face stream error {response.status_code}: {msg}",
                        retryable=response.status_code in (429, 500, 502, 503, 504),
                        raw={"message": msg},
                    )

                async for line in response.aiter_lines():
                    if line.strip():
                        yield line
        except httpx.TimeoutException as e:
            raise ProviderError(
                status_code=504,
                message=f"Hugging Face streaming timed out: {e or 'Timeout'}",
                retryable=True,
            ) from e
        except httpx.TransportError as e:
            raise ProviderError(
                status_code=503,
                message=f"Hugging Face streaming transport error: {e or 'Transport error'}",
                retryable=True,
            ) from e

    def normalize_error(self, status_code: int, body: dict[str, Any]) -> ProviderError:
        error_info = body.get("error", {})
        message = (
            error_info.get("message")
            if isinstance(error_info, dict)
            else str(body.get("message", "Hugging Face Error"))
        )
        retryable = status_code in (429, 500, 502, 503, 504)
        return ProviderError(
            status_code=status_code,
            message=message or "Unknown Hugging Face error",
            retryable=retryable,
            raw=body,
        )

    async def close(self) -> None:
        await self._client.aclose()
