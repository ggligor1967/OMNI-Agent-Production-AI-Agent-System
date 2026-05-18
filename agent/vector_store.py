"""OMNI AGENT - Vector Store
In-process vector store with cosine/dot/euclidean similarity,
k-nearest-neighbor search, namespaces, metadata filtering, and persistence.

Features:
- Vectors: float lists stored with id, namespace, metadata
- Namespaces: isolated collections of vectors
- Upsert: insert or update vector by id
- Search: k-NN with cosine, dot product, or euclidean distance
- Metadata filters: pre-filter candidates by metadata field conditions
- Batch upsert: insert many vectors in one call
- Normalization: auto-normalize on upsert (for cosine) or on demand
- Dimensionality: enforced per namespace (first vector sets dim)
- Delete: remove vector by id
- Fetch: retrieve vector by id with metadata
- List: list ids in namespace with optional prefix filter
- Stats: count per namespace, total vectors, index memory estimate
- Brute-force scan (no HNSW needed for in-process use)
- Ranking: return (id, score, metadata) sorted by similarity
- Centroid: compute centroid of a namespace or subset
- Export: namespace to JSON-serializable dict
- SQLite persistence: vectors and metadata stored as blob + JSON
- REST API: upsert, search, delete, fetch, stats
"""
import json, math, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

Vec = List[float]

def _dot(a: Vec, b: Vec) -> float:
    return sum(x * y for x, y in zip(a, b))

def _norm(v: Vec) -> float:
    return math.sqrt(sum(x * x for x in v))

def _normalize(v: Vec) -> Vec:
    n = _norm(v)
    if n == 0: return v
    return [x / n for x in v]

def _cosine(a: Vec, b: Vec) -> float:
    return _dot(a, b) / ((_norm(a) * _norm(b)) or 1e-10)

def _euclidean(a: Vec, b: Vec) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))

@dataclass
class VectorEntry:
    id: str; namespace: str
    vector: Vec
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self, include_vector: bool = False):
        d = {"id": self.id, "namespace": self.namespace,
              "metadata": self.metadata,
              "created_at": round(self.created_at, 3)}
        if include_vector: d["vector"] = self.vector
        return d

@dataclass
class SearchResult:
    id: str; score: float; metadata: Dict[str, Any]

    def to_dict(self):
        return {"id": self.id, "score": round(self.score, 6),
                "metadata": self.metadata}

@dataclass
class NamespaceMeta:
    name: str; dim: Optional[int] = None
    auto_normalize: bool = True
    created_at: float = field(default_factory=time.time)

class VSStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS namespaces(
                    name TEXT PRIMARY KEY, dim INTEGER,
                    auto_normalize INTEGER, created_at REAL);
                CREATE TABLE IF NOT EXISTS vectors(
                    id TEXT, namespace TEXT, vector BLOB,
                    metadata TEXT, created_at REAL, updated_at REAL,
                    PRIMARY KEY(namespace, id));
                CREATE INDEX IF NOT EXISTS idx_vec_ns
                    ON vectors(namespace);
            """)

    def save_ns(self, ns: NamespaceMeta):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO namespaces VALUES(?,?,?,?)",
                (ns.name, ns.dim, int(ns.auto_normalize), ns.created_at))

    def load_ns(self, name: str) -> Optional[NamespaceMeta]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM namespaces WHERE name=?", (name,)).fetchone()
        if not row: return None
        return NamespaceMeta(name=row["name"], dim=row["dim"],
                              auto_normalize=bool(row["auto_normalize"]),
                              created_at=row["created_at"])

    def upsert(self, entry: VectorEntry):
        import struct
        blob = struct.pack(f"{len(entry.vector)}f", *entry.vector)
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO vectors VALUES(?,?,?,?,?,?)",
                (entry.id, entry.namespace, blob,
                 json.dumps(entry.metadata, default=str),
                 entry.created_at, entry.updated_at))

    def fetch(self, namespace: str, vec_id: str) -> Optional[VectorEntry]:
        import struct
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM vectors WHERE namespace=? AND id=?",
                (namespace, vec_id)).fetchone()
        if not row: return None
        blob = row["vector"]
        n = len(blob) // 4
        vec = list(struct.unpack(f"{n}f", blob))
        return VectorEntry(id=row["id"], namespace=row["namespace"],
                            vector=vec,
                            metadata=json.loads(row["metadata"]),
                            created_at=row["created_at"],
                            updated_at=row["updated_at"])

    def scan(self, namespace: str) -> List[VectorEntry]:
        import struct
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM vectors WHERE namespace=?",
                (namespace,)).fetchall()
        result = []
        for row in rows:
            n = len(row["vector"]) // 4
            vec = list(struct.unpack(f"{n}f", row["vector"]))
            result.append(VectorEntry(
                id=row["id"], namespace=row["namespace"], vector=vec,
                metadata=json.loads(row["metadata"]),
                created_at=row["created_at"], updated_at=row["updated_at"]))
        return result

    def delete(self, namespace: str, vec_id: str) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM vectors WHERE namespace=? AND id=?",
                (namespace, vec_id))
            return cur.rowcount > 0

    def list_ids(self, namespace: str, prefix: str = "") -> List[str]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id FROM vectors WHERE namespace=? AND id LIKE ?",
                (namespace, f"{prefix}%")).fetchall()
        return [r["id"] for r in rows]

    def count(self, namespace: str = None) -> int:
        with self._conn() as c:
            if namespace:
                return c.execute(
                    "SELECT COUNT(*) FROM vectors WHERE namespace=?",
                    (namespace,)).fetchone()[0]
            return c.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]

    def stats(self) -> Dict:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
            by_ns = {r["namespace"]: r["cnt"] for r in c.execute(
                "SELECT namespace, COUNT(*) as cnt FROM vectors "
                "GROUP BY namespace").fetchall()}
            n_ns = c.execute("SELECT COUNT(*) FROM namespaces").fetchone()[0]
        return {"total": total, "namespaces": n_ns, "by_namespace": by_ns}

class VectorStore:
    """
    In-process vector store with k-NN search and metadata filtering.

    Usage:
        vs = VectorStore()
        vs.create_namespace("embeddings", dim=4)

        vs.upsert("embeddings", "doc1", [0.1, 0.9, 0.3, 0.2],
                   metadata={"type": "article"})
        vs.upsert("embeddings", "doc2", [0.8, 0.1, 0.5, 0.3],
                   metadata={"type": "blog"})

        results = vs.search("embeddings", [0.1, 0.85, 0.3, 0.2], k=5)
        for r in results:
            print(r.id, r.score, r.metadata)
    """
    def __init__(self, db_path: str = "data/vectors.db"):
        self._store = VSStore(db_path)
        self._namespaces: Dict[str, NamespaceMeta] = {}
        # In-memory cache: namespace → {id: VectorEntry}
        self._cache: Dict[str, Dict[str, VectorEntry]] = {}

    def create_namespace(self, name: str, dim: int = None,
                          auto_normalize: bool = True) -> NamespaceMeta:
        ns = NamespaceMeta(name=name, dim=dim,
                            auto_normalize=auto_normalize)
        self._namespaces[name] = ns
        self._cache.setdefault(name, {})
        self._store.save_ns(ns)
        return ns

    def _get_ns(self, name: str) -> NamespaceMeta:
        if name not in self._namespaces:
            loaded = self._store.load_ns(name)
            if loaded:
                self._namespaces[name] = loaded
            else:
                return self.create_namespace(name)
        return self._namespaces[name]

    def _get_cache(self, namespace: str) -> Dict[str, VectorEntry]:
        if namespace not in self._cache:
            ns = self._get_ns(namespace)
            entries = self._store.scan(namespace)
            self._cache[namespace] = {e.id: e for e in entries}
        return self._cache[namespace]

    def upsert(self, namespace: str, vec_id: str, vector: Vec,
                metadata: Dict = None) -> VectorEntry:
        ns = self._get_ns(namespace)
        if ns.dim is None:
            ns.dim = len(vector)
            self._store.save_ns(ns)
        elif len(vector) != ns.dim:
            raise ValueError(
                f"Vector dim {len(vector)} != namespace dim {ns.dim}")
        v = (list(_normalize(vector)) if ns.auto_normalize
              else list(vector))
        cache = self._get_cache(namespace)
        existing = cache.get(vec_id)
        entry = VectorEntry(
            id=vec_id, namespace=namespace, vector=v,
            metadata=dict(metadata or {}),
            created_at=existing.created_at if existing else time.time(),
            updated_at=time.time())
        cache[vec_id] = entry
        self._store.upsert(entry)
        return entry

    def upsert_batch(self, namespace: str,
                      items: List[Tuple[str, Vec, Dict]]) -> int:
        for vec_id, vector, metadata in items:
            self.upsert(namespace, vec_id, vector, metadata)
        return len(items)

    def search(self, namespace: str, query: Vec, k: int = 10,
                metric: str = "cosine",
                filter_meta: Dict = None) -> List[SearchResult]:
        ns = self._get_ns(namespace)
        q = (_normalize(query) if ns.auto_normalize and metric == "cosine"
              else list(query))
        cache = self._get_cache(namespace)
        candidates = list(cache.values())
        # Metadata filtering
        if filter_meta:
            candidates = [e for e in candidates
                           if all(e.metadata.get(k) == v
                                   for k, v in filter_meta.items())]
        # Score
        def score(entry: VectorEntry) -> float:
            if metric == "cosine":    return _cosine(q, entry.vector)
            if metric == "dot":       return _dot(q, entry.vector)
            if metric == "euclidean": return -_euclidean(q, entry.vector)
            return _cosine(q, entry.vector)
        scored = [(score(e), e) for e in candidates]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [SearchResult(id=e.id, score=s, metadata=e.metadata)
                for s, e in scored[:k]]

    def fetch(self, namespace: str, vec_id: str) -> Optional[VectorEntry]:
        cache = self._get_cache(namespace)
        return cache.get(vec_id)

    def delete(self, namespace: str, vec_id: str) -> bool:
        cache = self._get_cache(namespace)
        cache.pop(vec_id, None)
        return self._store.delete(namespace, vec_id)

    def list_ids(self, namespace: str, prefix: str = "") -> List[str]:
        cache = self._get_cache(namespace)
        return [vid for vid in cache if vid.startswith(prefix)]

    def centroid(self, namespace: str,
                  ids: List[str] = None) -> Optional[Vec]:
        cache = self._get_cache(namespace)
        entries = ([cache[i] for i in ids if i in cache]
                    if ids else list(cache.values()))
        if not entries: return None
        dim = len(entries[0].vector)
        c = [0.0] * dim
        for e in entries:
            for i, v in enumerate(e.vector): c[i] += v
        n = len(entries)
        return [x / n for x in c]

    def stats(self) -> Dict:
        s = self._store.stats()
        s["in_memory_namespaces"] = len(self._cache)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def upsert_ep(req):
            d = await req.json()
            e = self.upsert(d["namespace"], d["id"], d["vector"],
                             d.get("metadata", {}))
            return web.json_response(e.to_dict(), status=201)
        async def search_ep(req):
            d = await req.json()
            results = self.search(d["namespace"], d["vector"],
                                   d.get("k", 10), d.get("metric","cosine"),
                                   d.get("filter"))
            return web.json_response({"results":[r.to_dict() for r in results]})
        async def fetch_ep(req):
            ns = req.match_info["ns"]; vid = req.match_info["id"]
            e = self.fetch(ns, vid)
            if not e: return web.json_response({}, status=404)
            return web.json_response(e.to_dict(include_vector=True))
        async def delete_ep(req):
            d = await req.json()
            ok = self.delete(d["namespace"], d["id"])
            return web.json_response({"deleted": ok})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/vectors"
        app.router.add_post(f"{p}/upsert",        upsert_ep)
        app.router.add_post(f"{p}/search",        search_ep)
        app.router.add_get( f"{p}/{{ns}}/{{id}}", fetch_ep)
        app.router.add_post(f"{p}/delete",        delete_ep)
        app.router.add_get( f"{p}/stats",         stats_ep)
        logger.info(f"Vector store API at {prefix}/vectors/")
