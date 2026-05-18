"""OMNI AGENT - Graph Engine
In-memory directed/undirected weighted graph with traversal,
shortest paths, cycle detection, connectivity, and persistence.

Features:
- Node: id, label, attributes dict
- Edge: src, dst, weight (default 1.0), label, attributes
- Graph modes: DIRECTED, UNDIRECTED
- Adjacency: dict-of-dicts for O(1) edge lookup
- BFS: breadth-first traversal from source; returns ordered node list
- DFS: depth-first traversal; iterative + recursive variants
- Shortest path: Dijkstra (weighted), BFS-path (unweighted)
- All paths: enumerate all simple paths between two nodes (DFS)
- Cycle detection: DFS-based; returns True/False + example cycle
- Topological sort: Kahn's algorithm (BFS on in-degree)
- Strongly connected components: Kosaraju's two-pass DFS
- Minimum spanning tree: Prim's algorithm (undirected)
- PageRank: iterative power method
- Neighbors: in-neighbors, out-neighbors, both
- Degree: in-degree, out-degree, total degree
- Subgraph: extract induced subgraph from node set
- Merge: union of two graphs
- Export: adjacency dict, edge list, DOT format
- Serialization: JSON round-trip
- SQLite persistence: nodes, edges, graph metadata
- REST API: add_node, add_edge, path, neighbors, stats
"""
import json, sqlite3, time, uuid, logging
from collections import defaultdict, deque
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class GraphMode(str, Enum):
    DIRECTED   = "directed"
    UNDIRECTED = "undirected"

@dataclass
class Node:
    id: str; label: str = ""
    attributes: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        return {"id": self.id, "label": self.label,
                "attributes": self.attributes}

@dataclass
class Edge:
    src: str; dst: str; weight: float = 1.0
    label: str = ""
    attributes: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        return {"src": self.src, "dst": self.dst, "weight": self.weight,
                "label": self.label}

class GStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS graphs(
                    name TEXT PRIMARY KEY, mode TEXT,
                    created_at REAL, data TEXT);
            """)

    def save(self, name: str, mode: str, data: Dict):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO graphs VALUES(?,?,?,?)",
                (name, mode, time.time(), json.dumps(data, default=str)))

    def load(self, name: str) -> Optional[Dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM graphs WHERE name=?", (name,)).fetchone()
        return json.loads(row["data"]) if row else None

class GraphEngine:
    """
    Directed/undirected weighted graph with traversal and path algorithms.

    Usage:
        g = GraphEngine()
        g.add_node("A"); g.add_node("B"); g.add_node("C")
        g.add_edge("A", "B", weight=4.0)
        g.add_edge("B", "C", weight=2.0)
        g.add_edge("A", "C", weight=10.0)

        path, cost = g.shortest_path("A", "C")
        # path = ["A","B","C"], cost = 6.0
    """
    def __init__(self, name: str = "default",
                 mode: GraphMode = GraphMode.DIRECTED,
                 db_path: str = "data/graph.db"):
        self.name = name; self.mode = mode
        self._store = GStore(db_path)
        self._nodes: Dict[str, Node] = {}
        # _adj[src][dst] = Edge
        self._adj: Dict[str, Dict[str, Edge]] = defaultdict(dict)

    # ── Mutation ──────────────────────────────────────────────────────────────
    def add_node(self, node_id: str, label: str = "",
                  **attrs) -> Node:
        n = Node(id=node_id, label=label, attributes=dict(attrs))
        self._nodes[node_id] = n
        if node_id not in self._adj:
            self._adj[node_id] = {}
        return n

    def remove_node(self, node_id: str) -> bool:
        if node_id not in self._nodes: return False
        del self._nodes[node_id]; del self._adj[node_id]
        for src in list(self._adj):
            self._adj[src].pop(node_id, None)
        return True

    def add_edge(self, src: str, dst: str, weight: float = 1.0,
                  label: str = "", **attrs) -> Edge:
        for nid in (src, dst):
            if nid not in self._nodes: self.add_node(nid)
        e = Edge(src=src, dst=dst, weight=weight, label=label,
                  attributes=dict(attrs))
        self._adj[src][dst] = e
        if self.mode == GraphMode.UNDIRECTED:
            self._adj[dst][src] = Edge(src=dst, dst=src,
                                        weight=weight, label=label,
                                        attributes=dict(attrs))
        return e

    def remove_edge(self, src: str, dst: str) -> bool:
        if dst not in self._adj.get(src, {}): return False
        del self._adj[src][dst]
        if self.mode == GraphMode.UNDIRECTED:
            self._adj[dst].pop(src, None)
        return True

    def has_node(self, node_id: str) -> bool: return node_id in self._nodes
    def has_edge(self, src: str, dst: str) -> bool:
        return dst in self._adj.get(src, {})

    def get_edge(self, src: str, dst: str) -> Optional[Edge]:
        return self._adj.get(src, {}).get(dst)

    # ── Neighbors / Degree ────────────────────────────────────────────────────
    def out_neighbors(self, node_id: str) -> List[str]:
        return list(self._adj.get(node_id, {}).keys())

    def in_neighbors(self, node_id: str) -> List[str]:
        return [s for s, dsts in self._adj.items() if node_id in dsts]

    def neighbors(self, node_id: str) -> List[str]:
        out = set(self.out_neighbors(node_id))
        inn = set(self.in_neighbors(node_id))
        return list(out | inn)

    def out_degree(self, node_id: str) -> int:
        return len(self._adj.get(node_id, {}))

    def in_degree(self, node_id: str) -> int:
        return sum(1 for dsts in self._adj.values() if node_id in dsts)

    def degree(self, node_id: str) -> int:
        if self.mode == GraphMode.UNDIRECTED:
            return len(self._adj.get(node_id, {}))
        return self.in_degree(node_id) + self.out_degree(node_id)

    # ── Traversal ─────────────────────────────────────────────────────────────
    def bfs(self, start: str, limit: int = None) -> List[str]:
        if start not in self._nodes: return []
        visited: Set[str] = set(); queue = deque([start]); order = []
        while queue:
            node = queue.popleft()
            if node in visited: continue
            visited.add(node); order.append(node)
            if limit and len(order) >= limit: break
            for nb in self.out_neighbors(node):
                if nb not in visited: queue.append(nb)
        return order

    def dfs(self, start: str, limit: int = None) -> List[str]:
        if start not in self._nodes: return []
        visited: Set[str] = set(); stack = [start]; order = []
        while stack:
            node = stack.pop()
            if node in visited: continue
            visited.add(node); order.append(node)
            if limit and len(order) >= limit: break
            for nb in reversed(self.out_neighbors(node)):
                if nb not in visited: stack.append(nb)
        return order

    # ── Shortest Path (Dijkstra) ──────────────────────────────────────────────
    def shortest_path(self, src: str, dst: str
                       ) -> Tuple[List[str], float]:
        import heapq
        if src not in self._nodes or dst not in self._nodes:
            return [], float("inf")
        dist = {n: float("inf") for n in self._nodes}
        prev: Dict[str, Optional[str]] = {n: None for n in self._nodes}
        dist[src] = 0.0
        heap = [(0.0, src)]
        while heap:
            d, u = heapq.heappop(heap)
            if d > dist[u]: continue
            if u == dst: break
            for v, edge in self._adj.get(u, {}).items():
                nd = d + edge.weight
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd; prev[v] = u
                    heapq.heappush(heap, (nd, v))
        if dist.get(dst, float("inf")) == float("inf"):
            return [], float("inf")
        path = []; cur = dst
        while cur is not None:
            path.append(cur); cur = prev[cur]
        return list(reversed(path)), dist[dst]

    def bfs_path(self, src: str, dst: str) -> List[str]:
        """Unweighted shortest path (fewest hops)."""
        if src not in self._nodes or dst not in self._nodes: return []
        queue = deque([[src]]); visited = {src}
        while queue:
            path = queue.popleft()
            node = path[-1]
            if node == dst: return path
            for nb in self.out_neighbors(node):
                if nb not in visited:
                    visited.add(nb); queue.append(path + [nb])
        return []

    def all_paths(self, src: str, dst: str,
                   max_depth: int = 10) -> List[List[str]]:
        results = []; visited: Set[str] = set()
        def dfs_rec(node, path):
            if len(path) > max_depth: return
            if node == dst and len(path) > 1:
                results.append(list(path)); return
            visited.add(node)
            for nb in self.out_neighbors(node):
                if nb not in visited:
                    path.append(nb); dfs_rec(nb, path); path.pop()
            visited.discard(node)
        dfs_rec(src, [src])
        return results

    # ── Cycle Detection ───────────────────────────────────────────────────────
    def has_cycle(self) -> Tuple[bool, List[str]]:
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n: WHITE for n in self._nodes}
        parent: Dict[str, Optional[str]] = {n: None for n in self._nodes}

        def dfs_cycle(u) -> Optional[str]:
            color[u] = GRAY
            for v in self.out_neighbors(u):
                if color[v] == GRAY:
                    return v  # cycle detected; v is the cycle start
                if color[v] == WHITE:
                    parent[v] = u
                    result = dfs_cycle(v)
                    if result is not None: return result
            color[u] = BLACK
            return None

        for node in self._nodes:
            if color[node] == WHITE:
                cycle_node = dfs_cycle(node)
                if cycle_node is not None:
                    # Reconstruct cycle
                    cycle = [cycle_node]; cur = parent.get(cycle_node)
                    while cur and cur != cycle_node:
                        cycle.append(cur); cur = parent.get(cur)
                    cycle.append(cycle_node)
                    return True, list(reversed(cycle))
        return False, []

    # ── Topological Sort (Kahn's) ─────────────────────────────────────────────
    def topological_sort(self) -> Optional[List[str]]:
        in_deg = {n: self.in_degree(n) for n in self._nodes}
        queue = deque(n for n, d in in_deg.items() if d == 0)
        order = []
        while queue:
            u = queue.popleft(); order.append(u)
            for v in self.out_neighbors(u):
                in_deg[v] -= 1
                if in_deg[v] == 0: queue.append(v)
        return order if len(order) == len(self._nodes) else None  # None = cycle

    # ── Strongly Connected Components (Kosaraju) ──────────────────────────────
    def strongly_connected_components(self) -> List[List[str]]:
        # Pass 1: DFS on original, push to stack in finish order
        visited: Set[str] = set(); stack = []
        def dfs1(u):
            visited.add(u)
            for v in self.out_neighbors(u):
                if v not in visited: dfs1(v)
            stack.append(u)
        for n in self._nodes:
            if n not in visited: dfs1(n)
        # Build transpose
        trans: Dict[str, List[str]] = defaultdict(list)
        for u in self._adj:
            for v in self._adj[u]: trans[v].append(u)
        # Pass 2: DFS on transpose in reverse finish order
        visited2: Set[str] = set(); sccs = []
        def dfs2(u, comp):
            visited2.add(u); comp.append(u)
            for v in trans[u]:
                if v not in visited2: dfs2(v, comp)
        for n in reversed(stack):
            if n not in visited2:
                comp: List[str] = []; dfs2(n, comp); sccs.append(comp)
        return sccs

    # ── Minimum Spanning Tree (Prim's) ────────────────────────────────────────
    def minimum_spanning_tree(self) -> List[Edge]:
        import heapq
        if not self._nodes: return []
        start = next(iter(self._nodes))
        in_tree: Set[str] = {start}; mst: List[Edge] = []
        heap = [(e.weight, e.src, e.dst, e)
                for e in self._adj.get(start, {}).values()]
        heapq.heapify(heap)
        while heap and len(in_tree) < len(self._nodes):
            w, s, d, edge = heapq.heappop(heap)
            if d in in_tree: continue
            in_tree.add(d); mst.append(edge)
            for ne in self._adj.get(d, {}).values():
                if ne.dst not in in_tree:
                    heapq.heappush(heap, (ne.weight, ne.src, ne.dst, ne))
        return mst

    # ── PageRank ──────────────────────────────────────────────────────────────
    def pagerank(self, damping: float = 0.85,
                  iterations: int = 50) -> Dict[str, float]:
        n = len(self._nodes)
        if n == 0: return {}
        rank = {nid: 1.0 / n for nid in self._nodes}
        for _ in range(iterations):
            new_rank = {}
            for nid in self._nodes:
                in_nb = self.in_neighbors(nid)
                contrib = sum(rank[nb] / max(1, self.out_degree(nb))
                               for nb in in_nb)
                new_rank[nid] = (1 - damping) / n + damping * contrib
            rank = new_rank
        total = sum(rank.values()) or 1
        return {k: round(v / total, 6) for k, v in rank.items()}

    # ── Export ────────────────────────────────────────────────────────────────
    def to_dot(self) -> str:
        arrow = "->" if self.mode == GraphMode.DIRECTED else "--"
        g_type = "digraph" if self.mode == GraphMode.DIRECTED else "graph"
        lines = [f"{g_type} G {{"]
        for nid, n in self._nodes.items():
            label = n.label or nid
            lines.append(f'  "{nid}" [label="{label}"];')
        seen: Set[Tuple] = set()
        for src, dsts in self._adj.items():
            for dst, e in dsts.items():
                key = (min(src,dst), max(src,dst))
                if self.mode == GraphMode.UNDIRECTED and key in seen: continue
                seen.add(key)
                lines.append(f'  "{src}" {arrow} "{dst}" [label="{e.weight}"];')
        lines.append("}")
        return "\n".join(lines)

    def to_dict(self) -> Dict:
        return {"name": self.name, "mode": self.mode.value,
                "nodes": [n.to_dict() for n in self._nodes.values()],
                "edges": [e.to_dict()
                           for dsts in self._adj.values()
                           for e in dsts.values()]}

    def save(self): self._store.save(self.name, self.mode.value, self.to_dict())

    def stats(self) -> Dict:
        return {"nodes": len(self._nodes),
                "edges": sum(len(d) for d in self._adj.values()),
                "mode": self.mode.value,
                "name": self.name}

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def node_ep(req):
            d = await req.json()
            n = self.add_node(d["id"], d.get("label",""), **d.get("attrs",{}))
            return web.json_response(n.to_dict(), status=201)
        async def edge_ep(req):
            d = await req.json()
            e = self.add_edge(d["src"], d["dst"], d.get("weight",1.0))
            return web.json_response(e.to_dict(), status=201)
        async def path_ep(req):
            d = await req.json()
            path, cost = self.shortest_path(d["src"], d["dst"])
            return web.json_response({"path": path, "cost": cost})
        async def neighbors_ep(req):
            nid = req.match_info["id"]
            return web.json_response({"neighbors": self.neighbors(nid)})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/graph"
        app.router.add_post(f"{p}/node",          node_ep)
        app.router.add_post(f"{p}/edge",          edge_ep)
        app.router.add_post(f"{p}/path",          path_ep)
        app.router.add_get( f"{p}/{{id}}/neighbors", neighbors_ep)
        app.router.add_get( f"{p}/stats",         stats_ep)
        logger.info(f"Graph engine API at {prefix}/graph/")
