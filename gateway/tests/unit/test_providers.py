"""Unit tests for Provider adapters and Router."""

import pytest

from app.providers.base import ProviderRequest
from app.providers.mock import MockProvider
from app.providers.router import ProviderRouter


@pytest.mark.asyncio
async def test_mock_provider_send():
    provider = MockProvider()
    req = ProviderRequest(
        model="mock-gpt",
        messages=[{"role": "user", "content": "Hello Routewise!"}],
        temperature=0.7,
    )
    resp = await provider.send(req)
    assert resp.content != ""
    assert resp.model == "mock-gpt"
    assert resp.prompt_tokens > 0
    assert resp.finish_reason == "stop"
    await provider.close()


@pytest.mark.asyncio
async def test_mock_provider_stream():
    provider = MockProvider()
    req = ProviderRequest(
        model="mock-gpt",
        messages=[{"role": "user", "content": "Stream this test"}],
        temperature=0.7,
        stream=True,
    )
    chunks = []
    async for chunk in provider.stream(req):
        chunks.append(chunk)
    assert len(chunks) > 0
    assert any("[DONE]" in c for c in chunks)
    await provider.close()


@pytest.mark.asyncio
async def test_provider_router_fallback():
    router = ProviderRouter()
    req = ProviderRequest(
        model="mock-fast",
        messages=[{"role": "user", "content": "Test fallback"}],
        temperature=0.5,
    )
    provider_name, resp = await router.execute_with_fallback(req)
    assert provider_name == "mock"
    assert resp.content != ""


def test_model_translation():
    """Verify canonical model names translate to provider-specific identifiers."""
    router = ProviderRouter()

    # OpenRouter translations
    assert (
        router._translate_model_for_provider("openrouter", "gpt-4o-mini")
        == "openai/gpt-4o-mini"
    )
    assert (
        router._translate_model_for_provider("openrouter", "claude-3-haiku")
        == "anthropic/claude-3-haiku"
    )

    # Groq translations
    assert (
        router._translate_model_for_provider("groq", "gpt-4o-mini")
        == "llama-3.1-8b-instant"
    )
    assert (
        router._translate_model_for_provider("groq", "llama-3.3-70b")
        == "llama-3.3-70b-versatile"
    )

    # Gemini translations
    assert (
        router._translate_model_for_provider("gemini", "gpt-4o-mini")
        == "gemini-3.8-flash"
    )
    assert (
        router._translate_model_for_provider("gemini", "gemini-pro")
        == "gemini-3.8-flash"
    )

    # Hugging Face translations
    assert (
        router._translate_model_for_provider("huggingface", "deepseek-r1")
        == "deepseek-ai/DeepSeek-R1"
    )
    assert (
        router._translate_model_for_provider("huggingface", "llama-3.3-70b")
        == "meta-llama/Llama-3.3-70B-Instruct"
    )


@pytest.mark.asyncio
async def test_mock_fallback_off_raises_when_no_real_providers(monkeypatch):
    """When ENABLE_MOCK_FALLBACK is False, non-mock models fail with RuntimeError."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "enable_mock_fallback", False)
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "groq_api_key", "")
    monkeypatch.setattr(settings, "hf_api_key", "")

    router = ProviderRouter()
    req = ProviderRequest(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Hello"}],
    )
    with pytest.raises(RuntimeError, match="No healthy upstream providers configured"):
        await router.execute_with_fallback(req)


@pytest.mark.asyncio
async def test_mock_fallback_on_succeeds_for_real_model(monkeypatch):
    """When ENABLE_MOCK_FALLBACK is True, router falls back to mock provider."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "enable_mock_fallback", True)
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "groq_api_key", "")
    monkeypatch.setattr(settings, "hf_api_key", "")

    router = ProviderRouter()
    req = ProviderRequest(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Hello fallback"}],
    )
    provider_name, resp = await router.execute_with_fallback(req)
    assert provider_name == "mock"
    assert resp.content != ""

