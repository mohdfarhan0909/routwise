"""API route package."""

from app.api.admin import router as admin_router
from app.api.chat import router as chat_router
from app.api.health import router as health_router

__all__ = ["admin_router", "chat_router", "health_router"]
