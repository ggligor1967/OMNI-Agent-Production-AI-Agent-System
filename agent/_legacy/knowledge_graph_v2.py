"""OMNI Agent — Knowledge Graph V2: typed entities, relations, traversal, and inference."""
from __future__ import annotations
import json, sqlite3, time, uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple


class NodeType(str, Enum):
    ENTITY   = "entity"
    CONCEPT  = "concept"
    EVENT    = "event"
    PROPERTY = "property"
    DOCUMENT = "document"


class EdgeType(str, Enum):
    IS_A          = "is_a"
    HAS_PROPERTY  = "has_property"
    RELATED_TO    = "related_to"
    PART_OF       = "part_of"
    CAUSES        = "causes"
    PRECEDES      = "precedes"
    SYNONYMOUS    = "synonymous"
    MENTIONS      = "mentions"
    AUTHORED_BY   = "authored_by"
    CUSTOM        = "custom"


@dataclass
class Node:
    node_id: str
    label: str
    node_type: NodeType = NodeType.ENTITY
    properties: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    namespace: str = "default"
    confidence: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "label": self.label,
            "type": self.node_type.value,
            "properties": self.properties,
            "namespace": self.namespace,
            "confidence": self.confidence,
        }


@dataclass
class Edge:
    edge_id: str
    source_id: str
    target_id: str
    edge_type: EdgeType
    label: str = ""
    weight: float = 1.0
    properties: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    bidirectional: bool = False
    confidence: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "source": self.source_id,
            "target": self.target_id,
            "type": self.edge_type.value,
            "label": self.label,
            "weight": self.weight,
            "bidirectional": self.bidirectional,
        }


@dataclass
class PathResult:
    nodes: List[Node]
    edges: List[Edge]
    length: int
    total_weight: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "length": self.length,
            "total_weight": round(self.total_weight, 4),
            "nodes": [n.label for n in self.nodes],
            "edges": [e.edge_type.value for e in self.edges],
        }


