"""Admin API — tenant management, quotas, API keys, and usage tracking."""

import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth import generate_api_key, get_key_prefix, invalidate_auth_cache
from app.core.config import settings
from app.db.models import ApiKey, Quota, RequestLog, Tenant
from app.db.session import get_db


async def verify_admin_key(
    x_admin_key: str | None = Header(None, alias="X-Admin-Key"),
    authorization: str | None = Header(None),
) -> bool:
    """Enforce admin secret key authentication using constant-time comparison."""
    token = None
    if x_admin_key:
        token = x_admin_key.strip()
    elif authorization:
        auth_header = authorization.strip()
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
        else:
            token = auth_header

    if not token or not secrets.compare_digest(token, settings.admin_secret_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Invalid or missing admin secret key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return True


router = APIRouter(dependencies=[Depends(verify_admin_key)])


class TenantCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    rpm_limit: int = Field(default=60, ge=1)
    daily_budget: float = Field(default=1.00, ge=0.0)


class TenantUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    rpm_limit: int | None = Field(None, ge=1)
    daily_budget: float | None = Field(None, ge=0.0)
    status: str | None = Field(None, pattern="^(active|suspended|deleted)$")


class ApiKeyResponse(BaseModel):
    id: str
    prefix: str
    created_at: datetime
    revoked_at: datetime | None = None


class TenantResponse(BaseModel):
    id: str
    name: str
    status: str
    api_key: str | None = None  # Returned once on creation
    rpm_limit: int
    daily_budget: float
    created_at: datetime
    keys: list[ApiKeyResponse] = Field(default_factory=list)


class UsageSummary(BaseModel):
    tenant_id: str
    total_requests: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_cost_estimate: float
    cache_hits: int
    cache_hit_rate: float
    recent_logs: list[dict[str, Any]]


@router.post("/tenants", response_model=TenantResponse, status_code=status.HTTP_201_CREATED)
async def create_tenant(
    body: TenantCreate,
    db: AsyncSession = Depends(get_db),
) -> TenantResponse:
    """Create a new tenant, configure their default quota, and issue their first API key."""
    tenant = Tenant(name=body.name, status="active")
    db.add(tenant)
    await db.flush()

    quota = Quota(
        tenant_id=tenant.id,
        rpm_limit=body.rpm_limit,
        daily_budget=body.daily_budget,
    )
    db.add(quota)

    raw_key, hashed = generate_api_key()
    api_key = ApiKey(
        tenant_id=tenant.id,
        key_hash=hashed,
        prefix=get_key_prefix(raw_key),
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(tenant)

    return TenantResponse(
        id=str(tenant.id),
        name=tenant.name,
        status=tenant.status,
        api_key=raw_key,
        rpm_limit=quota.rpm_limit,
        daily_budget=quota.daily_budget,
        created_at=tenant.created_at,
        keys=[
            ApiKeyResponse(
                id=str(api_key.id),
                prefix=api_key.prefix,
                created_at=api_key.created_at,
            )
        ],
    )


@router.get("/tenants", response_model=list[TenantResponse])
async def list_tenants(
    db: AsyncSession = Depends(get_db),
) -> list[TenantResponse]:
    """List all registered tenants and their provisioned key prefixes."""
    stmt = select(Tenant).options(selectinload(Tenant.quota), selectinload(Tenant.api_keys))
    result = await db.execute(stmt)
    tenants = result.scalars().all()

    response = []
    for t in tenants:
        quota = t.quota
        keys = [
            ApiKeyResponse(
                id=str(k.id),
                prefix=k.prefix,
                created_at=k.created_at,
                revoked_at=k.revoked_at,
            )
            for k in t.api_keys
        ]
        response.append(
            TenantResponse(
                id=str(t.id),
                name=t.name,
                status=t.status,
                api_key=None,
                rpm_limit=quota.rpm_limit if quota else 60,
                daily_budget=quota.daily_budget if quota else 1.0,
                created_at=t.created_at,
                keys=keys,
            )
        )
    return response


@router.patch("/tenants/{tenant_id}", response_model=TenantResponse)
async def update_tenant(
    tenant_id: str,
    body: TenantUpdate,
    db: AsyncSession = Depends(get_db),
) -> TenantResponse:
    """Update tenant configuration, limits, or status (e.g. suspend/active)."""
    try:
        tenant_uuid = uuid.UUID(tenant_id)
    except ValueError as err:
        raise HTTPException(status_code=400, detail="Invalid tenant UUID format") from err

    stmt = (
        select(Tenant)
        .where(Tenant.id == tenant_uuid)
        .options(selectinload(Tenant.quota), selectinload(Tenant.api_keys))
    )
    result = await db.execute(stmt)
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    if body.name is not None:
        tenant.name = body.name
    if body.status is not None:
        tenant.status = body.status
        # If suspended or deleted, invalidate all cached keys for this tenant
        if body.status != "active":
            for k in tenant.api_keys:
                await invalidate_auth_cache(k.key_hash)

    if tenant.quota:
        if body.rpm_limit is not None:
            tenant.quota.rpm_limit = body.rpm_limit
        if body.daily_budget is not None:
            tenant.quota.daily_budget = body.daily_budget

    await db.commit()
    await db.refresh(tenant)

    keys = [
        ApiKeyResponse(
            id=str(k.id),
            prefix=k.prefix,
            created_at=k.created_at,
            revoked_at=k.revoked_at,
        )
        for k in tenant.api_keys
    ]
    return TenantResponse(
        id=str(tenant.id),
        name=tenant.name,
        status=tenant.status,
        api_key=None,
        rpm_limit=tenant.quota.rpm_limit if tenant.quota else 60,
        daily_budget=tenant.quota.daily_budget if tenant.quota else 1.0,
        created_at=tenant.created_at,
        keys=keys,
    )


@router.delete("/tenants/{tenant_id}")
async def delete_tenant(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Soft-delete/suspend tenant and revoke all their active API keys."""
    try:
        tenant_uuid = uuid.UUID(tenant_id)
    except ValueError as err:
        raise HTTPException(status_code=400, detail="Invalid tenant UUID format") from err

    stmt = select(Tenant).where(Tenant.id == tenant_uuid).options(selectinload(Tenant.api_keys))
    result = await db.execute(stmt)
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    tenant.status = "suspended"
    now = datetime.now(UTC)
    for k in tenant.api_keys:
        k.revoked_at = now

    await db.commit()

    # Invalidate auth cache AFTER DB commit
    for k in tenant.api_keys:
        await invalidate_auth_cache(k.key_hash)

    return {"status": "success", "detail": f"Tenant {tenant_id} suspended and keys revoked"}


@router.get("/tenants/{tenant_id}/usage", response_model=UsageSummary)
async def get_tenant_usage(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
) -> UsageSummary:
    """Get aggregated usage stats and recent requests for a tenant."""
    try:
        tenant_uuid = uuid.UUID(tenant_id)
    except ValueError as err:
        raise HTTPException(status_code=400, detail="Invalid tenant UUID format") from err

    tenant = await db.get(Tenant, tenant_uuid)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    agg_stmt = select(
        func.count(RequestLog.id).label("total_reqs"),
        func.coalesce(func.sum(RequestLog.prompt_tokens), 0).label("prompt_tokens"),
        func.coalesce(func.sum(RequestLog.completion_tokens), 0).label("completion_tokens"),
        func.coalesce(func.sum(RequestLog.cost_estimate), 0.0).label("total_cost"),
    ).where(RequestLog.tenant_id == tenant_uuid)

    agg_res = (await db.execute(agg_stmt)).one()
    total_reqs = int(agg_res.total_reqs)
    prompt_tokens = int(agg_res.prompt_tokens)
    comp_tokens = int(agg_res.completion_tokens)
    cost = float(agg_res.total_cost)

    cache_stmt = select(func.count(RequestLog.id)).where(
        RequestLog.tenant_id == tenant_uuid, RequestLog.cache_hit.is_(True)
    )
    cache_hits = int((await db.execute(cache_stmt)).scalar() or 0)
    hit_rate = (cache_hits / total_reqs) if total_reqs > 0 else 0.0

    recent_stmt = (
        select(RequestLog)
        .where(RequestLog.tenant_id == tenant_uuid)
        .order_by(RequestLog.created_at.desc())
        .limit(10)
    )
    recent_logs = (await db.execute(recent_stmt)).scalars().all()

    return UsageSummary(
        tenant_id=tenant_id,
        total_requests=total_reqs,
        total_prompt_tokens=prompt_tokens,
        total_completion_tokens=comp_tokens,
        total_cost_estimate=cost,
        cache_hits=cache_hits,
        cache_hit_rate=round(hit_rate, 4),
        recent_logs=[
            {
                "id": str(log_rec.id),
                "model": log_rec.model,
                "provider": log_rec.provider,
                "latency_ms": log_rec.latency_ms,
                "status": log_rec.status,
                "cost_estimate": log_rec.cost_estimate,
                "cache_hit": log_rec.cache_hit,
                "created_at": log_rec.created_at.isoformat(),
            }
            for log_rec in recent_logs
        ],
    )


@router.post("/tenants/{tenant_id}/keys")
async def create_api_key(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Generate and return an additional API key for a tenant."""
    try:
        tenant_uuid = uuid.UUID(tenant_id)
    except ValueError as err:
        raise HTTPException(status_code=400, detail="Invalid tenant UUID format") from err

    tenant = await db.get(Tenant, tenant_uuid)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    raw_key, hashed = generate_api_key()
    api_key_rec = ApiKey(
        tenant_id=tenant.id,
        key_hash=hashed,
        prefix=get_key_prefix(raw_key),
    )
    db.add(api_key_rec)
    await db.commit()

    return {
        "tenant_id": tenant_id,
        "key_id": str(api_key_rec.id),
        "api_key": raw_key,
        "prefix": api_key_rec.prefix,
    }


@router.post("/tenants/{tenant_id}/keys/revoke")
async def revoke_api_key(
    tenant_id: str,
    key_id: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Revoke all active keys (or a specific key) for a tenant."""
    try:
        tenant_uuid = uuid.UUID(tenant_id)
    except ValueError as err:
        raise HTTPException(status_code=400, detail="Invalid tenant UUID format") from err

    now = datetime.now(UTC)
    find_stmt = select(ApiKey).where(ApiKey.tenant_id == tenant_uuid, ApiKey.revoked_at.is_(None))
    if key_id:
        try:
            key_uuid = uuid.UUID(key_id)
            find_stmt = find_stmt.where(ApiKey.id == key_uuid)
        except ValueError as err:
            raise HTTPException(status_code=400, detail="Invalid key UUID format") from err

    keys_to_revoke = (await db.execute(find_stmt)).scalars().all()

    for k in keys_to_revoke:
        k.revoked_at = now

    # Commit revocation to DB first
    await db.commit()

    # Invalidate auth cache in Redis AFTER commit so concurrent requests cannot re-cache stale key
    for k in keys_to_revoke:
        await invalidate_auth_cache(k.key_hash)

    return {
        "status": "success",
        "revoked_count": str(len(keys_to_revoke)),
        "tenant_id": tenant_id,
    }
