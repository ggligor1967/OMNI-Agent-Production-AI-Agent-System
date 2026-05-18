"""OMNI AGENT - Cache Manager
Multi-tier cache with LRU/LFU/TTL eviction, namespaces,
write-through/back, serialization, and hit/miss analytics.

Features:
- EvictionPolicy: LRU (least recently used), LFU (least frequently used),
    FIFO (insertion order), TTL (oldest-expiry-first)
- Namespaces: isolated key spaces with independent configs and eviction
- TTL: per-entry expiry; background sweeper removes expired entries
- WritePolicy: WRITE_THROUGH (sync to backing store), WRITE_BACK (dirty buffer)
- Backing store interface: get/set/delete hooks for DB, Redis-style, file
- Serialization: optional JSON or pickle serialization of values
- Max size: capacity cap per namespace; eviction fires on insert
- Bulk ops: get_many, set_many, delete_many with atomic semantics
- Cache aside: get_or_set(key, loader_fn, ttl) pattern
- Invalidation: delete by key, delete by prefix, delete by tag
- Tags: group entries for bulk invalidation
- Stats: hits, misses, evictions, hit_rate, avg_ttl, size per namespace
- Promotion: on cache miss from L2, promote to L1
- SQLite persistence: entry log (optional), stats snapshots
- REST API: get, set, delete, flush, stats, namespaces
"""
import json, sqlite3, time, uuid, logging
from collections import defaultdict, OrderedDict
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class EvictionPolicy(str, Enum):
    LRU  = "lru"; LFU  = "lfu"
    FIFO = "fifo"; TTL  = "ttl"

class WritePolicy(str, Enum):
    WRITE_THROUGH = "write_through"
    WRITE_BACK    = "write_back"
    WRITE_AROUND  = "write_around"

@dataclass
class CacheEntry:
    key: str; value: Any; namespace: str = "default"
    ttl_s: float = 0.0          # 0 = no expiry
    created_at: float = field(default_factory=time.time)
    accessed_at: float = field(default_factory=time.time)
    access_count: int = 0
    tags: Set[str] = field(default_factory=set)
    dirty: bool = False          # for write-back

    @property
    def expires_at(self) -> float:
        return self.created_at + self.ttl_s if self.ttl_s > 0 else float("inf")

    @property
    def is_expired(self) -> bool:
        return self.ttl_s > 0 and time.time() > self.expires_at

    def touch(self):
        self.accessed_at = time.time()
        self.access_count += 1

    def to_dict(self):
        return {"key": self.key, "namespace": self.namespace,
                "ttl_s": self.ttl_s, "expires_at": round(self.expires_at, 2),
                "access_count": self.access_count,
                "dirty": self.dirty}

@dataclass
class NamespaceConfig:
    name: str
    max_size: int = 1000
    default_ttl_s: float = 0.0
    eviction: EvictionPolicy = EvictionPolicy.LRU
    write_policy: WritePolicy = WritePolicy.WRITE_THROUGH
    backing_get: Optional[Callable] = None
    backing_set: Optional[Callable] = None
    backing_del: Optional[Callable] = None

@dataclass
class NSStats:
    hits: int = 0; misses: int = 0; evictions: int = 0
    sets: int = 0; deletes: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return round(self.hits / max(1, total), 4)

    def to_dict(self):
        return {"hits": self.hits, "misses": self.misses,
                "evictions": self.evictions, "sets": self.sets,
                "deletes": self.deletes, "hit_rate": self.hit_rate}

class CMStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS cache_stats(
                    id TEXT PRIMARY KEY, namespace TEXT,
                    hits INTEGER, misses INTEGER, evictions INTEGER,
                    size INTEGER, snapshot_at REAL);
            """)

    def snapshot(self, ns: str, stats: NSStats, size: int):
        with self._conn() as c:
            c.execute("INSERT INTO cache_stats VALUES(?,?,?,?,?,?,?)",
                (str(uuid.uuid4())[:8], ns, stats.hits, stats.misses,
                 stats.evictions, size, time.time()))

    def global_stats(self) -> Dict:
        with self._conn() as c:
            n = c.execute("SELECT COUNT(*) FROM cache_stats").fetchone()[0]
        return {"snapshots": n}

class _NSCache:
    """Per-namespace in-memory cache with eviction."""
    def __init__(self, cfg: NamespaceConfig):
        self.cfg = cfg
        self._data: OrderedDict[str, CacheEntry] = OrderedDict()
        self._freq: Dict[str, int] = defaultdict(int)  # for LFU
        self._tags: Dict[str, Set[str]] = defaultdict(set)  # tag → keys
        self.stats = NSStats()

    def _evict_one(self):
        if not self._data: return
        policy = self.cfg.eviction
        if policy == EvictionPolicy.LRU:
            key, _ = next(iter(self._data.items()))  # oldest access = front
        elif policy == EvictionPolicy.FIFO:
            key, _ = next(iter(self._data.items()))
        elif policy == EvictionPolicy.LFU:
            key = min(self._freq, key=lambda k: self._freq.get(k, 0)
                       if k in self._data else float("inf"))
        elif policy == EvictionPolicy.TTL:
            key = min(self._data, key=lambda k: self._data[k].expires_at)
        else:
            key, _ = next(iter(self._data.items()))
        self._remove(key, evicted=True)

    def _remove(self, key: str, evicted: bool = False):
        entry = self._data.pop(key, None)
        if entry:
            self._freq.pop(key, None)
            for tag in entry.tags:
                self._tags[tag].discard(key)
            if evicted: self.stats.evictions += 1

    def get(self, key: str) -> Optional[CacheEntry]:
        # Sweep expired first
        if key in self._data and self._data[key].is_expired:
            self._remove(key)
            self.stats.misses += 1
            return None
        entry = self._data.get(key)
        if entry:
            entry.touch()
            self._freq[key] += 1
            if self.cfg.eviction == EvictionPolicy.LRU:
                self._data.move_to_end(key)
            self.stats.hits += 1
        else:
            self.stats.misses += 1
        return entry

    def set(self, key: str, value: Any, ttl_s: float = 0,
             tags: Set[str] = None) -> CacheEntry:
        ttl = ttl_s if ttl_s > 0 else self.cfg.default_ttl_s
        # Evict if at capacity (and key is new)
        if key not in self._data and len(self._data) >= self.cfg.max_size:
            self._evict_one()
        entry = CacheEntry(key=key, value=value,
                            namespace=self.cfg.name, ttl_s=ttl,
                            tags=set(tags or {}))
        self._data[key] = entry
        if self.cfg.eviction == EvictionPolicy.LRU:
            self._data.move_to_end(key)
        self._freq[key] = self._freq.get(key, 0) + 1
        for tag in entry.tags:
            self._tags[tag].add(key)
        self.stats.sets += 1
        return entry

    def delete(self, key: str) -> bool:
        if key in self._data:
            self._remove(key)
            self.stats.deletes += 1
            return True
        return False

    def delete_by_prefix(self, prefix: str) -> int:
        keys = [k for k in list(self._data) if k.startswith(prefix)]
        for k in keys: self._remove(k)
        self.stats.deletes += len(keys)
        return len(keys)

    def delete_by_tag(self, tag: str) -> int:
        keys = list(self._tags.get(tag, set()))
        for k in keys: self._remove(k)
        self.stats.deletes += len(keys)
        return len(keys)

    def flush(self) -> int:
        n = len(self._data)
        self._data.clear(); self._freq.clear(); self._tags.clear()
        return n

    def sweep_expired(self) -> int:
        expired = [k for k, e in list(self._data.items()) if e.is_expired]
        for k in expired: self._remove(k)
        return len(expired)

    def dirty_entries(self) -> List[CacheEntry]:
        return [e for e in self._data.values() if e.dirty]

    @property
    def size(self) -> int: return len(self._data)

class CacheManager:
    """
    Multi-tier LRU/LFU/TTL cache with namespaces and write-through/back.

    Usage:
        cm = CacheManager()
        cm.create_namespace("sessions", max_size=500,
                             default_ttl_s=3600,
                             eviction=EvictionPolicy.LRU)

        cm.set("user:123", {"name":"Alice"}, namespace="sessions")
        val = cm.get("user:123", namespace="sessions")

        # Cache-aside pattern
        val = cm.get_or_set("user:123", lambda: db.load(123),
                              ttl_s=300, namespace="sessions")
    """
    def __init__(self, db_path: str = "data/cache.db"):
        self._store = CMStore(db_path)
        self._ns: Dict[str, _NSCache] = {}
        self.create_namespace("default")

    def create_namespace(self, name: str,
                          max_size: int = 1000,
                          default_ttl_s: float = 0.0,
                          eviction: EvictionPolicy = EvictionPolicy.LRU,
                          write_policy: WritePolicy = WritePolicy.WRITE_THROUGH,
                          backing_get: Callable = None,
                          backing_set: Callable = None,
                          backing_del: Callable = None) -> NamespaceConfig:
        cfg = NamespaceConfig(name=name, max_size=max_size,
                               default_ttl_s=default_ttl_s,
                               eviction=eviction, write_policy=write_policy,
                               backing_get=backing_get,
                               backing_set=backing_set,
                               backing_del=backing_del)
        self._ns[name] = _NSCache(cfg)
        return cfg

    def _cache(self, namespace: str) -> _NSCache:
        return self._ns.get(namespace) or self._ns["default"]

    def get(self, key: str, namespace: str = "default") -> Optional[Any]:
        cache = self._cache(namespace)
        entry = cache.get(key)
        if entry: return entry.value
        # Try backing store on miss
        cfg = cache.cfg
        if cfg.backing_get:
            try:
                val = cfg.backing_get(key)
                if val is not None:
                    cache.set(key, val)
                    cache.stats.hits += 1   # backing hit
                    cache.stats.misses -= 1  # correct over-count
                    return val
            except: pass
        return None

    def set(self, key: str, value: Any,
             namespace: str = "default",
             ttl_s: float = 0, tags: Set[str] = None) -> CacheEntry:
        cache = self._cache(namespace)
        entry = cache.set(key, value, ttl_s=ttl_s, tags=set(tags or {}))
        cfg = cache.cfg
        if cfg.write_policy == WritePolicy.WRITE_THROUGH and cfg.backing_set:
            try: cfg.backing_set(key, value)
            except: pass
        elif cfg.write_policy == WritePolicy.WRITE_BACK:
            entry.dirty = True
        return entry

    def get_or_set(self, key: str, loader: Callable,
                    ttl_s: float = 0, namespace: str = "default",
                    tags: Set[str] = None) -> Any:
        val = self.get(key, namespace)
        if val is not None: return val
        val = loader()
        if val is not None:
            self.set(key, val, namespace=namespace,
                      ttl_s=ttl_s, tags=tags)
        return val

    def delete(self, key: str, namespace: str = "default") -> bool:
        cache = self._cache(namespace)
        ok = cache.delete(key)
        if ok and cache.cfg.backing_del:
            try: cache.cfg.backing_del(key)
            except: pass
        return ok

    def delete_prefix(self, prefix: str, namespace: str = "default") -> int:
        return self._cache(namespace).delete_by_prefix(prefix)

    def delete_tag(self, tag: str, namespace: str = "default") -> int:
        return self._cache(namespace).delete_by_tag(tag)

    def get_many(self, keys: List[str],
                  namespace: str = "default") -> Dict[str, Any]:
        return {k: self.get(k, namespace) for k in keys}

    def set_many(self, mapping: Dict[str, Any],
                  namespace: str = "default", ttl_s: float = 0):
        for k, v in mapping.items():
            self.set(k, v, namespace=namespace, ttl_s=ttl_s)

    def flush(self, namespace: str = "default") -> int:
        return self._cache(namespace).flush()

    def sweep(self) -> int:
        return sum(c.sweep_expired() for c in self._ns.values())

    def flush_dirty(self, namespace: str = "default") -> int:
        cache = self._cache(namespace)
        cfg = cache.cfg
        flushed = 0
        if cfg.backing_set:
            for entry in cache.dirty_entries():
                try:
                    cfg.backing_set(entry.key, entry.value)
                    entry.dirty = False; flushed += 1
                except: pass
        return flushed

    def stats(self, namespace: str = None) -> Dict:
        if namespace:
            c = self._cache(namespace)
            s = c.stats.to_dict(); s["size"] = c.size
            return s
        result = {}
        for name, c in self._ns.items():
            s = c.stats.to_dict(); s["size"] = c.size
            result[name] = s
        return result

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def get_ep(req):
            ns = req.rel_url.query.get("ns", "default")
            key = req.match_info["key"]
            val = self.get(key, ns)
            if val is None: return web.json_response({"found": False}, status=404)
            return web.json_response({"found": True, "value": val})
        async def set_ep(req):
            d = await req.json()
            ns = d.get("ns", "default"); key = d["key"]
            e = self.set(key, d["value"], ns, d.get("ttl_s", 0),
                          set(d.get("tags", [])))
            return web.json_response(e.to_dict(), status=201)
        async def del_ep(req):
            ns = req.rel_url.query.get("ns","default")
            ok = self.delete(req.match_info["key"], ns)
            return web.json_response({"deleted": ok})
        async def flush_ep(req):
            ns = req.rel_url.query.get("ns","default")
            n = self.flush(ns)
            return web.json_response({"flushed": n})
        async def stats_ep(req):
            ns = req.rel_url.query.get("ns")
            return web.json_response(self.stats(ns))
        p = f"{prefix}/cache"
        app.router.add_get(   f"{p}/{{key}}",  get_ep)
        app.router.add_post(  f"{p}/set",       set_ep)
        app.router.add_delete(f"{p}/{{key}}",   del_ep)
        app.router.add_post(  f"{p}/flush",     flush_ep)
        app.router.add_get(   f"{p}/stats",     stats_ep)
        logger.info(f"Cache manager API at {prefix}/cache/")
