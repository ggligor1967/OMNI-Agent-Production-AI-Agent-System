"""OMNI Agent — Cache Manager V2: multi-tier cache with eviction, TTL, and partitions."""
from __future__ import annotations
import hashlib, json, sqlite3, threading, time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class EvictionPolicy(str, Enum):
    LRU   = "lru"      # Least Recently Used
    LFU   = "lfu"      # Least Frequently Used
    FIFO  = "fifo"     # First In First Out
    TTL   = "ttl"      # expire by time only
    SLRU  = "slru"     # Segmented LRU (protected + probationary)


class CacheTier(str, Enum):
    L1 = "l1"   # in-memory (fast)
    L2 = "l2"   # SQLite (persistent, larger)


@dataclass
class CacheEntry:
    key: str
    value: Any
    partition: str = "default"
    created_at: float = field(default_factory=time.time)
    accessed_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    access_count: int = 0
    size_bytes: int = 0
    tier: CacheTier = CacheTier.L1

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and time.time() > self.expires_at

    @property
    def age_s(self) -> float:
        return time.time() - self.created_at

    def touch(self):
        self.accessed_at = time.time()
        self.access_count += 1


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    expirations: int = 0
    l1_hits: int = 0
    l2_hits: int = 0
    writes: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hits": self.hits, "misses": self.misses,
            "hit_rate": round(self.hit_rate, 4),
            "l1_hits": self.l1_hits, "l2_hits": self.l2_hits,
            "evictions": self.evictions,
            "expirations": self.expirations,
            "writes": self.writes,
        }


