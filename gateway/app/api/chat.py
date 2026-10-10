"""Chat completions endpoint — OpenAI-compatible."""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.semantic_cache import (
    async_embed_prompt,
    compute_context_hash,
    is_cacheable,
    lookup_cache,
    store_cache,
)
from app.core.auth import get_current_tenant
from app.core.rate_limiter import RateLimiter
from app.db.models import RequestLog, Tenant
from app.db.session import async_session, get_db, redis_client
from app.metrics.prometheus import (
    ACTIVE_STREAMS,
    CACHE_HIT_RATE,
    COST_TOTAL,
    RATE_LIMIT_REJECTIONS,
    REQUEST_COUNT,
    REQUEST_LATENCY,
    TOKENS_USED,
)
from app.providers.base import ProviderRequest
from app.providers.router import provider_router

logger = logging.getLogger(__name__)

router = APIRouter()
rate_limiter = RateLimiter(redis_client)

# Pricing table (per 1,000 tokens) by model family
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "mock": (0.0, 0.0),
    "free": (0.0, 0.0),
    "gpt-4o-mini": (0.00000015, 0.0000006),
    "gpt-4o": (0.0000025, 0.00001),
    "claude-3-haiku": (0.00000025, 0.00000125),
    "gemini": (0.000000075, 0.0000003),
    "deepseek": (0.00000014, 0.00000028),
    "llama-3": (0.0000002, 0.0000002),
    "mixtral": (0.00000024, 0.00000024),
}

BUDGET_LUA_SCRIPT = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local limit = tonumber(ARGV[1])
local to_add = tonumber(ARGV[2])

if limit > 0 and current >= limit then
    return 0
end

if to_add > 0 then
    redis.call('INCRBYFLOAT', KEYS[1], to_add)
    redis.call('EXPIRE', KEYS[1], 90000)
end
return 1
"""


def _estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Calculate model-specific cost estimate."""
    model_lower = model.lower()
    for key, (p_rate, c_rate) in MODEL_PRICING.items():
        if key in model_lower:
            return round((prompt_tokens * p_rate) + (completion_tokens * c_rate), 6)
    return round((prompt_tokens * 0.0000005) + (completion_tokens * 0.0000015), 6)


async def _check_and_update_budget(
    tenant_id: str, daily_budget: float, cost_to_add: float = 0.0
) -> bool:
    """Enforce daily tenant spending budget tracked atomically in Redis."""
    today_str = datetime.now(UTC).strftime("%Y-%m-%d")
    budget_key = f"budget:{tenant_id}:{today_str}"

    try:
        res = await redis_client.eval(
            BUDGET_LUA_SCRIPT,
            1,
            budget_key,
            str(daily_budget),
            str(cost_to_add),
        )
        return bool(res)
    except Exception as e:
        logger.warning("Redis budget tracking check failed (failing open): %s", e)
        return True


async def _async_log_request(
    tenant_id: uuid.UUID,
    model: str,
    provider: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    status_str: str,
    cache_hit: bool,
    cost_estimate: float,
) -> None:
    """Background task: Write request log asynchronously to avoid blocking user response."""
    try:
        async with async_session() as session:
            log = RequestLog(
                tenant_id=tenant_id,
                model=model,
                provider=provider,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
                status=status_str,
                cache_hit=cache_hit,
                cost_estimate=cost_estimate,
            )
            session.add(log)
            await session.commit()
    except Exception as e:
        logger.error("Failed to write async request log: %s", e)


class ChatMessage(BaseModel):
    role: str
    content: str
    name: str | None = None


class ChatCompletionRequest(BaseModel):
    model: str = Field(..., description="Target model ID, e.g. 'gpt-4o-mini', 'llama-3.1-8b'")
    messages: list[ChatMessage] = Field(..., min_length=1)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1)
    stream: bool = False


class ChatChoice(BaseModel):
    index: int
    message: dict[str, Any]
    finish_reason: str = "stop"


class ChatUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatChoice]
    usage: ChatUsage


