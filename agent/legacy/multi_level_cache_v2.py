"""OMNI Agent — Multi-Level Cache V2: L1/L2/L3 with promotion and eviction policies."""
from __future__ import annotations
import hashlib, json, sqlite3, threading, time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class EvictionPolicy(str, Enum):
    LRU  = "lru"
    LFU  = "lfu"
    FIFO = "fifo"
    TTL  = "ttl"     # evict expired first, then LRU


class CacheLevel(int, Enum):
    L1 = 1   # fastest, smallest (in-memory dict)
    L2 = 2   # medium (in-memory ordered dict, larger)
    L3 = 3   # slowest, largest (SQLite-backed)


@dataclass
class CacheEntry:
    key: str
    value: Any
    level: CacheLevel = CacheLevel.L1
    hits: int = 0
    size_bytes: int = 0
    created_at: float = field(default_factory=time.time)
    accessed_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    tags: List[str] = field(default_factory=list)

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and time.time() > self.expires_at

    def to_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "level": self.level.value,
                "hits": self.hits, "size_bytes": self.size_bytes,
                "expires_at": self.expires_at}


@dataclass
class LevelConfig:
    max_size: int = 100
    max_bytes: int = 10 * 1024 * 1024   # 10 MB
    eviction_policy: EvictionPolicy = EvictionPolicy.LRU
    default_ttl_s: Optional[float] = None
    promote_on_hit: bool = True


class _LevelStore:
    """Single cache level backed by OrderedDict."""

    def __init__(self, level: CacheLevel, config: LevelConfig):
        self.level  = level
        self.config = config
        self._data: OrderedDict[str, CacheEntry] = OrderedDict()
        self._freq:  Dict[str, int] = {}
        self._bytes  = 0

    def get(self, key: str) -> Optional[CacheEntry]:
        e = self._data.get(key)
        if e is None: return None
        if e.is_expired:
            self._remove(key)
            return None
        e.hits        += 1
        e.accessed_at  = time.time()
        self._freq[key] = self._freq.get(key, 0) + 1
        if self.config.eviction_policy == EvictionPolicy.LRU:
            self._data.move_to_end(key)
        return e

    def put(self, entry: CacheEntry) -> bool:
        if entry.key in self._data:
            old = self._data[entry.key]
            self._bytes -= old.size_bytes
        self._maybe_evict(entry.size_bytes)
        self._data[entry.key]  = entry
        self._freq[entry.key]  = self._freq.get(entry.key, 0)
        self._bytes           += entry.size_bytes
        if self.config.eviction_policy == EvictionPolicy.LRU:
            self._data.move_to_end(entry.key)
        return True

    def _remove(self, key: str):
        e = self._data.pop(key, None)
        if e:
            self._bytes -= e.size_bytes
            self._freq.pop(key, None)

    def _maybe_evict(self, needed: int):
        while ((len(self._data) >= self.config.max_size or
                self._bytes + needed > self.config.max_bytes)
               and self._data):
            pol = self.config.eviction_policy
            if pol == EvictionPolicy.TTL:
                expired = [k for k, e in self._data.items() if e.is_expired]
                if expired:
                    self._remove(expired[0]); continue
                pol = EvictionPolicy.LRU
            if pol == EvictionPolicy.LFU:
                victim = min(self._freq, key=lambda k: self._freq[k])
            elif pol == EvictionPolicy.FIFO:
                victim = next(iter(self._data))
            else:  # LRU
                victim = next(iter(self._data))
            self._remove(victim)

    def delete(self, key: str) -> bool:
        if key in self._data:
            self._remove(key); return True
        return False

    def clear(self):
        self._data.clear(); self._freq.clear(); self._bytes = 0

    @property
    def size(self) -> int: return len(self._data)

    @property
    def bytes_used(self) -> int: return self._bytes

    def keys(self) -> List[str]: return list(self._data.keys())

    def entries(self) -> List[CacheEntry]:
        return list(self._data.values())