class CacheManagerV2:
    """
    Two-tier cache manager:
    - L1: in-memory dict (fast, bounded by max_l1_size)
    - L2: SQLite (persistent, larger capacity)
    - Eviction policies: LRU, LFU, FIFO, TTL, SLRU
    - Partition support (isolated namespaces within L1/L2)
    - TTL per entry or global default
    - Write-through to L2 option
    - Cache warming from loader functions
    - Bulk invalidation by partition or tag prefix
    - Thread-safe
    """

    def __init__(
        self,
        max_l1_size: int = 256,
        default_ttl_s: Optional[float] = None,
        eviction_policy: EvictionPolicy = EvictionPolicy.LRU,
        write_through: bool = True,
        db_path: str = ":memory:",
    ):
        self.max_l1_size      = max_l1_size
        self.default_ttl_s    = default_ttl_s
        self.eviction_policy  = eviction_policy
        self.write_through    = write_through
        self._l1: Dict[str, CacheEntry] = {}
        self._fifo_order: List[str] = []       # for FIFO
        self._slru_protected: set = set()      # for SLRU
        self._lock = threading.RLock()
        self._stats = CacheStats()
        self._loaders: Dict[str, Callable] = {}
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS cm_l2 (
                key TEXT PRIMARY KEY, partition TEXT,
                value TEXT, expires_at REAL,
                created_at REAL, access_count INTEGER
            );
        """)
        self._db.commit()

    def _full_key(self, key: str, partition: str) -> str:
        return f"{partition}:{key}"

    # ── GET ───────────────────────────────────────────────────────────

    def get(self, key: str, partition: str = "default",
            default: Any = None) -> Any:
        fk = self._full_key(key, partition)
        with self._lock:
            # L1 check
            entry = self._l1.get(fk)
            if entry:
                if entry.is_expired:
                    self._evict_entry(fk)
                    self._stats.expirations += 1
                else:
                    entry.touch()
                    if self.eviction_policy == EvictionPolicy.SLRU:
                        self._slru_protected.add(fk)
                    self._stats.hits += 1
                    self._stats.l1_hits += 1
                    return entry.value

            # L2 check
            row = self._db.execute(
                "SELECT value,expires_at,access_count FROM cm_l2 WHERE key=?",
                (fk,)).fetchone()
            if row:
                val_str, exp, cnt = row
                if exp and time.time() > exp:
                    self._db.execute("DELETE FROM cm_l2 WHERE key=?", (fk,))
                    self._db.commit()
                    self._stats.expirations += 1
                else:
                    value = json.loads(val_str)
                    self._db.execute(
                        "UPDATE cm_l2 SET access_count=? WHERE key=?",
                        (cnt + 1, fk))
                    self._db.commit()
                    # Promote to L1
                    self._l1_set(fk, key, partition, value,
                                 exp, CacheTier.L2)
                    self._stats.hits += 1
                    self._stats.l2_hits += 1
                    return value

            # Miss — try loader
            loader = self._loaders.get(partition) or self._loaders.get("*")
            if loader:
                try:
                    value = loader(key)
                    if value is not None:
                        self.set(key, value, partition=partition)
                        self._stats.hits += 1
                        return value
                except Exception:
                    pass

            self._stats.misses += 1
            return default

    def get_many(self, keys: List[str],
                 partition: str = "default") -> Dict[str, Any]:
        return {k: self.get(k, partition) for k in keys}

    # ── SET ───────────────────────────────────────────────────────────

    def set(self, key: str, value: Any,
            partition: str = "default",
            ttl_s: Optional[float] = None) -> bool:
        fk = self._full_key(key, partition)
        ttl = ttl_s if ttl_s is not None else self.default_ttl_s
        exp = time.time() + ttl if ttl and ttl > 0 else None
        with self._lock:
            self._l1_set(fk, key, partition, value, exp, CacheTier.L1)
            if self.write_through:
                self._l2_set(fk, partition, value, exp)
            self._stats.writes += 1
        return True

    def set_many(self, mapping: Dict[str, Any],
                 partition: str = "default", **kwargs) -> int:
        count = 0
        for k, v in mapping.items():
            if self.set(k, v, partition=partition, **kwargs):
                count += 1
        return count

    def _l1_set(self, fk: str, key: str, partition: str,
                 value: Any, exp: Optional[float],
                 tier: CacheTier):
        if len(self._l1) >= self.max_l1_size and fk not in self._l1:
            self._evict()
        size = len(json.dumps(value, default=str).encode())
        entry = CacheEntry(key=key, value=value, partition=partition,
                           expires_at=exp, size_bytes=size, tier=tier)
        if fk not in self._l1:
            self._fifo_order.append(fk)
        self._l1[fk] = entry

    def _l2_set(self, fk: str, partition: str,
                 value: Any, exp: Optional[float]):
        try:
            self._db.execute(
                "INSERT OR REPLACE INTO cm_l2 VALUES (?,?,?,?,?,?)",
                (fk, partition, json.dumps(value, default=str),
                 exp, time.time(), 0))
            self._db.commit()
        except Exception:
            pass

    # ── DELETE / INVALIDATE ───────────────────────────────────────────

    def delete(self, key: str, partition: str = "default") -> bool:
        fk = self._full_key(key, partition)
        with self._lock:
            removed = fk in self._l1
            self._l1.pop(fk, None)
            if fk in self._fifo_order:
                self._fifo_order.remove(fk)
            self._slru_protected.discard(fk)
            self._db.execute("DELETE FROM cm_l2 WHERE key=?", (fk,))
            self._db.commit()
        return removed

    def invalidate_partition(self, partition: str) -> int:
        with self._lock:
            prefix = f"{partition}:"
            to_del = [k for k in self._l1 if k.startswith(prefix)]
            for k in to_del:
                self._l1.pop(k, None)
                self._fifo_order = [f for f in self._fifo_order if f != k]
                self._slru_protected.discard(k)
            self._db.execute("DELETE FROM cm_l2 WHERE partition=?", (partition,))
            self._db.commit()
        return len(to_del)

    def clear(self, partition: Optional[str] = None):
        with self._lock:
            if partition:
                self.invalidate_partition(partition)
            else:
                self._l1.clear()
                self._fifo_order.clear()
                self._slru_protected.clear()
                self._db.execute("DELETE FROM cm_l2")
                self._db.commit()

    # ── EVICTION ─────────────────────────────────────────────────────

    def _evict(self):
        if not self._l1:
            return
        self._stats.evictions += 1
        policy = self.eviction_policy

        if policy == EvictionPolicy.FIFO:
            victim = self._fifo_order[0] if self._fifo_order else next(iter(self._l1))
        elif policy == EvictionPolicy.LFU:
            victim = min(self._l1, key=lambda k: self._l1[k].access_count)
        elif policy == EvictionPolicy.TTL:
            expired = [k for k, e in self._l1.items() if e.is_expired]
            victim = expired[0] if expired else min(
                self._l1, key=lambda k: self._l1[k].accessed_at)
        elif policy == EvictionPolicy.SLRU:
            # Evict from probationary first
            prob = [k for k in self._l1 if k not in self._slru_protected]
            if prob:
                victim = min(prob, key=lambda k: self._l1[k].accessed_at)
            else:
                victim = min(self._l1, key=lambda k: self._l1[k].accessed_at)
        else:  # LRU default
            victim = min(self._l1, key=lambda k: self._l1[k].accessed_at)

        self._evict_entry(victim)

    def _evict_entry(self, fk: str):
        self._l1.pop(fk, None)
        if fk in self._fifo_order:
            self._fifo_order.remove(fk)
        self._slru_protected.discard(fk)

    # ── LOADER / WARM ─────────────────────────────────────────────────

    def register_loader(self, partition: str,
                         fn: Callable[[str], Any]):
        self._loaders[partition] = fn

    def warm(self, keys: List[str], partition: str = "default"):
        loader = self._loaders.get(partition) or self._loaders.get("*")
        if not loader:
            return
        for k in keys:
            try:
                v = loader(k)
                if v is not None:
                    self.set(k, v, partition=partition)
            except Exception:
                pass

    # ── EXISTS / TTL ─────────────────────────────────────────────────

    def exists(self, key: str, partition: str = "default") -> bool:
        return self.get(key, partition) is not None

    def ttl(self, key: str, partition: str = "default") -> Optional[float]:
        fk = self._full_key(key, partition)
        entry = self._l1.get(fk)
        if entry and entry.expires_at:
            return max(0.0, entry.expires_at - time.time())
        return None

    # ── STATS / INSPECT ───────────────────────────────────────────────

    def l1_size(self, partition: Optional[str] = None) -> int:
        if partition:
            prefix = f"{partition}:"
            return sum(1 for k in self._l1 if k.startswith(prefix))
        return len(self._l1)

    def l2_size(self, partition: Optional[str] = None) -> int:
        if partition:
            row = self._db.execute(
                "SELECT COUNT(*) FROM cm_l2 WHERE partition=?",
                (partition,)).fetchone()
        else:
            row = self._db.execute("SELECT COUNT(*) FROM cm_l2").fetchone()
        return row[0] if row else 0

    def stats(self) -> Dict[str, Any]:
        return {
            **self._stats.to_dict(),
            "l1_size": self.l1_size(),
            "l2_size": self.l2_size(),
            "max_l1": self.max_l1_size,
            "policy": self.eviction_policy.value,
        }
