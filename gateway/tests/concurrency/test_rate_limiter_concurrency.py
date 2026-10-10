"""Concurrency tests for Token Bucket Rate Limiter."""

import asyncio

import pytest

from app.core.rate_limiter import RateLimiter


class MockRedisConcurrencyClient:
    """In-memory Redis mock replicating the atomic Lua script for concurrency testing."""

    def __init__(self):
        self.buckets = {}
        self.lock = asyncio.Lock()

    async def script_load(self, script: str) -> str:
        return "mock_sha"

    async def evalsha(
        self,
        sha: str,
        numkeys: int,
        key: str,
        capacity_str: str,
        refill_rate_str: str,
        requested_str: str,
    ):
        async with self.lock:
            capacity = float(capacity_str)
            refill_rate = float(refill_rate_str)
            requested = float(requested_str)
            now = asyncio.get_event_loop().time()

            tokens, last_refill = self.buckets.get(key, (capacity, now))
            # Refill
            elapsed = max(0.0, now - last_refill)
            tokens = min(capacity, tokens + elapsed * refill_rate)

            # Consume
            if tokens >= requested:
                tokens -= requested
                self.buckets[key] = (tokens, now)
                return [1, int(tokens)]
            else:
                self.buckets[key] = (tokens, now)
                return [0, int(tokens)]


@pytest.mark.asyncio
async def test_concurrent_rate_limiting_atomicity():
    """Verify 30 concurrent requests against a capacity-10 bucket.

    Allows exactly 10 and rejects 20.
    """
    mock_redis = MockRedisConcurrencyClient()
    limiter = RateLimiter(mock_redis)

    tenant_id = "concurrent-tenant-123"
    capacity = 10
    total_concurrent_requests = 30

    async def make_request():
        allowed, _remaining = await limiter.check(
            tenant_id=tenant_id,
            max_tokens=capacity,
            refill_rate=0.001,  # negligible refill during burst
            requested=1,
        )
        return allowed

    # Fire all 30 requests concurrently
    tasks = [make_request() for _ in range(total_concurrent_requests)]
    results = await asyncio.gather(*tasks)

    allowed_count = sum(1 for r in results if r is True)
    rejected_count = sum(1 for r in results if r is False)

    assert allowed_count == capacity, f"Expected {capacity} allowed, got {allowed_count}"
    assert rejected_count == (total_concurrent_requests - capacity), (
        f"Expected {total_concurrent_requests - capacity} rejected, got {rejected_count}"
    )
