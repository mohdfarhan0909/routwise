"""Routewise LLM Gateway — Application Entry Point."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.admin import router as admin_router
from app.api.chat import router as chat_router
from app.api.health import router as health_router
from app.core.config import settings
from app.db.session import close_db, init_db
from app.metrics.prometheus import setup_metrics

logger = logging.getLogger(__name__)


async def _periodic_cache_eviction() -> None:
    """Background loop to periodically evict expired semantic cache entries."""
    from app.cache.semantic_cache import evict_expired

    while True:
        try:
            await asyncio.sleep(600)  # Evict every 10 minutes
            evicted = await evict_expired()
            if evicted > 0:
                logger.info("Evicted %d expired semantic cache entries", evicted)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Periodic cache eviction error: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup and shutdown lifecycle."""
    env = settings.app_env.lower()
    default_key = "rw_admin_supersecret_key_change_in_production"
    if env not in ("development", "dev", "local", "testing", "test") and (
        settings.admin_secret_key == default_key
    ):
        raise RuntimeError(
            f"FATAL: Insecure default ADMIN_SECRET_KEY in non-development env '{env}'. "
            "Configure a secure ADMIN_SECRET_KEY before starting."
        )

    await init_db()
    evict_task = asyncio.create_task(_periodic_cache_eviction())
    yield
    evict_task.cancel()
    with suppress(asyncio.CancelledError):
        await evict_task
    await close_db()


app = FastAPI(
    title="Routewise",
    description="OpenAI-compatible LLM Gateway with auth, rate limits, caching, and fallback",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — configured origins compatible with allow_credentials=True
cors_origins = list(
    set(
        [
            settings.dashboard_origin.rstrip("/"),
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:80",
            "http://localhost:8000",
        ]
    )
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount routes
app.include_router(health_router, tags=["health"])
app.include_router(chat_router, prefix="/v1", tags=["chat"])
app.include_router(admin_router, prefix="/admin", tags=["admin"])

# Prometheus metrics
setup_metrics(app)
