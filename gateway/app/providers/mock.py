"""Mock provider adapter for testing, load tests, and local simulation."""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator

import httpx

from app.core.config import settings
from app.providers.base import (
    BaseProvider,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
)

logger = logging.getLogger(__name__)


class MockProvider(BaseProvider):
    """Adapter for the mock provider service with standalone fallback."""

    name = "mock"

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or settings.mock_provider_url).rstrip("/")
        self._client = httpx.AsyncClient(timeout=10.0)

    async def send(self, request: ProviderRequest) -> ProviderResponse:
        """Send chat request to the mock provider service or simulate locally for mock models."""
        payload = {
            "model": request.model,
            "messages": request.messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": False,
        }

        try:
            url = f"{self.base_url}/v1/chat/completions"
            response = await self._client.post(url, json=payload)
            if response.status_code >= 400:
                body = (
                    response.json()
                    if response.headers.get("content-type", "").startswith("application/json")
                    else {"message": response.text}
                )
                msg_text = body.get("message")
                raise ProviderError(
                    status_code=response.status_code,
                    message=f"MockProvider service error {response.status_code}: {msg_text}",
                    retryable=response.status_code >= 500,
                    raw=body,
                )

            data = response.json()
            choice = data.get("choices", [{}])[0]
            message = choice.get("message", {})
            usage = data.get("usage", {})

            return ProviderResponse(
                content=message.get("content", ""),
                model=data.get("model", request.model),
                prompt_tokens=usage.get("prompt_tokens", 10),
                completion_tokens=usage.get("completion_tokens", 8),
                finish_reason=choice.get("finish_reason", "stop"),
                raw=data,
            )
        except Exception as e:
            if isinstance(e, ProviderError):
                raise

            # Only allow local simulation if the request explicitly targets mock or test simulation
            is_mock_target = (
                "mock" in request.model.lower()
                or request.extra.get("simulate", False)
                or settings.app_env in ("testing", "test")
            )
            if is_mock_target:
                logger.info("Mock service unreachable (%s), simulating locally for mock target", e)
                await asyncio.sleep(0.02)
                prompt_tokens = sum(len(m.get("content", "").split()) for m in request.messages)
                content = f"Mock response for prompt to model '{request.model}'."
                completion_tokens = len(content.split())

                return ProviderResponse(
                    content=content,
                    model=request.model,
                    prompt_tokens=max(1, prompt_tokens),
                    completion_tokens=max(1, completion_tokens),
                    finish_reason="stop",
                    raw={"simulated": True},
                )

            logger.error("Mock service unreachable (%s) and target is not a mock model", e)
            raise ProviderError(
                status_code=502,
                message=f"Mock service unreachable at {self.base_url}: {e}",
                retryable=True,
            ) from e

    async def stream(self, request: ProviderRequest) -> AsyncGenerator[str, None]:
        """Stream SSE chunks from mock provider or simulated generator."""
        payload = {
            "model": request.model,
            "messages": request.messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": True,
        }

        try:
            url = f"{self.base_url}/v1/chat/completions"
            async with self._client.stream("POST", url, json=payload) as response:
                if response.status_code >= 400:
                    raise ProviderError(
                        status_code=response.status_code,
                        message=f"MockProvider stream error: {response.status_code}",
                        retryable=response.status_code >= 500,
                    )
                async for line in response.aiter_lines():
                    if line.strip():
                        yield line
        except Exception as e:
            if isinstance(e, ProviderError):
                raise

            is_mock_target = (
                "mock" in request.model.lower()
                or request.extra.get("simulate", False)
                or settings.app_env in ("testing", "test")
            )
            if is_mock_target:
                words = f"This is a simulated streamed response for model {request.model}.".split()
                req_id = f"chatcmpl-mock-{uuid.uuid4().hex[:8]}"
                for i, word in enumerate(words):
                    chunk = {
                        "id": req_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": request.model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": word + " "},
                                "finish_reason": None if i < len(words) - 1 else "stop",
                            }
                        ],
                    }
                    yield f"data: {json.dumps(chunk)}"
                    await asyncio.sleep(0.02)
                yield "data: [DONE]"
            else:
                raise ProviderError(
                    status_code=502,
                    message=f"Mock stream service unreachable: {e}",
                    retryable=True,
                ) from e
