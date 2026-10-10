# Key Design Decisions

## 1. Token Bucket Rate Limiting with Redis Lua Scripting
- **Decision**: Use Redis with a server-side Lua script to implement continuous refill token bucket limiting.
- **Rationale**:
  - Eliminates race conditions in high-concurrency environments without requiring distributed locks.
  - Read, refill, and decrement occur in a single atomic network roundtrip.
  - Fail-Open Policy: If Redis is temporarily unreachable, requests are allowed through with a warning rather than causing a cascading total service denial.

## 2. PostgreSQL + pgvector for Semantic Caching
- **Decision**: Store vector embeddings directly in PostgreSQL using `pgvector` with HNSW cosine indexes rather than a dedicated vector database (like Pinecone or Milvus).
- **Rationale**:
  - Keeps state consolidated in a single primary database system alongside tenants, quotas, and logs.
  - Multi-tenant isolation is straightforward via relational joins on `tenant_id` and `context_hash`.
  - HNSW index provides sub-millisecond nearest neighbor search for 384-dimensional vectors.

## 3. Circuit Breaker for Upstream Providers
- **Decision**: Per-provider 3-state circuit breaker (`CLOSED`, `OPEN`, `HALF_OPEN`) with automatic cooldown.
- **Rationale**:
  - LLM providers experience intermittent rate limits (`429`) or partial outages.
  - Rapidly failing fast prevents connection pool exhaustion and threads piling up.
  - Probe request in `HALF_OPEN` state allows automatic, zero-downtime recovery when the provider restores service.

## 4. Only Cache Low-Temperature Deterministic Queries
- **Decision**: Restrict semantic caching to `temperature <= 0.3` and non-streaming requests.
- **Rationale**:
  - High-temperature queries explicitly seek non-deterministic, creative variation. Caching them would degrade user experience and violate prompt intent.
  - System prompt hashing (`context_hash`) prevents cross-prompt contamination where identical user queries expect different persona behaviors.
