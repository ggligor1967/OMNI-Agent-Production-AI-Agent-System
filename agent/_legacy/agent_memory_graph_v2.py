"""OMNI Agent — Agent Memory Graph V2: typed edges, temporal decay, similarity."""
from __future__ import annotations
import json, math, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


class NodeType(str, Enum):
    FACT      = "fact"
    CONCEPT   = "concept"
    ENTITY    = "entity"
    EVENT     = "event"
    SKILL     = "skill"
    GOAL      = "goal"
    EPISODE   = "episode"
    CUSTOM    = "custom"


class EdgeType(str, Enum):
    IS_A        = "is_a"
    HAS         = "has"
    RELATED_TO  = "related_to"
    CAUSES      = "causes"
    PRECEDES    = "precedes"
    PART_OF     = "part_of"
    CONTRADICTS = "contradicts"
    SUPPORTS    = "supports"
    DERIVED_FROM = "derived_from"
    CUSTOM      = "custom"


@dataclass
class MemoryNode:
    node_id: str
    label: str
    node_type: NodeType = NodeType.FACT
    content: str = ""
    embedding: Optional[List[float]] = None
    importance: float = 1.0
    confidence: float = 1.0
    access_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    decay_rate: float = 0.01    # per second; 0 = no decay
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def current_importance(self) -> float:
        """Time-decayed importance."""
        if self.decay_rate == 0:
            return self.importance
        elapsed = time.time() - self.last_accessed
        decayed = self.importance * math.exp(-self.decay_rate * elapsed)
        return max(0.0, decayed)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id, "label": self.label,
            "type": self.node_type.value, "content": self.content,
            "importance": round(self.current_importance, 4),
            "confidence": self.confidence,
            "access_count": self.access_count,
        }


@dataclass
class MemoryEdge:
    edge_id: str
    src_id: str
    dst_id: str
    edge_type: EdgeType = EdgeType.RELATED_TO
    weight: float = 1.0
    confidence: float = 1.0
    label: str = ""
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "edge_id": self.edge_id, "src": self.src_id, "dst": self.dst_id,
            "type": self.edge_type.value, "weight": self.weight,
        }


def _cosine(a: List[float], b: List[float]) -> float:
    if len(a) != len(b): return 0.0
    dot   = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if not norm_a or not norm_b: return 0.0
    return dot / (norm_a * norm_b)


