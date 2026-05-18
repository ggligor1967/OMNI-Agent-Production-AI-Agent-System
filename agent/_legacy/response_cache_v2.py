"""OMNI Agent — Response Cache V2: semantic + exact response caching with TTL and eviction."""
from __future__ import annotations
import hashlib, json, math, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class EvictionPolicy(str, Enum):
    LRU   = "lru"
    LFU   = "lfu"
    TTL   = "ttl"
    FIFO  = "fifo"


def _cosine(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na  = math.sqrt(sum(x*x for x in a))
    nb  = math.sqrt(sum(x*x for x in b))
    return dot / (na * nb) if na > 0 and nb > 0 else 0.0


def _default_embed(text: str, dim: int = 16) -> List[float]:
    h   = hashlib.md5(text.encode()).digest()
    raw = list(h) * (dim // 16 + 1)
    vec = [(b / 127.5) - 1.0 for b in raw[:dim]]
    n   = math.sqrt(sum(x*x for x in vec))
    return [x/n for x in vec] if n > 0 else vec


@dataclass
class CacheEntry:
    key: str
    prompt: str
    response: Any
    model_id: str = ""
    embedding: Optional[List[float]] = None
    ttl_s: Optional[float] = None
    created_at: float = field(default_factory=time.time)
    accessed_at: float = field(default_factory=time.time)
    access_count: int = 0
    token_count: int = 0
    cost_saved: float = 0.0
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_expired(self) -> bool:
        if self.ttl_s is None:
            return False
        return (time.time() - self.created_at) > self.ttl_s

    def touch(self):
        self.accessed_at = time.time()
        self.access_count += 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "model_id": self.model_id,
            "access_count": self.access_count,
            "cost_saved": round(self.cost_saved, 6),
            "is_expired": self.is_expired(),
            "age_s": round(time.time() - self.created_at, 1),
            "preview": str(self.response)[:80],
        }


class ResponseCacheV2:
    """
    Two-level response cache:
    - L1: exact match (SHA-256 key)
    - L2: semantic match (cosine similarity over embeddings)

    Features: TTL, LRU/LFU/TTL/FIFO eviction, cost tracking,
    tag-based invalidation, hit/miss stats, SQLite persistence.
    """

    def __init__(
        self,
        capacity: int = 1000,
        default_ttl_s: Optional[float] = None,
        semantic_threshold: float = 0.92,
        eviction_policy: EvictionPolicy = EvictionPolicy.LRU,
        embed_fn: Optional[Callable[[str], List[float]]] = None,
        cost_per_1k: float = 0.0,          # for cost savings calculation
        db_path: str = ":memory:",
    ):
        self.capacity           = capacity
        self.default_ttl_s      = default_ttl_s
        self.semantic_threshold = semantic_threshold
        self.eviction_policy    = eviction_policy
        self._embed_fn          = embed_fn or _default_embed
        self.cost_per_1k        = cost_per_1k
        self._entries: Dict[str, CacheEntry] = {}
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._hits_exact   = 0
        self._hits_semantic = 0
        self._misses        = 0

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS rc_entries (
                key TEXT PRIMARY KEY, prompt TEXT, model_id TEXT,
                token_count INTEGER, cost_saved REAL,
                created_at REAL, accessed_at REAL, access_count INTEGER
            );
            CREATE TABLE IF NOT EXISTS rc_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT, key TEXT, ts REAL
            );
        """)
        self._db.commit()

    # ── WRITE ─────────────────────────────────────────────────────────

    def put(self, prompt: str, response: Any,
            model_id: str = "",
            ttl_s: Optional[float] = None,
            token_count: int = 0,
            tags: Optional[List[str]] = None,
            metadata: Optional[Dict] = None,
            embed: bool = True) -> CacheEntry:
        key = hashlib.sha256(f"{model_id}:{prompt}".encode()).hexdigest()
        ttl = ttl_s if ttl_s is not None else self.default_ttl_s
        emb = self._embed_fn(prompt) if embed else None
        cost_saved = (token_count / 1000) * self.cost_per_1k

        entry = CacheEntry(
            key=key, prompt=prompt, response=response,
            model_id=model_id, embedding=emb, ttl_s=ttl,
            token_count=token_count, cost_saved=cost_saved,
            tags=list(tags or []), metadata=metadata or {})

        self._evict_if_needed()
        self._entries[key] = entry
        self._db.execute(
            "INSERT OR REPLACE INTO rc_entries VALUES (?,?,?,?,?,?,?,?)",
            (key, prompt[:200], model_id, token_count, cost_saved,
             entry.created_at, entry.accessed_at, 0))
        self._db.commit()
        self._log_event("put", key)
        return entry

    # ── READ ──────────────────────────────────────────────────────────

    def get(self, prompt: str, model_id: str = "",
            semantic: bool = True) -> Optional[CacheEntry]:
        # L1: exact
        key = hashlib.sha256(f"{model_id}:{prompt}".encode()).hexdigest()
        entry = self._entries.get(key)
        if entry and not entry.is_expired():
            entry.touch()
            self._hits_exact += 1
            self._log_event("hit_exact", key)
            return entry
        elif entry and entry.is_expired():
            del self._entries[key]

        # L2: semantic
        if semantic:
            q_emb = self._embed_fn(prompt)
            best_entry: Optional[CacheEntry] = None
            best_score = 0.0
            for e in self._entries.values():
                if e.model_id != model_id and model_id:
                    continue
                if e.is_expired() or e.embedding is None:
                    continue
                if len(e.embedding) != len(q_emb):
                    continue
                score = _cosine(q_emb, e.embedding)
                if score > best_score:
                    best_score = score
                    best_entry = e
            if best_entry and best_score >= self.semantic_threshold:
                best_entry.touch()
                self._hits_semantic += 1
                self._log_event("hit_semantic", best_entry.key)
                return best_entry

        self._misses += 1
        self._log_event("miss", key)
        return None

    def get_exact(self, prompt: str, model_id: str = "") -> Optional[CacheEntry]:
        return self.get(prompt, model_id=model_id, semantic=False)

    # ── INVALIDATION ──────────────────────────────────────────────────

    def invalidate(self, key: str) -> bool:
        if key in self._entries:
            del self._entries[key]
            self._db.execute("DELETE FROM rc_entries WHERE key=?", (key,))
            self._db.commit()
            return True
        return False

    def invalidate_by_tag(self, tag: str) -> int:
        to_del = [k for k, e in self._entries.items() if tag in e.tags]
        for k in to_del:
            self.invalidate(k)
        return len(to_del)

    def invalidate_by_model(self, model_id: str) -> int:
        to_del = [k for k, e in self._entries.items() if e.model_id == model_id]
        for k in to_del:
            self.invalidate(k)
        return len(to_del)

    def clear_expired(self) -> int:
        expired = [k for k, e in self._entries.items() if e.is_expired()]
        for k in expired:
            del self._entries[k]
        return len(expired)

    def clear(self):
        self._entries.clear()
        self._db.execute("DELETE FROM rc_entries")
        self._db.commit()

    # ── EVICTION ──────────────────────────────────────────────────────

    def _evict_if_needed(self):
        # Remove expired first
        self.clear_expired()
        if len(self._entries) < self.capacity:
            return
        n_evict = max(1, len(self._entries) - self.capacity + 1)
        if self.eviction_policy == EvictionPolicy.LRU:
            sorted_entries = sorted(self._entries.items(),
                                    key=lambda kv: kv[1].accessed_at)
        elif self.eviction_policy == EvictionPolicy.LFU:
            sorted_entries = sorted(self._entries.items(),
                                    key=lambda kv: kv[1].access_count)
        elif self.eviction_policy == EvictionPolicy.FIFO:
            sorted_entries = sorted(self._entries.items(),
                                    key=lambda kv: kv[1].created_at)
        else:  # TTL — evict nearest expiry
            sorted_entries = sorted(
                self._entries.items(),
                key=lambda kv: (kv[1].ttl_s or float("inf")) - (time.time() - kv[1].created_at))
        for key, _ in sorted_entries[:n_evict]:
            del self._entries[key]

    # ── ANALYTICS ─────────────────────────────────────────────────────

    def _log_event(self, event: str, key: str):
        self._db.execute(
            "INSERT INTO rc_events (event,key,ts) VALUES (?,?,?)",
            (event, key, time.time()))
        self._db.commit()

    def hit_rate(self) -> float:
        total = self._hits_exact + self._hits_semantic + self._misses
        return (self._hits_exact + self._hits_semantic) / total if total > 0 else 0.0

    def total_cost_saved(self) -> float:
        return sum(e.cost_saved * e.access_count for e in self._entries.values())

    def top_entries(self, n: int = 10) -> List[Dict[str, Any]]:
        return [e.to_dict() for e in
                sorted(self._entries.values(),
                       key=lambda e: e.access_count, reverse=True)[:n]]

    def event_log(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._db.execute(
            "SELECT event,key,ts FROM rc_events ORDER BY ts DESC LIMIT ?",
            (limit,)).fetchall()
        return [{"event": r[0], "key": r[1][:12], "ts": r[2]} for r in rows]

    def stats(self) -> Dict[str, Any]:
        return {
            "size": len(self._entries),
            "capacity": self.capacity,
            "hits_exact": self._hits_exact,
            "hits_semantic": self._hits_semantic,
            "misses": self._misses,
            "hit_rate": round(self.hit_rate(), 4),
            "total_cost_saved": round(self.total_cost_saved(), 6),
            "eviction_policy": self.eviction_policy.value,
        }
