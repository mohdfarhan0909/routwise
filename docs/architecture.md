# Routewise Gateway Architecture

## Overview

Routewise is an OpenAI-compatible reverse proxy / gateway for Large Language Models. It decouples client applications from upstream provider APIs (OpenRouter, Groq, local models) while adding security, cost controls, caching, and resiliency.

```
 Client Application (RAG / Assistant)
                 │
                 ▼  Authorization: Bearer rw_...
           [ Nginx LB ] (Optional Multi-replica)
                 │
        ┌────────┴────────┐
        ▼                 ▼
 Gateway Replica 1    Gateway Replica 2
   │      │              │      │
   │      └──────┬───────┘      │
   │             ▼              │
   │       [ Redis ]            │
   │   - Token Bucket RPM       │
   │   - In-memory rate state   │
   │                            │
   └─────────────┬──────────────┘
                 ▼
     [ PostgreSQL + pgvector ]
       - Tenant accounts & API keys
       - Request logs & audit trail
       - HNSW index for semantic cache
                 │
  ┌──────────────┼──────────────┐
  ▼              ▼              ▼
OpenRouter     Groq      Mock Provider (Testing)
```

## Request Flow Lifecycle

1. **Authentication & Admin Security (`app/core/auth.py`, `app/api/admin.py`)**:
   - Client sends API key in `Authorization: Bearer rw_<secret>` header.
   - Gateway hashes the key with SHA-256 and retrieves the `Tenant` and associated `Quota` from PostgreSQL.
   - Rejects inactive or suspended tenants with `401 Unauthorized` or `403 Forbidden`.
   - Admin endpoints (`/admin/*`) are strictly protected via an admin secret key (`X-Admin-Key: <secret>` or `Authorization: Bearer <secret>`). In non-development environments, the gateway refuses to boot if configured with the default secret key.
   - Tenant lifecycle is fully managed via `POST /admin/tenants`, `GET /admin/tenants`, `PATCH /admin/tenants/{tenant_id}` (update limits or status), and `DELETE /admin/tenants/{tenant_id}` (suspends tenant, revokes API keys, and invalidates Redis auth cache).

2. **Atomic Rate Limiter (`app/core/rate_limiter.py`)**:
   - Executes an atomic Redis Lua script implementing a continuous token bucket algorithm.
   - Evaluates tenant's current tokens against their assigned RPM limit.
   - Emits `X-RateLimit-Limit` and `X-RateLimit-Remaining` HTTP response headers.
   - Recovers automatically via script reload if Redis restarts and returns `NOSCRIPT`.
   - If Redis is down, fails open to prevent outage cascades.

3. **Semantic Caching (`app/cache/semantic_cache.py`)**:
   - For deterministic requests (`temperature <= 0.3`, non-streaming):
     - Calculates a SHA-256 hash across system prompts and all prior conversation turns (`context_hash`) to avoid context leakage.
     - Computes a 384-dimensional prompt embedding asynchronously in an executor pool to prevent blocking the event loop.
     - Searches pgvector `cache_entries` table with an HNSW cosine distance index scoped by tenant, model, and context hash.
     - If cosine similarity >= 0.95, returns the cached completion instantly without querying upstream providers.
     - Gracefully degrades to exact-hash matching and increments a Prometheus metric if model dependencies are missing.

4. **Dynamic Routing & Resilient Circuit Breakers (`app/providers/router.py`, `app/core/circuit_breaker.py`)**:
   - Upstream providers are wrapped in a 3-state circuit breaker (`CLOSED`, `OPEN`, `HALF_OPEN`).
   - Circuit breakers only count 5xx server errors and upstream timeouts towards failure threshold; 4xx client errors are passed through without penalizing provider health.
   - In `HALF_OPEN` state, exactly one probe request is permitted through; in-flight probe locks are safely released if requests abort.
   - If a provider triggers 5 consecutive failures, its circuit trips to `OPEN` for a 30s recovery period.
   - The router automatically falls back to secondary healthy providers (e.g., OpenRouter -> Groq). The mock provider is strictly gated behind the `ENABLE_MOCK_FALLBACK` environment switch, ensuring failed real queries result in HTTP 502 rather than deceptive mock responses in production.

5. **Observability & Accounting (`app/metrics/prometheus.py`)**:
   - Prometheus metrics record request latency, total tokens (prompt vs completion), estimated cost in USD, and cache hit/miss counts.
   - Detailed execution records are persisted asynchronously to `request_logs`.
