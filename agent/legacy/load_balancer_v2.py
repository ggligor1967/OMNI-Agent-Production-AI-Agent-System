"""OMNI Agent — Load Balancer V2: strategies, health checks, circuit breaking."""
from __future__ import annotations
import random, sqlite3, threading, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class LBStrategy(str, Enum):
    ROUND_ROBIN      = "round_robin"
    RANDOM           = "random"
    LEAST_CONN       = "least_conn"
    WEIGHTED_RR      = "weighted_rr"
    IP_HASH          = "ip_hash"
    LEAST_LATENCY    = "least_latency"


class BackendStatus(str, Enum):
    HEALTHY  = "healthy"
    UNHEALTHY = "unhealthy"
    DRAINING  = "draining"
    DISABLED  = "disabled"


class CircuitState(str, Enum):
    CLOSED   = "closed"     # normal
    OPEN     = "open"       # failing; reject requests
    HALF_OPEN = "half_open" # probe mode


@dataclass
class Backend:
    backend_id: str
    address: str
    weight: int = 1
    max_connections: int = 100
    status: BackendStatus = BackendStatus.HEALTHY
    active_conns: int = 0
    total_requests: int = 0
    total_errors: int = 0
    total_ms: float = 0.0
    consecutive_errors: int = 0
    last_health_check: Optional[float] = None
    circuit_state: CircuitState = CircuitState.CLOSED
    circuit_open_at: Optional[float] = None
    circuit_timeout_s: float = 30.0
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def avg_latency_ms(self) -> float:
        return self.total_ms / self.total_requests if self.total_requests else 0.0

    @property
    def error_rate(self) -> float:
        return self.total_errors / self.total_requests if self.total_requests else 0.0

    def is_available(self) -> bool:
        if self.status != BackendStatus.HEALTHY: return False
        if self.circuit_state == CircuitState.OPEN:
            # Check if timeout passed → try HALF_OPEN
            if (self.circuit_open_at and
                    time.time() - self.circuit_open_at >= self.circuit_timeout_s):
                self.circuit_state = CircuitState.HALF_OPEN
                return True
            return False
        return self.active_conns < self.max_connections

    def to_dict(self) -> Dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "address": self.address,
            "status": self.status.value,
            "circuit": self.circuit_state.value,
            "active_conns": self.active_conns,
            "total_requests": self.total_requests,
            "error_rate": round(self.error_rate, 3),
            "avg_latency_ms": round(self.avg_latency_ms, 2),
        }


@dataclass
class RequestRecord:
    record_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    backend_id: str = ""
    success: bool = True
    latency_ms: float = 0.0
    client_id: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {"record_id": self.record_id, "backend": self.backend_id,
                "success": self.success, "latency_ms": round(self.latency_ms, 2)}


