"""OMNI Agent — Semantic Cache: cosine-similarity cache for LLM responses."""
from __future__ import annotations
import hashlib, math, sqlite3, time, threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


def _dot(a: List[float], b: List[float]) -> float:
    return sum(x * y for x, y in zip(a, b))

def _norm(v: List[float]) -> float:
    return math.sqrt(sum(x * x for x in v))

def cosine_similarity(a: List[float], b: List[float]) -> float:
    if len(a) != len(b):
        raise ValueError("Vector dimension mismatch")
    na, nb = _norm(a), _norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return _dot(a, b) / (na * nb)


@dataclass
class CacheEntry:
    entry_id: str
    query: str
    response: str
    embedding: List[float]
    created_at: float = field(default_factory=time.time)
    last_hit: Optional[float] = None
    hit_count: int = 0
    ttl: float = 3600.0        # seconds; -1 = forever
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_expired(self) -> bool:
        if self.ttl < 0:
            return False
        return time.time() - self.created_at > self.ttl

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "query": self.query,
            "response": self.response[:80] + "..." if len(self.response) > 80 else self.response,
            "created_at": self.created_at,
            "hit_count": self.hit_count,
            "ttl": self.ttl,
            "expired": self.is_expired(),
        }


class SemanticCache:
    """
    Cache that retrieves stored responses by semantic similarity of query embeddings.
    Falls back to exact-match hash for zero-cost lookups when available.
    """

    def __init__(
        self,
        threshold: float = 0.92,
        max_size: int = 1000,
        default_ttl: float = 3600.0,
        db_path: str = ":memory:",
    ):
        if not 0.0 < threshold <= 1.0:
            raise ValueError("threshold must be in (0, 1]")
        self.threshold = threshold
        self.max_size = max_size
        self.default_ttl = default_ttl
        self._entries: Dict[str, CacheEntry] = {}   # entry_id → CacheEntry
        self._hash_index: Dict[str, str] = {}        # sha256(query) → entry_id
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS sc_entries (
                entry_id TEXT PRIMARY KEY, query TEXT, response TEXT,
                created_at REAL, hit_count INTEGER, ttl REAL
            );
            CREATE TABLE IF NOT EXISTS sc_stats (
                ts REAL, hits INTEGER, misses INTEGER, evictions INTEGER, size INTEGER
            );
        """)
        self._db.commit()

    def _entry_id(self, query: str) -> str:
        return hashlib.sha256(query.encode()).hexdigest()[:16]

    # ── WRITE ─────────────────────────────────────────────────────────

    def put(self, query: str, response: str, embedding: List[float],
            ttl: Optional[float] = None,
            metadata: Optional[Dict] = None) -> CacheEntry:
        """Store a query-response pair with its embedding."""
        with self._lock:
            self._evict_if_needed()
            eid = self._entry_id(query)
            entry = CacheEntry(
                entry_id=eid,
                query=query,
                response=response,
                embedding=embedding,
                ttl=ttl if ttl is not None else self.default_ttl,
                metadata=metadata or {},
            )
            self._entries[eid] = entry
            self._hash_index[hashlib.sha256(query.encode()).hexdigest()] = eid
            self._db.execute(
                "INSERT OR REPLACE INTO sc_entries VALUES (?,?,?,?,?,?)",
                (eid, query, response, entry.created_at, 0, entry.ttl))
            self._db.commit()
        return entry

    # ── READ ──────────────────────────────────────────────────────────

    def get_exact(self, query: str) -> Optional[CacheEntry]:
        """O(1) exact match lookup."""
        key = hashlib.sha256(query.encode()).hexdigest()
        eid = self._hash_index.get(key)
        if eid is None:
            return None
        entry = self._entries.get(eid)
        if entry is None or entry.is_expired():
            return None
        entry.hit_count += 1
        entry.last_hit = time.time()
        self._hits += 1
        return entry

    def get_semantic(self, query_embedding: List[float],
                     top_k: int = 1) -> List[Tuple[CacheEntry, float]]:
        """Return top_k entries above threshold, sorted by similarity desc."""
        candidates: List[Tuple[CacheEntry, float]] = []
        with self._lock:
            for entry in list(self._entries.values()):
                if entry.is_expired():
                    continue
                if len(entry.embedding) != len(query_embedding):
                    continue
                sim = cosine_similarity(query_embedding, entry.embedding)
                if sim >= self.threshold:
                    candidates.append((entry, sim))
        candidates.sort(key=lambda x: x[1], reverse=True)
        result = candidates[:top_k]
        for entry, _ in result:
            entry.hit_count += 1
            entry.last_hit = time.time()
            self._hits += 1
        if not result:
            self._misses += 1
        return result

    def get(self, query: str, query_embedding: List[float]) -> Optional[str]:
        """Combined lookup: exact first, then semantic. Returns response text or None."""
        exact = self.get_exact(query)
        if exact:
            return exact.response
        results = self.get_semantic(query_embedding, top_k=1)
        if results:
            return results[0][0].response
        self._misses += 1
        return None

    # ── INVALIDATION ──────────────────────────────────────────────────

    def invalidate(self, entry_id: str) -> bool:
        with self._lock:
            entry = self._entries.pop(entry_id, None)
            if entry is None:
                return False
            key = hashlib.sha256(entry.query.encode()).hexdigest()
            self._hash_index.pop(key, None)
            self._db.execute("DELETE FROM sc_entries WHERE entry_id=?", (entry_id,))
            self._db.commit()
        return True

    def invalidate_by_query(self, query: str) -> bool:
        eid = self._entry_id(query)
        return self.invalidate(eid)

    def flush(self):
        with self._lock:
            self._entries.clear()
            self._hash_index.clear()
            self._db.execute("DELETE FROM sc_entries")
            self._db.commit()

    def flush_expired(self) -> int:
        expired = [eid for eid, e in self._entries.items() if e.is_expired()]
        for eid in expired:
            self.invalidate(eid)
        return len(expired)

    # ── EVICTION ──────────────────────────────────────────────────────

    def _evict_if_needed(self):
        if len(self._entries) < self.max_size:
            return
        # Evict LRU (lowest last_hit or created_at)
        lru = min(self._entries.values(),
                  key=lambda e: e.last_hit or e.created_at)
        self._entries.pop(lru.entry_id, None)
        key = hashlib.sha256(lru.query.encode()).hexdigest()
        self._hash_index.pop(key, None)
        self._evictions += 1

    def resize(self, new_max: int):
        self.max_size = new_max
        with self._lock:
            while len(self._entries) > self.max_size:
                self._evict_if_needed()

    # ── STATS ─────────────────────────────────────────────────────────

    def list_entries(self) -> List[Dict[str, Any]]:
        return [e.to_dict() for e in self._entries.values()]

    def stats(self) -> Dict[str, Any]:
        total = self._hits + self._misses
        return {
            "size": len(self._entries),
            "max_size": self.max_size,
            "threshold": self.threshold,
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
            "hit_rate": self._hits / total if total > 0 else 0.0,
        }
