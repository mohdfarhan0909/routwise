"""Unit tests for RateLimiter NoScript recovery and atomic budget script."""

from unittest.mock import AsyncMock

import pytest
from redis.exceptions import NoScriptError

from app.api.chat import _check_and_update_budget
from app.core.rate_limiter import TOKEN_BUCKET_SCRIPT, RateLimiter


@pytest.mark.asyncio
async def test_rate_limiter_noscript_recovery():
    """Verify that NoScriptError causes RateLimiter to reload the script and retry."""
    mock_redis = AsyncMock()

    # First evalsha raises NoScriptError, second succeeds with [1, 5]
    mock_redis.evalsha.side_effect = [
        NoScriptError("NOSCRIPT No such script"),
        [1, 5],
    ]
    mock_redis.script_load.return_value = "new_sha_123"

    limiter = RateLimiter(mock_redis)
    allowed, remaining = await limiter.check(tenant_id="t1", max_tokens=10)

    assert allowed is True
    assert remaining == 5
    assert mock_redis.script_load.await_count == 2
    mock_redis.script_load.assert_called_with(TOKEN_BUCKET_SCRIPT)
    assert mock_redis.evalsha.await_count == 2


@pytest.mark.asyncio
async def test_rate_limiter_fails_open_on_redis_error():
    """RateLimiter fails open when Redis is unreachable."""
    mock_redis = AsyncMock()
    mock_redis.evalsha.side_effect = ConnectionError("Redis disconnected")
    mock_redis.script_load.return_value = "sha"

    limiter = RateLimiter(mock_redis)
    allowed, remaining = await limiter.check(tenant_id="t1", max_tokens=10)

    assert allowed is True
    assert remaining == -1


@pytest.mark.asyncio
async def test_check_and_update_budget_allowed(monkeypatch):
    """Budget check allows request when Redis eval returns 1."""
    import app.api.chat as chat_module

    mock_redis = AsyncMock()
    mock_redis.eval.return_value = 1
    monkeypatch.setattr(chat_module, "redis_client", mock_redis)

    ok = await _check_and_update_budget("tenant_abc", daily_budget=5.0, cost_to_add=0.01)
    assert ok is True
    assert mock_redis.eval.await_count == 1


@pytest.mark.asyncio
async def test_check_and_update_budget_exceeded(monkeypatch):
    """Budget check rejects request when Redis eval returns 0."""
    import app.api.chat as chat_module

    mock_redis = AsyncMock()
    mock_redis.eval.return_value = 0
    monkeypatch.setattr(chat_module, "redis_client", mock_redis)

    ok = await _check_and_update_budget("tenant_abc", daily_budget=5.0, cost_to_add=0.01)
    assert ok is False


@pytest.mark.asyncio
async def test_check_and_update_budget_fails_open_on_error(monkeypatch):
    """Budget check fails open if Redis throws an exception."""
    import app.api.chat as chat_module

    mock_redis = AsyncMock()
    mock_redis.eval.side_effect = ConnectionError("Redis down")
    monkeypatch.setattr(chat_module, "redis_client", mock_redis)

    ok = await _check_and_update_budget("tenant_abc", daily_budget=5.0, cost_to_add=0.01)
    assert ok is True
