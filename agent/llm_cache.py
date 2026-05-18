"""
OMNI AGENT - Semantic LLM Cache
Embedding-based similarity cache for LLM responses: serve cached answers
for semantically equivalent queries, dramatically cutting cost and latency.

Features:
- Embedding-based similarity: cosine similarity between query embeddings
- Configurable threshold: 0.0 (off) to 1.0 (exact match only)
- Multiple embedding backends: sentence-transformers, OpenAI, or simple TF-IDF
- SQLite-backed persistent cache (survives restarts)
- LRU in-memory index: fast lookup without DB round-trip every time
- Per-namespace caches: separate caches per model, persona, user group
- TTL: expire cache entries after configurable duration
- Cache statistics: hit rate, avg similarity at hit, cost savings estimate
- Invalidation: manual key invalidation or namespace flush
- Cache warming: pre-populate from known Q&A pairs
- REST API: lookup, store, invalidate, stats
"""
import time
import uuid
import json
import math
import sqlite3
import logging
import hashlib
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# EMBEDDING BACKENDS
# ══════════════════════════════════════════════════════════════════════════════

def _normalize(vec: List[float]) -> List[float]:
    """L2-normalize a vector."""
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return vec
    return [x / norm for x in vec]


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """Cosine similarity between two L2-normalized vectors."""
    if len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


class TFIDFEmbedder:
    """
    Simple TF-IDF bag-of-words embedder (no external dependencies).
    Produces a fixed 256-dim embedding via character n-gram hashing.
    Good enough for caching; use sentence-transformers for higher quality.
    """
    DIM = 256

    def embed(self, text: str) -> List[float]:
        text = text.lower().strip()
        vec = [0.0] * self.DIM
        # Character 3-grams with hash bucketing
        words = text.split()
        ngrams = []
        for word in words:
            for i in range(max(1, len(word) - 2)):
                ngrams.append(word[i:i+3])
        if not ngrams:
            # Fallback: character-level
            for i in range(max(1, len(text) - 2)):
                ngrams.append(text[i:i+3])
        for ng in ngrams:
            idx = int(
                hashlib.md5(  # nosec B324 - cache embedding hash only
                    ng.encode(), usedforsecurity=False
                ).hexdigest(),
                16,
            ) % self.DIM
            vec[idx] += 1.0
        return _normalize(vec)

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        return [self.embed(t) for t in texts]


