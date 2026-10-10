"""Integration tests for Routewise Gateway end-to-end chat flow and admin security."""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.auth import get_current_tenant
from app.db.models import Quota, Tenant
from app.main import app


@pytest.fixture
def mock_active_tenant():
    """Create a mock Tenant instance with quota."""
    t = Tenant(
        id=uuid.uuid4(),
        name="Integration Test Tenant",
        status="active",
    )
    t.quota = Quota(tenant_id=t.id, rpm_limit=100, daily_budget=10.0)
    return t


@pytest.mark.asyncio
async def test_chat_completions_missing_auth():
    """Verify missing Authorization header returns 401 (not 422)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "mock-gpt",
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )
        assert resp.status_code == 401
        assert "Missing Authorization header" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_admin_api_rejects_without_secret_key():
    """Verify that reaching /admin without admin secret returns 401."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/admin/tenants")
        assert resp.status_code == 401
        assert "admin secret key" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_admin_api_rejects_wrong_secret_key():
    """Wrong admin key is still rejected."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/admin/tenants",
            headers={"X-Admin-Key": "wrong_key_12345"},
        )
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_chat_completions_mock_flow(mock_active_tenant):
    """Verify full end-to-end chat completion execution using dependency override."""
    app.dependency_overrides[get_current_tenant] = lambda: mock_active_tenant

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            payload = {
                "model": "mock-gpt",
                "temperature": 0.0,
                "messages": [
                    {"role": "system", "content": "You are a test assistant."},
                    {"role": "user", "content": "Hello Routewise End-to-End!"},
                ],
            }
            resp = await client.post(
                "/v1/chat/completions",
                json=payload,
                headers={"Authorization": "Bearer rw_dummy_test_key"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "choices" in data
            assert len(data["choices"]) > 0
            assert "content" in data["choices"][0]["message"]
            assert data["usage"]["total_tokens"] > 0
            assert "X-RateLimit-Limit" in resp.headers
            assert resp.headers.get("X-Cache-Hit") in ("true", "false")
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_health_check_endpoint():
    """Verify public health liveness probe."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
