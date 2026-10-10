# Routewise — LLM Gateway

An OpenAI-compatible LLM gateway that sits between client apps and LLM providers, handling authentication, rate limiting, semantic caching, retries with backoff, provider fallback, and cost tracking.

## Why?

- **Eliminate duplicate costs** — semantic cache catches repeated/similar prompts
- **Survive outages** — automatic provider fallback with circuit breaker and retries with exponential backoff + jitter
- **Per-tenant control** — API keys, quotas, rate limits, daily budget enforcement, usage dashboards
- **Vendor freedom** — swap providers without changing client code
- **One endpoint** — all apps call the gateway, not individual providers

## Architecture

```
Client App / RAG Assistant
        │  (OpenAI-compatible request)
        ▼
     Nginx  ──► Gateway replica 1 ┐
             ├► Gateway replica 2 ├──► Redis (rate limits, auth cache, budget tracking)
             └► Gateway replica 3 ┘──► PostgreSQL + pgvector (tenants, logs, semantic cache)
                     │
                     ├──► Provider A: OpenRouter (with retries + backoff)
                     ├──► Provider B: Groq (fallback)
                     └──► Mock provider (load tests only)

Prometheus scrapes /metrics ──► Grafana
React admin dashboard ──► Admin API (secret-key protected)
```

## Quick Start

```bash
# Copy and fill in .env (or adjust settings)
cp .env.example .env

# Start core stack: Gateway, PostgreSQL, Redis, and Nginx (no profiles needed)
docker compose up -d

# Scale to 3 gateway replicas with automatic Nginx load balancing
docker compose up -d --scale gateway=3

# Start with full stack (monitoring + dashboard + mock-provider + RAG assistant)
docker compose --profile full up -d

# Or run individual profiles (e.g., monitoring, dashboard, testing, rag)
docker compose --profile monitoring up -d
```

## Tech Stack

| Layer         | Choice                                          |
|---------------|-------------------------------------------------|
| Gateway       | Python 3.11, FastAPI (async), httpx             |
| State         | Redis; PostgreSQL + pgvector                    |
| Dashboard     | React + Vite + Lucide icons                     |
| Packaging     | Docker, Docker Compose                          |
| Load Balancer | Nginx                                           |
| CI/CD         | GitHub Actions (lint, type-check, test, build)   |
| Monitoring    | Prometheus + Grafana                            |
| Load Testing  | Locust                                          |
| Providers     | OpenRouter, Google Gemini, Hugging Face, Groq, Mock |

## Project Structure

```
routewise/
  gateway/
    app/
      api/            # routes: chat, admin, health
      core/           # auth (with Redis cache), rate limiter (Redis Lua), circuit breaker
      providers/      # base.py, openrouter.py, gemini.py, huggingface.py, groq.py, mock.py, router.py
      cache/          # semantic cache (pgvector cosine, async embedding)
      db/             # models, session
      metrics/
    tests/
      unit/           # auth, circuit breaker, providers, semantic cache
      concurrency/    # rate limiter atomicity under burst
      integration/    # full chat flow, admin auth
    loadtests/        # Locust scripts
  mock-provider/
  dashboard/          # React admin app (Vite)
  rag-client/         # College assistant (first real client)
  deploy/             # nginx, prometheus configs
  .github/workflows/
  docs/               # architecture, design decisions
```

## What's Implemented

