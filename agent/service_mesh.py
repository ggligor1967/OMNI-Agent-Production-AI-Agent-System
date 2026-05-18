"""OMNI AGENT - Service Mesh
Service registry with load balancing, health tracking, circuit breaking,
weighted routing, and request metrics.

Features:
- Service registry: named services with multiple instances (endpoints)
- Instance: id, host, port, weight, metadata, health status
- Health: HEALTHY, DEGRADED, UNHEALTHY, UNKNOWN; periodic check fn
- Load balancing strategies: ROUND_ROBIN, RANDOM, LEAST_CONN,
    WEIGHTED (by weight), IP_HASH (sticky by client key)
- Circuit breaker per instance: open after N consecutive failures
- Request tracking: in-flight connections per instance
- Register / deregister / update instances
- Resolve: pick best instance for a service given strategy
- Retry: on resolve, skip unhealthy or open-circuit instances
- Tags: instances tagged; tag-based filtering on resolve
- Canary: send X% of traffic to tagged canary instances
- Timeout awareness: record call duration; per-instance latency stats
- Hooks: on_register, on_deregister, on_health_change
- Heartbeat: instances send keepalive; expire if missed > TTL
- Stats: per-service and per-instance request counts, errors, latency
- SQLite persistence: registry, health history, request log
- REST API: register, deregister, resolve, health, stats
"""
import random, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class HealthStatus(str, Enum):
    HEALTHY   = "healthy"
    DEGRADED  = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN   = "unknown"

class LBStrategy(str, Enum):
    ROUND_ROBIN = "round_robin"
    RANDOM      = "random"
    LEAST_CONN  = "least_conn"
    WEIGHTED    = "weighted"
    IP_HASH     = "ip_hash"

_STATUS_RANK = {HealthStatus.HEALTHY: 0, HealthStatus.DEGRADED: 1,
                HealthStatus.UNKNOWN: 2, HealthStatus.UNHEALTHY: 3}

@dataclass
class Instance:
    id: str; service: str; host: str; port: int
    weight: float = 1.0
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    health: HealthStatus = HealthStatus.UNKNOWN
    registered_at: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)
    # Stats
    requests: int = 0; errors: int = 0
    in_flight: int = 0
    total_latency_ms: float = 0.0
    # Circuit breaker state
    cb_failures: int = 0; cb_open: bool = False
    cb_opened_at: float = 0.0

    @property
    def avg_latency_ms(self) -> float:
        return (self.total_latency_ms / self.requests
                if self.requests > 0 else 0.0)

    @property
    def error_rate(self) -> float:
        return self.errors / self.requests if self.requests > 0 else 0.0

    def is_available(self) -> bool:
        return (self.health != HealthStatus.UNHEALTHY
                and not self.cb_open)

    def to_dict(self):
        return {"id": self.id, "service": self.service,
                "host": self.host, "port": self.port,
                "weight": self.weight, "tags": self.tags,
                "health": self.health.value,
                "requests": self.requests, "errors": self.errors,
                "in_flight": self.in_flight,
                "avg_latency_ms": round(self.avg_latency_ms, 2),
                "cb_open": self.cb_open}

@dataclass
class ServiceConfig:
    name: str
    lb_strategy: LBStrategy = LBStrategy.ROUND_ROBIN
    cb_threshold: int = 5        # failures before open
    cb_recovery_s: float = 30.0  # half-open probe after this
    heartbeat_ttl_s: float = 60.0
    canary_tag: str = "canary"
    canary_pct: float = 0.0      # 0-100
    _rr_index: int = field(default=0, repr=False)

class SMStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS instances(
                    id TEXT PRIMARY KEY, service TEXT, host TEXT,
                    port INTEGER, weight REAL, tags TEXT,
                    metadata TEXT, health TEXT,
                    registered_at REAL, last_heartbeat REAL);
                CREATE TABLE IF NOT EXISTS req_log(
                    id TEXT PRIMARY KEY, service TEXT, instance_id TEXT,
                    latency_ms REAL, error INTEGER, ts REAL);
                CREATE INDEX IF NOT EXISTS idx_inst_svc
                    ON instances(service);
            """)

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def save(self, inst: Instance):
        import json
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO instances VALUES(?,?,?,?,?,?,?,?,?,?)",
                (inst.id, inst.service, inst.host, inst.port,
                 inst.weight, json.dumps(inst.tags),
                 json.dumps(inst.metadata, default=str),
                 inst.health.value, inst.registered_at, inst.last_heartbeat))

    def delete(self, inst_id: str):
        with self._conn() as c:
            c.execute("DELETE FROM instances WHERE id=?", (inst_id,))

    def log_request(self, service: str, inst_id: str,
                     latency_ms: float, error: bool):
        with self._conn() as c:
            c.execute("INSERT INTO req_log VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4())[:8], service, inst_id,
                 latency_ms, int(error), time.time()))

    def stats(self) -> Dict:
        with self._conn() as c:
            ni = c.execute("SELECT COUNT(*) FROM instances").fetchone()[0]
            nr = c.execute("SELECT COUNT(*) FROM req_log").fetchone()[0]
            by_svc = {r["service"]: r["cnt"] for r in c.execute(
                "SELECT service, COUNT(*) as cnt FROM instances "
                "GROUP BY service").fetchall()}
        return {"total_instances": ni, "total_requests": nr,
                "by_service": by_svc}

class ServiceMesh:
    """
    Service registry with load balancing and health tracking.

    Usage:
        mesh = ServiceMesh()
        mesh.register_service("api", lb_strategy=LBStrategy.ROUND_ROBIN)

        mesh.add_instance("api", "10.0.0.1", 8080, weight=1.0)
        mesh.add_instance("api", "10.0.0.2", 8080, weight=2.0)

        inst = mesh.resolve("api")
        # Use inst.host, inst.port
        mesh.record_call("api", inst.id, latency_ms=45, error=False)
    """
    def __init__(self, db_path: str = "data/mesh.db"):
        self._store = SMStore(db_path)
        self._services: Dict[str, ServiceConfig] = {}
        self._instances: Dict[str, Dict[str, Instance]] = {}  # svc → {id: inst}
        self._hooks_register:  List[Callable] = []
        self._hooks_deregister: List[Callable] = []
        self._hooks_health:    List[Callable] = []

    def on_register(self, fn):   self._hooks_register.append(fn)
    def on_deregister(self, fn): self._hooks_deregister.append(fn)
    def on_health(self, fn):     self._hooks_health.append(fn)

    def register_service(self, name: str,
                           lb_strategy: LBStrategy = LBStrategy.ROUND_ROBIN,
                           cb_threshold: int = 5,
                           cb_recovery_s: float = 30.0,
                           heartbeat_ttl_s: float = 60.0,
                           canary_tag: str = "canary",
                           canary_pct: float = 0.0) -> ServiceConfig:
        svc = ServiceConfig(name=name, lb_strategy=lb_strategy,
                             cb_threshold=cb_threshold,
                             cb_recovery_s=cb_recovery_s,
                             heartbeat_ttl_s=heartbeat_ttl_s,
                             canary_tag=canary_tag,
                             canary_pct=canary_pct)
        self._services[name] = svc
        self._instances.setdefault(name, {})
        return svc

    def _get_svc(self, name: str) -> ServiceConfig:
        if name not in self._services:
            return self.register_service(name)
        return self._services[name]

    def add_instance(self, service: str, host: str, port: int,
                      weight: float = 1.0, tags: List[str] = None,
                      metadata: Dict = None) -> Instance:
        svc = self._get_svc(service)
        inst = Instance(
            id=str(uuid.uuid4())[:12], service=service,
            host=host, port=port, weight=weight,
            tags=list(tags or []), metadata=dict(metadata or {}))
        self._instances.setdefault(service, {})[inst.id] = inst
        self._store.save(inst)
        for h in self._hooks_register:
            try: h(inst)
            except: pass
        return inst

    def remove_instance(self, inst_id: str) -> bool:
        for svc, insts in self._instances.items():
            if inst_id in insts:
                inst = insts.pop(inst_id)
                self._store.delete(inst_id)
                for h in self._hooks_deregister:
                    try: h(inst)
                    except: pass
                return True
        return False

    def set_health(self, inst_id: str, status: HealthStatus):
        for insts in self._instances.values():
            if inst_id in insts:
                inst = insts[inst_id]
                old = inst.health
                inst.health = status
                if old != status:
                    for h in self._hooks_health:
                        try: h(inst, old, status)
                        except: pass
                self._store.save(inst)
                return

    def heartbeat(self, inst_id: str):
        for insts in self._instances.values():
            if inst_id in insts:
                inst = insts[inst_id]
                inst.last_heartbeat = time.time()
                if inst.health == HealthStatus.UNKNOWN:
                    inst.health = HealthStatus.HEALTHY
                self._store.save(inst)
                return

    def expire_stale(self, service: str = None):
        """Mark instances as UNHEALTHY if heartbeat TTL exceeded."""
        svcs = ([service] if service else list(self._services.keys()))
        for sname in svcs:
            svc = self._services.get(sname)
            if not svc: continue
            for inst in list(self._instances.get(sname, {}).values()):
                if (time.time() - inst.last_heartbeat
                        > svc.heartbeat_ttl_s):
                    self.set_health(inst.id, HealthStatus.UNHEALTHY)

    def _check_cb(self, inst: Instance, svc: ServiceConfig):
        """Probe half-open: reset if recovery window passed."""
        if inst.cb_open:
            if time.time() - inst.cb_opened_at > svc.cb_recovery_s:
                inst.cb_open = False; inst.cb_failures = 0

    def resolve(self, service: str,
                 tags: List[str] = None,
                 client_key: str = None) -> Optional[Instance]:
        svc = self._get_svc(service)
        pool = list(self._instances.get(service, {}).values())
        # Check circuit breakers
        for inst in pool:
            self._check_cb(inst, svc)
        # Filter available
        available = [i for i in pool if i.is_available()]
        if tags:
            tag_set = set(tags)
            available = [i for i in available
                          if tag_set.issubset(set(i.tags))]
        if not available:
            return None
        # Canary routing
        if svc.canary_pct > 0 and random.random() * 100 < svc.canary_pct:
            canaries = [i for i in available if svc.canary_tag in i.tags]
            if canaries:
                available = canaries
        return self._pick(svc, available, client_key)

    def _pick(self, svc: ServiceConfig,
               pool: List[Instance],
               client_key: str = None) -> Instance:
        if len(pool) == 1: return pool[0]
        strategy = svc.lb_strategy
        if strategy == LBStrategy.RANDOM:
            return random.choice(pool)
        if strategy == LBStrategy.LEAST_CONN:
            return min(pool, key=lambda i: i.in_flight)
        if strategy == LBStrategy.WEIGHTED:
            total = sum(i.weight for i in pool)
            r = random.uniform(0, total); cum = 0
            for inst in pool:
                cum += inst.weight
                if r <= cum: return inst
            return pool[-1]
        if strategy == LBStrategy.IP_HASH and client_key:
            idx = hash(client_key) % len(pool)
            return pool[idx]
        # ROUND_ROBIN (default)
        idx = svc._rr_index % len(pool)
        svc._rr_index += 1
        return pool[idx]

    def record_call(self, service: str, inst_id: str,
                     latency_ms: float, error: bool = False):
        insts = self._instances.get(service, {})
        inst = insts.get(inst_id)
        if not inst: return
        inst.requests += 1
        inst.total_latency_ms += latency_ms
        if error:
            inst.errors += 1
            inst.cb_failures += 1
            svc = self._services.get(service)
            if svc and inst.cb_failures >= svc.cb_threshold:
                inst.cb_open = True
                inst.cb_opened_at = time.time()
        else:
            inst.cb_failures = 0
        self._store.log_request(service, inst_id, latency_ms, error)

    def acquire(self, inst_id: str):
        """Increment in-flight counter."""
        for insts in self._instances.values():
            if inst_id in insts:
                insts[inst_id].in_flight += 1; return

    def release(self, inst_id: str):
        """Decrement in-flight counter."""
        for insts in self._instances.values():
            if inst_id in insts:
                insts[inst_id].in_flight = max(0, insts[inst_id].in_flight - 1)
                return

    def service_stats(self, service: str) -> Dict:
        insts = list(self._instances.get(service, {}).values())
        return {
            "service": service,
            "instance_count": len(insts),
            "available": sum(1 for i in insts if i.is_available()),
            "total_requests": sum(i.requests for i in insts),
            "total_errors": sum(i.errors for i in insts),
            "instances": [i.to_dict() for i in insts]}

    def stats(self) -> Dict:
        s = self._store.stats()
        s["services"] = len(self._services)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def reg_ep(req):
            d = await req.json()
            inst = self.add_instance(d["service"], d["host"], d["port"],
                                      d.get("weight",1.0),
                                      d.get("tags",[]), d.get("metadata",{}))
            return web.json_response(inst.to_dict(), status=201)
        async def dereg_ep(req):
            d = await req.json()
            ok = self.remove_instance(d["id"])
            return web.json_response({"removed": ok})
        async def resolve_ep(req):
            d = await req.json()
            inst = self.resolve(d["service"], d.get("tags"),
                                 d.get("client_key"))
            if not inst:
                return web.json_response({"error":"no instance"}, status=503)
            return web.json_response(inst.to_dict())
        async def health_ep(req):
            svc = req.match_info["service"]
            return web.json_response(self.service_stats(svc))
        async def stats_ep(req):
            return web.json_response(self.stats())
        p = f"{prefix}/mesh"
        app.router.add_post(f"{p}/register",         reg_ep)
        app.router.add_post(f"{p}/deregister",       dereg_ep)
        app.router.add_post(f"{p}/resolve",          resolve_ep)
        app.router.add_get( f"{p}/{{service}}/health", health_ep)
        app.router.add_get( f"{p}/stats",            stats_ep)
        logger.info(f"Service mesh API at {prefix}/mesh/")
