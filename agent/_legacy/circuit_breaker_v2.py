"""OMNI Agent — Circuit Breaker V2: per-endpoint breaker with half-open probing & bulkhead."""
from __future__ import annotations
import asyncio, threading, time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional, Type


class BreakerState(str, Enum):
    CLOSED    = "closed"      # normal operation
    OPEN      = "open"        # refusing calls
    HALF_OPEN = "half_open"   # probing recovery


class BreakerOpenError(Exception):
    pass


class BulkheadFullError(Exception):
    pass


@dataclass
class BreakerConfig:
    failure_threshold: int   = 5      # failures to open
    success_threshold: int   = 2      # successes in half-open to close
    timeout_s: float         = 30.0   # open → half-open after this
    half_open_max_calls: int = 3      # max concurrent probes
    excluded_exceptions: List[Type[Exception]] = field(default_factory=list)


@dataclass
class BreakerStats:
    calls: int = 0
    successes: int = 0
    failures: int = 0
    rejected: int = 0
    state_changes: int = 0
    last_failure_ts: Optional[float] = None
    last_success_ts: Optional[float] = None

    @property
    def failure_rate(self) -> float:
        return self.failures / self.calls if self.calls > 0 else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "calls": self.calls,
            "successes": self.successes,
            "failures": self.failures,
            "rejected": self.rejected,
            "failure_rate": round(self.failure_rate, 4),
            "state_changes": self.state_changes,
        }


class CircuitBreaker:
    """Single circuit breaker for one endpoint/service."""

    def __init__(self, name: str, config: Optional[BreakerConfig] = None):
        self.name = name
        self.config = config or BreakerConfig()
        self._state = BreakerState.CLOSED
        self._failure_count = 0
        self._half_open_successes = 0
        self._half_open_calls = 0
        self._opened_at: Optional[float] = None
        self._lock = threading.Lock()
        self.stats = BreakerStats()
        self._on_state_change: List[Callable] = []

    @property
    def state(self) -> BreakerState:
        self._check_timeout()
        return self._state

    def _check_timeout(self):
        if self._state == BreakerState.OPEN and self._opened_at:
            if time.time() - self._opened_at >= self.config.timeout_s:
                self._transition(BreakerState.HALF_OPEN)

    def _transition(self, new_state: BreakerState):
        if self._state == new_state:
            return
        self._state = new_state
        self.stats.state_changes += 1
        if new_state == BreakerState.OPEN:
            self._opened_at = time.time()
            self._half_open_successes = 0
            self._half_open_calls = 0
        elif new_state == BreakerState.CLOSED:
            self._failure_count = 0
            self._half_open_successes = 0
            self._half_open_calls = 0
        for hook in self._on_state_change:
            try:
                hook(self.name, new_state)
            except Exception:
                pass

    def _record_success(self):
        self.stats.calls += 1
        self.stats.successes += 1
        self.stats.last_success_ts = time.time()
        with self._lock:
            if self._state == BreakerState.HALF_OPEN:
                self._half_open_successes += 1
                if self._half_open_successes >= self.config.success_threshold:
                    self._transition(BreakerState.CLOSED)
            elif self._state == BreakerState.CLOSED:
                self._failure_count = 0

    def _record_failure(self, exc: Exception):
        # Don't count excluded exceptions as failures
        if any(isinstance(exc, t) for t in self.config.excluded_exceptions):
            return
        self.stats.calls += 1
        self.stats.failures += 1
        self.stats.last_failure_ts = time.time()
        with self._lock:
            if self._state == BreakerState.HALF_OPEN:
                self._transition(BreakerState.OPEN)
            elif self._state == BreakerState.CLOSED:
                self._failure_count += 1
                if self._failure_count >= self.config.failure_threshold:
                    self._transition(BreakerState.OPEN)

    def _allow_call(self) -> bool:
        state = self.state  # triggers timeout check
        if state == BreakerState.CLOSED:
            return True
        if state == BreakerState.OPEN:
            self.stats.rejected += 1
            return False
        # HALF_OPEN
        with self._lock:
            if self._half_open_calls < self.config.half_open_max_calls:
                self._half_open_calls += 1
                return True
            self.stats.rejected += 1
            return False

    def call(self, fn: Callable, *args, **kwargs) -> Any:
        if not self._allow_call():
            raise BreakerOpenError(f"Circuit breaker '{self.name}' is OPEN")
        try:
            result = fn(*args, **kwargs)
            self._record_success()
            return result
        except Exception as exc:
            self._record_failure(exc)
            raise

    async def call_async(self, fn: Callable[..., Coroutine], *args, **kwargs) -> Any:
        if not self._allow_call():
            raise BreakerOpenError(f"Circuit breaker '{self.name}' is OPEN")
        try:
            result = await fn(*args, **kwargs)
            self._record_success()
            return result
        except Exception as exc:
            self._record_failure(exc)
            raise

    def reset(self):
        with self._lock:
            self._failure_count = 0
            self._half_open_successes = 0
            self._half_open_calls = 0
            self._transition(BreakerState.CLOSED)

    def on_state_change(self, fn: Callable):
        self._on_state_change.append(fn)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self._failure_count,
            **self.stats.to_dict(),
        }


class Bulkhead:
    """Limits concurrent calls to a resource (thread-pool isolation pattern)."""

    def __init__(self, name: str, max_concurrent: int, queue_size: int = 0):
        self.name = name
        self.max_concurrent = max_concurrent
        self.queue_size = queue_size
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active = 0
        self._rejected = 0
        self._total = 0

    async def call(self, fn: Callable[..., Coroutine], *args, **kwargs) -> Any:
        if self._active >= self.max_concurrent:
            self._rejected += 1
            raise BulkheadFullError(f"Bulkhead '{self.name}' is full ({self.max_concurrent})")
        self._active += 1
        self._total += 1
        try:
            return await fn(*args, **kwargs)
        finally:
            self._active -= 1

    def stats(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "max_concurrent": self.max_concurrent,
            "active": self._active,
            "total": self._total,
            "rejected": self._rejected,
        }


class CircuitBreakerRegistry:
    """Registry of named circuit breakers with global stats."""

    def __init__(self):
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._bulkheads: Dict[str, Bulkhead] = {}

    def get_or_create(self, name: str,
                      config: Optional[BreakerConfig] = None) -> CircuitBreaker:
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(name, config)
        return self._breakers[name]

    def get_bulkhead(self, name: str, max_concurrent: int = 10) -> Bulkhead:
        if name not in self._bulkheads:
            self._bulkheads[name] = Bulkhead(name, max_concurrent)
        return self._bulkheads[name]

    def reset_all(self):
        for b in self._breakers.values():
            b.reset()

    def stats_all(self) -> Dict[str, Any]:
        return {name: b.to_dict() for name, b in self._breakers.items()}

    def open_count(self) -> int:
        return sum(1 for b in self._breakers.values()
                   if b.state == BreakerState.OPEN)

    def list_breakers(self) -> List[str]:
        return list(self._breakers.keys())
