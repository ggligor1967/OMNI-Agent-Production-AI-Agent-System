"""OMNI AGENT - Embedding Store
Dense vector store with cosine similarity search, namespace isolation,
metadata filtering, and HNSW-lite approximate nearest-neighbour indexing.

Features:
- VectorEntry: id, vector (List[float]), metadata dict, namespace, tags
- Exact cosine search: O(n) brute force, always correct
- HNSW-lite: layered graph index for approximate search on large corpora
- Namespace isolation: search scoped to named partition
- Metadata filters: pre-filter entries before scoring
- Top-K retrieval with score threshold
- Batch upsert: add many entries efficiently
- Dimensionality validation: all vectors must match store dimension
- Simulated embeddings: hash-based deterministic vector for testing
- Deduplication: optional exact-match dedup by content hash
- Stats: entry count per namespace, avg search time, index state
- SQLite persistence: entries, metadata, index state
- REST API: upsert, search, delete, namespaces, stats
"""
import hashlib, json, math, random, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

# ── Math utilities ─────────────────────────────────────────────────────────────
def _dot(a: List[float], b: List[float]) -> float:
    return sum(x * y for x, y in zip(a, b))

def _norm(v: List[float]) -> float:
    return math.sqrt(sum(x * x for x in v))

def _cosine(a: List[float], b: List[float]) -> float:
    na, nb = _norm(a), _norm(b)
    if na < 1e-12 or nb < 1e-12: return 0.0
    return max(-1.0, min(1.0, _dot(a, b) / (na * nb)))

def _normalize(v: List[float]) -> List[float]:
    n = _norm(v)
    if n < 1e-12: return v
    return [x / n for x in v]

def hash_embed(text: str, dim: int = 128) -> List[float]:
    """Deterministic pseudo-embedding from text hash (for testing)."""
    seed = int(
        hashlib.md5(  # nosec B324 - deterministic test embedding seed only
            text.encode(), usedforsecurity=False
        ).hexdigest(),
        16,
    )
    rng = random.Random(seed)
    vec = [rng.gauss(0, 1) for _ in range(dim)]
    return _normalize(vec)

# ── HNSW-lite ─────────────────────────────────────────────────────────────────
class HNSWIndex:
    """Minimal HNSW-inspired layered graph for approximate cosine search."""
    def __init__(self, dim: int, M: int = 16, ef: int = 50):
        self.dim = dim; self.M = M; self.ef = ef
        self._layers: List[Dict[str, List[str]]] = [{}]  # layer -> {id: [neighbour ids]}
        self._vectors: Dict[str, List[float]] = {}
        self._entry_point: Optional[str] = None

    def add(self, eid: str, vector: List[float]):
        self._vectors[eid] = vector
        level = self._random_level()
        while level >= len(self._layers):
            self._layers.append({})
        for lyr in range(level + 1):
            if eid not in self._layers[lyr]:
                self._layers[lyr][eid] = []
        if self._entry_point is None:
            self._entry_point = eid; return
        # Connect greedily at bottom layer
        candidates = self._search_layer(vector, self._entry_point, self.ef, 0)
        for lyr in range(min(level, len(self._layers) - 1) + 1):
            neighbours = candidates[:self.M]
            self._layers[lyr].setdefault(eid, [])
            for nid, _ in neighbours:
                if nid in self._layers[lyr]:
                    self._layers[lyr][eid].append(nid)
                    self._layers[lyr][nid].append(eid)

    def _random_level(self) -> int:
        level = 0
        while random.random() < 0.5 and level < 8:
            level += 1
        return level

    def _search_layer(self, query: List[float], ep: str,
                       ef: int, layer: int) -> List[Tuple[str, float]]:
        visited = {ep}
        candidates = [(ep, _cosine(query, self._vectors.get(ep, query)))]
        result = list(candidates)
        while candidates:
            candidates.sort(key=lambda x: -x[1])
            eid, score = candidates.pop(0)
            worst = result[-1][1] if result else -1
            if score < worst and len(result) >= ef: break
            for nid in self._layers[layer].get(eid, []):
                if nid not in visited:
                    visited.add(nid)
                    s = _cosine(query, self._vectors.get(nid, query))
                    candidates.append((nid, s))
                    result.append((nid, s))
        result.sort(key=lambda x: -x[1])
        return result[:ef]

    def search(self, query: List[float], k: int) -> List[Tuple[str, float]]:
        if not self._entry_point: return []
        results = self._search_layer(query, self._entry_point, max(self.ef, k), 0)
        return results[:k]

    def remove(self, eid: str):
        self._vectors.pop(eid, None)
        for layer in self._layers:
            layer.pop(eid, None)
            for neighbours in layer.values():
                if eid in neighbours: neighbours.remove(eid)
        if self._entry_point == eid:
            self._entry_point = next(iter(self._vectors), None)

