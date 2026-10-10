"""Provider router with retries, exponential backoff, jitter, circuit breakers, and fallback."""

import asyncio
import copy
import logging
import random
from collections.abc import AsyncGenerator

import httpx

from app.core.circuit_breaker import CircuitBreaker, CircuitState
from app.core.config import settings
from app.metrics.prometheus import CIRCUIT_STATE
from app.providers.base import BaseProvider, ProviderError, ProviderRequest, ProviderResponse
from app.providers.gemini import GeminiProvider
from app.providers.groq import GroqProvider
from app.providers.huggingface import HuggingFaceProvider
from app.providers.mock import MockProvider
from app.providers.openrouter import OpenRouterProvider

logger = logging.getLogger(__name__)

# Model namespace translations per provider
PROVIDER_MODEL_MAP: dict[str, dict[str, str]] = {
    "openrouter": {
        "gpt-4o-mini": "openai/gpt-4o-mini",
        "gpt-4o": "openai/gpt-4o",
        "claude-3-haiku": "anthropic/claude-3-haiku",
        "llama-3.1-8b": "meta-llama/llama-3.1-8b-instruct",
        "llama-3.3-70b": "meta-llama/llama-3.3-70b-instruct",
        "gemini-flash": "google/gemini-2.0-flash-001",
        "gemini-pro": "google/gemini-pro-1.5",
    },
    "gemini": {
        "gpt-4o-mini": "gemini-3.8-flash",
        "gemini-flash": "gemini-3.8-flash",
        "gemini-1.5-flash": "gemini-3.8-flash",
        "gemini-2.5-flash": "gemini-3.8-flash",
        "gemini-pro": "gemini-3.8-flash",
    },
    "huggingface": {
        "gpt-4o-mini": "deepseek-ai/DeepSeek-R1",
        "deepseek-r1": "deepseek-ai/DeepSeek-R1",
        "deepseek-v3": "deepseek-ai/DeepSeek-V3",
        "llama-3.3-70b": "meta-llama/Llama-3.3-70B-Instruct",
    },
    "groq": {
        "gpt-4o-mini": "llama-3.1-8b-instant",  # Equivalent fast/low-cost fallback tier
        "llama-3.1-8b": "llama-3.1-8b-instant",
        "llama-3.3-70b": "llama-3.3-70b-versatile",
        "llama-3-8b": "llama-3.1-8b-instant",
        "mixtral": "mixtral-8x7b-32768",
        "mixtral-8x7b": "mixtral-8x7b-32768",
    },
}


