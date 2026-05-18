"""OMNI Agent — Data Lineage: provenance tracking for data transformations."""
from __future__ import annotations
import sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set


class NodeType(str, Enum):
    SOURCE      = "source"
    TRANSFORM   = "transform"
    SINK        = "sink"
    MODEL       = "model"
    DATASET     = "dataset"
    FEATURE     = "feature"


@dataclass
class LineageNode:
    node_id: str
    name: str
    node_type: NodeType
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "node_type": self.node_type.value,
            "created_at": self.created_at,
            "metadata": self.metadata,
            "tags": self.tags,
        }


@dataclass
class LineageEdge:
    edge_id: str
    source_id: str
    target_id: str
    transform: str = ""
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "transform": self.transform,
            "created_at": self.created_at,
        }


class LineageGraph:
    """DAG-based data lineage tracker with persistence."""

    def __init__(self, db_path: str = ":memory:"):
        self._nodes: Dict[str, LineageNode] = {}
        self._edges: Dict[str, LineageEdge] = {}
        # adjacency: out-edges per node
        self._out: Dict[str, List[str]] = {}   # node_id → [edge_id]
        self._in: Dict[str, List[str]] = {}    # node_id → [edge_id]
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS ln_nodes (
                node_id TEXT PRIMARY KEY, name TEXT, node_type TEXT,
                created_at REAL, metadata TEXT, tags TEXT
            );
            CREATE TABLE IF NOT EXISTS ln_edges (
                edge_id TEXT PRIMARY KEY, source_id TEXT, target_id TEXT,
                transform TEXT, created_at REAL, metadata TEXT
            );
        """)
        self._db.commit()

    # ── NODES ─────────────────────────────────────────────────────────

    def add_node(self, name: str, node_type: NodeType = NodeType.DATASET,
                 metadata: Optional[Dict] = None,
                 tags: Optional[List[str]] = None,
                 node_id: Optional[str] = None) -> LineageNode:
        import json
        nid = node_id or str(uuid.uuid4())
        node = LineageNode(
            node_id=nid, name=name, node_type=node_type,
            metadata=metadata or {}, tags=tags or [])
        self._nodes[nid] = node
        self._out[nid] = []
        self._in[nid] = []
        self._db.execute(
            "INSERT OR REPLACE INTO ln_nodes VALUES (?,?,?,?,?,?)",
            (nid, name, node_type.value, node.created_at,
             json.dumps(node.metadata), json.dumps(node.tags)))
        self._db.commit()
        return node

    def get_node(self, node_id: str) -> Optional[LineageNode]:
        return self._nodes.get(node_id)

    def find_nodes(self, name: Optional[str] = None,
                   node_type: Optional[NodeType] = None,
                   tag: Optional[str] = None) -> List[LineageNode]:
        results = list(self._nodes.values())
        if name:
            results = [n for n in results if name.lower() in n.name.lower()]
        if node_type:
            results = [n for n in results if n.node_type == node_type]
        if tag:
            results = [n for n in results if tag in n.tags]
        return results

    def delete_node(self, node_id: str):
        # Remove edges involving this node
        edges_to_remove = [
            e.edge_id for e in self._edges.values()
            if e.source_id == node_id or e.target_id == node_id
        ]
        for eid in edges_to_remove:
            self._remove_edge(eid)
        self._nodes.pop(node_id, None)
        self._out.pop(node_id, None)
        self._in.pop(node_id, None)
        self._db.execute("DELETE FROM ln_nodes WHERE node_id=?", (node_id,))
        self._db.commit()

    # ── EDGES ─────────────────────────────────────────────────────────

    def add_edge(self, source_id: str, target_id: str,
                 transform: str = "",
                 metadata: Optional[Dict] = None) -> LineageEdge:
        import json
        if source_id not in self._nodes:
            raise KeyError(f"Source node not found: {source_id}")
        if target_id not in self._nodes:
            raise KeyError(f"Target node not found: {target_id}")
        eid = str(uuid.uuid4())
        edge = LineageEdge(
            edge_id=eid, source_id=source_id, target_id=target_id,
            transform=transform, metadata=metadata or {})
        self._edges[eid] = edge
        self._out[source_id].append(eid)
        self._in[target_id].append(eid)
        self._db.execute(
            "INSERT INTO ln_edges VALUES (?,?,?,?,?,?)",
            (eid, source_id, target_id, transform, edge.created_at,
             json.dumps(edge.metadata)))
        self._db.commit()
        return edge

    def _remove_edge(self, edge_id: str):
        edge = self._edges.pop(edge_id, None)
        if edge:
            self._out.get(edge.source_id, [])
            if edge_id in self._out.get(edge.source_id, []):
                self._out[edge.source_id].remove(edge_id)
            if edge_id in self._in.get(edge.target_id, []):
                self._in[edge.target_id].remove(edge_id)
        self._db.execute("DELETE FROM ln_edges WHERE edge_id=?", (edge_id,))
        self._db.commit()

    # ── TRAVERSAL ─────────────────────────────────────────────────────

    def upstream(self, node_id: str, depth: int = -1) -> List[str]:
        """Return all ancestor node_ids (BFS)."""
        return self._bfs(node_id, direction="up", depth=depth)

    def downstream(self, node_id: str, depth: int = -1) -> List[str]:
        """Return all descendant node_ids (BFS)."""
        return self._bfs(node_id, direction="down", depth=depth)

    def _bfs(self, start: str, direction: str, depth: int) -> List[str]:
        visited: List[str] = []
        queue = [(start, 0)]
        seen: Set[str] = {start}
        while queue:
            current, d = queue.pop(0)
            if direction == "up":
                edge_ids = self._in.get(current, [])
                neighbours = [self._edges[eid].source_id for eid in edge_ids]
            else:
                edge_ids = self._out.get(current, [])
                neighbours = [self._edges[eid].target_id for eid in edge_ids]
            for nb in neighbours:
                if nb not in seen:
                    seen.add(nb)
                    next_d = d + 1
                    if depth == -1 or next_d <= depth:
                        visited.append(nb)
                        queue.append((nb, next_d))
        return visited

    def path(self, source_id: str, target_id: str) -> Optional[List[str]]:
        """Shortest path from source to target (BFS)."""
        if source_id == target_id:
            return [source_id]
        prev: Dict[str, Optional[str]] = {source_id: None}
        queue = [source_id]
        while queue:
            node = queue.pop(0)
            for eid in self._out.get(node, []):
                nb = self._edges[eid].target_id
                if nb not in prev:
                    prev[nb] = node
                    if nb == target_id:
                        # reconstruct
                        p, cur = [], nb
                        while cur is not None:
                            p.append(cur)
                            cur = prev[cur]
                        return list(reversed(p))
                    queue.append(nb)
        return None

    def root_nodes(self) -> List[LineageNode]:
        """Nodes with no incoming edges (sources)."""
        return [n for nid, n in self._nodes.items()
                if not self._in.get(nid)]

    def leaf_nodes(self) -> List[LineageNode]:
        """Nodes with no outgoing edges (sinks)."""
        return [n for nid, n in self._nodes.items()
                if not self._out.get(nid)]

    def has_cycle(self) -> bool:
        """DFS-based cycle detection."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {nid: WHITE for nid in self._nodes}
        def dfs(u: str) -> bool:
            color[u] = GRAY
            for eid in self._out.get(u, []):
                v = self._edges[eid].target_id
                if color[v] == GRAY:
                    return True
                if color[v] == WHITE and dfs(v):
                    return True
            color[u] = BLACK
            return False
        return any(dfs(n) for n in self._nodes if color[n] == WHITE)

    # ── EXPORT ────────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodes": [n.to_dict() for n in self._nodes.values()],
            "edges": [e.to_dict() for e in self._edges.values()],
        }

    def stats(self) -> Dict[str, Any]:
        return {
            "nodes": len(self._nodes),
            "edges": len(self._edges),
            "roots": len(self.root_nodes()),
            "leaves": len(self.leaf_nodes()),
            "has_cycle": self.has_cycle(),
        }