class KnowledgeGraphV2:
    """
    Typed knowledge graph with:
    - Typed nodes and edges
    - BFS/DFS traversal
    - Shortest path (Dijkstra-lite)
    - Neighbourhood queries
    - Pattern matching (triple patterns)
    - Inference rules
    - Namespace support
    - SQLite persistence
    """

    def __init__(self, db_path: str = ":memory:"):
        self._nodes: Dict[str, Node] = {}
        self._edges: Dict[str, Edge] = {}
        # Adjacency: source_id → [edge_id]
        self._out_edges: Dict[str, List[str]] = defaultdict(list)
        self._in_edges:  Dict[str, List[str]] = defaultdict(list)
        # Label index
        self._label_index: Dict[str, List[str]] = defaultdict(list)
        self._inference_rules: List[Callable] = []
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS kg_nodes (
                node_id TEXT PRIMARY KEY, label TEXT, node_type TEXT,
                properties TEXT, namespace TEXT, confidence REAL, created_at REAL
            );
            CREATE TABLE IF NOT EXISTS kg_edges (
                edge_id TEXT PRIMARY KEY, source_id TEXT, target_id TEXT,
                edge_type TEXT, label TEXT, weight REAL,
                bidirectional INTEGER, confidence REAL, created_at REAL
            );
        """)
        self._db.commit()

    # ── NODES ─────────────────────────────────────────────────────────

    def add_node(self, label: str,
                 node_type: NodeType = NodeType.ENTITY,
                 properties: Optional[Dict] = None,
                 namespace: str = "default",
                 confidence: float = 1.0,
                 node_id: Optional[str] = None) -> Node:
        nid  = node_id or str(uuid.uuid4())[:10]
        node = Node(node_id=nid, label=label, node_type=node_type,
                    properties=properties or {}, namespace=namespace,
                    confidence=confidence)
        self._nodes[nid] = node
        self._label_index[label.lower()].append(nid)
        self._db.execute(
            "INSERT OR REPLACE INTO kg_nodes VALUES (?,?,?,?,?,?,?)",
            (nid, label, node_type.value, json.dumps(properties or {}),
             namespace, confidence, node.created_at))
        self._db.commit()
        return node

    def get_node(self, node_id: str) -> Optional[Node]:
        return self._nodes.get(node_id)

    def find_by_label(self, label: str,
                       exact: bool = True) -> List[Node]:
        if exact:
            ids = self._label_index.get(label.lower(), [])
            return [self._nodes[i] for i in ids if i in self._nodes]
        q = label.lower()
        return [n for n in self._nodes.values() if q in n.label.lower()]

    def update_node(self, node_id: str, **kwargs) -> bool:
        node = self._nodes.get(node_id)
        if not node:
            return False
        for k, v in kwargs.items():
            if hasattr(node, k):
                setattr(node, k, v)
        node.updated_at = time.time()
        return True

    def delete_node(self, node_id: str) -> bool:
        node = self._nodes.pop(node_id, None)
        if not node:
            return False
        # Remove associated edges
        edge_ids = list(self._out_edges.pop(node_id, []))
        edge_ids += list(self._in_edges.pop(node_id, []))
        for eid in set(edge_ids):
            self._edges.pop(eid, None)
        self._label_index[node.label.lower()] = [
            i for i in self._label_index.get(node.label.lower(), [])
            if i != node_id]
        return True

    # ── EDGES ─────────────────────────────────────────────────────────

    def add_edge(self, source_id: str, target_id: str,
                 edge_type: EdgeType = EdgeType.RELATED_TO,
                 label: str = "",
                 weight: float = 1.0,
                 properties: Optional[Dict] = None,
                 bidirectional: bool = False,
                 confidence: float = 1.0,
                 edge_id: Optional[str] = None) -> Optional[Edge]:
        if source_id not in self._nodes or target_id not in self._nodes:
            return None
        eid  = edge_id or str(uuid.uuid4())[:10]
        edge = Edge(edge_id=eid, source_id=source_id, target_id=target_id,
                    edge_type=edge_type, label=label, weight=weight,
                    properties=properties or {}, bidirectional=bidirectional,
                    confidence=confidence)
        self._edges[eid] = edge
        self._out_edges[source_id].append(eid)
        self._in_edges[target_id].append(eid)
        if bidirectional:
            # Create a reverse edge id so neighbors() resolves correctly
            rev_eid = eid + "_rev"
            rev_edge = Edge(edge_id=rev_eid, source_id=target_id,
                            target_id=source_id, edge_type=edge_type,
                            label=label, weight=weight,
                            properties=properties or {},
                            bidirectional=False, confidence=confidence)
            self._edges[rev_eid] = rev_edge
            self._out_edges[target_id].append(rev_eid)
            self._in_edges[source_id].append(rev_eid)
        self._db.execute(
            "INSERT OR REPLACE INTO kg_edges VALUES (?,?,?,?,?,?,?,?,?)",
            (eid, source_id, target_id, edge_type.value, label, weight,
             int(bidirectional), confidence, edge.created_at))
        self._db.commit()
        return edge

    def get_edge(self, edge_id: str) -> Optional[Edge]:
        return self._edges.get(edge_id)

    def delete_edge(self, edge_id: str) -> bool:
        edge = self._edges.pop(edge_id, None)
        if not edge:
            return False
        self._out_edges[edge.source_id] = [
            e for e in self._out_edges[edge.source_id] if e != edge_id]
        self._in_edges[edge.target_id] = [
            e for e in self._in_edges[edge.target_id] if e != edge_id]
        return True

    # ── TRAVERSAL ─────────────────────────────────────────────────────

    def neighbors(self, node_id: str,
                  direction: str = "out",
                  edge_type: Optional[EdgeType] = None) -> List[Node]:
        if direction == "out":
            eids = self._out_edges.get(node_id, [])
            get_other = lambda e: e.target_id
        elif direction == "in":
            eids = self._in_edges.get(node_id, [])
            get_other = lambda e: e.source_id
        else:
            eids = list(self._out_edges.get(node_id, [])) + \
                   list(self._in_edges.get(node_id, []))
            get_other = lambda e: (e.target_id if e.source_id == node_id
                                   else e.source_id)
        result = []
        for eid in eids:
            edge = self._edges.get(eid)
            if edge and (edge_type is None or edge.edge_type == edge_type):
                other = self._nodes.get(get_other(edge))
                if other and other not in result:
                    result.append(other)
        return result

    def bfs(self, start_id: str, max_depth: int = 3,
             edge_type: Optional[EdgeType] = None) -> List[Node]:
        if start_id not in self._nodes:
            return []
        visited: Set[str] = {start_id}
        queue: deque = deque([(start_id, 0)])
        result: List[Node] = []
        while queue:
            nid, depth = queue.popleft()
            result.append(self._nodes[nid])
            if depth >= max_depth:
                continue
            for nbr in self.neighbors(nid, "both", edge_type):
                if nbr.node_id not in visited:
                    visited.add(nbr.node_id)
                    queue.append((nbr.node_id, depth + 1))
        return result

    def dfs(self, start_id: str, max_depth: int = 5,
             edge_type: Optional[EdgeType] = None) -> List[Node]:
        if start_id not in self._nodes:
            return []
        visited: Set[str] = set()
        result: List[Node] = []

        def _dfs(nid: str, depth: int):
            if depth > max_depth or nid in visited:
                return
            visited.add(nid)
            result.append(self._nodes[nid])
            for nbr in self.neighbors(nid, "out", edge_type):
                _dfs(nbr.node_id, depth + 1)

        _dfs(start_id, 0)
        return result

    def shortest_path(self, source_id: str,
                       target_id: str) -> Optional[PathResult]:
        """Dijkstra shortest path by edge weight."""
        import heapq
        if source_id not in self._nodes or target_id not in self._nodes:
            return None
        dist:   Dict[str, float] = {source_id: 0.0}
        prev:   Dict[str, Tuple[Optional[str], Optional[str]]] = {source_id: (None, None)}
        heap = [(0.0, source_id)]
        visited: Set[str] = set()

        while heap:
            d, nid = heapq.heappop(heap)
            if nid in visited:
                continue
            visited.add(nid)
            if nid == target_id:
                break
            for eid in self._out_edges.get(nid, []):
                edge = self._edges.get(eid)
                if not edge:
                    continue
                nd = d + edge.weight
                if nd < dist.get(edge.target_id, float("inf")):
                    dist[edge.target_id] = nd
                    prev[edge.target_id] = (nid, eid)
                    heapq.heappush(heap, (nd, edge.target_id))

        if target_id not in dist:
            return None
        # Reconstruct
        path_nodes, path_edges = [], []
        cur = target_id
        while cur is not None:
            path_nodes.append(self._nodes[cur])
            p_nid, p_eid = prev.get(cur, (None, None))
            if p_eid:
                path_edges.append(self._edges[p_eid])
            cur = p_nid
        path_nodes.reverse()
        path_edges.reverse()
        return PathResult(nodes=path_nodes, edges=path_edges,
                          length=len(path_edges),
                          total_weight=dist[target_id])

    # ── PATTERN MATCHING ──────────────────────────────────────────────

    def match_triples(self, subject: Optional[str] = None,
                       predicate: Optional[EdgeType] = None,
                       obj: Optional[str] = None) -> List[Tuple[Node, Edge, Node]]:
        """Match (subject_label, predicate, object_label) patterns."""
        results = []
        for edge in self._edges.values():
            src = self._nodes.get(edge.source_id)
            tgt = self._nodes.get(edge.target_id)
            if not src or not tgt:
                continue
            if subject and subject.lower() not in src.label.lower():
                continue
            if predicate and edge.edge_type != predicate:
                continue
            if obj and obj.lower() not in tgt.label.lower():
                continue
            results.append((src, edge, tgt))
        return results

    # ── INFERENCE ─────────────────────────────────────────────────────

    def add_inference_rule(self, fn: Callable[["KnowledgeGraphV2"], List[Tuple]]):
        """fn(graph) → [(src_id, edge_type, tgt_id, weight)]"""
        self._inference_rules.append(fn)

    def run_inference(self) -> int:
        """Apply all inference rules, return number of new edges created."""
        added = 0
        for rule in self._inference_rules:
            try:
                inferences = rule(self)
                for src_id, etype, tgt_id, weight in inferences:
                    if src_id in self._nodes and tgt_id in self._nodes:
                        # Only add if not already connected
                        existing = any(
                            self._edges[e].target_id == tgt_id and
                            self._edges[e].edge_type == etype
                            for e in self._out_edges.get(src_id, []))
                        if not existing:
                            self.add_edge(src_id, tgt_id, etype,
                                          weight=weight, confidence=0.7)
                            added += 1
            except Exception:
                pass
        return added

    # ── STATS ─────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        type_counts: Dict[str, int] = {}
        for n in self._nodes.values():
            type_counts[n.node_type.value] = type_counts.get(n.node_type.value, 0) + 1
        edge_counts: Dict[str, int] = {}
        for e in self._edges.values():
            edge_counts[e.edge_type.value] = edge_counts.get(e.edge_type.value, 0) + 1
        return {
            "nodes": len(self._nodes),
            "edges": len(self._edges),
            "node_types": type_counts,
            "edge_types": edge_counts,
            "namespaces": len({n.namespace for n in self._nodes.values()}),
        }
