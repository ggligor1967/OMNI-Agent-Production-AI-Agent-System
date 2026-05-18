"""OMNI Agent — Connection Pool V2: generic pool with health checks and circuit breaker."""
from __future__ import annotations
import queue, threading, time, uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Generator, List, Optional


class ConnectionState(str, Enum):
    IDLE     = "idle"
    IN_USE   = "in_use"
    BROKEN   = "broken"
    DRAINING = "draining"


class PoolState(str, Enum):
    OPEN   = "open"
    CLOSED = "closed"
    PAUSED = "paused"


@dataclass
class PooledConnection:
    conn_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    connection: Any = None
    state: ConnectionState = ConnectionState.IDLE
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    use_count: int = 0
    error_count: int = 0
    pool_id: str = ""

    @property
    def age_s(self) -> float:
        return time.time() - self.created_at

    @property
    def idle_s(self) -> float:
        return time.time() - self.last_used_at

    def to_dict(self) -> Dict[str, Any]:
        return {
            "conn_id": self.conn_id,
            "state": self.state.value,
            "age_s": round(self.age_s, 1),
            "idle_s": round(self.idle_s, 1),
            "use_count": self.use_count,
            "error_count": self.error_count,
        }


@dataclass
class PoolStats:
    total_acquired: int = 0
    total_released: int = 0
    total_timeouts: int = 0
    total_errors: int = 0
    total_health_failures: int = 0
    connections_created: int = 0
    connections_destroyed: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "acquired": self.total_acquired,
            "released": self.total_released,
            "timeouts": self.total_timeouts,
            "errors": self.total_errors,
            "health_failures": self.total_health_failures,
            "created": self.connections_created,
            "destroyed": self.connections_destroyed,
        }


class PoolExhaustedError(Exception):
    pass


