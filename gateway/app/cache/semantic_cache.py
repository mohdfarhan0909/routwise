"""Semantic cache using pgvector for similarity search.

Scoped by tenant, model, context hash, and token limits to prevent leakage.
Only caches when safe: low temperature, non-streaming/deterministic completions.
Async embedding execution via thread pool to preserve event loop concurrency.
"""

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from prometheus_client import Counter
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import CacheEntry
from app.db.session import async_session

logger = logging.getLogger(__name__)

# Prometheus metric to alert if running in degraded/fallback embedding mode
FALLBACK_EMBEDDINGS_TOTAL = Counter(
    "routewise_fallback_embeddings_total",
    "Count of fallback pseudo-embeddings generated when sentence-transformers is unavailable",
)

# Lazy-loaded embedding model
_embedding_model: Any = None
_model_failed_to_load: bool = False


def _get_embedding_model() -> Any:
    global _embedding_model, _model_failed_to_load
    if _embedding_model is None and not _model_failed_to_load:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]

            _embedding_model = SentenceTransformer(settings.embedding_model)
            logger.info("Loaded sentence-transformers model: %s", settings.embedding_model)
        except Exception as e:
            _model_failed_to_load = True
            logger.warning(
                "CRITICAL: sentence-transformers model '%s' failed to load (%s). "
                "Falling back to exact-match pseudo embeddings. "
                "Install torch and sentence-transformers for true semantic vector matching.",
                settings.embedding_model,
                e,
            )
    return _embedding_model


def embed_prompt(text: str) -> list[float]:
    """Generate 384-dimensional normalized vector embedding for prompt text (CPU-bound)."""
    model = _get_embedding_model()
    if model is not None:
        try:
            embedding = model.encode(text, normalize_embeddings=True)
            return [float(x) for x in embedding]
        except Exception as e:
            logger.warning("Model inference failed, falling back to pseudo-embedding: %s", e)

    # Track fallback execution via Prometheus metric
    FALLBACK_EMBEDDINGS_TOTAL.inc()

    # Deterministic 384-dimensional pseudo-embedding derived from SHA-256
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    expanded = (digest * 12)[:384]
    vec = np.array([float(b) / 255.0 for b in expanded], dtype=np.float32)
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return [float(x) for x in vec]


async def async_embed_prompt(text: str) -> list[float]:
    """Non-blocking async wrapper that executes prompt embedding in an asyncio worker thread."""
    return await asyncio.to_thread(embed_prompt, text)


def compute_context_hash(
    messages: list[dict[str, Any]],
    max_tokens: int | None = None,
) -> str:
    """Compute SHA-256 hash of conversational context and parameter limits prior to final prompt.

    Includes system prompts, prior turns, and max_tokens constraint to ensure identical
    ending questions across conversations or requests with different token budgets do not collide
    or serve truncated responses.
    """
    if len(messages) <= 1:
        prior = [m for m in messages if m.get("role") == "system"]
    else:
        prior = list(messages[:-1])

    if max_tokens is not None:
        prior.append({"_system_param": f"max_tokens:{max_tokens}"})

    raw_str = json.dumps(prior, sort_keys=True)
    return hashlib.sha256(raw_str.encode("utf-8")).hexdigest()


def is_cacheable(temperature: float, stream: bool = False) -> bool:
    """Determine if a request is eligible for semantic caching."""
    if not settings.cache_enabled:
        return False
    # Cache only deterministic requests (temperature <= 0.3) and non-streaming
    return (temperature <= 0.3) and not stream


async def lookup_cache(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    model: str,
    context_hash: str,
    embedding: list[float],
    threshold: float | None = None,
) -> str | None:
    """Look up cached response with cosine similarity above threshold."""
    if threshold is None:
        threshold = settings.cache_similarity_threshold

    now = datetime.now(UTC)
    max_distance = 1.0 - threshold

    try:
        distance_expr = CacheEntry.prompt_embedding.cosine_distance(embedding)
        stmt = (
            select(CacheEntry.response, distance_expr.label("distance"))
            .where(
                CacheEntry.tenant_id == tenant_id,
                CacheEntry.model == model,
                CacheEntry.context_hash == context_hash,
                CacheEntry.expires_at > now,
                distance_expr <= max_distance,
            )
            .order_by("distance")
            .limit(1)
        )

        result = await db.execute(stmt)
        row = result.first()
        if row:
            logger.info("Semantic cache HIT (distance: %.4f)", row.distance)
            return str(row.response)
        return None
    except Exception as e:
        logger.warning("Semantic cache lookup failed (failing open): %s", e)
        return None


async def store_cache(
    tenant_id: uuid.UUID,
    model: str,
    context_hash: str,
    embedding: list[float],
    response_json_str: str,
    ttl_seconds: int | None = None,
    db: AsyncSession | None = None,
) -> None:
    """Store prompt embedding and response in cache. Creates own session if called in background."""
    if ttl_seconds is None:
        ttl_seconds = settings.cache_ttl_seconds

    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=ttl_seconds)

    async def _insert_with_session(session: AsyncSession) -> None:
        entry = CacheEntry(
            tenant_id=tenant_id,
            model=model,
            context_hash=context_hash,
            prompt_embedding=embedding,
            response=response_json_str,
            expires_at=expires_at,
        )
        session.add(entry)
        await session.commit()
        logger.info("Stored response in semantic cache for model '%s'", model)

    try:
        if db is not None:
            await _insert_with_session(db)
        else:
            async with async_session() as session:
                await _insert_with_session(session)
    except Exception as e:
        logger.warning("Failed to store entry in semantic cache: %s", e)


async def evict_expired(db: AsyncSession | None = None) -> int:
    """Evict expired entries from the cache."""
    now = datetime.now(UTC)

    async def _do_evict(session: AsyncSession) -> int:
        stmt = delete(CacheEntry).where(CacheEntry.expires_at <= now)
        res = await session.execute(stmt)
        await session.commit()
        rowcount = getattr(res, "rowcount", 0) or 0
        return int(rowcount)

    try:
        if db is not None:
            return await _do_evict(db)
        async with async_session() as session:
            return await _do_evict(session)
    except Exception as e:
        logger.warning("Failed to evict expired cache: %s", e)
        return 0
