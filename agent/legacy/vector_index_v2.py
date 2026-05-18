"""OMNI Agent — Vector Index V2: flat + HNSW-inspired approximate nearest neighbor search."""
from __future__ import annotations
import hashlib, json, math, random, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


class DistanceMetric(str, Enum):
    COSINE     = "cosine"
    EUCLIDEAN  = "euclidean"
    DOT        = "dot"
    MANHATTAN  = "manhattan"


def _norm(v: List[float]) -> float:
    return math.sqrt(sum(x * x for x in v))


def _cosine_dist(a: List[float], b: List[float]) -> float:
    n = _norm(a) * _norm(b)
    return 1.0 - (sum(x * y for x, y in zip(a, b)) / n) if n > 0 else 1.0


def _euclidean(a: List[float], b: List[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def _dot_dist(a: List[float], b: List[float]) -> float:
    return -sum(x * y for x, y in zip(a, b))  # negate so smaller = closer


def _manhattan(a: List[float], b: List[float]) -> float:
    return sum(abs(x - y) for x, y in zip(a, b))


def _dist(a: List[float], b: List[float], metric: DistanceMetric) -> float:
    if metric == DistanceMetric.COSINE:    return _cosine_dist(a, b)
    if metric == DistanceMetric.EUCLIDEAN: return _euclidean(a, b)
    if metric == DistanceMetric.DOT:       return _dot_dist(a, b)
    if metric == DistanceMetric.MANHATTAN: return _manhattan(a, b)
    return _cosine_dist(a, b)


@dataclass
class VectorRecord:
    record_id: str
    vector: List[float]
    payload: Dict[str, Any] = field(default_factory=dict)
    namespace: str = "default"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "record_id": self.record_id,
            "dim": len(self.vector),
            "namespace": self.namespace,
            "payload": self.payload,
            "created_at": self.created_at,
        }


@dataclass
class SearchHit:
    record: VectorRecord
    distance: float
    rank: int

    @property
    def similarity(self) -> float:
        return 1.0 - min(1.0, max(0.0, self.distance))

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.record.to_dict(),
            "distance": round(self.distance, 6),
            "similarity": round(self.similarity, 6),
            "rank": self.rank,
        }