- **Chat completions** (`POST /v1/chat/completions`): Full pipeline — auth → rate limit → budget check → semantic cache → provider routing → metrics → background logging.
- **Streaming** (SSE): With token/cost/latency accounting logged on stream completion, and client-disconnect handling.
- **Tenant auth**: SHA-256 hashed API keys, Redis auth cache (60s TTL), revocation with cache invalidation.
- **Admin API** (`/admin/*`): Protected by `ADMIN_SECRET_KEY` (with startup protection refusing insecure default keys in production). Includes full tenant management:
  - `POST /admin/tenants` — Create tenant, configure RPM quota & budget, return key.
  - `GET /admin/tenants` — List tenants and key prefixes.
  - `PATCH /admin/tenants/{tenant_id}` — Update name, quota limits, or status (e.g. suspend).
  - `DELETE /admin/tenants/{tenant_id}` — Soft-delete / suspend tenant and revoke all keys with cache invalidation.
  - `GET /admin/tenants/{tenant_id}/usage` — Aggregate requests, prompt/completion tokens, cost, and cache hit metrics.
  - `POST /admin/tenants/{tenant_id}/keys` & `POST /admin/tenants/{tenant_id}/keys/revoke` — Key issuance & revocation.
- **Provider Routing & Mock Fallback Switch**: Maps canonical models to provider identifiers with automatic retries and failover. `ENABLE_MOCK_FALLBACK=false` ensures real production requests fail with 502 Bad Gateway rather than silently returning fake mock responses when upstreams fail.
- **Rate limiter**: Atomic Redis Lua script implementing token bucket. Uses `Redis TIME` (no clock skew), `HSET` (not deprecated `HMSET`), and recovers from `NOSCRIPT` on restarts. Fails open on Redis outage.
- **Daily budget enforcement**: Tracked per-tenant in Redis with daily keys.
- **Circuit breaker**: 3-state (CLOSED/OPEN/HALF_OPEN). Single-probe gating in HALF_OPEN with probe release on abort. Client 4xx errors filtered out; only 429 and 5xx trip the breaker.
- **Retries with backoff**: Up to 2 retries per provider with full-jitter exponential backoff before falling to next provider. Catches httpx timeouts and transport errors as retryable.
- **Semantic cache**: pgvector HNSW cosine index. Context hash includes all prior turns (not just system prompt). Async embedding via `asyncio.to_thread`. Fallback pseudo-embedding emits Prometheus counter alert.
- **Cost tracking**: Per-model pricing table. Labeled as estimates (not measured billing data).
- **Prometheus metrics**: Request count, latency histogram, token usage, cost, cache hit/miss, circuit state, rate limit rejections, active streams, fallback embedding count.
- **Background logging**: `RequestLog` writes via `BackgroundTasks` to avoid blocking responses.
- **Health probes**: `/health` (liveness), `/ready` (Postgres + Redis connectivity checks).

## Known Limitations & Trade-offs

- **Circuit breaker state is per-process.** With 3 replicas, each learns provider failures independently. A Redis-backed shared breaker would fix this but adds complexity. Documented as an accepted trade-off for now.
- **sentence-transformers pulls in torch**, making the Docker image large (~2GB+). For production, consider CPU-only torch, ONNX runtime, or an embedding API. The fallback pseudo-embedding (SHA-based) makes the cache exact-match only.
- **Auth lookup hits Postgres on cache miss.** Redis auth cache (60s TTL) mitigates this, but a cold-start burst will still hit the DB.
- **Type Checking & Linting**: `mypy --strict` and `ruff` are fully configured, validated, and passing with zero errors across all 24 gateway source files and unit tests.
- **CD is a TODO.** CI builds the Docker image but doesn't push to a registry or deploy.

## Benchmarks

_To be filled with measured numbers after load testing._

| Metric               | Baseline (1 replica) | Scaled (3 replicas) |
|----------------------|---------------------:|--------------------:|
| RPS                  |                      |                     |
| p50 latency (ms)     |                      |                     |
| p95 latency (ms)     |                      |                     |
| p99 latency (ms)     |                      |                     |
| Cache hit rate        |                      |                     |
| Failure recovery     |                      |                     |

## Design Decisions

See [docs/design_decisions.md](docs/design_decisions.md) for detailed rationale on:
- Why Redis Lua for rate limiting (atomicity, no distributed locks)
- Why pgvector over a dedicated vector DB (consolidation, relational tenant isolation)
- Why circuit breaker with client 4xx filtering
- Why cache eligibility is restricted to low-temperature non-streaming requests

## License

MIT