@router.post(
    "/chat/completions",
    response_model=ChatCompletionResponse,
    responses={
        200: {"description": "Successful chat completion or SSE stream"},
        401: {"description": "Missing or invalid API key"},
        429: {"description": "Tenant rate limit or daily budget exceeded"},
        502: {"description": "All upstream LLM providers failed"},
    },
)
async def create_chat_completion(
    request: ChatCompletionRequest,
    response: Response,
    background_tasks: BackgroundTasks,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """OpenAI-compatible chat completions proxy with auth, rate limits, caching, and fallback."""
    overall_start = time.monotonic()
    tenant_id_str = str(tenant.id)
    rpm_limit = tenant.quota.rpm_limit if tenant.quota else 60
    daily_budget = tenant.quota.daily_budget if tenant.quota else 1.0

    # 1. Rate Limiting Check (Token Bucket)
    allowed, remaining = await rate_limiter.check(
        tenant_id=tenant_id_str,
        max_tokens=rpm_limit,
    )
    response.headers["X-RateLimit-Limit"] = str(rpm_limit)
    response.headers["X-RateLimit-Remaining"] = str(max(0, remaining))

    if not allowed:
        RATE_LIMIT_REJECTIONS.labels(tenant_id=tenant_id_str).inc()
        REQUEST_COUNT.labels(
            tenant_id=tenant_id_str,
            model=request.model,
            provider="gateway",
            status="rate_limited",
            cache_hit="false",
        ).inc()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded. Your tier limit is {rpm_limit} RPM.",
            headers={"Retry-After": "60"},
        )

    # 2. Daily Spending Budget Check
    budget_ok = await _check_and_update_budget(tenant_id_str, daily_budget, cost_to_add=0.0)
    if not budget_ok:
        REQUEST_COUNT.labels(
            tenant_id=tenant_id_str,
            model=request.model,
            provider="gateway",
            status="budget_exceeded",
            cache_hit="false",
        ).inc()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Daily spending budget (${daily_budget:.2f}) exceeded.",
            headers={"Retry-After": "3600"},
        )

    # 3. Format input messages
    messages_dict = [{"role": m.role, "content": m.content} for m in request.messages]
    last_user_message = next(
        (m["content"] for m in reversed(messages_dict) if m["role"] == "user"), ""
    )

    # 4. Semantic Cache Lookup (deterministic & non-streaming)
    can_cache = is_cacheable(request.temperature, request.stream)
    prompt_embedding: list[float] | None = None
    context_hash = ""

    if can_cache and last_user_message:
        context_hash = compute_context_hash(messages_dict, max_tokens=request.max_tokens)
        prompt_embedding = await async_embed_prompt(last_user_message)

        cached_result = await lookup_cache(
            db=db,
            tenant_id=tenant.id,
            model=request.model,
            context_hash=context_hash,
            embedding=prompt_embedding,
        )

        if cached_result:
            measured_latency = max(1, int((time.monotonic() - overall_start) * 1000))
            CACHE_HIT_RATE.labels(result="hit").inc()
            REQUEST_COUNT.labels(
                tenant_id=tenant_id_str,
                model=request.model,
                provider="cache",
                status="success",
                cache_hit="true",
            ).inc()

            cached_data = json.loads(cached_result)
            prompt_tokens = cached_data.get("usage", {}).get("prompt_tokens", 0)
            completion_tokens = cached_data.get("usage", {}).get("completion_tokens", 0)

            background_tasks.add_task(
                _async_log_request,
                tenant_id=tenant.id,
                model=request.model,
                provider="cache",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=measured_latency,
                status_str="cached",
                cache_hit=True,
                cost_estimate=0.0,
            )

            response.headers["X-Cache-Hit"] = "true"
            return ChatCompletionResponse(**cached_data)

        CACHE_HIT_RATE.labels(result="miss").inc()

    # 5. Route to Upstream Provider
    provider_req = ProviderRequest(
        model=request.model,
        messages=messages_dict,
        temperature=request.temperature,
        max_tokens=request.max_tokens,
        stream=request.stream,
    )

    # --- Streaming Route ---
    if request.stream:
        ACTIVE_STREAMS.inc()
        stream_start = time.monotonic()
        try:
            provider_name, stream_gen = await provider_router.stream_with_fallback(provider_req)

            async def logged_stream_wrapper() -> AsyncGenerator[str, None]:
                stream_tokens = 0
                completed_normally = False
                try:
                    async for chunk in stream_gen:
                        if "delta" in chunk and "content" in chunk:
                            try:
                                clean_chunk = chunk
                                if clean_chunk.startswith("data:"):
                                    clean_chunk = clean_chunk[5:].strip()
                                data_obj = json.loads(clean_chunk)
                                delta_text = (
                                    data_obj.get("choices", [{}])[0]
                                    .get("delta", {})
                                    .get("content", "")
                                )
                                if delta_text:
                                    words = len(delta_text.split())
                                    stream_tokens += max(1, int(words * 1.3))
                                else:
                                    stream_tokens += 1
                            except Exception:
                                stream_tokens += 1
                        yield f"{chunk}\n\n"
                    completed_normally = True
                except Exception as stream_err:
                    logger.warning("Streaming disconnected: %s", stream_err)
                finally:

                    async def _finalize() -> None:
                        ACTIVE_STREAMS.dec()
                        stream_latency = int((time.monotonic() - stream_start) * 1000)
                        approx_p_tokens = sum(
                            len(m.get("content", "").split()) for m in messages_dict
                        )
                        approx_c_tokens = max(1, stream_tokens)
                        stream_cost = _estimate_cost(
                            request.model, approx_p_tokens, approx_c_tokens
                        )

                        status_flag = "success" if completed_normally else "cancelled"
                        REQUEST_COUNT.labels(
                            tenant_id=tenant_id_str,
                            model=request.model,
                            provider=provider_name,
                            status=status_flag,
                            cache_hit="false",
                        ).inc()
                        REQUEST_LATENCY.labels(provider=provider_name, model=request.model).observe(
                            stream_latency / 1000.0
                        )
                        TOKENS_USED.labels(
                            tenant_id=tenant_id_str, model=request.model, direction="prompt"
                        ).inc(approx_p_tokens)
                        TOKENS_USED.labels(
                            tenant_id=tenant_id_str, model=request.model, direction="completion"
                        ).inc(approx_c_tokens)
                        COST_TOTAL.labels(
                            tenant_id=tenant_id_str, model=request.model, provider=provider_name
                        ).inc(stream_cost)

                        await _check_and_update_budget(tenant_id_str, daily_budget, stream_cost)
                        await _async_log_request(
                            tenant_id=tenant.id,
                            model=request.model,
                            provider=provider_name,
                            prompt_tokens=approx_p_tokens,
                            completion_tokens=approx_c_tokens,
                            latency_ms=stream_latency,
                            status_str=status_flag,
                            cache_hit=False,
                            cost_estimate=stream_cost,
                        )

                    # Shield accounting from client cancellation
                    await asyncio.shield(_finalize())

            return StreamingResponse(
                logged_stream_wrapper(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        except Exception as e:
            ACTIVE_STREAMS.dec()
            REQUEST_COUNT.labels(
                tenant_id=tenant_id_str,
                model=request.model,
                provider="unknown",
                status="error",
                cache_hit="false",
            ).inc()
            logger.error("Streaming failure: %s", e)
            raise HTTPException(
                status_code=502, detail=f"LLM Provider streaming failure: {e}"
            ) from e

    # --- Non-streaming Route ---
    provider_start = time.monotonic()
    try:
        provider_name, provider_resp = await provider_router.execute_with_fallback(provider_req)
    except Exception as e:
        REQUEST_COUNT.labels(
            tenant_id=tenant_id_str,
            model=request.model,
            provider="unknown",
            status="error",
            cache_hit="false",
        ).inc()
        logger.error("Provider execution failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"All upstream LLM providers failed: {e}",
        ) from e

    latency_ms = int((time.monotonic() - provider_start) * 1000)
    cost = _estimate_cost(
        request.model, provider_resp.prompt_tokens, provider_resp.completion_tokens
    )

    # 6. Metrics & Background Accounting
    REQUEST_LATENCY.labels(provider=provider_name, model=request.model).observe(latency_ms / 1000.0)
    REQUEST_COUNT.labels(
        tenant_id=tenant_id_str,
        model=request.model,
        provider=provider_name,
        status="success",
        cache_hit="false",
    ).inc()
    TOKENS_USED.labels(tenant_id=tenant_id_str, model=request.model, direction="prompt").inc(
        provider_resp.prompt_tokens
    )
    TOKENS_USED.labels(tenant_id=tenant_id_str, model=request.model, direction="completion").inc(
        provider_resp.completion_tokens
    )
    COST_TOTAL.labels(tenant_id=tenant_id_str, model=request.model, provider=provider_name).inc(
        cost
    )

    await _check_and_update_budget(tenant_id_str, daily_budget, cost)

    resp_payload = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": provider_resp.model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": provider_resp.content,
                },
                "finish_reason": provider_resp.finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": provider_resp.prompt_tokens,
            "completion_tokens": provider_resp.completion_tokens,
            "total_tokens": provider_resp.prompt_tokens + provider_resp.completion_tokens,
        },
    }

    # 7. Store in Semantic Cache if eligible (only real providers, never mock fallback)
    if can_cache and prompt_embedding is not None and provider_name != "mock":
        background_tasks.add_task(
            store_cache,
            tenant_id=tenant.id,
            model=request.model,
            context_hash=context_hash,
            embedding=prompt_embedding,
            response_json_str=json.dumps(resp_payload),
        )

    # 8. Background write to RequestLog
    background_tasks.add_task(
        _async_log_request,
        tenant_id=tenant.id,
        model=request.model,
        provider=provider_name,
        prompt_tokens=provider_resp.prompt_tokens,
        completion_tokens=provider_resp.completion_tokens,
        latency_ms=latency_ms,
        status_str="success",
        cache_hit=False,
        cost_estimate=cost,
    )

    response.headers["X-Cache-Hit"] = "false"
    return ChatCompletionResponse(**resp_payload)
