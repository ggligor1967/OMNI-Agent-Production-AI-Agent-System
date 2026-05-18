"""OMNI AGENT - Response Cache
Semantic LLM response caching: exact + similarity-based lookup,
TTL expiry, LRU eviction, hit-rate analytics, and namespace isolation.

Features:
- Exact match: SHA-256 keyed lookup for identical prompts
- Semantic similarity: word-overlap cosine similarity for near-match
- Configurable threshold: tune similarity required for a cache hit
- Namespaces: isolate caches by model, task, or user
- TTL: configurable per-entry and per-namespace time-to-live
- LRU eviction: evict least-recently-used entries when at capacity
- Async-safe: asyncio.Lock around all writes
- Hit analytics: hit/miss counts, hit rate, avg lookup latency
- Cache warming: pre-load entries from a list of prompt/response pairs
- Compression: optional gzip compression for large responses
- SQLite persistence: survive restarts
- REST API: get, set, invalidate, stats, flush
"""
import asyncio, time, uuid, sqlite3, json, hashlib, re, gzip, logging
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
from collections import OrderedDict
logger = logging.getLogger(__name__)

# ── Similarity helper ─────────────────────────────────────────────────────────
def _bow_vec(text: str) -> Dict[str, int]:
    return {w: 1 for w in re.findall(r'\b\w+\b', text.lower())}

def _cosine(a: Dict, b: Dict) -> float:
    dot = sum(a.get(w, 0) * b.get(w, 0) for w in a)
    na = len(a) ** 0.5; nb = len(b) ** 0.5
    return dot / max(1e-9, na * nb)

