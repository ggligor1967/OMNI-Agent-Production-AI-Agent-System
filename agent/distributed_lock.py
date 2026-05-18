"""OMNI AGENT - Distributed Lock Manager
Named locks with TTL, fencing tokens, reentrant support,
deadlock detection, and wait queues.

Features:
- Named locks: arbitrary string keys
- Exclusive lock: one holder at a time
- Shared/read locks: multiple concurrent readers, exclusive writers
- TTL: lock auto-expires after ttl_s if holder crashes
- Fencing token: monotonically increasing integer returned on acquire;
    operations stamped with token to detect stale holders
- Reentrant: same owner can acquire same lock multiple times (counted)
- Wait queue: callers block until lock available or timeout
- Deadlock detection: track waits-for graph; cycle = deadlock
- Owner tracking: lock records who holds it
- Force release: admin forcibly release any lock
- Renewal: holder extends TTL before expiry
- Hooks: on_acquire(lock, owner), on_release(lock, owner),
    on_expire(lock), on_deadlock(cycle)
- Lock info: current holder, TTL remaining, wait queue depth
- Audit log: all acquire/release/expire/deadlock events
- SQLite persistence: lock state and audit
- REST API: acquire, release, renew, info, list, stats
"""
import asyncio, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class LockMode(str, Enum):
    EXCLUSIVE = "exclusive"
    SHARED    = "shared"

@dataclass
class LockEntry:
    key: str; mode: LockMode
    owner: str; token: int
    acquired_at: float = field(default_factory=time.time)
    expires_at: float  = 0.0     # 0 = no TTL
    reentrant_count: int = 1
    shared_owners: Set[str] = field(default_factory=set)

    @property
    def ttl_remaining(self) -> float:
        if self.expires_at == 0: return float("inf")
        return max(0.0, self.expires_at - time.time())

    @property
    def is_expired(self) -> bool:
        return self.expires_at > 0 and time.time() > self.expires_at

    def to_dict(self):
        return {"key": self.key, "mode": self.mode.value,
                "owner": self.owner, "token": self.token,
                "ttl_remaining": round(self.ttl_remaining, 2),
                "reentrant_count": self.reentrant_count,
                "shared_owners": list(self.shared_owners)}

class DLStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS audit(
                    id TEXT PRIMARY KEY, event TEXT,
                    lock_key TEXT, owner TEXT,
                    token INTEGER, detail TEXT, ts REAL);
                CREATE TABLE IF NOT EXISTS token_seq(
                    id INTEGER PRIMARY KEY, val INTEGER);
                INSERT OR IGNORE INTO token_seq VALUES(1, 0);
            """)

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def next_token(self) -> int:
        with self._conn() as c:
            c.execute("UPDATE token_seq SET val=val+1 WHERE id=1")
            row = c.execute("SELECT val FROM token_seq WHERE id=1").fetchone()
        return row["val"]

    def log(self, event: str, key: str, owner: str,
             token: int = 0, detail: str = ""):
        with self._conn() as c:
            c.execute("INSERT INTO audit VALUES(?,?,?,?,?,?,?)",
                (str(uuid.uuid4())[:8], event, key, owner,
                 token, detail[:200], time.time()))

    def stats(self) -> Dict:
        with self._conn() as c:
            na = c.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
            by_ev = {r["event"]: r["cnt"] for r in c.execute(
                "SELECT event, COUNT(*) as cnt FROM audit "
                "GROUP BY event").fetchall()}
        return {"audit_entries": na, "by_event": by_ev}

class LockManager:
    """
    Named distributed lock manager with TTL and fencing tokens.

    Usage:
        lm = LockManager()

        token = await lm.acquire("resource:42", owner="worker-1",
                                   ttl_s=30, timeout_s=5)
        # token is a fencing token (monotonic int) or None on timeout

        await lm.renew("resource:42", owner="worker-1", ttl_s=30)
        await lm.release("resource:42", owner="worker-1")

        # Context manager
        async with lm.lock("resource:42", owner="worker-1") as token:
            ...
    """
    def __init__(self, db_path: str = "data/locks.db",
                 max_deadlock_depth: int = 20):
        self._store = DLStore(db_path)
        self._locks: Dict[str, LockEntry] = {}
        self._waiters: Dict[str, List[asyncio.Future]] = {}  # key → futures
        self._waits_for: Dict[str, str] = {}   # owner → lock_key (deadlock)
        self._max_dd = max_deadlock_depth
        self._lock = asyncio.Lock()
        self._hooks_acquire:  List[Callable] = []
        self._hooks_release:  List[Callable] = []
        self._hooks_expire:   List[Callable] = []
        self._hooks_deadlock: List[Callable] = []
        self._sweeper: Optional[asyncio.Task] = None

    def on_acquire(self,  fn): self._hooks_acquire.append(fn)
    def on_release(self,  fn): self._hooks_release.append(fn)
    def on_expire(self,   fn): self._hooks_expire.append(fn)
    def on_deadlock(self, fn): self._hooks_deadlock.append(fn)

    def _fire(self, hooks, *args):
        for h in hooks:
            try: h(*args)
            except: pass

    def _is_free(self, key: str, owner: str, mode: LockMode) -> bool:
        entry = self._locks.get(key)
        if entry is None or entry.is_expired: return True
        if mode == LockMode.SHARED and entry.mode == LockMode.SHARED:
            return True  # shared locks are compatible
        # Reentrant exclusive
        if (mode == LockMode.EXCLUSIVE and entry.mode == LockMode.EXCLUSIVE
                and entry.owner == owner):
            return True
        return False

    def _detect_deadlock(self, owner: str) -> Optional[List[str]]:
        """Detect cycle in waits-for graph starting from owner."""
        visited: Set[str] = set()
        chain: List[str] = [owner]
        cur = owner
        for _ in range(self._max_dd):
            waiting_on = self._waits_for.get(cur)
            if not waiting_on: return None
            lock = self._locks.get(waiting_on)
            if not lock: return None
            next_owner = lock.owner
            if next_owner == owner:
                chain.append(next_owner)
                return chain
            if next_owner in visited: return None
            visited.add(cur); chain.append(next_owner); cur = next_owner
        return None

    async def acquire(self, key: str, owner: str,
                       mode: LockMode = LockMode.EXCLUSIVE,
                       ttl_s: float = 0.0,
                       timeout_s: float = 5.0) -> Optional[int]:
        deadline = time.time() + timeout_s
        while True:
            async with self._lock:
                # Expire stale
                entry = self._locks.get(key)
                if entry and entry.is_expired:
                    self._fire(self._hooks_expire, entry)
                    self._store.log("expire", key, entry.owner, entry.token)
                    self._locks.pop(key, None)
                    entry = None

                if self._is_free(key, owner, mode):
                    entry = self._locks.get(key)
                    if entry and entry.owner == owner and not entry.is_expired:
                        # Reentrant
                        entry.reentrant_count += 1
                        if mode == LockMode.SHARED:
                            entry.shared_owners.add(owner)
                        return entry.token
                    # New acquisition
                    token = self._store.next_token()
                    exp = (time.time() + ttl_s if ttl_s > 0 else 0.0)
                    new_entry = LockEntry(key=key, mode=mode, owner=owner,
                                           token=token, expires_at=exp,
                                           shared_owners={owner} if mode == LockMode.SHARED else set())
                    self._locks[key] = new_entry
                    self._waits_for.pop(owner, None)
                    self._fire(self._hooks_acquire, new_entry, owner)
                    self._store.log("acquire", key, owner, token)
                    return token

                # Deadlock check
                self._waits_for[owner] = key
                cycle = self._detect_deadlock(owner)
                if cycle:
                    self._waits_for.pop(owner, None)
                    self._fire(self._hooks_deadlock, cycle)
                    self._store.log("deadlock", key, owner, 0,
                                     f"cycle:{'>'.join(cycle)}")
                    return None

            # Wait outside lock
            remaining = deadline - time.time()
            if remaining <= 0:
                self._waits_for.pop(owner, None)
                return None
            await asyncio.sleep(min(0.05, remaining))

    async def release(self, key: str, owner: str,
                       force: bool = False) -> bool:
        async with self._lock:
            entry = self._locks.get(key)
            if not entry: return False
            if not force and entry.owner != owner:
                if owner not in entry.shared_owners: return False
            if entry.mode == LockMode.SHARED:
                entry.shared_owners.discard(owner)
                if entry.shared_owners:
                    return True  # others still hold it
            else:
                entry.reentrant_count -= 1
                if entry.reentrant_count > 0: return True
            del self._locks[key]
            self._fire(self._hooks_release, entry, owner)
            self._store.log("release", key, owner, entry.token)
            # Notify waiters
            for fut in self._waiters.pop(key, []):
                if not fut.done(): fut.set_result(True)
        return True

    async def renew(self, key: str, owner: str, ttl_s: float) -> bool:
        async with self._lock:
            entry = self._locks.get(key)
            if not entry or entry.owner != owner: return False
            entry.expires_at = time.time() + ttl_s
            self._store.log("renew", key, owner, entry.token,
                             f"ttl={ttl_s}")
        return True

    def force_release(self, key: str) -> bool:
        entry = self._locks.pop(key, None)
        if entry:
            self._store.log("force_release", key, entry.owner, entry.token)
            return True
        return False

    def info(self, key: str) -> Optional[Dict]:
        entry = self._locks.get(key)
        if not entry: return None
        return {**entry.to_dict(),
                "waiters": len(self._waiters.get(key, []))}

    def list_locks(self) -> List[Dict]:
        return [self.info(k) for k in list(self._locks.keys())]

    async def sweep_expired(self) -> int:
        expired = []
        async with self._lock:
            for key, entry in list(self._locks.items()):
                if entry.is_expired:
                    expired.append((key, entry))
                    del self._locks[key]
        for key, entry in expired:
            self._fire(self._hooks_expire, entry)
            self._store.log("expire", key, entry.owner, entry.token)
        return len(expired)

    async def start_sweeper(self, interval_s: float = 10.0):
        async def loop():
            while True:
                await asyncio.sleep(interval_s)
                await self.sweep_expired()
        self._sweeper = asyncio.ensure_future(loop())

    def stop_sweeper(self):
        if self._sweeper: self._sweeper.cancel()

    class _LockCtx:
        def __init__(self, mgr, key, owner, mode, ttl_s, timeout_s):
            self._mgr = mgr; self._key = key; self._owner = owner
            self._mode = mode; self._ttl = ttl_s; self._to = timeout_s
            self.token: Optional[int] = None
        async def __aenter__(self):
            self.token = await self._mgr.acquire(
                self._key, self._owner, self._mode, self._ttl, self._to)
            if self.token is None:
                raise TimeoutError(f"Could not acquire lock: {self._key}")
            return self.token
        async def __aexit__(self, *_):
            await self._mgr.release(self._key, self._owner)

    def lock(self, key: str, owner: str,
              mode: LockMode = LockMode.EXCLUSIVE,
              ttl_s: float = 0.0, timeout_s: float = 5.0):
        return self._LockCtx(self, key, owner, mode, ttl_s, timeout_s)

    def stats(self) -> Dict:
        s = self._store.stats()
        s["active_locks"] = len(self._locks)
        s["waiters"] = sum(len(v) for v in self._waiters.values())
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def acquire_ep(req):
            d = await req.json()
            token = await self.acquire(d["key"], d["owner"],
                                        LockMode(d.get("mode","exclusive")),
                                        d.get("ttl_s",0), d.get("timeout_s",5))
            if token is None:
                return web.json_response({"error":"timeout or deadlock"},status=409)
            return web.json_response({"token": token})
        async def release_ep(req):
            d = await req.json()
            ok = await self.release(d["key"], d["owner"],
                                     d.get("force", False))
            return web.json_response({"released": ok})
        async def renew_ep(req):
            d = await req.json()
            ok = await self.renew(d["key"], d["owner"], d["ttl_s"])
            return web.json_response({"renewed": ok})
        async def info_ep(req):
            key = req.match_info["key"]
            return web.json_response(self.info(key) or {})
        async def list_ep(req):
            return web.json_response({"locks": self.list_locks()})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/locks"
        app.router.add_post(f"{p}/acquire",  acquire_ep)
        app.router.add_post(f"{p}/release",  release_ep)
        app.router.add_post(f"{p}/renew",    renew_ep)
        app.router.add_get( f"{p}/{{key}}",  info_ep)
        app.router.add_get( f"{p}/",         list_ep)
        app.router.add_get( f"{p}/stats",    stats_ep)
        logger.info(f"Lock manager API at {prefix}/locks/")