class LoadBalancerV2:
    """
    Load balancer:
    - Multiple strategies: round-robin, random, least-conn,
      weighted-RR, IP-hash, least-latency
    - Backend registry with weight, max-connections
    - Health check support (pluggable check function)
    - Circuit breaker per backend (closed/open/half-open)
    - Active connection tracking
    - Automatic unhealthy backend removal from rotation
    - Sticky sessions (client_id → backend mapping, TTL)
    - Drain mode (graceful removal)
    - Request routing with context passthrough
    - Latency and error tracking per backend
    - Request history
    - SQLite persistence
    """

    def __init__(self, strategy: LBStrategy = LBStrategy.ROUND_ROBIN,
                 db_path: str = ":memory:",
                 error_threshold: int = 5,
                 health_check_fn: Optional[Callable[[Backend], bool]] = None):
        self._backends:  Dict[str, Backend] = {}
        self._strategy   = strategy
        self._rr_index   = 0
        self._sessions:  Dict[str, Tuple[str, float]] = {}  # client → (bid, expiry)
        self._records:   List[RequestRecord] = []
        self._error_threshold = error_threshold
        self._health_fn  = health_check_fn
        self._lock       = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS lb_backends (
                backend_id TEXT PRIMARY KEY, address TEXT, weight INTEGER,
                status TEXT, circuit TEXT, total_requests INTEGER,
                total_errors INTEGER, avg_latency_ms REAL
            );
            CREATE TABLE IF NOT EXISTS lb_requests (
                record_id TEXT PRIMARY KEY, backend_id TEXT, success INTEGER,
                latency_ms REAL, ts REAL
            );
        """)
        self._db.commit()

    # ── BACKEND MANAGEMENT ────────────────────────────────────────────

    def add_backend(self, address: str,
                     weight: int = 1,
                     max_connections: int = 100,
                     circuit_timeout_s: float = 30.0,
                     tags: Optional[List[str]] = None,
                     backend_id: Optional[str] = None,
                     metadata: Optional[Dict] = None) -> Backend:
        bid = backend_id or str(uuid.uuid4())[:8]
        b   = Backend(backend_id=bid, address=address, weight=weight,
                       max_connections=max_connections,
                       circuit_timeout_s=circuit_timeout_s,
                       tags=list(tags or []), metadata=metadata or {})
        with self._lock:
            self._backends[bid] = b
        self._persist_backend(b)
        return b

    def remove_backend(self, backend_id: str) -> bool:
        with self._lock:
            return self._backends.pop(backend_id, None) is not None

    def drain_backend(self, backend_id: str):
        b = self._backends.get(backend_id)
        if b: b.status = BackendStatus.DRAINING

    def enable_backend(self, backend_id: str):
        b = self._backends.get(backend_id)
        if b:
            b.status = BackendStatus.HEALTHY
            b.circuit_state = CircuitState.CLOSED
            b.consecutive_errors = 0

    def disable_backend(self, backend_id: str):
        b = self._backends.get(backend_id)
        if b: b.status = BackendStatus.DISABLED

    # ── SELECTION ────────────────────────────────────────────────────

    def _available(self) -> List[Backend]:
        return [b for b in self._backends.values() if b.is_available()]

    def _select(self, client_id: str = "") -> Optional[Backend]:
        available = self._available()
        if not available: return None
        s = self._strategy
        if s == LBStrategy.ROUND_ROBIN:
            b = available[self._rr_index % len(available)]
            self._rr_index += 1
            return b
        if s == LBStrategy.RANDOM:
            return random.choice(available)
        if s == LBStrategy.LEAST_CONN:
            return min(available, key=lambda b: b.active_conns)
        if s == LBStrategy.LEAST_LATENCY:
            return min(available, key=lambda b: b.avg_latency_ms)
        if s == LBStrategy.WEIGHTED_RR:
            pool = [b for b in available for _ in range(b.weight)]
            if not pool: return None
            b = pool[self._rr_index % len(pool)]
            self._rr_index += 1
            return b
        if s == LBStrategy.IP_HASH and client_id:
            idx = hash(client_id) % len(available)
            return available[idx]
        return available[0]

    def _sticky_select(self, client_id: str,
                        ttl_s: float = 300.0) -> Optional[Backend]:
        if client_id:
            entry = self._sessions.get(client_id)
            if entry:
                bid, exp = entry
                if time.time() < exp and bid in self._backends:
                    b = self._backends[bid]
                    if b.is_available(): return b
        return None

    # ── ROUTING ──────────────────────────────────────────────────────

    def route(self, handler: Callable[[Backend], Any],
               client_id: str = "",
               sticky: bool = False,
               sticky_ttl_s: float = 300.0) -> Tuple[Any, RequestRecord]:
        with self._lock:
            if sticky:
                b = self._sticky_select(client_id, sticky_ttl_s) or self._select(client_id)
            else:
                b = self._select(client_id)
            if not b:
                rec = RequestRecord(success=False, latency_ms=0.0,
                                     client_id=client_id)
                rec.backend_id = "__none__"
                self._records.append(rec)
                return None, rec
            b.active_conns += 1

        t0  = time.time()
        rec = RequestRecord(backend_id=b.backend_id, client_id=client_id)
        try:
            result = handler(b)
            rec.success     = True
            rec.latency_ms  = (time.time() - t0) * 1000
            with self._lock:
                b.total_requests     += 1
                b.total_ms           += rec.latency_ms
                b.consecutive_errors  = 0
                if b.circuit_state == CircuitState.HALF_OPEN:
                    b.circuit_state = CircuitState.CLOSED
                if sticky and client_id:
                    self._sessions[client_id] = (
                        b.backend_id, time.time() + sticky_ttl_s)
        except Exception as exc:
            rec.success    = False
            rec.latency_ms = (time.time() - t0) * 1000
            with self._lock:
                b.total_requests    += 1
                b.total_errors      += 1
                b.total_ms          += rec.latency_ms
                b.consecutive_errors += 1
                if b.consecutive_errors >= self._error_threshold:
                    b.circuit_state  = CircuitState.OPEN
                    b.circuit_open_at = time.time()
                    b.status = BackendStatus.UNHEALTHY
            result = None
        finally:
            with self._lock:
                b.active_conns = max(0, b.active_conns - 1)

        self._records.append(rec)
        self._persist_request(rec)
        self._persist_backend(b)
        return result, rec

    # ── HEALTH CHECKS ────────────────────────────────────────────────

    def health_check_all(self) -> Dict[str, bool]:
        if not self._health_fn: return {}
        results: Dict[str, bool] = {}
        for b in list(self._backends.values()):
            try:
                ok = self._health_fn(b)
            except Exception:
                ok = False
            b.last_health_check = time.time()
            if ok:
                if b.status == BackendStatus.UNHEALTHY:
                    b.status = BackendStatus.HEALTHY
                    b.circuit_state = CircuitState.CLOSED
                    b.consecutive_errors = 0
            else:
                b.status = BackendStatus.UNHEALTHY
            results[b.backend_id] = ok
        return results

    # ── QUERY ─────────────────────────────────────────────────────────

    def list_backends(self, status: Optional[BackendStatus] = None) -> List[Dict]:
        backends = list(self._backends.values())
        if status: backends = [b for b in backends if b.status == status]
        return [b.to_dict() for b in backends]

    def request_history(self, limit: int = 50) -> List[Dict]:
        return [r.to_dict() for r in self._records[-limit:]]

    def _persist_backend(self, b: Backend):
        self._db.execute(
            "INSERT OR REPLACE INTO lb_backends VALUES (?,?,?,?,?,?,?,?)",
            (b.backend_id, b.address, b.weight, b.status.value,
             b.circuit_state.value, b.total_requests,
             b.total_errors, b.avg_latency_ms))
        self._db.commit()

    def _persist_request(self, r: RequestRecord):
        self._db.execute(
            "INSERT INTO lb_requests VALUES (?,?,?,?,?)",
            (r.record_id, r.backend_id, int(r.success), r.latency_ms, r.ts))
        self._db.commit()

    def stats(self) -> Dict[str, Any]:
        total = len(self._records)
        success = sum(1 for r in self._records if r.success)
        return {
            "backends": len(self._backends),
            "healthy": sum(1 for b in self._backends.values()
                           if b.status == BackendStatus.HEALTHY),
            "total_requests": total,
            "success_rate": round(success / total, 3) if total else 0.0,
            "strategy": self._strategy.value,
        }