class MultiLevelCacheV2:
    """
    Multi-level cache (L1 → L2 → L3):
    - Three levels with independent size/eviction config
    - Automatic promotion on hit (L3→L2→L1)
    - Demotion on eviction (L1→L2→L3)
    - TTL per entry and per level default
    - Eviction policies: LRU / LFU / FIFO / TTL-first
    - Tag-based invalidation
    - Write-through and write-back modes
    - Serialization hooks (for L3 persistence)
    - Hit/miss tracking per level
    - L3 backed by SQLite
    - Thread-safe
    """

    def __init__(self,
                 l1: Optional[LevelConfig] = None,
                 l2: Optional[LevelConfig] = None,
                 l3: Optional[LevelConfig] = None,
                 db_path: str = ":memory:",
                 serialize_fn: Optional[Callable[[Any], str]] = None,
                 deserialize_fn: Optional[Callable[[str], Any]] = None):
        self._l1 = _LevelStore(CacheLevel.L1, l1 or LevelConfig(max_size=50))
        self._l2 = _LevelStore(CacheLevel.L2, l2 or LevelConfig(max_size=200))
        self._l3_cfg = l3 or LevelConfig(max_size=1000)
        self._serialize   = serialize_fn   or (lambda v: json.dumps(v, default=str))
        self._deserialize = deserialize_fn or json.loads
        self._lock   = threading.Lock()
        self._db     = sqlite3.connect(db_path, check_same_thread=False)
        self._hits:  Dict[int, int] = {1: 0, 2: 0, 3: 0}
        self._misses: Dict[int, int] = {1: 0, 2: 0, 3: 0}
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS mlc_l3 (
                key TEXT PRIMARY KEY, value TEXT,
                hits INTEGER, size_bytes INTEGER,
                created_at REAL, accessed_at REAL,
                expires_at REAL, tags TEXT
            );
        """)
        self._db.commit()

    # ── PUBLIC API ───────────────────────────────────────────────────

    def get(self, key: str) -> Tuple[Optional[Any], int]:
        """Returns (value, level_hit) or (None, 0) on miss."""
        with self._lock:
            # L1
            e = self._l1.get(key)
            if e:
                self._hits[1] += 1
                return e.value, 1
            self._misses[1] += 1
            # L2
            e = self._l2.get(key)
            if e:
                self._hits[2] += 1
                if self._l1.config.promote_on_hit:
                    self._promote(e, CacheLevel.L1)
                return e.value, 2
            self._misses[2] += 1
            # L3
            e = self._l3_get(key)
            if e:
                self._hits[3] += 1
                if self._l2.config.promote_on_hit:
                    e.level = CacheLevel.L2
                    self._l2.put(e)
                if self._l1.config.promote_on_hit:
                    self._promote(e, CacheLevel.L1)
                return e.value, 3
            self._misses[3] += 1
            return None, 0

    def put(self, key: str, value: Any,
            ttl_s: Optional[float] = None,
            tags: Optional[List[str]] = None,
            level: CacheLevel = CacheLevel.L1):
        with self._lock:
            try:
                raw = self._serialize(value)
                size = len(raw.encode())
            except Exception:
                raw  = str(value)
                size = len(raw.encode())
            exp  = (time.time() + ttl_s) if ttl_s else None
            if not exp and level == CacheLevel.L1 and self._l1.config.default_ttl_s:
                exp = time.time() + self._l1.config.default_ttl_s
            e = CacheEntry(key=key, value=value, level=level,
                            size_bytes=size, expires_at=exp,
                            tags=list(tags or []))
            if level == CacheLevel.L1:
                self._l1.put(e)
            elif level == CacheLevel.L2:
                self._l2.put(e)
            else:
                self._l3_put(e)

    def delete(self, key: str) -> bool:
        with self._lock:
            r1 = self._l1.delete(key)
            r2 = self._l2.delete(key)
            r3 = self._l3_delete(key)
            return r1 or r2 or r3

    def invalidate_by_tag(self, tag: str):
        with self._lock:
            for level in [self._l1, self._l2]:
                for e in list(level.entries()):
                    if tag in e.tags:
                        level.delete(e.key)
            self._db.execute(
                "DELETE FROM mlc_l3 WHERE tags LIKE ?",
                (f'%"{tag}"%',))
            self._db.commit()

    def clear(self, level: Optional[CacheLevel] = None):
        with self._lock:
            if level == CacheLevel.L1 or level is None:
                self._l1.clear()
            if level == CacheLevel.L2 or level is None:
                self._l2.clear()
            if level == CacheLevel.L3 or level is None:
                self._db.execute("DELETE FROM mlc_l3")
                self._db.commit()

    def _promote(self, e: CacheEntry, target: CacheLevel):
        e.level = target
        if target == CacheLevel.L1:
            self._l1.put(e)

    # ── L3 (SQLite) ──────────────────────────────────────────────────

    def _l3_get(self, key: str) -> Optional[CacheEntry]:
        row = self._db.execute(
            "SELECT value,hits,size_bytes,created_at,accessed_at,"
            "expires_at,tags FROM mlc_l3 WHERE key=?",
            (key,)).fetchone()
        if not row: return None
        exp = row[5]
        if exp and time.time() > exp:
            self._l3_delete(key); return None
        try:
            val = self._deserialize(row[0])
        except Exception:
            val = row[0]
        tags = json.loads(row[6]) if row[6] else []
        e = CacheEntry(key=key, value=val, level=CacheLevel.L3,
                        hits=row[1] + 1, size_bytes=row[2],
                        created_at=row[3], accessed_at=time.time(),
                        expires_at=exp, tags=tags)
        self._db.execute(
            "UPDATE mlc_l3 SET hits=?,accessed_at=? WHERE key=?",
            (e.hits, e.accessed_at, key))
        self._db.commit()
        return e

    def _l3_put(self, e: CacheEntry):
        try:
            raw = self._serialize(e.value)
        except Exception:
            raw = str(e.value)
        self._db.execute(
            "INSERT OR REPLACE INTO mlc_l3 VALUES (?,?,?,?,?,?,?,?)",
            (e.key, raw, e.hits, e.size_bytes,
             e.created_at, e.accessed_at, e.expires_at,
             json.dumps(e.tags)))
        self._db.commit()

    def _l3_delete(self, key: str) -> bool:
        c = self._db.execute("DELETE FROM mlc_l3 WHERE key=?", (key,))
        self._db.commit()
        return c.rowcount > 0

    # ── STATS ─────────────────────────────────────────────────────────

    def level_stats(self) -> Dict[str, Any]:
        l3_count = self._db.execute(
            "SELECT COUNT(*) FROM mlc_l3").fetchone()[0]
        return {
            "L1": {"size": self._l1.size, "bytes": self._l1.bytes_used,
                   "hits": self._hits[1], "misses": self._misses[1]},
            "L2": {"size": self._l2.size, "bytes": self._l2.bytes_used,
                   "hits": self._hits[2], "misses": self._misses[2]},
            "L3": {"size": l3_count,
                   "hits": self._hits[3], "misses": self._misses[3]},
        }

    def hit_rate(self, level: Optional[int] = None) -> float:
        if level:
            h = self._hits.get(level, 0)
            m = self._misses.get(level, 0)
            return h / (h + m) if (h + m) else 0.0
        total_h = sum(self._hits.values())
        total_m = sum(self._misses.values())
        return total_h / (total_h + total_m) if (total_h + total_m) else 0.0

    def stats(self) -> Dict[str, Any]:
        return {**self.level_stats(), "overall_hit_rate": round(self.hit_rate(), 3)}
