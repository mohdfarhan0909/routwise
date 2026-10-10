"""Unit tests for semantic caching logic."""

from app.cache.semantic_cache import (
    compute_context_hash,
    embed_prompt,
    is_cacheable,
)


def test_embed_prompt_dimensions():
    vec = embed_prompt("What is the capital of France?")
    assert len(vec) == 384
    # Check that it is normalized (magnitude close to 1)
    norm = sum(x**2 for x in vec) ** 0.5
    assert abs(norm - 1.0) < 0.01


def test_compute_context_hash_includes_prior_turns():
    """Full prior conversation is included so 'yes' in different contexts doesn't collide."""
    messages_1 = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is 2+2?"},
        {"role": "assistant", "content": "4"},
        {"role": "user", "content": "Why?"},
    ]
    messages_2 = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Is Python good?"},
        {"role": "assistant", "content": "Yes, very."},
        {"role": "user", "content": "Why?"},
    ]

    # Same ending "Why?" but different prior conversation → different context hash
    hash_1 = compute_context_hash(messages_1)
    hash_2 = compute_context_hash(messages_2)
    assert hash_1 != hash_2


def test_compute_context_hash_same_prior():
    """Identical prior context produces identical hash."""
    messages_a = [
        {"role": "system", "content": "You are a math tutor."},
        {"role": "user", "content": "explain"},
    ]
    messages_b = [
        {"role": "system", "content": "You are a math tutor."},
        {"role": "user", "content": "different question"},
    ]
    # Both have same prior (just the system message) → same context hash
    hash_a = compute_context_hash(messages_a)
    hash_b = compute_context_hash(messages_b)
    assert hash_a == hash_b


def test_is_cacheable(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "cache_enabled", True)
    assert is_cacheable(temperature=0.0, stream=False) is True
    assert is_cacheable(temperature=0.2, stream=False) is True
    assert is_cacheable(temperature=0.7, stream=False) is False
    assert is_cacheable(temperature=0.1, stream=True) is False

    monkeypatch.setattr(settings, "cache_enabled", False)
    assert is_cacheable(temperature=0.0, stream=False) is False
