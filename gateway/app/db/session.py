import logging
from collections.abc import AsyncGenerator
from contextlib import suppress

import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

logger = logging.getLogger(__name__)

engine = create_async_engine(
    settings.effective_database_url,
    echo=settings.debug,
    pool_size=10,
    max_overflow=5,
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Redis client (shared)
redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields an async DB session."""
    async with async_session() as session:
        yield session


async def init_db() -> None:
    """Run on startup — create vector extension and tables if needed."""
    from app.db.models import Base

    try:
        async with engine.begin() as conn:
            try:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
            except Exception:
                logger.warning(
                    "Could not execute CREATE EXTENSION vector; maybe already installed"
                )
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database initialized")
    except Exception as e:
        if settings.app_env.lower() in ("development", "dev", "local", "testing", "test"):
            logger.warning(
                "Database unavailable (%s); continuing in development mode without persistence",
                e,
            )
        else:
            raise


async def close_db() -> None:
    """Run on shutdown — close connections."""
    with suppress(Exception):
        await engine.dispose()
    with suppress(Exception):
        await redis_client.aclose()
    logger.info("Database connections closed")

