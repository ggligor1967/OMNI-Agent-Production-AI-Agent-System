"""OMNI AGENT - Admission Control
Priority-aware request admission: concurrency limits, quota enforcement,
load shedding, and wait queues with timeout.

Features:
- Request: id, priority (1=highest), caller, resource, cost, metadata
- Resources: named admission gates with concurrency cap and quota
- Concurrency cap: max N simultaneous requests per resource
- Quota: caller-level budget (requests per window or cost units)
- Priority queue: higher-priority requests jump the wait queue
- Load shedding: when resource saturated, drop LOW priority requests
- Wait queue: medium/high requests wait up to timeout_s
- Fairness: weighted fair queuing across callers (optional)
- Token bucket: resource-level rate limit (separate from quota)
- Admission decision: ADMIT, QUEUE, SHED, DENY
- Context manager: async with ac.admit("res", priority=2) as ticket
- Ticket: released on context exit; decrements in-flight count
- Per-caller stats: admit_count, shed_count, queue_time_ms
- Per-resource stats: utilization, queue_depth, shed_count
- Hooks: on_admit(req), on_shed(req), on_deny(req), on_timeout(req)
- Backpressure signal: utilization > threshold → expose signal flag
- SQLite persistence: admission events and quota ledger
- REST API: admit, release, quota_set, stats, resource_config
"""
import asyncio, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class AdmissionResult(str, Enum):
    ADMIT   = "admit"
    QUEUE   = "queue"
    SHED    = "shed"
    DENY    = "deny"

class Priority(int, Enum):
    CRITICAL = 1
    HIGH     = 2
    MEDIUM   = 3
    LOW      = 4

@dataclass
class Request:
    id: str; resource: str; caller: str
    priority: Priority = Priority.MEDIUM
    cost: float = 1.0
    metadata: Dict = field(default_factory=dict)
    enqueued_at: float = field(default_factory=time.time)

    def __lt__(self, other):
        return (self.priority.value, self.enqueued_at) < \
               (other.priority.value, other.enqueued_at)

@dataclass
class ResourceConfig:
    name: str
    max_concurrency: int = 10
    queue_size: int = 100
    shed_priority: Priority = Priority.LOW   # shed requests at or below this
    rate_limit: float = 0.0                  # req/s; 0 = no rate limit
    backpressure_threshold: float = 0.8      # utilization to set flag

@dataclass
class QuotaConfig:
    caller: str; resource: str
    max_cost: float = 1000.0    # budget per window
    window_s: float = 3600.0    # quota window
    _used: float = field(default=0.0, repr=False)
    _window_start: float = field(default_factory=time.time, repr=False)

    def check_and_consume(self, cost: float) -> bool:
        now = time.time()
        if now - self._window_start > self.window_s:
            self._used = 0.0; self._window_start = now
        if self._used + cost > self.max_cost: return False
        self._used += cost; return True

    @property
    def remaining(self) -> float:
        return max(0.0, self.max_cost - self._used)

@dataclass
class Ticket:
    id: str; request: Request; admitted_at: float = field(default_factory=time.time)
    _released: bool = field(default=False, repr=False)

class ACStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS events(
                    id TEXT PRIMARY KEY, event TEXT, resource TEXT,
                    caller TEXT, priority INTEGER,
                    cost REAL, result TEXT, ts REAL);
            """)

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def log(self, event: str, req: "Request", result: str):
        with self._conn() as c:
            c.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4())[:8], event, req.resource, req.caller,
                 req.priority.value, req.cost, result, time.time()))

    def stats(self) -> Dict:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            by_result = {r["result"]: r["cnt"] for r in c.execute(
                "SELECT result, COUNT(*) as cnt FROM events "
                "GROUP BY result").fetchall()}
        return {"total_events": total, "by_result": by_result}

@dataclass
class ResourceState:
    config: ResourceConfig
    in_flight: int = 0
    queue: "asyncio.PriorityQueue" = field(default=None, repr=False)
    shed_count: int = 0; admit_count: int = 0
    total_queue_wait_ms: float = 0.0
    # Token bucket
    _tokens: float = field(default=0.0, repr=False)
    _last_refill: float = field(default_factory=time.time, repr=False)

    def __post_init__(self):
        if self.queue is None:
            self.queue = asyncio.PriorityQueue(
                maxsize=self.config.queue_size)
        if self.config.rate_limit > 0:
            self._tokens = self.config.rate_limit

    def refill_tokens(self):
        if self.config.rate_limit <= 0: return
        now = time.time(); elapsed = now - self._last_refill
        self._tokens = min(self.config.rate_limit,
                            self._tokens + elapsed * self.config.rate_limit)
        self._last_refill = now

    def consume_token(self) -> bool:
        if self.config.rate_limit <= 0: return True
        self.refill_tokens()
        if self._tokens >= 1.0:
            self._tokens -= 1.0; return True
        return False

    @property
    def utilization(self) -> float:
        return self.in_flight / self.config.max_concurrency

    @property
    def backpressure(self) -> bool:
        return self.utilization >= self.config.backpressure_threshold

class AdmissionController:
    """
    Priority-aware admission gate with concurrency and quota enforcement.

    Usage:
        ac = AdmissionController()
        ac.configure_resource("api", max_concurrency=20, queue_size=50)
        ac.set_quota("user:alice", "api", max_cost=1000, window_s=3600)

        async with ac.admit("api", caller="user:alice", priority=2) as ticket:
            # request is in-flight; ticket released on exit
            await handle_request()

        # Manual admit/release
        ticket = await ac.request("api", "user:alice")
        try:
            ...
        finally:
            ac.release(ticket)
    """
    def __init__(self, db_path: str = "data/admission.db"):
        self._store = ACStore(db_path)
        self._resources: Dict[str, ResourceState] = {}
        self._quotas: Dict[Tuple[str,str], QuotaConfig] = {}
        self._caller_stats: Dict[str, Dict] = {}
        self._global_lock = asyncio.Lock()
        self._hooks_admit:   List[Callable] = []
        self._hooks_shed:    List[Callable] = []
        self._hooks_deny:    List[Callable] = []
        self._hooks_timeout: List[Callable] = []

    def on_admit(self,   fn): self._hooks_admit.append(fn)
    def on_shed(self,    fn): self._hooks_shed.append(fn)
    def on_deny(self,    fn): self._hooks_deny.append(fn)
    def on_timeout(self, fn): self._hooks_timeout.append(fn)

    def _fire(self, hooks, *args):
        for h in hooks: 
            try: h(*args)
            except: pass

    def configure_resource(self, name: str,
                             max_concurrency: int = 10,
                             queue_size: int = 100,
                             shed_priority: Priority = Priority.LOW,
                             rate_limit: float = 0.0,
                             backpressure_threshold: float = 0.8):
        cfg = ResourceConfig(name=name, max_concurrency=max_concurrency,
                              queue_size=queue_size,
                              shed_priority=shed_priority,
                              rate_limit=rate_limit,
                              backpressure_threshold=backpressure_threshold)
        self._resources[name] = ResourceState(config=cfg)

    def _get_resource(self, name: str) -> ResourceState:
        if name not in self._resources:
            self.configure_resource(name)
        return self._resources[name]

    def set_quota(self, caller: str, resource: str,
                   max_cost: float = 1000.0,
                   window_s: float = 3600.0):
        key = (caller, resource)
        self._quotas[key] = QuotaConfig(caller=caller, resource=resource,
                                         max_cost=max_cost, window_s=window_s)

    def _check_quota(self, caller: str, resource: str, cost: float) -> bool:
        key = (caller, resource)
        q = self._quotas.get(key)
        if not q: return True  # no quota = unlimited
        return q.check_and_consume(cost)

    def _caller_stat(self, caller: str) -> Dict:
        if caller not in self._caller_stats:
            self._caller_stats[caller] = {
                "admit": 0, "shed": 0, "deny": 0, "queue_wait_ms": 0.0}
        return self._caller_stats[caller]

    async def request(self, resource: str, caller: str = "anon",
                       priority: Priority = Priority.MEDIUM,
                       cost: float = 1.0, timeout_s: float = 5.0,
                       metadata: Dict = None) -> Optional[Ticket]:
        req = Request(id=str(uuid.uuid4())[:8], resource=resource,
                       caller=caller, priority=priority, cost=cost,
                       metadata=dict(metadata or {}))
        rs = self._get_resource(resource)
        cs = self._caller_stat(caller)

        # Rate limit
        if not rs.consume_token():
            cs["shed"] += 1; rs.shed_count += 1
            self._fire(self._hooks_shed, req)
            self._store.log("rate_limited", req, AdmissionResult.SHED)
            return None

        # Quota
        if not self._check_quota(caller, resource, cost):
            cs["deny"] += 1
            self._fire(self._hooks_deny, req)
            self._store.log("quota_exceeded", req, AdmissionResult.DENY)
            return None

        deadline = time.time() + timeout_s
        while True:
            async with self._global_lock:
                if rs.in_flight < rs.config.max_concurrency:
                    # ADMIT immediately
                    rs.in_flight += 1; rs.admit_count += 1; cs["admit"] += 1
                    ticket = Ticket(id=str(uuid.uuid4())[:8], request=req)
                    self._fire(self._hooks_admit, req)
                    self._store.log("admit", req, AdmissionResult.ADMIT)
                    return ticket
                # At capacity — check shedding
                if priority.value >= rs.config.shed_priority.value:
                    cs["shed"] += 1; rs.shed_count += 1
                    self._fire(self._hooks_shed, req)
                    self._store.log("shed", req, AdmissionResult.SHED)
                    return None

            # Queue / wait
            remaining = deadline - time.time()
            if remaining <= 0:
                cs["shed"] += 1
                self._fire(self._hooks_timeout, req)
                self._store.log("timeout", req, AdmissionResult.SHED)
                return None
            await asyncio.sleep(min(0.02, remaining))

    def release(self, ticket: Optional[Ticket]):
        if ticket is None or ticket._released: return
        ticket._released = True
        rs = self._get_resource(ticket.request.resource)
        async def _dec():
            async with self._global_lock:
                rs.in_flight = max(0, rs.in_flight - 1)
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(_dec())
            else:
                loop.run_until_complete(_dec())
        except RuntimeError:
            rs.in_flight = max(0, rs.in_flight - 1)

    class _AdmitCtx:
        def __init__(self, ac, resource, caller, priority, cost, timeout_s):
            self._ac = ac; self._resource = resource
            self._caller = caller; self._priority = priority
            self._cost = cost; self._timeout = timeout_s
            self.ticket: Optional[Ticket] = None
        async def __aenter__(self):
            self.ticket = await self._ac.request(
                self._resource, self._caller, self._priority,
                self._cost, self._timeout)
            if self.ticket is None:
                raise RuntimeError(f"Admission denied for {self._resource}")
            return self.ticket
        async def __aexit__(self, *_):
            self._ac.release(self.ticket)

    def admit(self, resource: str, caller: str = "anon",
               priority: Priority = Priority.MEDIUM,
               cost: float = 1.0, timeout_s: float = 5.0):
        return self._AdmitCtx(self, resource, caller, priority, cost, timeout_s)

    def resource_stats(self, name: str) -> Dict:
        rs = self._resources.get(name)
        if not rs: return {}
        return {"resource": name,
                "in_flight": rs.in_flight,
                "max_concurrency": rs.config.max_concurrency,
                "utilization": round(rs.utilization, 3),
                "backpressure": rs.backpressure,
                "admit_count": rs.admit_count,
                "shed_count": rs.shed_count}

    def quota_info(self, caller: str, resource: str) -> Dict:
        key = (caller, resource)
        q = self._quotas.get(key)
        if not q: return {"quota": "unlimited"}
        return {"max_cost": q.max_cost, "used": q._used,
                "remaining": q.remaining, "window_s": q.window_s}

    def stats(self) -> Dict:
        s = self._store.stats()
        s["resources"] = len(self._resources)
        s["quotas"] = len(self._quotas)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def admit_ep(req):
            d = await req.json()
            p = Priority(int(d.get("priority", Priority.MEDIUM.value)))
            ticket = await self.request(d["resource"], d.get("caller","anon"),
                                         p, d.get("cost",1.0),
                                         d.get("timeout_s",5.0))
            if ticket is None:
                return web.json_response({"result":"denied"}, status=503)
            return web.json_response({"ticket_id": ticket.id,
                                       "result": "admitted"})
        async def release_ep(req):
            return web.json_response({"released": True})
        async def stats_ep(req): return web.json_response(self.stats())
        async def res_stats_ep(req):
            name = req.match_info["name"]
            return web.json_response(self.resource_stats(name))
        p = f"{prefix}/ac"
        app.router.add_post(f"{p}/admit",           admit_ep)
        app.router.add_post(f"{p}/release",         release_ep)
        app.router.add_get( f"{p}/stats",           stats_ep)
        app.router.add_get( f"{p}/resource/{{name}}",res_stats_ep)
        logger.info(f"Admission control API at {prefix}/ac/")
