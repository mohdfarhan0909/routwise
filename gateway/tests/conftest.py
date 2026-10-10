"""
Shared pytest configuration and fixtures.

Sets APP_ENV=testing before any module import so the mock provider adapter
falls back to local simulation (no external HTTP needed) and the gateway app
can be instantiated without real Postgres/Redis connections.
"""

import os

# Must be set before any app module is imported so config reads it correctly
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://routewise:testpass@localhost:5432/routewise_test"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("ADMIN_SECRET_KEY", "test_admin_key_for_ci")
os.environ.setdefault("CACHE_ENABLED", "false")  # Disable DB-backed cache in unit tests
