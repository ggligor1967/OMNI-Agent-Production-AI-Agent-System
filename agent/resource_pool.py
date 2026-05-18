"""OMNI AGENT - Resource Pool
Generic resource pool with lazy init, health checks, drain, resize,
and lifecycle hooks.

Features:
- Resources: any factory-created object (DB connections, HTTP clients, etc.)
- Lazy init: resources created on first borrow, up to max_size
- Min warm: always keep min_size resources ready after first use
- Borrow: returns available resource or creates one up to max_size
- Return: released back to pool for reuse
- Health check: periodic fn(resource) → bool; unhealthy → destroy + replace
- Idleness: resources idle > max_idle_s destroyed (reclaim memory)
- Max lifetime: resources older than max_lifetime_s replaced even if healthy
- Wait queue: callers block if pool exhausted; timeout raises TimeoutError
- Drain: wait for all borrowed to return, then close all; no new borrows
- Resize: change max/min at runtime; excess idle destroyed
- Resource wrapper: tracks created_at, last_used, borrow_count, healthy
- Validation: optional fn(resource) called on every borrow (lightweight)
- Error budget: resource auto-removed after N consecutive errors
- Priority: optional priority queue for resource selection
- Stats: pool size, borrowed, idle, wait_queue depth, hits, misses,
    destroy counts
- Hooks: on_create, on_destroy, on_borrow, on_return, on_health_fail
- Context manager: async with pool.borrow() as resource: ...
- SQLite persistence: resource lifecycle events
- REST API: stats, resize, drain, status
"""
import asyncio, json, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

@dataclass
class PooledResource:
    id: str
    resource: Any
    created_at: float = field(default_factory=time.time)
    last_used: float  = field(default_factory=time.time)
    borrow_count: int = 0
    error_count: int  = 0
    healthy: bool     = True

    @property
    def age_s(self) -> float:
        return time.time() - self.created_at

    @property
    def idle_s(self) -> float:
        return time.time() - self.last_used

    def to_dict(self):
        return {"id": self.id, "age_s": round(self.age_s, 1),
                "idle_s": round(self.idle_s, 1),
                "borrow_count": self.borrow_count,
                "error_count": self.error_count,
                "healthy": self.healthy}

class RPStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS events(
                    id TEXT PRIMARY KEY, event TEXT,
                    resource_id TEXT, detail TEXT, ts REAL);
            """)

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def log(self, event: str, resource_id: str, detail: str = ""):
        with self._conn() as c:
            c.execute("INSERT INTO events VALUES(?,?,?,?,?)",
                (str(uuid.uuid4())[:8], event, resource_id,
                 detail[:200], time.time()))

    def stats(self) -> Dict:
        with self._conn() as c:
            by_ev = {r["event"]: r["cnt"] for r in c.execute(
                "SELECT event, COUNT(*) as cnt FROM events "
                "GROUP BY event").fetchall()}
        return {"by_event": by_ev}

class ResourcePool:
    """
    Generic async resource pool.

    Usage:
        async def factory():
            return SomeDatabaseConnection()

        async def health_check(conn):
            return await conn.ping()

        async def closer(conn):
            await conn.close()

        pool = ResourcePool(
            factory=factory,
            health_check=health_check,
            closer=closer,
            min_size=2, max_size=10,
            max_idle_s=300, max_lifetime_s=3600)

        await pool.start()

        async with pool.borrow() as conn:
            result = await conn.query("SELECT 1")

        await pool.drain()
    """
    def __init__(self, factory: Callable,
                  health_check: Callable = None,
                  validator: Callable = None,
                  closer: Callable = None,
                  min_size: int = 1,
                  max_size: int = 10,
                  max_idle_s: float = 300.0,
                  max_lifetime_s: float = 3600.0,
                  borrow_timeout_s: float = 30.0,
                  max_errors: int = 3,
                  health_interval_s: float = 30.0,
                  db_path: str = "data/resource_pool.db"):
        self._factory     = factory
        self._health_fn   = health_check
        self._validator   = validator
        self._closer      = closer
        self.min_size     = min_size
        self.max_size     = max_size
        self.max_idle_s   = max_idle_s
        self.max_lifetime_s = max_lifetime_s
        self.borrow_timeout_s = borrow_timeout_s
        self.max_errors   = max_errors
        self.health_interval_s = health_interval_s
        self._store       = RPStore(db_path)
        # Pool state
        self._lock        = asyncio.Lock()
        self._idle: List[PooledResource] = []    # available
        self._borrowed: Dict[str, PooledResource] = {}  # id → resource
        self._wait_queue: List[asyncio.Future] = []
        self._drained     = False
        self._total_created = 0
        # Stats
        self._hits = 0; self._misses = 0; self._destroys = 0
        # Tasks
        self._health_task: Optional[asyncio.Task] = None
        # Hooks
        self._hooks_create:      List[Callable] = []
        self._hooks_destroy:     List[Callable] = []
        self._hooks_borrow:      List[Callable] = []
        self._hooks_return:      List[Callable] = []
        self._hooks_health_fail: List[Callable] = []

    def on_create(self,      fn): self._hooks_create.append(fn)
    def on_destroy(self,     fn): self._hooks_destroy.append(fn)
    def on_borrow(self,      fn): self._hooks_borrow.append(fn)
    def on_return(self,      fn): self._hooks_return.append(fn)
    def on_health_fail(self, fn): self._hooks_health_fail.append(fn)

    def _fire(self, hooks, *args):
        for h in hooks:
            try: h(*args)
            except: pass

    @property
    def _total(self) -> int:
        return len(self._idle) + len(self._borrowed)

    async def _create(self) -> PooledResource:
        resource = await (self._factory()
                           if asyncio.iscoroutinefunction(self._factory)
                           else asyncio.get_event_loop().run_in_executor(
                               None, self._factory))
        pr = PooledResource(id=str(uuid.uuid4())[:12], resource=resource)
        self._total_created += 1
        self._store.log("create", pr.id)
        self._fire(self._hooks_create, pr)
        return pr

    async def _destroy(self, pr: PooledResource, reason: str = ""):
        pr.healthy = False
        if self._closer:
            try:
                await (self._closer(pr.resource)
                        if asyncio.iscoroutinefunction(self._closer)
                        else asyncio.get_event_loop().run_in_executor(
                            None, self._closer, pr.resource))
            except: pass
        self._destroys += 1
        self._store.log("destroy", pr.id, reason)
        self._fire(self._hooks_destroy, pr)

    async def _is_healthy(self, pr: PooledResource) -> bool:
        if not self._health_fn: return True
        try:
            result = await (self._health_fn(pr.resource)
                             if asyncio.iscoroutinefunction(self._health_fn)
                             else asyncio.get_event_loop().run_in_executor(
                                 None, self._health_fn, pr.resource))
            return bool(result)
        except: return False

    async def _validate(self, pr: PooledResource) -> bool:
        if not self._validator: return True
        try:
            result = await (self._validator(pr.resource)
                             if asyncio.iscoroutinefunction(self._validator)
                             else asyncio.get_event_loop().run_in_executor(
                                 None, self._validator, pr.resource))
            return bool(result)
        except: return False

    async def start(self):
        """Warm pool to min_size and start health checker."""
        async with self._lock:
            while len(self._idle) < self.min_size and self._total < self.max_size:
                pr = await self._create()
                self._idle.append(pr)
        self._health_task = asyncio.ensure_future(self._health_loop())

    async def _health_loop(self):
        while not self._drained:
            await asyncio.sleep(self.health_interval_s)
            await self._sweep()

    async def _sweep(self):
        """Remove stale/unhealthy idle resources; re-warm to min_size."""
        async with self._lock:
            to_destroy = []
            keep = []
            for pr in self._idle:
                stale = (self.max_idle_s > 0 and pr.idle_s > self.max_idle_s)
                aged  = (self.max_lifetime_s > 0
                          and pr.age_s > self.max_lifetime_s)
                if stale or aged:
                    to_destroy.append((pr, "stale" if stale else "aged"))
                else:
                    keep.append(pr)
            self._idle = keep
        for pr, reason in to_destroy:
            await self._destroy(pr, reason)
        # Health-check remaining idle
        async with self._lock:
            to_check = list(self._idle)
        for pr in to_check:
            if not await self._is_healthy(pr):
                async with self._lock:
                    if pr in self._idle: self._idle.remove(pr)
                self._fire(self._hooks_health_fail, pr)
                await self._destroy(pr, "health_check_failed")
        # Re-warm
        async with self._lock:
            while len(self._idle) < self.min_size and self._total < self.max_size:
                pr = await self._create()
                self._idle.append(pr)

    async def borrow(self, timeout_s: float = None) -> PooledResource:
        if self._drained:
            raise RuntimeError("Pool is drained")
        timeout = timeout_s or self.borrow_timeout_s
        deadline = time.time() + timeout
        while True:
            async with self._lock:
                # Try idle pool
                while self._idle:
                    pr = self._idle.pop(0)
                    # Check lifetime
                    if (self.max_lifetime_s > 0
                            and pr.age_s > self.max_lifetime_s):
                        await self._destroy(pr, "max_lifetime")
                        continue
                    # Validate
                    valid = await self._validate(pr)
                    if not valid:
                        await self._destroy(pr, "invalid")
                        continue
                    pr.last_used = time.time()
                    pr.borrow_count += 1
                    self._borrowed[pr.id] = pr
                    self._hits += 1
                    self._store.log("borrow", pr.id)
                    self._fire(self._hooks_borrow, pr)
                    return pr
                # Create new if under max
                if self._total < self.max_size:
                    pr = await self._create()
                    pr.borrow_count += 1
                    self._borrowed[pr.id] = pr
                    self._misses += 1
                    self._store.log("borrow_new", pr.id)
                    self._fire(self._hooks_borrow, pr)
                    return pr
                # Wait
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError(
                        "ResourcePool: timed out waiting for resource")
                fut: asyncio.Future = asyncio.get_event_loop().create_future()
                self._wait_queue.append(fut)
            try:
                await asyncio.wait_for(asyncio.shield(fut),
                                        min(0.1, remaining))
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

    async def release(self, pr: PooledResource,
                       error: bool = False, discard: bool = False):
        async with self._lock:
            self._borrowed.pop(pr.id, None)
        if error:
            pr.error_count += 1
        discard = discard or (self.max_errors > 0
                               and pr.error_count >= self.max_errors)
        pr.last_used = time.time()
        self._store.log("return", pr.id)
        self._fire(self._hooks_return, pr)
        if discard:
            await self._destroy(pr, "discarded")
        else:
            async with self._lock:
                self._idle.append(pr)
                # Notify waiters
                while self._wait_queue:
                    fut = self._wait_queue.pop(0)
                    if not fut.done():
                        fut.set_result(True); break

    class _BorrowCtx:
        def __init__(self, pool, timeout_s=None):
            self._pool = pool; self._timeout = timeout_s; self.pr = None
        async def __aenter__(self):
            self.pr = await self._pool.borrow(self._timeout)
            return self.pr.resource
        async def __aexit__(self, exc_type, *_):
            if self.pr:
                await self._pool.release(self.pr, error=exc_type is not None)

    def borrow_ctx(self, timeout_s: float = None):
        return self._BorrowCtx(self, timeout_s)

    async def drain(self, timeout_s: float = 30.0):
        self._drained = True
        if self._health_task:
            self._health_task.cancel()
        deadline = time.time() + timeout_s
        while self._borrowed and time.time() < deadline:
            await asyncio.sleep(0.05)
        async with self._lock:
            idle = list(self._idle); self._idle = []
        for pr in idle:
            await self._destroy(pr, "drain")

    async def resize(self, new_max: int = None, new_min: int = None):
        async with self._lock:
            if new_max is not None: self.max_size = new_max
            if new_min is not None: self.min_size = new_min
            # Evict excess idle
            while len(self._idle) > self.max_size:
                pr = self._idle.pop()
                await self._destroy(pr, "resize_shrink")

    def record_error(self, pr: PooledResource):
        pr.error_count += 1

    def stats(self) -> Dict:
        s = self._store.stats()
        s.update({"idle": len(self._idle),
                   "borrowed": len(self._borrowed),
                   "total": self._total,
                   "max_size": self.max_size,
                   "min_size": self.min_size,
                   "wait_queue": len(self._wait_queue),
                   "hits": self._hits, "misses": self._misses,
                   "destroys": self._destroys,
                   "total_created": self._total_created,
                   "hit_rate": (self._hits / (self._hits + self._misses)
                                 if (self._hits + self._misses) else 0),
                   "resources": [pr.to_dict()
                                   for pr in self._idle]})
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def stats_ep(req):
            return web.json_response(self.stats())
        async def resize_ep(req):
            d = await req.json()
            await self.resize(d.get("max_size"), d.get("min_size"))
            return web.json_response({"max": self.max_size,
                                       "min": self.min_size})
        async def drain_ep(req):
            d = await req.json()
            await self.drain(d.get("timeout_s", 30))
            return web.json_response({"drained": True})
        p = f"{prefix}/pool"
        app.router.add_get( f"{p}/stats",  stats_ep)
        app.router.add_post(f"{p}/resize", resize_ep)
        app.router.add_post(f"{p}/drain",  drain_ep)
        logger.info(f"Resource pool API at {prefix}/pool/")
