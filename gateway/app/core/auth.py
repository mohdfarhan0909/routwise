"""Authentication utilities — API key hashing, verification, caching, and FastAPI dependency."""

import hashlib
import json
import logging
import secrets
import uuid

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.db.models import ApiKey, Quota, Tenant
from app.db.session import get_db, redis_client

logger = logging.getLogger(__name__)


def generate_api_key() -> tuple[str, str]:
    """Generate a new API key. Returns (raw_key, key_hash).

    The raw key is shown to the user once. Only the hash is stored.
    """
    raw = settings.api_key_prefix + secrets.token_urlsafe(32)
    hashed = hash_api_key(raw)
    return raw, hashed


def hash_api_key(raw_key: str) -> str:
    """Hash an API key using SHA-256."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


def get_key_prefix(raw_key: str) -> str:
    """Extract the display prefix from a raw key (first 8 chars)."""
    return raw_key[: len(settings.api_key_prefix) + 4]


async def invalidate_auth_cache(key_hash: str) -> None:
    """Invalidate cached auth credentials in Redis."""
    try:
        await redis_client.delete(f"auth:{key_hash}")
    except Exception as e:
        logger.warning("Failed to invalidate auth cache in Redis: %s", e)


async def get_current_tenant(
    authorization: str | None = Header(None, description="Bearer <api_key> or raw api_key"),
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    """FastAPI dependency to authenticate and resolve the current tenant from API key."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = authorization.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key in Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Fast path for development key (used by local testing and RAG client)
    is_dev = settings.app_env.lower() in ("development", "dev", "local", "testing", "test")
    if is_dev and token in (settings.gateway_api_key, "rw_dev_key"):
        dev_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        dev_tenant = Tenant(
            id=dev_id,
            name="Development Tenant",
            status="active",
        )
        dev_tenant.quota = Quota(
            tenant_id=dev_id,
            rpm_limit=settings.default_rpm_limit,
            daily_budget=settings.default_daily_budget,
        )
        return dev_tenant

    key_hash = hash_api_key(token)
    cache_key = f"auth:{key_hash}"

    # Try fast Redis cache hit first
    try:
        cached_raw = await redis_client.get(cache_key)
        if cached_raw:
            data = json.loads(cached_raw)
            # Reconstruct detached Tenant instance with Quota
            tenant = Tenant(
                id=uuid.UUID(data["id"]),
                name=data["name"],
                status=data["status"],
            )
            tenant.quota = Quota(
                tenant_id=tenant.id,
                rpm_limit=data.get("rpm_limit", 60),
                daily_budget=data.get("daily_budget", 1.0),
            )
            return tenant
    except Exception as e:
        logger.debug("Redis auth cache read error (falling through to DB): %s", e)

    # Postgres lookup
    stmt = (
        select(ApiKey)
        .where(ApiKey.key_hash == key_hash)
        .options(selectinload(ApiKey.tenant).selectinload(Tenant.quota))
    )
    try:
        result = await db.execute(stmt)
        api_key_record = result.scalar_one_or_none()
    except Exception as e:
        logger.warning("Database unavailable during auth check: %s", e)
        if is_dev:
            dev_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
            dev_tenant = Tenant(
                id=dev_id,
                name="Development Fallback Tenant",
                status="active",
            )
            dev_tenant.quota = Quota(
                tenant_id=dev_id,
                rpm_limit=settings.default_rpm_limit,
                daily_budget=settings.default_daily_budget,
            )
            return dev_tenant
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication database unavailable",
        ) from e

    if api_key_record is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if api_key_record.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key has been revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )

    tenant = api_key_record.tenant
    if tenant is None or tenant.status != "active":
        t_status = tenant.status if tenant else "unknown"
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Tenant account is inactive or suspended (status: {t_status})",
        )

    # Store in Redis auth cache (TTL 60 seconds)
    try:
        payload = {
            "id": str(tenant.id),
            "name": tenant.name,
            "status": tenant.status,
            "rpm_limit": tenant.quota.rpm_limit if tenant.quota else 60,
            "daily_budget": tenant.quota.daily_budget if tenant.quota else 1.0,
        }
        await redis_client.setex(cache_key, 60, json.dumps(payload))
    except Exception as e:
        logger.debug("Failed to write auth to Redis cache: %s", e)

    return tenant