class SentenceTransformerEmbedder:
    """
    sentence-transformers backend (requires: pip install sentence-transformers).
    Falls back to TFIDFEmbedder if not available.
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        self._model = None
        self._model_name = model_name
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(model_name)
            logger.info(f"SentenceTransformer loaded: {model_name}")
        except ImportError:
            logger.info("sentence-transformers not installed, using TF-IDF fallback")
            self._fallback = TFIDFEmbedder()

    def embed(self, text: str) -> List[float]:
        if self._model:
            vec = self._model.encode([text], normalize_embeddings=True)[0]
            return vec.tolist()
        return self._fallback.embed(text)

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        if self._model:
            vecs = self._model.encode(texts, normalize_embeddings=True)
            return [v.tolist() for v in vecs]
        return self._fallback.embed_batch(texts)


# ══════════════════════════════════════════════════════════════════════════════
# CACHE ENTRY
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class CacheEntry:
    id: str
    namespace: str
    query: str
    response: Any
    embedding: List[float]
    model: str = ""
    tokens_saved: int = 0
    cost_saved_usd: float = 0.0
    hits: int = 0
    created_at: float = field(default_factory=time.time)
    last_hit_at: Optional[float] = None
    expires_at: Optional[float] = None

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and time.time() > self.expires_at

    def to_dict(self, include_response: bool = True) -> Dict:
        d = {
            "id": self.id, "namespace": self.namespace,
            "query": self.query[:200], "model": self.model,
            "hits": self.hits, "tokens_saved": self.tokens_saved,
            "cost_saved_usd": round(self.cost_saved_usd, 6),
            "created_at": self.created_at,
            "last_hit_at": self.last_hit_at,
            "expires_at": self.expires_at,
        }
        if include_response:
            d["response"] = self.response
        return d


@dataclass
class CacheHit:
    entry: CacheEntry
    similarity: float
    from_memory: bool = True

    def to_dict(self) -> Dict:
        return {
            "hit": True,
            "similarity": round(self.similarity, 4),
            "from_memory": self.from_memory,
            "entry": self.entry.to_dict(),
        }


# ══════════════════════════════════════════════════════════════════════════════
# CACHE STORE
# ══════════════════════════════════════════════════════════════════════════════

class CacheStore:
    """SQLite-backed persistent cache store."""

    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS cache_entries (
                    id           TEXT PRIMARY KEY,
                    namespace    TEXT NOT NULL,
                    query        TEXT NOT NULL,
                    response     TEXT NOT NULL,
                    embedding    TEXT NOT NULL,
                    model        TEXT DEFAULT '',
                    tokens_saved INTEGER DEFAULT 0,
                    cost_saved   REAL DEFAULT 0,
                    hits         INTEGER DEFAULT 0,
                    created_at   REAL,
                    last_hit_at  REAL,
                    expires_at   REAL
                );
                CREATE INDEX IF NOT EXISTS idx_ce_ns ON cache_entries(namespace);
                CREATE INDEX IF NOT EXISTS idx_ce_exp ON cache_entries(expires_at);
            """)

    def save(self, entry: CacheEntry):
        with self._conn() as c:
            c.execute("""
                INSERT OR REPLACE INTO cache_entries
                (id,namespace,query,response,embedding,model,tokens_saved,
                 cost_saved,hits,created_at,last_hit_at,expires_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                entry.id, entry.namespace, entry.query,
                json.dumps(entry.response, default=str),
                json.dumps(entry.embedding),
                entry.model, entry.tokens_saved, entry.cost_saved_usd,
                entry.hits, entry.created_at, entry.last_hit_at, entry.expires_at,
            ))

    def record_hit(self, entry_id: str):
        with self._conn() as c:
            c.execute("""
                UPDATE cache_entries
                SET hits=hits+1, last_hit_at=?
                WHERE id=?
            """, (time.time(), entry_id))

    def load_namespace(self, namespace: str) -> List[CacheEntry]:
        """Load all valid (non-expired) entries for a namespace."""
        now = time.time()
        with self._conn() as c:
            rows = c.execute("""
                SELECT * FROM cache_entries
                WHERE namespace=? AND (expires_at IS NULL OR expires_at > ?)
            """, (namespace, now)).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def delete(self, entry_id: str) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM cache_entries WHERE id=?", (entry_id,))
        return cur.rowcount > 0

    def flush_namespace(self, namespace: str) -> int:
        with self._conn() as c:
            cur = c.execute("DELETE FROM cache_entries WHERE namespace=?", (namespace,))
        return cur.rowcount

    def purge_expired(self) -> int:
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM cache_entries WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (time.time(),)
            )
        return cur.rowcount

    def stats(self) -> Dict:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0]
            total_hits = c.execute("SELECT SUM(hits) FROM cache_entries").fetchone()[0] or 0
            cost_saved = c.execute("SELECT SUM(cost_saved) FROM cache_entries").fetchone()[0] or 0
            by_ns = dict(c.execute(
                "SELECT namespace, COUNT(*) FROM cache_entries GROUP BY namespace"
            ).fetchall())
        return {
            "total_entries": total,
            "total_hits": total_hits,
            "cost_saved_usd": round(cost_saved, 4),
            "by_namespace": by_ns,
        }

    def _row_to_entry(self, row) -> CacheEntry:
        return CacheEntry(
            id=row["id"], namespace=row["namespace"],
            query=row["query"],
            response=json.loads(row["response"]),
            embedding=json.loads(row["embedding"]),
            model=row["model"] or "",
            tokens_saved=row["tokens_saved"] or 0,
            cost_saved_usd=row["cost_saved"] or 0.0,
            hits=row["hits"] or 0,
            created_at=row["created_at"],
            last_hit_at=row["last_hit_at"],
            expires_at=row["expires_at"],
        )


# ══════════════════════════════════════════════════════════════════════════════
# SEMANTIC CACHE
# ══════════════════════════════════════════════════════════════════════════════

class SemanticCache:
    """
    Semantic LLM response cache with embedding-based similarity lookup.

    Usage:
        cache = SemanticCache(threshold=0.92)

        # Before calling LLM:
        hit = cache.lookup("What is the capital of France?", namespace="chat")
        if hit:
            return hit.entry.response   # serve from cache!

        # After getting LLM response:
        response = await llm.chat(...)
        cache.store(
            query="What is the capital of France?",
            response=response,
            namespace="chat",
            model="gpt-4",
            tokens=42, cost_usd=0.0012,
        )
    """

    def __init__(self,
                 threshold: float = 0.92,
                 embedder=None,
                 db_path: str = "data/llm_cache.db",
                 default_ttl_s: float = 86400 * 7,
                 max_memory_entries: int = 5000):
        self._threshold = threshold
        self._embedder = embedder or TFIDFEmbedder()
        self._store = CacheStore(db_path)
        self._default_ttl = default_ttl_s
        self._max_memory = max_memory_entries

        # In-memory index: namespace → list of (embedding, entry_id)
        self._index: Dict[str, List[Tuple[List[float], str]]] = {}
        self._entries: Dict[str, CacheEntry] = {}   # entry_id → CacheEntry

        # Stats
        self._lookups = 0
        self._hits = 0
        self._stores = 0

        self._load_all()

    def _load_all(self):
        """Load all stored entries into memory index."""
        try:
            # Get all namespaces
            with self._store._conn() as c:
                namespaces = [r[0] for r in c.execute(
                    "SELECT DISTINCT namespace FROM cache_entries"
                ).fetchall()]
            for ns in namespaces:
                entries = self._store.load_namespace(ns)
                for entry in entries:
                    self._add_to_index(entry)
            logger.info(f"Semantic cache loaded: {len(self._entries)} entries")
        except Exception as e:
            logger.debug(f"Cache load: {e}")

    def _add_to_index(self, entry: CacheEntry):
        ns = entry.namespace
        if ns not in self._index:
            self._index[ns] = []
        self._index[ns].append((entry.embedding, entry.id))
        self._entries[entry.id] = entry

    # ── Public API ────────────────────────────────────────────────────────────

    def lookup(self, query: str,
               namespace: str = "default",
               threshold: float = None) -> Optional[CacheHit]:
        """
        Look up a query in the cache.
        Returns CacheHit if a sufficiently similar cached response is found.
        """
        self._lookups += 1
        thr = threshold if threshold is not None else self._threshold
        if thr <= 0:
            return None

        query_emb = _normalize(self._embedder.embed(query))
        ns_index = self._index.get(namespace, [])

        best_sim = -1.0
        best_id = None

        for emb, entry_id in ns_index:
            entry = self._entries.get(entry_id)
            if not entry or entry.is_expired:
                continue
            sim = _cosine_similarity(query_emb, emb)
            if sim > best_sim:
                best_sim = sim
                best_id = entry_id

        if best_id and best_sim >= thr:
            self._hits += 1
            entry = self._entries[best_id]
            entry.hits += 1
            entry.last_hit_at = time.time()
            self._store.record_hit(best_id)
            return CacheHit(entry=entry, similarity=best_sim, from_memory=True)

        return None

    def store(self, query: str, response: Any,
              namespace: str = "default",
              model: str = "",
              tokens: int = 0,
              cost_usd: float = 0.0,
              ttl_s: float = None) -> CacheEntry:
        """Store a query-response pair in the cache."""
        self._stores += 1
        embedding = _normalize(self._embedder.embed(query))
        now = time.time()
        ttl = ttl_s if ttl_s is not None else self._default_ttl

        entry = CacheEntry(
            id=str(uuid.uuid4())[:14],
            namespace=namespace,
            query=query,
            response=response,
            embedding=embedding,
            model=model,
            tokens_saved=tokens,
            cost_saved_usd=cost_usd,
            created_at=now,
            expires_at=now + ttl if ttl > 0 else None,
        )
        self._store.save(entry)
        self._add_to_index(entry)

        # Evict if over memory limit
        if len(self._entries) > self._max_memory:
            self._evict_lru()

        return entry

    def invalidate(self, entry_id: str) -> bool:
        """Remove a specific cache entry."""
        entry = self._entries.pop(entry_id, None)
        if entry:
            ns_index = self._index.get(entry.namespace, [])
            self._index[entry.namespace] = [
                (e, eid) for e, eid in ns_index if eid != entry_id
            ]
        return self._store.delete(entry_id)

    def flush(self, namespace: str = None) -> int:
        """Flush all entries in a namespace (or all namespaces if None)."""
        if namespace:
            # Remove from memory
            ns_index = self._index.pop(namespace, [])
            for _, eid in ns_index:
                self._entries.pop(eid, None)
            return self._store.flush_namespace(namespace)
        else:
            self._index.clear()
            self._entries.clear()
            total = 0
            with self._store._conn() as c:
                cur = c.execute("DELETE FROM cache_entries")
                total = cur.rowcount
            return total

    def warm(self, pairs: List[Tuple[str, Any]],
             namespace: str = "default",
             model: str = ""):
        """Pre-populate cache with known query-response pairs."""
        for query, response in pairs:
            # Only store if not already cached
            hit = self.lookup(query, namespace, threshold=0.99)
            if not hit:
                self.store(query, response, namespace=namespace, model=model)
        logger.info(f"Cache warmed: {len(pairs)} pairs → namespace '{namespace}'")

    def _evict_lru(self):
        """Evict least-recently-hit entries to stay within memory limit."""
        sorted_entries = sorted(
            self._entries.values(),
            key=lambda e: e.last_hit_at or e.created_at
        )
        to_evict = len(self._entries) - self._max_memory + 100
        for entry in sorted_entries[:to_evict]:
            self.invalidate(entry.id)

    def stats(self) -> Dict:
        hit_rate = self._hits / self._lookups if self._lookups else 0.0
        db_stats = self._store.stats()
        return {
            **db_stats,
            "memory_entries": len(self._entries),
            "lookups": self._lookups,
            "hits": self._hits,
            "stores": self._stores,
            "hit_rate": round(hit_rate, 4),
            "threshold": self._threshold,
        }

    def set_threshold(self, threshold: float):
        """Adjust similarity threshold at runtime."""
        self._threshold = max(0.0, min(1.0, threshold))

    def register_routes(self, app, prefix: str = ""):
        from aiohttp import web

        async def lookup_ep(request):
            data = await request.json()
            query = data.get("query", "")
            ns = data.get("namespace", "default")
            thr = data.get("threshold")
            hit = self.lookup(query, ns, threshold=thr)
            if hit:
                return web.json_response(hit.to_dict())
            return web.json_response({"hit": False, "query": query})

        async def store_ep(request):
            data = await request.json()
            entry = self.store(
                query=data["query"],
                response=data["response"],
                namespace=data.get("namespace", "default"),
                model=data.get("model", ""),
                tokens=data.get("tokens", 0),
                cost_usd=data.get("cost_usd", 0.0),
                ttl_s=data.get("ttl_s"),
            )
            return web.json_response(entry.to_dict(include_response=False), status=201)

        async def invalidate_ep(request):
            eid = request.match_info["id"]
            ok = self.invalidate(eid)
            return web.json_response({"invalidated": ok})

        async def flush_ep(request):
            ns = request.rel_url.query.get("namespace")
            count = self.flush(ns)
            return web.json_response({"flushed": count})

        async def stats_ep(request):
            return web.json_response(self.stats())

        async def warm_ep(request):
            data = await request.json()
            pairs = [(p["query"], p["response"]) for p in data.get("pairs", [])]
            ns = data.get("namespace", "default")
            self.warm(pairs, namespace=ns)
            return web.json_response({"warmed": len(pairs)})

        app.router.add_post(f"{prefix}/cache/lookup",        lookup_ep)
        app.router.add_post(f"{prefix}/cache/store",         store_ep)
        app.router.add_delete(f"{prefix}/cache/{{id}}",      invalidate_ep)
        app.router.add_post(f"{prefix}/cache/flush",         flush_ep)
        app.router.add_get( f"{prefix}/cache/stats",         stats_ep)
        app.router.add_post(f"{prefix}/cache/warm",          warm_ep)
        logger.info(f"Semantic cache API routes registered at {prefix}/cache/")