class AgentMemoryGraphV2:
    """
    Agent memory graph:
    - Nodes with types (fact/concept/entity/event/goal/episode)
    - Typed, weighted directed edges
    - Temporal importance decay (exponential)
    - Semantic similarity search (cosine on embeddings)
    - Keyword search over label + content
    - BFS / DFS / weighted path traversal
    - Shortest path (Dijkstra by edge weight)
    - Neighborhood retrieval (k-hop)
    - Consolidation: merge low-importance similar nodes
    - Contradiction detection (CONTRADICTS edges)
    - Importance boosting on access
    - Pruning: remove nodes below importance threshold
    - Snapshot / restore
    - SQLite persistence
    """

    def __init__(self, embed_fn: Optional[Callable[[str], List[float]]] = None,
                 db_path: str = ":memory:",
                 importance_boost: float = 0.05):
        self._nodes: Dict[str, MemoryNode] = {}
        self._edges: Dict[str, MemoryEdge] = {}
        self._embed_fn = embed_fn
        self._boost    = importance_boost
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS mg_nodes (
                node_id TEXT PRIMARY KEY, label TEXT, node_type TEXT,
                content TEXT, importance REAL, confidence REAL,
                access_count INTEGER, created_at REAL
            );
            CREATE TABLE IF NOT EXISTS mg_edges (
                edge_id TEXT PRIMARY KEY, src_id TEXT, dst_id TEXT,
                edge_type TEXT, weight REAL, confidence REAL, label TEXT
            );
        """)
        self._db.commit()

    # ── NODES ─────────────────────────────────────────────────────────

    def add_node(self, label: str,
                  node_type: NodeType = NodeType.FACT,
                  content: str = "",
                  importance: float = 1.0,
                  confidence: float = 1.0,
                  decay_rate: float = 0.0,
                  tags: Optional[List[str]] = None,
                  node_id: Optional[str] = None,
                  metadata: Optional[Dict] = None) -> MemoryNode:
        nid = node_id or str(uuid.uuid4())[:8]
        emb = None
        if self._embed_fn and content:
            try: emb = self._embed_fn(content)
            except Exception: pass
        n = MemoryNode(
            node_id=nid, label=label, node_type=node_type,
            content=content, embedding=emb,
            importance=importance, confidence=confidence,
            decay_rate=decay_rate, tags=list(tags or []),
            metadata=metadata or {})
        self._nodes[nid] = n
        self._persist_node(n)
        return n

    def update_node(self, node_id: str, **kwargs) -> bool:
        n = self._nodes.get(node_id)
        if not n: return False
        for k, v in kwargs.items():
            if hasattr(n, k): setattr(n, k, v)
        self._persist_node(n)
        return True

    def remove_node(self, node_id: str) -> bool:
        if node_id not in self._nodes: return False
        del self._nodes[node_id]
        # remove incident edges
        to_del = [eid for eid, e in self._edges.items()
                  if e.src_id == node_id or e.dst_id == node_id]
        for eid in to_del:
            del self._edges[eid]
        self._db.execute("DELETE FROM mg_nodes WHERE node_id=?", (node_id,))
        self._db.execute(
            "DELETE FROM mg_edges WHERE src_id=? OR dst_id=?",
            (node_id, node_id))
        self._db.commit()
        return True

    def get_node(self, node_id: str) -> Optional[MemoryNode]:
        n = self._nodes.get(node_id)
        if n:
            n.access_count  += 1
            n.last_accessed  = time.time()
            n.importance     = min(1.0, n.importance + self._boost)
        return n

    # ── EDGES ─────────────────────────────────────────────────────────

    def add_edge(self, src_id: str, dst_id: str,
                  edge_type: EdgeType = EdgeType.RELATED_TO,
                  weight: float = 1.0,
                  confidence: float = 1.0,
                  label: str = "",
                  edge_id: Optional[str] = None,
                  metadata: Optional[Dict] = None) -> MemoryEdge:
        if src_id not in self._nodes or dst_id not in self._nodes:
            raise KeyError("Both nodes must exist")
        eid = edge_id or str(uuid.uuid4())[:8]
        e   = MemoryEdge(edge_id=eid, src_id=src_id, dst_id=dst_id,
                          edge_type=edge_type, weight=weight,
                          confidence=confidence, label=label,
                          metadata=metadata or {})
        self._edges[eid] = e
        self._persist_edge(e)
        return e

    def remove_edge(self, edge_id: str) -> bool:
        if edge_id not in self._edges: return False
        del self._edges[edge_id]
        self._db.execute("DELETE FROM mg_edges WHERE edge_id=?", (edge_id,))
        self._db.commit()
        return True

    def neighbors(self, node_id: str,
                   edge_type: Optional[EdgeType] = None,
                   direction: str = "out") -> List[MemoryNode]:
        result = []
        for e in self._edges.values():
            if edge_type and e.edge_type != edge_type: continue
            nid = None
            if direction in ("out", "both") and e.src_id == node_id:
                nid = e.dst_id
            elif direction in ("in", "both") and e.dst_id == node_id:
                nid = e.src_id
            if nid and nid in self._nodes:
                result.append(self._nodes[nid])
        return result

    # ── TRAVERSAL ────────────────────────────────────────────────────

    def bfs(self, start_id: str, max_depth: int = 3) -> List[str]:
        if start_id not in self._nodes: return []
        visited: Set[str] = set(); queue = [(start_id, 0)]
        order: List[str] = []
        while queue:
            nid, depth = queue.pop(0)
            if nid in visited or depth > max_depth: continue
            visited.add(nid); order.append(nid)
            for nb in self.neighbors(nid):
                if nb.node_id not in visited:
                    queue.append((nb.node_id, depth + 1))
        return order

    def dfs(self, start_id: str, max_depth: int = 5) -> List[str]:
        if start_id not in self._nodes: return []
        visited: Set[str] = set()
        order: List[str] = []
        def _dfs(nid: str, depth: int):
            if nid in visited or depth > max_depth: return
            visited.add(nid); order.append(nid)
            for nb in self.neighbors(nid):
                _dfs(nb.node_id, depth + 1)
        _dfs(start_id, 0)
        return order

    def shortest_path(self, src_id: str,
                       dst_id: str) -> Optional[List[str]]:
        """Dijkstra by edge weight (lower = closer)."""
        import heapq
        if src_id not in self._nodes or dst_id not in self._nodes:
            return None
        dist: Dict[str, float] = {src_id: 0.0}
        prev: Dict[str, Optional[str]] = {src_id: None}
        pq = [(0.0, src_id)]
        while pq:
            d, nid = heapq.heappop(pq)
            if nid == dst_id: break
            if d > dist.get(nid, math.inf): continue
            for e in self._edges.values():
                if e.src_id != nid: continue
                nd = d + e.weight
                if nd < dist.get(e.dst_id, math.inf):
                    dist[e.dst_id] = nd
                    prev[e.dst_id] = nid
                    heapq.heappush(pq, (nd, e.dst_id))
        if dst_id not in dist: return None
        path: List[str] = []
        cur: Optional[str] = dst_id
        while cur: path.append(cur); cur = prev.get(cur)
        return list(reversed(path))

    # ── SEARCH ───────────────────────────────────────────────────────

    def search(self, query: str, top_k: int = 10,
                node_type: Optional[NodeType] = None) -> List[MemoryNode]:
        q = query.lower()
        nodes = [n for n in self._nodes.values()
                 if (not node_type or n.node_type == node_type)
                 and (q in n.label.lower() or q in n.content.lower())]
        nodes.sort(key=lambda n: n.current_importance, reverse=True)
        return nodes[:top_k]

    def semantic_search(self, query: str,
                         top_k: int = 10) -> List[Tuple[MemoryNode, float]]:
        if not self._embed_fn: return []
        try: q_emb = self._embed_fn(query)
        except Exception: return []
        scored = []
        for n in self._nodes.values():
            if n.embedding:
                score = _cosine(q_emb, n.embedding)
                scored.append((n, score))
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]

    # ── MAINTENANCE ──────────────────────────────────────────────────

    def prune(self, min_importance: float = 0.05) -> int:
        to_del = [nid for nid, n in self._nodes.items()
                  if n.current_importance < min_importance]
        for nid in to_del:
            self.remove_node(nid)
        return len(to_del)

    def contradictions(self) -> List[Tuple[str, str]]:
        return [(e.src_id, e.dst_id) for e in self._edges.values()
                if e.edge_type == EdgeType.CONTRADICTS]

    def snapshot(self) -> Dict[str, Any]:
        return {
            "nodes": {nid: n.to_dict() for nid, n in self._nodes.items()},
            "edges": {eid: e.to_dict() for eid, e in self._edges.items()},
        }

    def _persist_node(self, n: MemoryNode):
        self._db.execute(
            "INSERT OR REPLACE INTO mg_nodes VALUES (?,?,?,?,?,?,?,?)",
            (n.node_id, n.label, n.node_type.value, n.content,
             n.importance, n.confidence, n.access_count, n.created_at))
        self._db.commit()

    def _persist_edge(self, e: MemoryEdge):
        self._db.execute(
            "INSERT OR REPLACE INTO mg_edges VALUES (?,?,?,?,?,?,?)",
            (e.edge_id, e.src_id, e.dst_id, e.edge_type.value,
             e.weight, e.confidence, e.label))
        self._db.commit()

    def stats(self) -> Dict[str, Any]:
        return {
            "nodes": len(self._nodes),
            "edges": len(self._edges),
            "contradictions": len(self.contradictions()),
        }