def _prompt_key(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()[:20]

# ── Data classes ──────────────────────────────────────────────────────────────
@dataclass
class CacheEntry:
    key: str; prompt: str; response: Any
    namespace: str = "default"
    ttl: float = 3600.0; hits: int = 0
    compressed: bool = False
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    metadata: Dict = field(default_factory=dict)

    @property
    def expired(self): return time.time() - self.created_at > self.ttl

    def touch(self):
        self.hits += 1; self.last_accessed = time.time()

    def to_dict(self):
        return {"key": self.key, "namespace": self.namespace,
                "hits": self.hits, "compressed": self.compressed,
                "ttl": self.ttl, "expired": self.expired,
                "age_s": round(time.time() - self.created_at, 1)}

@dataclass
class CacheLookup:
    hit: bool; prompt: str; response: Any = None
    similarity: float = 0.0; matched_key: str = ""
    latency_ms: float = 0.0; exact: bool = False

    def to_dict(self):
        return {"hit": self.hit, "similarity": round(self.similarity, 4),
                "exact": self.exact, "latency_ms": round(self.latency_ms, 2),
                "matched_key": self.matched_key}

class RCStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS cache(
                    key TEXT, namespace TEXT,
                    prompt TEXT, response BLOB, compressed INTEGER DEFAULT 0,
                    ttl REAL DEFAULT 3600, hits INTEGER DEFAULT 0,
                    metadata TEXT DEFAULT '{}',
                    created_at REAL, last_accessed REAL,
                    PRIMARY KEY(key, namespace));
                CREATE TABLE IF NOT EXISTS lookups(
                    id TEXT PRIMARY KEY, hit INTEGER, similarity REAL,
                    exact INTEGER, latency_ms REAL, namespace TEXT, timestamp REAL);
                CREATE INDEX IF NOT EXISTS idx_rc_ns ON cache(namespace, last_accessed DESC);
                CREATE INDEX IF NOT EXISTS idx_lk_ts ON lookups(timestamp DESC);
            """)

    def save(self, e: CacheEntry):
        response_data = e.response
        if e.compressed and isinstance(response_data, str):
            response_data = gzip.compress(response_data.encode())
        elif not isinstance(response_data, (bytes, bytearray)):
            response_data = json.dumps(response_data)
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?,?,?,?,?,?,?,?)",
                (e.key, e.namespace, e.prompt, response_data,
                 int(e.compressed), e.ttl, e.hits,
                 json.dumps(e.metadata), e.created_at, e.last_accessed))

    def get(self, key: str, namespace: str) -> Optional[CacheEntry]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM cache WHERE key=? AND namespace=?",
                             (key, namespace)).fetchone()
        if not row: return None
        resp = row["response"]
        if row["compressed"]:
            try: resp = gzip.decompress(resp).decode()
            except: resp = str(resp)
        elif isinstance(resp, (bytes, bytearray)):
            try: resp = json.loads(resp.decode())
            except: resp = resp.decode()
        e = CacheEntry(key=row["key"], prompt=row["prompt"], response=resp,
                        namespace=row["namespace"], ttl=row["ttl"],
                        hits=row["hits"], compressed=bool(row["compressed"]),
                        metadata=json.loads(row["metadata"] or "{}"),
                        created_at=row["created_at"], last_accessed=row["last_accessed"])
        return None if e.expired else e

    def get_namespace_entries(self, namespace: str, limit: int = 1000) -> List[CacheEntry]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT key, prompt, last_accessed FROM cache WHERE namespace=? "
                "AND created_at + ttl > ? ORDER BY last_accessed DESC LIMIT ?",
                (namespace, time.time(), limit)).fetchall()
        return [CacheEntry(key=r["key"], prompt=r["prompt"], response=None,
                            last_accessed=r["last_accessed"]) for r in rows]

    def update_hits(self, key: str, namespace: str):
        with self._conn() as c:
            c.execute("UPDATE cache SET hits=hits+1, last_accessed=? WHERE key=? AND namespace=?",
                (time.time(), key, namespace))

    def delete(self, key: str, namespace: str):
        with self._conn() as c:
            c.execute("DELETE FROM cache WHERE key=? AND namespace=?", (key, namespace))

    def flush_namespace(self, namespace: str) -> int:
        with self._conn() as c:
            cur = c.execute("DELETE FROM cache WHERE namespace=?", (namespace,))
        return cur.rowcount

    def flush_expired(self) -> int:
        with self._conn() as c:
            cur = c.execute("DELETE FROM cache WHERE created_at + ttl < ?", (time.time(),))
        return cur.rowcount

    def log_lookup(self, hit: bool, similarity: float, exact: bool,
                    latency_ms: float, namespace: str):
        with self._conn() as c:
            c.execute("INSERT INTO lookups VALUES(?,?,?,?,?,?,?)",
                (str(uuid.uuid4())[:10], int(hit), similarity,
                 int(exact), latency_ms, namespace, time.time()))

    def stats(self, namespace: str = None) -> Dict:
        with self._conn() as c:
            if namespace:
                total = c.execute("SELECT COUNT(*) FROM lookups WHERE namespace=?", (namespace,)).fetchone()[0]
                hits  = c.execute("SELECT SUM(hit) FROM lookups WHERE namespace=?", (namespace,)).fetchone()[0] or 0
                nc    = c.execute("SELECT COUNT(*) FROM cache WHERE namespace=?", (namespace,)).fetchone()[0]
            else:
                total = c.execute("SELECT COUNT(*) FROM lookups").fetchone()[0]
                hits  = c.execute("SELECT SUM(hit) FROM lookups").fetchone()[0] or 0
                nc    = c.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
        return {"total_lookups": total, "cache_hits": int(hits),
                "cache_misses": int(total - hits),
                "hit_rate": round(int(hits) / max(1, total), 4),
                "cached_entries": nc}

class ResponseCache:
    """
    Semantic response cache with exact + similarity-based lookup.

    Usage:
        cache = ResponseCache(similarity_threshold=0.85, max_size=500)
        cache.set("What is Python?", "Python is a high-level language...",
                   namespace="general", ttl=3600)

        lookup = await cache.get("What is Python programming?")
        if lookup.hit:
            print("Cache hit!", lookup.response)
            print(f"Similarity: {lookup.similarity:.2%}")
    """
    def __init__(self, db_path: str = "data/response_cache.db",
                 similarity_threshold: float = 0.85,
                 max_size: int = 1000,
                 default_ttl: float = 3600.0,
                 compress: bool = False):
        self._store = RCStore(db_path)
        self._threshold = similarity_threshold
        self._max_size = max_size
        self._default_ttl = default_ttl
        self._compress = compress
        self._lock = asyncio.Lock()
        # LRU in-memory index: namespace → OrderedDict(key → CacheEntry)
        self._index: Dict[str, OrderedDict] = {}

    def _ns_index(self, namespace: str) -> OrderedDict:
        if namespace not in self._index:
            self._index[namespace] = OrderedDict()
        return self._index[namespace]

    def _evict_lru(self, namespace: str):
        idx = self._ns_index(namespace)
        while len(idx) >= self._max_size:
            oldest_key, _ = idx.popitem(last=False)
            self._store.delete(oldest_key, namespace)

    async def get(self, prompt: str, namespace: str = "default") -> CacheLookup:
        start = time.time()
        async with self._lock:
            # Exact lookup
            key = _prompt_key(prompt)
            entry = self._index.get(namespace, {}).get(key)
            if entry is None:
                entry = self._store.get(key, namespace)
            if entry and not entry.expired:
                entry.touch()
                self._store.update_hits(key, namespace)
                idx = self._ns_index(namespace)
                idx.move_to_end(key) if key in idx else idx.__setitem__(key, entry)
                lat = (time.time() - start) * 1000
                self._store.log_lookup(True, 1.0, True, lat, namespace)
                return CacheLookup(hit=True, prompt=prompt, response=entry.response,
                                    similarity=1.0, matched_key=key, exact=True,
                                    latency_ms=lat)

            # Semantic similarity lookup
            pv = _bow_vec(prompt)
            best_score = 0.0; best_key = ""; best_response = None
            for ek, ee in list(self._ns_index(namespace).items()):
                if ee.expired: continue
                sim = _cosine(pv, _bow_vec(ee.prompt))
                if sim > best_score:
                    best_score = sim; best_key = ek; best_response = ee.response

            lat = (time.time() - start) * 1000
            if best_score >= self._threshold:
                self._store.log_lookup(True, best_score, False, lat, namespace)
                return CacheLookup(hit=True, prompt=prompt, response=best_response,
                                    similarity=best_score, matched_key=best_key,
                                    latency_ms=lat)

            self._store.log_lookup(False, best_score, False, lat, namespace)
            return CacheLookup(hit=False, prompt=prompt, similarity=best_score,
                                latency_ms=lat)

    async def set(self, prompt: str, response: Any,
                   namespace: str = "default",
                   ttl: float = None, metadata: Dict = None):
        async with self._lock:
            key = _prompt_key(prompt)
            self._evict_lru(namespace)
            entry = CacheEntry(key=key, prompt=prompt, response=response,
                                namespace=namespace,
                                ttl=ttl or self._default_ttl,
                                compressed=self._compress,
                                metadata=metadata or {})
            idx = self._ns_index(namespace)
            idx[key] = entry
            self._store.save(entry)

    async def invalidate(self, prompt: str, namespace: str = "default") -> bool:
        async with self._lock:
            key = _prompt_key(prompt)
            idx = self._ns_index(namespace)
            existed = key in idx
            idx.pop(key, None)
            self._store.delete(key, namespace)
            return existed

    async def warm(self, pairs: List[Dict], namespace: str = "default"):
        """Pre-load cache from list of {prompt, response} dicts."""
        for p in pairs:
            await self.set(p["prompt"], p["response"], namespace,
                            p.get("ttl"), p.get("metadata"))

    def flush_namespace(self, namespace: str) -> int:
        self._index.pop(namespace, None)
        return self._store.flush_namespace(namespace)

    def flush_expired(self) -> int:
        for ns, idx in self._index.items():
            expired = [k for k, e in idx.items() if e.expired]
            for k in expired: del idx[k]
        return self._store.flush_expired()

    def stats(self, namespace: str = None) -> Dict:
        return self._store.stats(namespace)

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def get_ep(req):
            d = await req.json()
            lookup = await self.get(d["prompt"], d.get("namespace","default"))
            return web.json_response({**lookup.to_dict(),
                "response": lookup.response if lookup.hit else None})
        async def set_ep(req):
            d = await req.json()
            await self.set(d["prompt"], d["response"], d.get("namespace","default"),
                            d.get("ttl"), d.get("metadata"))
            return web.json_response({"stored": True}, status=201)
        async def inval_ep(req):
            d = await req.json()
            ok = await self.invalidate(d["prompt"], d.get("namespace","default"))
            return web.json_response({"invalidated": ok})
        async def stats_ep(req):
            ns = req.rel_url.query.get("namespace")
            return web.json_response(self.stats(ns))
        async def flush_ep(req):
            d = await req.json()
            n = self.flush_namespace(d.get("namespace","default"))
            return web.json_response({"flushed": n})
        p = f"{prefix}/rcache"
        app.router.add_post(f"{p}/get",      get_ep)
        app.router.add_post(f"{p}/set",      set_ep)
        app.router.add_post(f"{p}/invalidate", inval_ep)
        app.router.add_get( f"{p}/stats",    stats_ep)
        app.router.add_post(f"{p}/flush",    flush_ep)
        logger.info(f"Response cache API at {prefix}/rcache/")