class ProviderRouter:
    """Manages provider adapters, retries with backoff, circuit breakers, and automated fallback."""

    def __init__(self) -> None:
        self.providers: dict[str, BaseProvider] = {
            "openrouter": OpenRouterProvider(),
            "gemini": GeminiProvider(),
            "huggingface": HuggingFaceProvider(),
            "groq": GroqProvider(),
            "mock": MockProvider(),
        }

        self.circuit_breakers: dict[str, CircuitBreaker] = {
            name: CircuitBreaker(provider_name=name, failure_threshold=5, recovery_timeout=30.0)
            for name in self.providers
        }

    def _sync_circuit_metric(self, name: str, cb: CircuitBreaker) -> None:
        state_val = 0
        if cb.state == CircuitState.OPEN:
            state_val = 1
        elif cb.state == CircuitState.HALF_OPEN:
            state_val = 2
        CIRCUIT_STATE.labels(provider=name).set(state_val)

    def _translate_model_for_provider(self, provider_name: str, model: str) -> str:
        """Map canonical model identifiers to provider-specific names."""
        mapping = PROVIDER_MODEL_MAP.get(provider_name, {})
        model_lower = model.lower()
        if model_lower in mapping:
            return mapping[model_lower]
        for key, val in mapping.items():
            if key in model_lower:
                return val
        return model

    def _get_fallback_chain(self, requested_model: str) -> list[str]:
        """Determine order of providers to try."""
        chain = []
        if "mock" in requested_model.lower():
            return ["mock"]

        if settings.openrouter_api_key:
            chain.append("openrouter")
        if settings.gemini_api_key:
            chain.append("gemini")
        if settings.hf_api_key:
            chain.append("huggingface")
        if settings.groq_api_key:
            chain.append("groq")

        # Prioritize Gemini if gemini model explicitly requested
        if "gemini" in requested_model.lower() and "gemini" in chain:
            chain.remove("gemini")
            chain.insert(0, "gemini")

        # Prioritize HuggingFace if deepseek or hf requested
        if (
            "deepseek" in requested_model.lower() or "hf" in requested_model.lower()
        ) and "huggingface" in chain:
            chain.remove("huggingface")
            chain.insert(0, "huggingface")

        # Prioritize Groq if groq or llama model explicitly requested
        if (
            "groq" in requested_model.lower() or "llama" in requested_model.lower()
        ) and "groq" in chain:
            chain.remove("groq")
            chain.insert(0, "groq")

        # Only add mock provider as fallback if explicitly configured
        if settings.enable_mock_fallback and "mock" not in chain:
            chain.append("mock")

        return chain

    async def execute_with_fallback(
        self,
        request: ProviderRequest,
        max_retries: int = 2,
        base_delay: float = 0.2,
        max_delay: float = 2.0,
    ) -> tuple[str, ProviderResponse]:
        """Send request to providers in fallback order with full jitter and circuit breakers."""
        chain = self._get_fallback_chain(request.model)
        if not chain:
            raise RuntimeError(
                f"No healthy upstream providers configured for requested model '{request.model}'"
            )

        last_error: Exception | None = None

        for provider_name in chain:
            provider = self.providers.get(provider_name)
            cb = self.circuit_breakers.get(provider_name)
            if not provider or not cb:
                continue

            self._sync_circuit_metric(provider_name, cb)

            if not await cb.allow_request():
                logger.warning("Circuit breaker OPEN for provider '%s', skipping", provider_name)
                continue

            # Clone request and translate model identifier for this specific provider
            req_for_provider = copy.copy(request)
            req_for_provider.model = self._translate_model_for_provider(
                provider_name, request.model
            )

            probe_succeeded = False
            last_status_code: int | None = None
            try:
                # Retry loop with full jitter exponential backoff
                for attempt in range(max_retries + 1):
                    try:
                        logger.info(
                            "Routing request to provider '%s' (model: %s, attempt: %d)",
                            provider_name,
                            req_for_provider.model,
                            attempt + 1,
                        )
                        response = await provider.send(req_for_provider)
                        await cb.record_success()
                        self._sync_circuit_metric(provider_name, cb)
                        probe_succeeded = True
                        return provider_name, response
                    except Exception as e:
                        last_error = e
                        if isinstance(e, ProviderError):
                            last_status_code = e.status_code
                            is_retryable = e.retryable
                        elif isinstance(e, httpx.TimeoutException):
                            last_status_code = 504
                            is_retryable = True
                        elif isinstance(e, httpx.TransportError):
                            last_status_code = 503
                            is_retryable = True
                        else:
                            err_str = str(e).lower()
                            is_retryable = any(
                                code in err_str
                                for code in [
                                    "429",
                                    "500",
                                    "502",
                                    "503",
                                    "504",
                                    "timeout",
                                    "connection",
                                ]
                            )

                        if attempt < max_retries and is_retryable:
                            # Full jitter: uniform(0, min(max_delay, base_delay * 2^attempt))
                            backoff = random.uniform(0, min(max_delay, base_delay * (2**attempt)))
                            logger.warning(
                                "Provider '%s' retryable error (attempt %d/%d): %s. Backoff %.2fs",
                                provider_name,
                                attempt + 1,
                                max_retries + 1,
                                e,
                                backoff,
                            )
                            await asyncio.sleep(backoff)
                        else:
                            logger.warning(
                                "Provider '%s' failed attempt %d: %s",
                                provider_name,
                                attempt + 1,
                                e,
                            )
                            break

                # If all retries failed, record failure with status code to respect 4xx filters
                await cb.record_failure(status_code=last_status_code)
                self._sync_circuit_metric(provider_name, cb)

            finally:
                if not probe_succeeded:
                    await cb.release_probe()

        raise RuntimeError(f"All available providers failed. Last error: {last_error}")

    async def stream_with_fallback(
        self, request: ProviderRequest
    ) -> tuple[str, AsyncGenerator[str, None]]:
        """Stream response with fallback if primary provider cannot initiate stream."""
        chain = self._get_fallback_chain(request.model)
        if not chain:
            raise RuntimeError(
                f"No healthy upstream providers configured for requested model '{request.model}'"
            )

        last_error: Exception | None = None

        for provider_name in chain:
            provider = self.providers.get(provider_name)
            cb = self.circuit_breakers.get(provider_name)
            if not provider or not cb:
                continue

            self._sync_circuit_metric(provider_name, cb)

            if not await cb.allow_request():
                continue

            req_for_provider = copy.copy(request)
            req_for_provider.model = self._translate_model_for_provider(
                provider_name, request.model
            )

            probe_succeeded = False
            last_status_code: int | None = None
            try:
                stream_gen = provider.stream(req_for_provider)
                first_chunk = await stream_gen.__anext__()
                await cb.record_success()
                self._sync_circuit_metric(provider_name, cb)
                probe_succeeded = True

                async def _make_stream(
                    initial: str, gen: AsyncGenerator[str, None]
                ) -> AsyncGenerator[str, None]:
                    yield initial
                    async for chunk in gen:
                        yield chunk

                return provider_name, _make_stream(first_chunk, stream_gen)
            except StopAsyncIteration:
                await cb.record_success()
                probe_succeeded = True

                async def _empty_stream() -> AsyncGenerator[str, None]:
                    if False:
                        yield ""

                return provider_name, _empty_stream()
            except Exception as e:
                logger.warning("Streaming failed on provider '%s': %s", provider_name, e)
                last_error = e
                if isinstance(e, ProviderError):
                    last_status_code = e.status_code
                elif isinstance(e, httpx.TimeoutException):
                    last_status_code = 504
                elif isinstance(e, httpx.TransportError):
                    last_status_code = 503
                await cb.record_failure(status_code=last_status_code)
                self._sync_circuit_metric(provider_name, cb)
            finally:
                if not probe_succeeded:
                    await cb.release_probe()

        raise RuntimeError(f"All streaming providers failed. Last error: {last_error}")


provider_router = ProviderRouter()
