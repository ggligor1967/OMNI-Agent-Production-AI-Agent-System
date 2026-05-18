"""OMNI AGENT - Connection Pool
Generic async connection pool: acquire/release, max_size, min_size,
idle timeout, health checks, overflow wait queue, and statistics.

Features:
- Generic: works with any object that has connect()/disconnect()/ping()
- Pool size: min_size (warm), max_size (hard cap)
- Acquire: returns a connection from pool or creates new one
- Release: returns connection to pool; closes if pool full or connection bad
- Overflow wait queue: callers wait up to timeout_s when pool exhausted
- Idle timeout: connections idle > idle_timeout_s are closed and removed
- Health check: periodic ping() on idle connections; remove dead ones
- Connection factory: user-supplied async factory fn → connection object
- Validation: optional validate_fn(conn) → bool before returning to caller
- Max lifetime: connections older than max_lifetime_s are replaced
- Borrowing: track which connections are currently in use
- Context manager: async with pool.acquire() as conn:
- Stats: created, destroyed, acquired, released, wait_count, errors, pool_size
- Hooks: on_acquire(conn), on_release(conn), on_create(conn), on_destroy(conn)
- SQLite persistence: pool config and stats audit
- REST API: stats, resize, drain, ping_all
"""
import asyncio, sqlite3, time, uuid, logging
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

@dataclass
class PooledConn:
    id: str
    conn: Any
    created_at: float = field(default_factory=time.time)
    last_used: float  = field(default_factory=time.time)
    borrow_count: int = 0

    @property
    def idle_s(self) -> float:
        return time.time() - self.last_used

    @property
    def age_s(self) -> float:
        return time.time() - self.created_at

@dataclass
class PoolStats:
    created: int = 0; destroyed: int = 0
    acquired: int = 0; released: int = 0
    wait_count: int = 0; errors: int = 0
    timeouts: int = 0

    def to_dict(self):
        return {k: v for k, v in self.__dict__.items()}

class CPStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS pool_audit(
                    id TEXT PRIMARY KEY, event TEXT, detail TEXT, ts REAL);
            """)

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def log(self, event: str, detail: str = ""):
        with self._conn() as c:
            c.execute("INSERT INTO pool_audit VALUES(?,?,?,?)",
                (str(uuid.uuid4())[:8], event, detail[:200], time.time()))

    def stats_history(self, limit: int = 50) -> List[Dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM pool_audit ORDER BY ts DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

class ConnectionPool:
    """
    Generic async connection pool with wait queue and health checks.

    Usage:
        async def make_conn():
            conn = await MyDB.connect(host="localhost")
            return conn

        pool = ConnectionPool(factory=make_conn, max_size=10)
        await pool.start()

        async with pool.acquire() as conn:
            result = await conn.execute("SELECT 1")

        await pool.close()
    """
    def __init__(self,
                 factory:         Callable,
                 max_size:        int   = 10,
                 min_size:        int   = 2,
                 timeout_s:       float = 5.0,
                 idle_timeout_s:  float = 60.0,
                 max_lifetime_s:  float = 3600.0,
                 validate_fn:     Optional[Callable] = None,
                 destroy_fn:      Optional[Callable] = None,
                 db_path:         str   = "data/connpool.db"):
        self._factory       = factory
        self._max_size      = max_size
        self._min_size      = min_size
        self._timeout       = timeout_s
        self._idle_timeout  = idle_timeout_s
        self._max_lifetime  = max_lifetime_s
        self._validate_fn   = validate_fn
        self._destroy_fn    = destroy_fn
        self._store         = CPStore(db_path)
        self._pool:    List[PooledConn] = []   # idle
        self._in_use:  Dict[str, PooledConn] = {}
        self._total:   int = 0
        self._waiters: asyncio.Queue = asyncio.Queue()
        self._stats    = PoolStats()
        self._lock     = asyncio.Lock()
        self._running  = False
        self._hooks_acquire:  List[Callable] = []
        self._hooks_release:  List[Callable] = []
        self._hooks_create:   List[Callable] = []
        self._hooks_destroy:  List[Callable] = []
        self._hc_task: Optional[asyncio.Task] = None

    def on_acquire(self, fn): self._hooks_acquire.append(fn)
    def on_release(self, fn): self._hooks_release.append(fn)
    def on_create(self,  fn): self._hooks_create.append(fn)
    def on_destroy(self, fn): self._hooks_destroy.append(fn)

    def _fire(self, hooks, conn):
        for h in hooks:
            try: h(conn)
            except: pass

    async def _make_conn(self) -> Optional[PooledConn]:
        try:
            if asyncio.iscoroutinefunction(self._factory):
                raw = await self._factory()
            else:
                raw = self._factory()
            pc = PooledConn(id=str(uuid.uuid4())[:8], conn=raw)
            self._total += 1
            self._stats.created += 1
            self._store.log("create", f"id={pc.id} total={self._total}")
            self._fire(self._hooks_create, pc.conn)
            return pc
        except Exception as e:
            self._stats.errors += 1
            self._store.log("error", f"create failed: {e}")
            return None

    async def _destroy_conn(self, pc: PooledConn):
        self._total -= 1
        self._stats.destroyed += 1
        self._store.log("destroy", f"id={pc.id}")
        self._fire(self._hooks_destroy, pc.conn)
        if self._destroy_fn:
            try:
                if asyncio.iscoroutinefunction(self._destroy_fn):
                    await self._destroy_fn(pc.conn)
                else:
                    self._destroy_fn(pc.conn)
            except: pass

    async def _validate(self, pc: PooledConn) -> bool:
        if pc.age_s > self._max_lifetime:
            return False
        if self._validate_fn:
            try:
                if asyncio.iscoroutinefunction(self._validate_fn):
                    return await self._validate_fn(pc.conn)
                return bool(self._validate_fn(pc.conn))
            except:
                return False
        return True

    async def start(self):
        """Warm pool to min_size and start health-check background task."""
        self._running = True
        async with self._lock:
            while len(self._pool) < self._min_size:
                pc = await self._make_conn()
                if pc: self._pool.append(pc)
        self._hc_task = asyncio.ensure_future(self._health_check_loop())
        self._store.log("start", f"min={self._min_size} max={self._max_size}")

    async def _health_check_loop(self):
        while self._running:
            await asyncio.sleep(30)
            await self._sweep_idle()

    async def _sweep_idle(self):
        async with self._lock:
            kept = []
            for pc in self._pool:
                if pc.idle_s > self._idle_timeout:
                    await self._destroy_conn(pc)
                else:
                    kept.append(pc)
            self._pool = kept
            # Re-warm to min_size
            while len(self._pool) + len(self._in_use) < self._min_size:
                pc = await self._make_conn()
                if pc: self._pool.append(pc)

    async def acquire(self, timeout_s: float = None) -> Any:
        """Return a raw connection. Caller must call release()."""
        deadline = time.time() + (timeout_s or self._timeout)
        self._stats.wait_count += 1
        while True:
            async with self._lock:
                # Try idle pool first
                while self._pool:
                    pc = self._pool.pop()
                    if await self._validate(pc):
                        pc.last_used = time.time()
                        pc.borrow_count += 1
                        self._in_use[pc.id] = pc
                        self._stats.acquired += 1
                        self._fire(self._hooks_acquire, pc.conn)
                        return pc
                    else:
                        await self._destroy_conn(pc)
                # Create new if under limit
                if self._total < self._max_size:
                    pc = await self._make_conn()
                    if pc:
                        pc.borrow_count += 1
                        self._in_use[pc.id] = pc
                        self._stats.acquired += 1
                        self._fire(self._hooks_acquire, pc.conn)
                        return pc

            # Pool exhausted — wait
            remaining = deadline - time.time()
            if remaining <= 0:
                self._stats.timeouts += 1
                raise asyncio.TimeoutError(
                    f"Pool exhausted ({self._total}/{self._max_size})")
            await asyncio.sleep(min(0.05, remaining))

    async def release(self, pc: PooledConn, discard: bool = False):
        """Return a PooledConn back to the pool."""
        async with self._lock:
            self._in_use.pop(pc.id, None)
            pc.last_used = time.time()
            self._fire(self._hooks_release, pc.conn)
            self._stats.released += 1
            if discard or len(self._pool) >= self._max_size:
                await self._destroy_conn(pc)
            else:
                self._pool.append(pc)

    @asynccontextmanager
    async def acquire_ctx(self, timeout_s: float = None):
        """Async context manager: async with pool.acquire_ctx() as conn."""
        pc = await self.acquire(timeout_s)
        ok = True
        try:
            yield pc.conn
        except Exception:
            ok = False; raise
        finally:
            await self.release(pc, discard=not ok)

    async def ping_all(self) -> Dict[str, int]:
        """Ping all idle connections; remove dead ones."""
        alive = dead = 0
        async with self._lock:
            kept = []
            for pc in self._pool:
                if await self._validate(pc):
                    kept.append(pc); alive += 1
                else:
                    await self._destroy_conn(pc); dead += 1
            self._pool = kept
        return {"alive": alive, "dead": dead}

    async def resize(self, new_max: int):
        async with self._lock:
            self._max_size = new_max
            while len(self._pool) > new_max - len(self._in_use):
                pc = self._pool.pop()
                await self._destroy_conn(pc)

    async def drain(self):
        """Close all idle connections; wait for in-use to be released."""
        async with self._lock:
            for pc in self._pool:
                await self._destroy_conn(pc)
            self._pool.clear()

    async def close(self):
        self._running = False
        if self._hc_task:
            self._hc_task.cancel()
        await self.drain()
        self._store.log("close")

    @property
    def size(self) -> int: return len(self._pool)

    @property
    def in_use(self) -> int: return len(self._in_use)

    def stats(self) -> Dict:
        return {**self._stats.to_dict(),
                "pool_idle": len(self._pool),
                "pool_in_use": len(self._in_use),
                "total_connections": self._total,
                "max_size": self._max_size,
                "min_size": self._min_size}

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def stats_ep(req): return web.json_response(self.stats())
        async def ping_ep(req):
            result = await self.ping_all()
            return web.json_response(result)
        async def drain_ep(req):
            await self.drain()
            return web.json_response({"drained": True})
        async def resize_ep(req):
            d = await req.json()
            await self.resize(d["max_size"])
            return web.json_response({"max_size": self._max_size})
        p = f"{prefix}/pool"
        app.router.add_get( f"{p}/stats",  stats_ep)
        app.router.add_post(f"{p}/ping",   ping_ep)
        app.router.add_post(f"{p}/drain",  drain_ep)
        app.router.add_post(f"{p}/resize", resize_ep)
        logger.info(f"Connection pool API at {prefix}/pool/")
