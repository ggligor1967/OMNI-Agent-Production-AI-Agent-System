"""OMNI AGENT - Circuit Breaker
Fault-tolerance pattern: CLOSED/OPEN/HALF_OPEN states with configurable
failure thresholds, timeouts, and probe-based recovery.

Features:
- States: CLOSED (normal), OPEN (blocking), HALF_OPEN (probing)
- CLOSED → OPEN: failure_count >= threshold within window_s
- OPEN → HALF_OPEN: after timeout_s elapses
- HALF_OPEN → CLOSED: probe_successes consecutive successes
- HALF_OPEN → OPEN: any failure resets probe counter
- Failure types: exception, timeout, or user-defined predicate
- Success/failure tracking: rolling window (last N calls)
- Timeout wrapping: asyncio.wait_for with configurable timeout_s
- Fallback: optional fn called when circuit is OPEN
- Half-open probe limit: only allow N concurrent probes
- Manual override: force_open(), force_close()
- Metrics: state, failure_rate, last_failure_time, trips count
- Multiple breakers: registry pattern for named instances
- Decorator: @circuit_breaker("name") wraps sync/async functions
- State change hooks: on_open, on_close, on_half_open callbacks
- SQLite persistence: state transitions, failure log
- REST API: status, reset, force_open, force_close, stats
"""
import asyncio, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class State(str, Enum):
    CLOSED    = "closed"
    OPEN      = "open"
    HALF_OPEN = "half_open"

class CircuitOpenError(Exception):
    """Raised when a call is attempted on an OPEN circuit."""
    def __init__(self, name: str, retry_after: float = 0):
        self.retry_after = retry_after
        super().__init__(f"Circuit {name!r} is OPEN. Retry after {retry_after:.1f}s")

@dataclass
class CallRecord:
    success: bool; ts: float; latency_ms: float; error: str = ""

@dataclass
class CBConfig:
    name: str
    failure_threshold: int = 5      # failures in window to trip
    window_s: float = 60.0           # rolling failure window
    timeout_s: float = 30.0          # open circuit timeout
    probe_successes: int = 2         # successes needed to close from half-open
    probe_concurrency: int = 1       # max concurrent probes in half-open
    call_timeout_s: float = 0.0      # 0 = no timeout on individual calls
    failure_predicate: Optional[Callable] = None  # fn(result) -> bool = failure?

class CBStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS transitions(
                    id TEXT PRIMARY KEY, name TEXT,
                    from_state TEXT, to_state TEXT,
                    reason TEXT DEFAULT '', created_at REAL);
                CREATE TABLE IF NOT EXISTS failures(
                    id TEXT PRIMARY KEY, name TEXT,
                    error TEXT, latency_ms REAL, created_at REAL);
                CREATE INDEX IF NOT EXISTS idx_trans_name
                    ON transitions(name, created_at DESC);
            """)

    def log_transition(self, name: str, from_s: str, to_s: str, reason: str = ""):
        with self._conn() as c:
            c.execute("INSERT INTO transitions VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4())[:8], name, from_s, to_s, reason, time.time()))

    def log_failure(self, name: str, error: str, latency_ms: float):
        with self._conn() as c:
            c.execute("INSERT INTO failures VALUES(?,?,?,?,?)",
                (str(uuid.uuid4())[:8], name, error[:300], latency_ms, time.time()))

    def stats(self) -> Dict:
        with self._conn() as c:
            nt = c.execute("SELECT COUNT(*) FROM transitions").fetchone()[0]
            nf = c.execute("SELECT COUNT(*) FROM failures").fetchone()[0]
            trips = c.execute(
                "SELECT COUNT(*) FROM transitions WHERE to_state='open'"
            ).fetchone()[0]
        return {"transitions": nt, "failures_logged": nf, "total_trips": trips}

class _BreakerInstance:
    def __init__(self, config: CBConfig, store: CBStore):
        self.config = config
        self._store = store
        self._state = State.CLOSED
        self._calls: List[CallRecord] = []     # rolling window
        self._opened_at: float = 0.0
        self._probe_ok: int = 0
        self._probe_inflight: int = 0
        self._trips: int = 0
        self._hooks: Dict[str, List[Callable]] = {
            "on_open": [], "on_close": [], "on_half_open": []}
        self._forced: Optional[State] = None

    @property
    def state(self) -> State:
        if self._forced: return self._forced
        return self._state

    def _transition(self, new_state: State, reason: str = ""):
        old = self._state
        self._state = new_state
        self._store.log_transition(self.config.name, old.value, new_state.value, reason)
        if new_state == State.OPEN:
            self._trips += 1
            for h in self._hooks["on_open"]: 
                try: h(self)
                except: pass
        elif new_state == State.CLOSED:
            for h in self._hooks["on_close"]:
                try: h(self)
                except: pass
        elif new_state == State.HALF_OPEN:
            for h in self._hooks["on_half_open"]:
                try: h(self)
                except: pass

    def _prune_window(self):
        cutoff = time.time() - self.config.window_s
        self._calls = [c for c in self._calls if c.ts >= cutoff]

    def _failure_count(self) -> int:
        self._prune_window()
        return sum(1 for c in self._calls if not c.success)

    def _check_open(self):
        if self._failure_count() >= self.config.failure_threshold:
            self._opened_at = time.time()
            self._transition(State.OPEN, "failure threshold reached")

    def _maybe_probe(self) -> bool:
        """If OPEN and timeout elapsed, move to HALF_OPEN and allow probe."""
        if self._state == State.OPEN:
            elapsed = time.time() - self._opened_at
            if elapsed >= self.config.timeout_s:
                self._probe_ok = 0
                self._probe_inflight = 0
                self._transition(State.HALF_OPEN, "timeout elapsed")
        if self._state == State.HALF_OPEN:
            return self._probe_inflight < self.config.probe_concurrency
        return False

    def allow_request(self) -> bool:
        if self._forced: return self._forced != State.OPEN
        if self._state == State.CLOSED: return True
        if self._state == State.OPEN:
            self._maybe_probe()
            return self._state == State.HALF_OPEN and self._maybe_probe()
        return self._maybe_probe()

    def record_success(self, latency_ms: float = 0):
        rec = CallRecord(success=True, ts=time.time(), latency_ms=latency_ms)
        self._calls.append(rec)
        if self._state == State.HALF_OPEN:
            self._probe_inflight = max(0, self._probe_inflight - 1)
            self._probe_ok += 1
            if self._probe_ok >= self.config.probe_successes:
                self._transition(State.CLOSED, "probe successes met")

    def record_failure(self, error: str = "", latency_ms: float = 0):
        rec = CallRecord(success=False, ts=time.time(),
                          latency_ms=latency_ms, error=error)
        self._calls.append(rec)
        self._store.log_failure(self.config.name, error, latency_ms)
        if self._state == State.HALF_OPEN:
            self._probe_inflight = max(0, self._probe_inflight - 1)
            self._probe_ok = 0
            self._opened_at = time.time()
            self._transition(State.OPEN, "probe failed")
        else:
            self._check_open()

    def force_open(self):
        self._forced = State.OPEN
        self._opened_at = time.time()

    def force_close(self):
        self._forced = None
        self._state = State.CLOSED
        self._calls.clear()

    def retry_after(self) -> float:
        if self._state == State.OPEN:
            return max(0.0, self.config.timeout_s - (time.time() - self._opened_at))
        return 0.0

    @property
    def failure_rate(self) -> float:
        self._prune_window()
        if not self._calls: return 0.0
        return sum(1 for c in self._calls if not c.success) / len(self._calls)

    def status(self) -> Dict:
        self._prune_window()
        total = len(self._calls)
        fails = sum(1 for c in self._calls if not c.success)
        return {"name": self.config.name, "state": self.state.value,
                "failure_rate": round(self.failure_rate, 4),
                "failure_count": fails, "call_count": total,
                "trips": self._trips,
                "retry_after_s": round(self.retry_after(), 2)}

class CircuitBreakerRegistry:
    """
    Registry of named circuit breakers with call wrapping and decorator.

    Usage:
        registry = CircuitBreakerRegistry()
        registry.register("payment_api", failure_threshold=3, timeout_s=10)

        async def charge_card(amount):
            return await registry.call("payment_api", _charge_impl, amount)

        # Or as decorator:
        @registry.protect("payment_api")
        async def charge_card(amount):
            ...
    """
    def __init__(self, db_path: str = "data/circuit.db"):
        self._store = CBStore(db_path)
        self._breakers: Dict[str, _BreakerInstance] = {}

    def register(self, name: str, **config_kwargs) -> _BreakerInstance:
        cfg = CBConfig(name=name, **config_kwargs)
        inst = _BreakerInstance(cfg, self._store)
        self._breakers[name] = inst
        return inst

    def get(self, name: str) -> Optional[_BreakerInstance]:
        return self._breakers.get(name)

    def on(self, name: str, event: str, fn: Callable):
        inst = self._breakers.get(name)
        if inst and event in inst._hooks:
            inst._hooks[event].append(fn)

    async def call(self, name: str, fn: Callable, *args,
                    fallback: Callable = None, **kwargs) -> Any:
        inst = self._breakers.get(name)
        if not inst:
            return await fn(*args, **kwargs) if asyncio.iscoroutinefunction(fn) \
                else fn(*args, **kwargs)
        if not inst.allow_request():
            retry = inst.retry_after()
            if fallback:
                return await fallback() if asyncio.iscoroutinefunction(fallback) \
                    else fallback()
            raise CircuitOpenError(name, retry)
        if inst.state == State.HALF_OPEN:
            inst._probe_inflight += 1
        start = time.time()
        try:
            timeout = inst.config.call_timeout_s
            if asyncio.iscoroutinefunction(fn):
                coro = fn(*args, **kwargs)
                result = await (asyncio.wait_for(coro, timeout) if timeout else coro)
            else:
                result = fn(*args, **kwargs)
            latency = (time.time() - start) * 1000
            # Check user predicate
            pred = inst.config.failure_predicate
            if pred and pred(result):
                inst.record_failure("predicate failed", latency)
                if fallback:
                    return await fallback() if asyncio.iscoroutinefunction(fallback) \
                        else fallback()
                raise CircuitOpenError(name, inst.retry_after())
            inst.record_success(latency)
            return result
        except CircuitOpenError: raise
        except asyncio.TimeoutError as e:
            latency = (time.time() - start) * 1000
            inst.record_failure("timeout", latency)
            if fallback:
                return await fallback() if asyncio.iscoroutinefunction(fallback) \
                    else fallback()
            raise
        except Exception as e:
            latency = (time.time() - start) * 1000
            inst.record_failure(str(e), latency)
            if fallback:
                return await fallback() if asyncio.iscoroutinefunction(fallback) \
                    else fallback()
            raise

    def protect(self, name: str, fallback: Callable = None):
        """Decorator factory for protecting a function with a circuit breaker."""
        import functools
        def decorator(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                return await self.call(name, fn, *args,
                                        fallback=fallback, **kwargs)
            @functools.wraps(fn)
            def sync_wrapper(*args, **kwargs):
                import asyncio as _aio
                loop = _aio.new_event_loop()
                return loop.run_until_complete(
                    self.call(name, fn, *args, fallback=fallback, **kwargs))
            return async_wrapper if asyncio.iscoroutinefunction(fn) else sync_wrapper
        return decorator

    def all_status(self) -> Dict[str, Dict]:
        return {n: b.status() for n, b in self._breakers.items()}

    def stats(self) -> Dict:
        s = self._store.stats()
        s["breakers"] = len(self._breakers)
        s["open"] = sum(1 for b in self._breakers.values()
                         if b.state == State.OPEN)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def status_ep(req):
            return web.json_response(self.all_status())
        async def force_open_ep(req):
            d = await req.json()
            b = self.get(d["name"])
            if b: b.force_open()
            return web.json_response({"forced_open": bool(b)})
        async def force_close_ep(req):
            d = await req.json()
            b = self.get(d["name"])
            if b: b.force_close()
            return web.json_response({"forced_close": bool(b)})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/cb"
        app.router.add_get( f"{p}/status",      status_ep)
        app.router.add_post(f"{p}/force_open",  force_open_ep)
        app.router.add_post(f"{p}/force_close", force_close_ep)
        app.router.add_get( f"{p}/stats",       stats_ep)
        logger.info(f"Circuit breaker API at {prefix}/cb/")
