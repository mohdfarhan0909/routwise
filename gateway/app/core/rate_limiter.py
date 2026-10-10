"""Token bucket rate limiter backed by Redis with an atomic Lua script.

The Lua script performs read → refill → decrement atomically, using Redis server-side
time (`TIME`) to avoid clock skew across gateway replicas.

Fail-open policy: if Redis is unreachable, requests pass through (with an alert logged).
"""

import logging

import redis.asyncio as aioredis
from redis.exceptions import NoScriptError

from app.core.config import settings

logger = logging.getLogger(__name__)

# Atomic Lua script: token bucket with continuous refill using Redis TIME
# KEYS[1] = bucket key, e.g. "rl:{tenant_id}"
# ARGV[1] = max_tokens (bucket capacity)
# ARGV[2] = refill_rate (tokens per second)
# ARGV[3] = requested (tokens to consume, default 1)
#
# Returns: [allowed (0|1), tokens_remaining]
TOKEN_BUCKET_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local requested = tonumber(ARGV[3])

-- Server-side authoritative timestamp (seconds + microseconds)
local r_time = redis.call('TIME')
local now = tonumber(r_time[1]) + (tonumber(r_time[2]) / 1000000.0)

local bucket = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(bucket[1])
local last_refill = tonumber(bucket[2])

if tokens == nil or last_refill == nil then
    tokens = capacity
    last_refill = now
end

-- Refill tokens based on elapsed server time
local elapsed = math.max(0, now - last_refill)
tokens = math.min(capacity, tokens + elapsed * refill_rate)

-- Check and consume
local allowed = 0
if tokens >= requested then
    tokens = tokens - requested
    allowed = 1
end

-- Persist state using HSET (replaces deprecated HMSET)
redis.call('HSET', key, 'tokens', tostring(tokens), 'last_refill', tostring(now))
local ttl = math.ceil(capacity / refill_rate) + 60
redis.call('EXPIRE', key, ttl)

return {allowed, math.floor(tokens)}
"""


class RateLimiter:
    """Per-tenant token bucket rate limiter with Redis-authoritative time."""

    def __init__(self, redis_client: aioredis.Redis) -> None:
        self._redis = redis_client
        self._script_sha: str | None = None

    async def _ensure_script(self) -> str:
        if self._script_sha is None:
            self._script_sha = await self._redis.script_load(TOKEN_BUCKET_SCRIPT)
        return self._script_sha

    async def check(
        self,
        tenant_id: str,
        max_tokens: int | None = None,
        refill_rate: float | None = None,
        requested: int = 1,
    ) -> tuple[bool, int]:
        """Check and consume tokens.

        Returns (allowed, remaining_tokens).
        Falls open on Redis failure.
        """
        if max_tokens is None:
            max_tokens = settings.default_rpm_limit
        if refill_rate is None:
            refill_rate = max_tokens / 60.0  # tokens per second for RPM

        try:
            sha = await self._ensure_script()
            try:
                result = await self._redis.evalsha(
                    sha,
                    1,
                    f"rl:{tenant_id}",
                    str(max_tokens),
                    str(refill_rate),
                    str(requested),
                )
            except NoScriptError:
                # Script cache was cleared (e.g. Redis restarted); re-load script and retry
                logger.info("Lua script not found in Redis, reloading and retrying...")
                self._script_sha = await self._redis.script_load(TOKEN_BUCKET_SCRIPT)
                result = await self._redis.evalsha(
                    self._script_sha,
                    1,
                    f"rl:{tenant_id}",
                    str(max_tokens),
                    str(refill_rate),
                    str(requested),
                )
            allowed = bool(result[0])
            remaining = int(result[1])
            return allowed, remaining
        except Exception:
            logger.warning("Redis unavailable — rate limiter failing open", exc_info=True)
            return True, -1
