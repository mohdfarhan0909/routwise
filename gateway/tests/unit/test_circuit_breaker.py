"""Unit tests for Circuit Breaker."""

import asyncio

import pytest

from app.core.circuit_breaker import CircuitBreaker, CircuitState


@pytest.mark.asyncio
async def test_circuit_breaker_initial_state():
    cb = CircuitBreaker(provider_name="test_p", failure_threshold=3, recovery_timeout=0.2)
    assert cb.state == CircuitState.CLOSED
    assert await cb.allow_request() is True


@pytest.mark.asyncio
async def test_circuit_breaker_trips_to_open():
    """Circuit opens after reaching failure_threshold consecutive server failures."""
    cb = CircuitBreaker(provider_name="test_p", failure_threshold=1, recovery_timeout=0.2)

    # First server error (no status_code → infrastructure failure) trips the breaker
    await cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert await cb.allow_request() is False


@pytest.mark.asyncio
async def test_circuit_breaker_ignores_client_4xx():
    """Client errors (400, 404, 422) must NOT trip the circuit."""
    cb = CircuitBreaker(provider_name="test_p", failure_threshold=3, recovery_timeout=0.2)

    await cb.record_failure(status_code=400)
    assert cb.state == CircuitState.CLOSED
    await cb.record_failure(status_code=404)
    assert cb.state == CircuitState.CLOSED
    await cb.record_failure(status_code=422)
    assert cb.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_circuit_breaker_429_trips():
    """429 Too Many Requests from upstream IS a circuit-tripping failure."""
    cb = CircuitBreaker(provider_name="test_p", failure_threshold=1, recovery_timeout=0.2)

    await cb.record_failure(status_code=429)
    assert cb.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_circuit_breaker_recovers_to_half_open_and_closed():
    cb = CircuitBreaker(provider_name="test_p", failure_threshold=1, recovery_timeout=0.1)

    await cb.record_failure()
    assert cb.state == CircuitState.OPEN

    # Wait for cooldown timeout
    await asyncio.sleep(0.15)
    assert cb.state == CircuitState.HALF_OPEN

    # First request allowed (probe)
    assert await cb.allow_request() is True
    # Second concurrent request blocked during probe
    assert await cb.allow_request() is False

    # Record success to close circuit
    await cb.record_success()
    assert cb.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_circuit_breaker_multi_failure_threshold():
    """Circuit stays CLOSED until failure_threshold is reached."""
    cb = CircuitBreaker(provider_name="test_p", failure_threshold=3, recovery_timeout=0.2)
    assert cb.state == CircuitState.CLOSED

    # Failure 1
    await cb.record_failure()
    assert cb.state == CircuitState.CLOSED
    assert await cb.allow_request() is True

    # Failure 2
    await cb.record_failure()
    assert cb.state == CircuitState.CLOSED
    assert await cb.allow_request() is True

    # Failure 3: trips to OPEN
    await cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert await cb.allow_request() is False


@pytest.mark.asyncio
async def test_circuit_breaker_release_probe():
    """release_probe releases in-flight probe slot when request aborts."""
    cb = CircuitBreaker(provider_name="test_p", failure_threshold=1, recovery_timeout=0.05)
    await cb.record_failure()
    assert cb.state == CircuitState.OPEN

    await asyncio.sleep(0.08)
    assert cb.state == CircuitState.HALF_OPEN

    # First probe acquired
    assert await cb.allow_request() is True
    # Second concurrent probe is blocked
    assert await cb.allow_request() is False

    # Simulate probe aborted / cancelled
    await cb.release_probe()

    # Next attempt can now acquire the probe
    assert await cb.allow_request() is True

