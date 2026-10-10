"""Prometheus metrics setup."""

from fastapi import FastAPI
from prometheus_client import Counter, Gauge, Histogram, make_asgi_app

# Request metrics
REQUEST_COUNT = Counter(
    "routewise_requests_total",
    "Total requests processed",
    ["tenant_id", "model", "provider", "status", "cache_hit"],
)

REQUEST_LATENCY = Histogram(
    "routewise_request_latency_seconds",
    "Request latency in seconds",
    ["provider", "model"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
)

TOKENS_USED = Counter(
    "routewise_tokens_total",
    "Total tokens consumed",
    ["tenant_id", "model", "direction"],  # direction: prompt | completion
)

COST_TOTAL = Counter(
    "routewise_cost_dollars_total",
    "Estimated cost in USD",
    ["tenant_id", "model", "provider"],
)

CACHE_HIT_RATE = Counter(
    "routewise_cache_lookups_total",
    "Semantic cache lookups",
    ["result"],  # hit | miss
)

ACTIVE_STREAMS = Gauge(
    "routewise_active_streams",
    "Currently active SSE streams",
)

CIRCUIT_STATE = Gauge(
    "routewise_circuit_breaker_state",
    "Circuit breaker state (0=closed, 1=open, 2=half_open)",
    ["provider"],
)

RATE_LIMIT_REJECTIONS = Counter(
    "routewise_rate_limit_rejections_total",
    "Requests rejected by rate limiter",
    ["tenant_id"],
)


def setup_metrics(app: FastAPI) -> None:
    """Mount the Prometheus /metrics endpoint."""
    metrics_app = make_asgi_app()
    app.mount("/metrics", metrics_app)
