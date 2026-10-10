"""Health and readiness check endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db, redis_client

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
async def health_check() -> dict[str, str]:
    """Basic liveness check."""
    return {"status": "ok"}


@router.get("/ready")
async def readiness_check(db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    """Readiness check — verifies DB and Redis connectivity."""
    checks = {"postgres": "ok", "redis": "ok"}
    is_ready = True

    try:
        await db.execute(text("SELECT 1"))
    except Exception as e:
        logger.error("Readiness check failed for PostgreSQL: %s", e)
        checks["postgres"] = "unhealthy"
        is_ready = False

    try:
        await redis_client.ping()
    except Exception as e:
        logger.error("Readiness check failed for Redis: %s", e)
        checks["redis"] = "unhealthy"
        is_ready = False

    if not is_ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "not_ready", "checks": checks},
        )

    return {"status": "ready", **checks}