class VectorIndexV2:
    """
    In-memory vector index supporting:
    - Flat (exact) search
    - Approximate HNSW-inspired search (multi-layer graph)
    - Namespace isolation
    - Payload filtering
    - CRUD operations
    - SQLite persistence
    - Distance metrics: cosine, euclidean, dot, manhattan
    """

    def __init__(
        self,
        dim: int = 128,
        metric: DistanceMetric = DistanceMetric.COSINE,
        hnsw_m: int = 8,            # HNSW connections per node
        hnsw_ef: int = 32,          # search beam width
        db_path: str = ":memory:",
    ):
        self.dim    = dim
        self.metric = metric
        self.hnsw_m = hnsw_m
        self.hnsw_ef = hnsw_ef
        self._records: Dict[str, VectorRecord] = {}
        self._ns_index: Dict[str, Set[str]] = {}      # namespace → record_ids
        # HNSW layers: layer_idx → {record_id: [neighbor_ids]}
        self._layers: List[Dict[str, List[str]]] = [{}, {}]
        self._entry_point: Optional[str] = None
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._search_count = 0
        self._insert_count = 0

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS vi_records (
                record_id TEXT PRIMARY KEY, namespace TEXT,
                vector TEXT, payload TEXT, created_at REAL
            );
        """)
        self._db.commit()

    # ── INSERT / UPDATE / DELETE ──────────────────────────────────────

    def insert(self, vector: List[float],
               payload: Optional[Dict] = None,
               namespace: str = "default",
               record_id: Optional[str] = None) -> VectorRecord:
        if len(vector) != self.dim:
            raise ValueError(f"Expected dim={self.dim}, got {len(vector)}")
        rid = record_id or str(uuid.uuid4())
        rec = VectorRecord(record_id=rid, vector=list(vector),
                           payload=payload or {}, namespace=namespace)
        self._records[rid] = rec
        self._ns_index.setdefault(namespace, set()).add(rid)
        self._hnsw_insert(rec)
        self._db.execute(
            "INSERT OR REPLACE INTO vi_records VALUES (?,?,?,?,?)",
            (rid, namespace, json.dumps(vector), json.dumps(payload or {}),
             rec.created_at))
        self._db.commit()
        self._insert_count += 1
        return rec

    def upsert(self, record_id: str, vector: List[float],
               payload: Optional[Dict] = None,
               namespace: str = "default") -> VectorRecord:
        if record_id in self._records:
            self.delete(record_id)
        return self.insert(vector, payload, namespace, record_id)

    def delete(self, record_id: str) -> bool:
        rec = self._records.pop(record_id, None)
        if rec:
            self._ns_index.get(rec.namespace, set()).discard(record_id)
            for layer in self._layers:
                layer.pop(record_id, None)
                for neighbors in layer.values():
                    if record_id in neighbors:
                        neighbors.remove(record_id)
            if self._entry_point == record_id:
                self._entry_point = next(iter(self._records), None)
            self._db.execute("DELETE FROM vi_records WHERE record_id=?",
                             (record_id,))
            self._db.commit()
            return True
        return False

    def get(self, record_id: str) -> Optional[VectorRecord]:
        return self._records.get(record_id)

    def update_payload(self, record_id: str, payload: Dict[str, Any]) -> bool:
        rec = self._records.get(record_id)
        if rec:
            rec.payload.update(payload)
            rec.updated_at = time.time()
            return True
        return False

    # ── HNSW INTERNALS ────────────────────────────────────────────────

    def _hnsw_insert(self, rec: VectorRecord):
        """Simplified HNSW: maintain 2 layers of M nearest neighbors."""
        if not self._entry_point:
            self._entry_point = rec.record_id
            for layer in self._layers:
                layer[rec.record_id] = []
            return

        for layer in self._layers:
            layer[rec.record_id] = []
            # Find M nearest existing nodes
            candidates = [(r, _dist(rec.vector, r.vector, self.metric))
                          for r in self._records.values()
                          if r.record_id != rec.record_id
                          and r.record_id in layer]
            candidates.sort(key=lambda x: x[1])
            neighbors = [c.record_id for c, _ in candidates[:self.hnsw_m]]
            layer[rec.record_id] = neighbors
            # Bidirectional links
            for nid in neighbors:
                if nid in layer and len(layer[nid]) < self.hnsw_m * 2:
                    layer[nid].append(rec.record_id)

    # ── SEARCH ────────────────────────────────────────────────────────

    def search(self, query: List[float], top_k: int = 10,
               namespace: Optional[str] = None,
               filter_fn: Optional[Callable[[VectorRecord], bool]] = None,
               approximate: bool = False) -> List[SearchHit]:
        self._search_count += 1
        if approximate and len(self._records) > 100:
            candidates = self._hnsw_search(query, top_k * 3)
        else:
            candidates = list(self._records.values())

        if namespace:
            ns_ids = self._ns_index.get(namespace, set())
            candidates = [r for r in candidates if r.record_id in ns_ids]
        if filter_fn:
            candidates = [r for r in candidates if filter_fn(r)]

        scored = [(r, _dist(query, r.vector, self.metric))
                  for r in candidates
                  if len(r.vector) == len(query)]
        scored.sort(key=lambda x: x[1])
        return [SearchHit(record=r, distance=d, rank=i + 1)
                for i, (r, d) in enumerate(scored[:top_k])]

    def _hnsw_search(self, query: List[float], k: int) -> List[VectorRecord]:
        """Greedy search on HNSW graph."""
        if not self._entry_point:
            return []
        visited: Set[str] = set()
        entry = self._records.get(self._entry_point)
        if not entry:
            return list(self._records.values())[:k]
        best = [(entry, _dist(query, entry.vector, self.metric))]
        frontier = [entry]
        visited.add(entry.record_id)

        for layer in reversed(self._layers):
            improved = True
            while improved:
                improved = False
                for node in list(frontier):
                    for nid in layer.get(node.record_id, []):
                        if nid in visited:
                            continue
                        visited.add(nid)
                        nbr = self._records.get(nid)
                        if nbr:
                            d = _dist(query, nbr.vector, self.metric)
                            best.append((nbr, d))
                            if d < best[0][1]:
                                frontier = [nbr]
                                improved = True

        best.sort(key=lambda x: x[1])
        return [r for r, _ in best[:k]]

    def search_by_id(self, record_id: str, top_k: int = 10,
                     **kwargs) -> List[SearchHit]:
        rec = self._records.get(record_id)
        if not rec:
            return []
        hits = self.search(rec.vector, top_k + 1, **kwargs)
        return [h for h in hits if h.record.record_id != record_id][:top_k]

    # ── NAMESPACE OPS ─────────────────────────────────────────────────

    def list_namespaces(self) -> List[str]:
        return list(self._ns_index.keys())

    def namespace_size(self, namespace: str) -> int:
        return len(self._ns_index.get(namespace, set()))

    def delete_namespace(self, namespace: str) -> int:
        ids = list(self._ns_index.get(namespace, set()))
        for rid in ids:
            self.delete(rid)
        return len(ids)

    # ── STATS ─────────────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self._records)

    def stats(self) -> Dict[str, Any]:
        return {
            "records": len(self._records),
            "namespaces": len(self._ns_index),
            "dim": self.dim,
            "metric": self.metric.value,
            "inserts": self._insert_count,
            "searches": self._search_count,
            "hnsw_m": self.hnsw_m,
        }
