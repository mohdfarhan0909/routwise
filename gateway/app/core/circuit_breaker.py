"""Circuit breaker for provider failover.

States:
  CLOSED    — requests flow normally
  OPEN      — provider is down, reject immediately and fall back
  HALF_OPEN — send a single probe request; if it succeeds, close; if it fails, re-open

Client 4xx errors (e.g. 400 Bad Request, 404) are ignored by the failure counter so bad
client queries cannot trip upstream provider circuits.
"""

import asyncio
import logging
import time
from enum import Enum

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Per-provider circuit breaker with single-probe half-open gating and failure threshold."""

    def __init__(
        self,
        provider_name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        probe_timeout: float = 15.0,
    ) -> None:
        self.provider_name = provider_name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.probe_timeout = probe_timeout

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._half_open_probe_in_flight = False
        self._probe_start_time = 0.0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN and (
            time.monotonic() - self._last_failure_time >= self.recovery_timeout
        ):
            return CircuitState.HALF_OPEN
        return self._state

    async def allow_request(self) -> bool:
        """Check if a request is allowed through.

        In HALF_OPEN state, permits exactly ONE in-flight probe request at a time.
        Automatically resets the probe flag if a probe was abandoned or timed out.
        """
        async with self._lock:
            current = self.state
            if current == CircuitState.CLOSED:
                return True
            if current == CircuitState.HALF_OPEN:
                now = time.monotonic()
                # Auto-recover wedged probe if abandoned/timed out
                if self._half_open_probe_in_flight and (
                    now - self._probe_start_time > self.probe_timeout
                ):
                    logger.warning(
                        "HALF_OPEN probe for %s exceeded probe timeout (%.1fs); releasing lock",
                        self.provider_name,
                        self.probe_timeout,
                    )
                    self._half_open_probe_in_flight = False

                if not self._half_open_probe_in_flight:
                    self._half_open_probe_in_flight = True
                    self._probe_start_time = now
                    logger.info(
                        "Allowing single probe request in HALF_OPEN for %s", self.provider_name
                    )
                    return True
                return False
            return False

    async def release_probe(self) -> None:
        """Release the probe slot if the probe request was cancelled or aborted."""
        async with self._lock:
            self._half_open_probe_in_flight = False

    async def record_success(self) -> None:
        """Record a successful call and reset circuit."""
        async with self._lock:
            self._failure_count = 0
            self._half_open_probe_in_flight = False
            if self._state != CircuitState.CLOSED:
                logger.info("Circuit CLOSED for %s", self.provider_name)
            self._state = CircuitState.CLOSED

    async def record_failure(self, status_code: int | None = None) -> None:
        """Record a failed call.

        Filters out client errors (400, 404, 422 etc.) so bad client queries
        cannot maliciously or accidentally trip upstream circuits.
        Only transitions to OPEN if failure_threshold is reached or if probing in HALF_OPEN.
        """
        if status_code is not None and 400 <= status_code < 500 and status_code != 429:
            # Client error, don't penalize upstream circuit
            async with self._lock:
                self._half_open_probe_in_flight = False
            return

        async with self._lock:
            current = self.state
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            self._half_open_probe_in_flight = False

            if current == CircuitState.HALF_OPEN or self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                logger.warning(
                    "Circuit OPENED for %s (failures: %d/%d, state: %s)",
                    self.provider_name,
                    self._failure_count,
                    self.failure_threshold,
                    self._state.value,
                )
            else:
                logger.info(
                    "Recorded failure for %s (count: %d/%d, still CLOSED)",
                    self.provider_name,
                    self._failure_count,
                    self.failure_threshold,
                )