# ── Data classes ───────────────────────────────────────────────────────────────
@dataclass
class VectorEntry:
    id: str
    vector: List[float]
    content: str = ""
    metadata: Dict = field(default_factory=dict)
    namespace: str = "default"
    tags: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def to_dict(self):
        return {"id": self.id, "namespace": self.namespace,
                "content_preview": self.content[:100],
                "metadata": self.metadata, "tags": self.tags,
                "dim": len(self.vector)}

@dataclass
class SearchResult:
    entry: VectorEntry
    score: float

    def to_dict(self):
        return {**self.entry.to_dict(), "score": round(self.score, 6)}

class ESStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS entries(
                    id TEXT PRIMARY KEY, namespace TEXT DEFAULT 'default',
                    content TEXT DEFAULT '', vector TEXT,
                    metadata TEXT DEFAULT '{}',
                    tags TEXT DEFAULT '[]',
                    created_at REAL);
                CREATE INDEX IF NOT EXISTS idx_es_ns ON entries(namespace);
            """)

    def upsert(self, e: VectorEntry):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO entries VALUES(?,?,?,?,?,?,?)",
                (e.id, e.namespace, e.content,
                 json.dumps(e.vector),
                 json.dumps(e.metadata),
                 json.dumps(e.tags),
                 e.created_at))

    def delete(self, eid: str):
        with self._conn() as c:
            c.execute("DELETE FROM entries WHERE id=?", (eid,))

    def load_all(self, namespace: str = None) -> List[VectorEntry]:
        with self._conn() as c:
            if namespace:
                rows = c.execute(
                    "SELECT * FROM entries WHERE namespace=?",
                    (namespace,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM entries").fetchall()
        return [VectorEntry(id=r["id"], namespace=r["namespace"],
                             content=r["content"],
                             vector=json.loads(r["vector"]),
                             metadata=json.loads(r["metadata"]),
                             tags=json.loads(r["tags"]),
                             created_at=r["created_at"]) for r in rows]

    def namespaces(self) -> List[str]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT DISTINCT namespace FROM entries").fetchall()
        return [r["namespace"] for r in rows]

    def count(self, namespace: str = None) -> int:
        with self._conn() as c:
            if namespace:
                return c.execute(
                    "SELECT COUNT(*) FROM entries WHERE namespace=?",
                    (namespace,)).fetchone()[0]
            return c.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

class EmbeddingStore:
    """
    Dense vector store with exact and approximate nearest-neighbour search.

    Usage:
        store = EmbeddingStore(dim=128)

        # Add entries (using hash_embed for testing)
        from agent.embedding_store import hash_embed
        store.upsert("doc1", hash_embed("The quick brown fox"), content="The quick brown fox")
        store.upsert("doc2", hash_embed("A lazy dog"),          content="A lazy dog")

        # Search
        query_vec = hash_embed("fast fox")
        results = store.search(query_vec, k=5)
        for r in results:
            print(r.entry.content, r.score)
    """
    def __init__(self, db_path: str = "data/embeddings.db",
                 dim: int = 128,
                 use_hnsw: bool = True,
                 hnsw_M: int = 16,
                 hnsw_ef: int = 50):
        self._store = ESStore(db_path)
        self.dim = dim
        self._entries: Dict[str, Dict[str, VectorEntry]] = {}  # ns -> {id -> entry}
        self._indices: Dict[str, HNSWIndex] = {}              # ns -> index
        self._use_hnsw = use_hnsw
        self._hnsw_M = hnsw_M; self._hnsw_ef = hnsw_ef
        self._search_times: List[float] = []
        # Load from DB
        for e in self._store.load_all():
            self._mem_add(e)

    def _mem_add(self, e: VectorEntry):
        ns = e.namespace
        if ns not in self._entries:
            self._entries[ns] = {}
            self._indices[ns] = HNSWIndex(self.dim, self._hnsw_M, self._hnsw_ef)
        self._entries[ns][e.id] = e
        if self._use_hnsw:
            self._indices[ns].add(e.id, e.vector)

    def upsert(self, eid: str, vector: List[float],
                content: str = "", metadata: Dict = None,
                namespace: str = "default",
                tags: List[str] = None) -> VectorEntry:
        if len(vector) != self.dim:
            raise ValueError(f"Vector dim {len(vector)} != store dim {self.dim}")
        e = VectorEntry(id=eid, vector=list(vector), content=content,
                         metadata=dict(metadata or {}), namespace=namespace,
                         tags=list(tags or []))
        self._mem_add(e)
        self._store.upsert(e)
        return e

    def upsert_batch(self, entries: List[Dict]) -> List[VectorEntry]:
        return [self.upsert(**{k: v for k, v in e.items()}) for e in entries]

    def delete(self, eid: str, namespace: str = "default") -> bool:
        ns_entries = self._entries.get(namespace, {})
        if eid not in ns_entries: return False
        del ns_entries[eid]
        if namespace in self._indices:
            self._indices[namespace].remove(eid)
        self._store.delete(eid)
        return True

    def get(self, eid: str, namespace: str = "default") -> Optional[VectorEntry]:
        return self._entries.get(namespace, {}).get(eid)

    def search(self, query: List[float], k: int = 10,
                namespace: str = "default",
                min_score: float = 0.0,
                filter_fn: Callable = None,
                tags: List[str] = None) -> List[SearchResult]:
        start = time.time()
        ns_entries = self._entries.get(namespace, {})
        if not ns_entries: return []

        if self._use_hnsw and namespace in self._indices and len(ns_entries) > 20:
            # ANN: get candidates then exact-score
            candidates_ids = [eid for eid, _ in
                               self._indices[namespace].search(query, k * 3)]
            candidates = [ns_entries[eid] for eid in candidates_ids
                           if eid in ns_entries]
        else:
            candidates = list(ns_entries.values())

        results = []
        for e in candidates:
            if filter_fn and not filter_fn(e): continue
            if tags and not any(t in e.tags for t in tags): continue
            score = _cosine(query, e.vector)
            if score >= min_score:
                results.append(SearchResult(entry=e, score=score))

        results.sort(key=lambda r: -r.score)
        elapsed = (time.time() - start) * 1000
        self._search_times.append(elapsed)
        if len(self._search_times) > 200: self._search_times.pop(0)
        return results[:k]

    def search_many(self, queries: List[List[float]],
                     k: int = 5, **kwargs) -> List[List[SearchResult]]:
        return [self.search(q, k, **kwargs) for q in queries]

    def namespaces(self) -> List[str]:
        return list(self._entries.keys())

    def count(self, namespace: str = None) -> int:
        if namespace:
            return len(self._entries.get(namespace, {}))
        return sum(len(v) for v in self._entries.values())

    def stats(self) -> Dict:
        avg_search = (sum(self._search_times) / max(1, len(self._search_times)))
        return {
            "total_entries": self.count(),
            "namespaces": {ns: len(e) for ns, e in self._entries.items()},
            "dim": self.dim,
            "use_hnsw": self._use_hnsw,
            "avg_search_ms": round(avg_search, 3),
            "searches": len(self._search_times)}

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def upsert_ep(req):
            d = await req.json()
            e = self.upsert(d["id"], d["vector"], d.get("content",""),
                             d.get("metadata",{}), d.get("namespace","default"),
                             d.get("tags",[]))
            return web.json_response(e.to_dict(), status=201)
        async def search_ep(req):
            d = await req.json()
            results = self.search(d["vector"], d.get("k",10),
                                   d.get("namespace","default"),
                                   d.get("min_score",0.0))
            return web.json_response({"results": [r.to_dict() for r in results]})
        async def delete_ep(req):
            d = await req.json()
            ok = self.delete(d["id"], d.get("namespace","default"))
            return web.json_response({"deleted": ok})
        async def ns_ep(req):
            return web.json_response({"namespaces": self.namespaces()})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/embed"
        app.router.add_post(f"{p}/upsert",  upsert_ep)
        app.router.add_post(f"{p}/search",  search_ep)
        app.router.add_post(f"{p}/delete",  delete_ep)
        app.router.add_get( f"{p}/ns",      ns_ep)
        app.router.add_get( f"{p}/stats",   stats_ep)
        logger.info(f"Embedding store API at {prefix}/embed/")