class ConnectionPoolV2:
    """
    Generic connection pool:
    - Min/max pool size with dynamic scaling
    - Configurable acquire timeout
    - Health check function per connection
    - Automatic reconnection on broken connections
    - Max connection lifetime and idle eviction
    - Max uses per connection (recycle after N uses)
    - Connection validation on borrow
    - Pool-level circuit breaker (pause if too many failures)
    - Context manager for safe acquire/release
    - Per-pool stats
    - Thread-safe
    """

    def __init__(
        self,
        factory: Callable[[], Any],
        min_size: int = 2,
        max_size: int = 10,
        acquire_timeout_s: float = 5.0,
        max_lifetime_s: float = 3600.0,
        max_idle_s: float = 600.0,
        max_uses: int = 0,              # 0 = unlimited
        health_check: Optional[Callable[[Any], bool]] = None,
        on_connect: Optional[Callable[[Any], None]] = None,
        on_disconnect: Optional[Callable[[Any], None]] = None,
        pool_id: str = "default",
    ):
        self.factory          = factory
        self.min_size         = min_size
        self.max_size         = max_size
        self.acquire_timeout  = acquire_timeout_s
        self.max_lifetime_s   = max_lifetime_s
        self.max_idle_s       = max_idle_s
        self.max_uses         = max_uses
        self.health_check     = health_check
        self.on_connect       = on_connect
        self.on_disconnect    = on_disconnect
        self.pool_id          = pool_id
        self._state           = PoolState.OPEN
        self._all:  Dict[str, PooledConnection] = {}   # conn_id → conn
        self._idle: queue.Queue = queue.Queue()
        self._lock  = threading.RLock()
        self._stats = PoolStats()
        self._cb_failures = 0
        self._cb_threshold = 5
        self._cb_reset_after = 30.0
        self._cb_tripped_at: Optional[float] = None
        # Pre-fill min connections
        for _ in range(min_size):
            try: self._create_connection()
            except Exception: pass

    # ── FACTORY ───────────────────────────────────────────────────────

    def _create_connection(self) -> Optional[PooledConnection]:
        try:
            raw  = self.factory()
            conn = PooledConnection(connection=raw, pool_id=self.pool_id)
            with self._lock:
                self._all[conn.conn_id] = conn
                self._stats.connections_created += 1
            if self.on_connect:
                try: self.on_connect(raw)
                except Exception: pass
            self._idle.put(conn.conn_id)
            return conn
        except Exception:
            return None

    def _destroy_connection(self, conn: PooledConnection):
        with self._lock:
            self._all.pop(conn.conn_id, None)
            self._stats.connections_destroyed += 1
        conn.state = ConnectionState.DRAINING
        if self.on_disconnect:
            try: self.on_disconnect(conn.connection)
            except Exception: pass

    def _is_healthy(self, conn: PooledConnection) -> bool:
        if conn.state == ConnectionState.BROKEN:
            return False
        if self.max_lifetime_s and conn.age_s > self.max_lifetime_s:
            return False
        if self.max_uses and conn.use_count >= self.max_uses:
            return False
        if self.health_check:
            try:
                return bool(self.health_check(conn.connection))
            except Exception:
                self._stats.total_health_failures += 1
                return False
        return True

    # ── CIRCUIT BREAKER ───────────────────────────────────────────────

    def _check_circuit(self):
        if self._cb_tripped_at:
            if time.time() - self._cb_tripped_at > self._cb_reset_after:
                self._cb_failures = 0
                self._cb_tripped_at = None
            else:
                raise PoolExhaustedError("Circuit breaker open — pool paused")

    def _record_cb_failure(self):
        self._cb_failures += 1
        if self._cb_failures >= self._cb_threshold:
            self._cb_tripped_at = time.time()

    # ── ACQUIRE / RELEASE ─────────────────────────────────────────────

    def acquire(self, timeout: Optional[float] = None) -> PooledConnection:
        if self._state == PoolState.CLOSED:
            raise PoolExhaustedError("Pool is closed")
        self._check_circuit()
        deadline = time.time() + (timeout or self.acquire_timeout)

        while time.time() < deadline:
            # Try idle queue
            try:
                cid = self._idle.get(timeout=0.05)
                conn = self._all.get(cid)
                if conn is None:
                    continue
                # Evict stale or idle connections
                if (self.max_idle_s and conn.idle_s > self.max_idle_s):
                    self._destroy_connection(conn)
                    # Try to maintain min size
                    with self._lock:
                        if len(self._all) < self.min_size:
                            self._create_connection()
                    continue
                if not self._is_healthy(conn):
                    self._destroy_connection(conn)
                    with self._lock:
                        if len(self._all) < self.min_size:
                            self._create_connection()
                    continue
                conn.state     = ConnectionState.IN_USE
                conn.last_used_at = time.time()
                conn.use_count += 1
                self._stats.total_acquired += 1
                return conn
            except queue.Empty:
                pass

            # Try to grow pool
            with self._lock:
                if len(self._all) < self.max_size:
                    new_conn = self._create_connection()
                    if new_conn:
                        try:
                            cid = self._idle.get_nowait()
                            conn = self._all.get(cid)
                            if conn:
                                conn.state = ConnectionState.IN_USE
                                conn.last_used_at = time.time()
                                conn.use_count += 1
                                self._stats.total_acquired += 1
                                return conn
                        except queue.Empty:
                            pass

        self._stats.total_timeouts += 1
        raise PoolExhaustedError(
            f"Could not acquire connection within {timeout or self.acquire_timeout}s")

    def release(self, conn: PooledConnection,
                mark_broken: bool = False):
        if mark_broken:
            conn.state = ConnectionState.BROKEN
            conn.error_count += 1
            self._record_cb_failure()
            self._destroy_connection(conn)
            with self._lock:
                if len(self._all) < self.min_size:
                    try: self._create_connection()
                    except Exception: pass
        else:
            conn.state = ConnectionState.IDLE
            self._idle.put(conn.conn_id)
        self._stats.total_released += 1

    @contextmanager
    def connection(self, timeout: Optional[float] = None
                   ) -> Generator[PooledConnection, None, None]:
        conn = self.acquire(timeout)
        try:
            yield conn
        except Exception:
            self.release(conn, mark_broken=True)
            self._stats.total_errors += 1
            raise
        else:
            self.release(conn)

    # ── MANAGEMENT ────────────────────────────────────────────────────

    def close(self):
        self._state = PoolState.CLOSED
        with self._lock:
            for conn in list(self._all.values()):
                self._destroy_connection(conn)

    def evict_idle(self) -> int:
        evicted = 0
        to_evict = []
        with self._lock:
            for cid, conn in self._all.items():
                if (conn.state == ConnectionState.IDLE and
                        self.max_idle_s and conn.idle_s > self.max_idle_s):
                    to_evict.append(conn)
        for conn in to_evict:
            try: self._idle.get_nowait()
            except queue.Empty: pass
            self._destroy_connection(conn)
            evicted += 1
        return evicted

    def resize(self, new_max: int):
        self.max_size = new_max

    # ── STATS ─────────────────────────────────────────────────────────

    @property
    def idle_count(self) -> int:
        return self._idle.qsize()

    @property
    def total_count(self) -> int:
        return len(self._all)

    @property
    def in_use_count(self) -> int:
        return sum(1 for c in self._all.values()
                   if c.state == ConnectionState.IN_USE)

    def list_connections(self) -> List[Dict]:
        return [c.to_dict() for c in self._all.values()]

    def stats(self) -> Dict[str, Any]:
        return {
            "pool_id": self.pool_id,
            "state": self._state.value,
            "total": self.total_count,
            "idle": self.idle_count,
            "in_use": self.in_use_count,
            "min_size": self.min_size,
            "max_size": self.max_size,
            "circuit_open": self._cb_tripped_at is not None,
            **self._stats.to_dict(),
        }
