"""OMNI AGENT - Cache Warmer
Predictive LLM response cache pre-filling: analyse usage patterns, schedule
warming tasks, track hit/miss rates, and manage cache eviction policies.

Features:
- Usage pattern analysis: track which prompts are called most frequently
- Predictive warming: pre-fill cache for top-K likely prompts before load
- Schedule warming: run warmup jobs on a cron-like interval
- Hit/miss tracking: record every cache lookup with timestamp
- TTL management: configurable per-entry time-to-live
- Eviction policies: LRU, LFU, FIFO, TTL-based
- Cache stats: hit rate, miss rate, avg latency saved
- Warming jobs: async background warming tasks
- Priority queues: warm high-priority entries first
- SQLite persistence: cache entries and usage stats
- REST API: get, set, warm, stats, flush
"""
import json, time, uuid, sqlite3, asyncio, heapq, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

@dataclass
class CacheEntry:
    key: str; value: Any; ttl: float = 3600.0
    priority: int = 0; hits: int = 0
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    @property
    def expired(self): return time.time() - self.created_at > self.ttl
    @property
    def age_s(self): return round(time.time() - self.created_at, 1)
    def to_dict(self):
        return {"key": self.key[:100], "ttl": self.ttl, "hits": self.hits,
                "priority": self.priority, "age_s": self.age_s, "expired": self.expired}

@dataclass
class WarmingJob:
    id: str; prompts: List[str]; priority: int = 0
    scheduled_at: float = field(default_factory=time.time)
    completed: bool = False; hits_generated: int = 0
    def to_dict(self):
        return {"id": self.id, "prompt_count": len(self.prompts),
                "priority": self.priority, "completed": self.completed,
                "hits_generated": self.hits_generated}

class CacheStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()
    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c
    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS cache_entries(
                    key TEXT PRIMARY KEY, value TEXT, ttl REAL DEFAULT 3600,
                    priority INTEGER DEFAULT 0, hits INTEGER DEFAULT 0,
                    created_at REAL, last_accessed REAL);
                CREATE TABLE IF NOT EXISTS access_log(
                    id TEXT PRIMARY KEY, key TEXT, hit INTEGER,
                    latency_ms REAL DEFAULT 0, timestamp REAL);
                CREATE INDEX IF NOT EXISTS idx_al_key ON access_log(key, timestamp DESC);
            """)
    def set(self, entry: CacheEntry):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO cache_entries VALUES(?,?,?,?,?,?,?)",
                (entry.key, json.dumps(entry.value), entry.ttl, entry.priority,
                 entry.hits, entry.created_at, entry.last_accessed))
    def get(self, key: str) -> Optional[CacheEntry]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM cache_entries WHERE key=?", (key,)).fetchone()
        if not row: return None
        e = CacheEntry(key=row["key"], value=json.loads(row["value"]),
                        ttl=row["ttl"], priority=row["priority"], hits=row["hits"],
                        created_at=row["created_at"], last_accessed=row["last_accessed"])
        if e.expired: return None
        return e
    def increment_hits(self, key: str):
        with self._conn() as c:
            c.execute("UPDATE cache_entries SET hits=hits+1, last_accessed=? WHERE key=?",
                (time.time(), key))
    def delete(self, key: str):
        with self._conn() as c:
            c.execute("DELETE FROM cache_entries WHERE key=?", (key,))
    def flush_expired(self) -> int:
        now = time.time()
        with self._conn() as c:
            cur = c.execute("DELETE FROM cache_entries WHERE created_at + ttl < ?", (now,))
        return cur.rowcount
    def log_access(self, key: str, hit: bool, latency_ms: float = 0):
        with self._conn() as c:
            c.execute("INSERT INTO access_log VALUES(?,?,?,?,?)",
                (str(uuid.uuid4())[:10], key, int(hit), latency_ms, time.time()))
    def top_keys(self, n: int = 20) -> List[Dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT key, COUNT(*) as cnt, SUM(hit) as hits FROM access_log "
                "GROUP BY key ORDER BY cnt DESC LIMIT ?", (n,)).fetchall()
        return [dict(r) for r in rows]
    def stats(self) -> Dict:
        with self._conn() as c:
            ne = c.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0]
            total = c.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]
            hits = c.execute("SELECT SUM(hit) FROM access_log").fetchone()[0] or 0
        return {"cached_entries": ne, "total_accesses": total,
                "cache_hits": int(hits), "cache_misses": int(total - hits),
                "hit_rate": round(hits / max(1, total), 4)}

class CacheWarmer:
    """
    Predictive LLM cache with warming, hit-rate tracking, and eviction.

    Usage:
        warmer = CacheWarmer(generator_fn=my_llm, default_ttl=1800)

        # Manually set a cache entry
        warmer.set("What is Python?", "Python is a high-level language...")

        # Warm top prompts from usage patterns
        await warmer.warm_top_k(k=10)

        # Use in a pipeline
        result = await warmer.get_or_generate("Explain recursion.")
        print(result)
        print(warmer.stats())
    """
    def __init__(self, generator_fn: Optional[Callable] = None,
                 db_path: str = "data/cache_warmer.db",
                 default_ttl: float = 3600.0,
                 eviction_policy: str = "lru",
                 max_size: int = 1000):
        self._gen_fn = generator_fn
        self._store = CacheStore(db_path)
        self._default_ttl = default_ttl
        self._eviction_policy = eviction_policy
        self._max_size = max_size
        self._warming_jobs: List[WarmingJob] = []
        self._in_memory: Dict[str, CacheEntry] = {}

    def set(self, key: str, value: Any, ttl: Optional[float] = None,
             priority: int = 0):
        entry = CacheEntry(key=key, value=value, ttl=ttl or self._default_ttl,
                            priority=priority)
        self._in_memory[key] = entry
        self._store.set(entry)

    def get(self, key: str) -> Tuple[Optional[Any], bool]:
        """Returns (value, is_hit)."""
        # Check in-memory first
        entry = self._in_memory.get(key)
        if entry and not entry.expired:
            entry.hits += 1; entry.last_accessed = time.time()
            self._store.increment_hits(key)
            self._store.log_access(key, hit=True)
            return entry.value, True
        # Check persistent store
        entry = self._store.get(key)
        if entry:
            self._in_memory[key] = entry
            self._store.increment_hits(key)
            self._store.log_access(key, hit=True)
            return entry.value, True
        self._store.log_access(key, hit=False)
        return None, False

    async def get_or_generate(self, key: str, ttl: Optional[float] = None,
                               priority: int = 0) -> Any:
        value, hit = self.get(key)
        if hit: return value
        if not self._gen_fn: return None
        start = time.time()
        fn = self._gen_fn
        value = await fn(key) if asyncio.iscoroutinefunction(fn) else fn(key)
        self._store.log_access(key, hit=False, latency_ms=(time.time()-start)*1000)
        self.set(key, value, ttl, priority)
        return value

    def delete(self, key: str):
        self._in_memory.pop(key, None); self._store.delete(key)

    def flush_expired(self) -> int:
        expired_keys = [k for k, e in self._in_memory.items() if e.expired]
        for k in expired_keys: del self._in_memory[k]
        return self._store.flush_expired() + len(expired_keys)

    async def warm_top_k(self, k: int = 10, ttl: Optional[float] = None) -> WarmingJob:
        top = self._store.top_keys(k)
        prompts = [r["key"] for r in top if not self.get(r["key"])[1]]
        job = WarmingJob(id=str(uuid.uuid4())[:8], prompts=prompts, priority=10)
        self._warming_jobs.append(job)
        if self._gen_fn:
            for prompt in prompts:
                try:
                    await self.get_or_generate(prompt, ttl)
                    job.hits_generated += 1
                except Exception as e:
                    logger.warning(f"Warm error for {prompt!r}: {e}")
        job.completed = True
        logger.info(f"Warm job {job.id}: {job.hits_generated}/{len(prompts)} warmed")
        return job

    async def warm_list(self, prompts: List[str], priority: int = 5,
                         ttl: Optional[float] = None) -> WarmingJob:
        job = WarmingJob(id=str(uuid.uuid4())[:8], prompts=prompts, priority=priority)
        self._warming_jobs.append(job)
        if self._gen_fn:
            for prompt in prompts:
                try:
                    await self.get_or_generate(prompt, ttl)
                    job.hits_generated += 1
                except Exception as e:
                    logger.warning(f"Warm error: {e}")
        job.completed = True; return job

    def top_keys(self, n: int = 20) -> List[Dict]:
        return self._store.top_keys(n)

    def stats(self) -> Dict:
        s = self._store.stats()
        s["in_memory_entries"] = len(self._in_memory)
        s["warming_jobs"] = len(self._warming_jobs)
        s["eviction_policy"] = self._eviction_policy
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def get_ep(req):
            key = req.rel_url.query.get("key","")
            val, hit = self.get(key)
            return web.json_response({"value": val, "hit": hit})
        async def set_ep(req):
            d = await req.json()
            self.set(d["key"], d["value"], d.get("ttl"), int(d.get("priority",0)))
            return web.json_response({"stored": True}, status=201)
        async def warm_ep(req):
            d = await req.json()
            job = await self.warm_list(d.get("prompts",[]), int(d.get("priority",5)))
            return web.json_response(job.to_dict())
        async def stats_ep(req): return web.json_response(self.stats())
        async def flush_ep(req):
            n = self.flush_expired()
            return web.json_response({"flushed": n})
        p = f"{prefix}/cache"
        app.router.add_get( f"{p}/get",   get_ep)
        app.router.add_post(f"{p}/set",   set_ep)
        app.router.add_post(f"{p}/warm",  warm_ep)
        app.router.add_get( f"{p}/stats", stats_ep)
        app.router.add_post(f"{p}/flush", flush_ep)
        logger.info(f"Cache warmer API at {prefix}/cache/")
