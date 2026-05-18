"""OMNI AGENT - Knowledge Graph
Entity/relation graph with typed nodes and edges, BFS/DFS traversal,
shortest path, subgraph extraction, and persistence.

Features:
- Node: id, type, label, properties dict, tags
- Edge: id, from_id, to_id, relation (typed string), weight, properties
- Directed + undirected modes
- BFS traversal from a node with depth limit
- DFS traversal with cycle detection
- Shortest path: Dijkstra (weighted) and BFS (unweighted)
- Subgraph: extract N-hop neighbourhood of a node
- Neighbour query: in-edges, out-edges, both
- Type filtering: query nodes by type, edges by relation
- Merge: combine two nodes into one (re-routing edges)
- Pattern matching: find nodes matching property predicates
- Centrality: degree centrality per node
- Connected components: union-find
- SQLite persistence: nodes, edges
- REST API: add_node, add_edge, query, path, subgraph, stats
"""
import json, sqlite3, time, uuid, logging
from collections import defaultdict, deque
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# ALIASES - GraphStore is the canonical name expected by core.py
# KGStore exists for backward compatibility
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Node:
    id: str; label: str; node_type: str = "entity"
    properties: Dict = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def to_dict(self):
        return {"id": self.id, "label": self.label,
                "type": self.node_type, "properties": self.properties,
                "tags": self.tags}


@dataclass
class Edge:
    id: str; from_id: str; to_id: str; relation: str
    weight: float = 1.0
    properties: Dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self):
        return {"id": self.id, "from": self.from_id, "to": self.to_id,
                "relation": self.relation, "weight": self.weight,
                "properties": self.properties}


class GraphStore:
    """Knowledge Graph Store - SQLite-based storage for nodes and edges."""
    def __init__(self, db_path):
        self.db_path = db_path
        self._mem_conn = None
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        else:
            self._mem_conn = sqlite3.connect(":memory:")
            self._mem_conn.row_factory = sqlite3.Row
        self._init()

    def _conn(self):
        if self._mem_conn is not None:
            return self._mem_conn
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS nodes(
                    id TEXT PRIMARY KEY, label TEXT, node_type TEXT DEFAULT 'entity',
                    properties TEXT DEFAULT '{}',
                    tags TEXT DEFAULT '[]', created_at REAL);
                CREATE TABLE IF NOT EXISTS edges(
                    id TEXT PRIMARY KEY, from_id TEXT, to_id TEXT,
                    relation TEXT, weight REAL DEFAULT 1.0,
                    properties TEXT DEFAULT '{}', created_at REAL);
                CREATE INDEX IF NOT EXISTS idx_edge_from ON edges(from_id);
                CREATE INDEX IF NOT EXISTS idx_edge_to   ON edges(to_id);
                CREATE INDEX IF NOT EXISTS idx_node_type ON nodes(node_type);
            """)

    def save_node(self, n: Node):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO nodes VALUES(?,?,?,?,?,?)",
                (n.id, n.label, n.node_type,
                 json.dumps(n.properties), json.dumps(n.tags), n.created_at))

    def save_edge(self, e: Edge):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO edges VALUES(?,?,?,?,?,?,?)",
                (e.id, e.from_id, e.to_id, e.relation,
                 e.weight, json.dumps(e.properties), e.created_at))

    def delete_node(self, nid: str):
        with self._conn() as c:
            c.execute("DELETE FROM nodes WHERE id=?", (nid,))
            c.execute("DELETE FROM edges WHERE from_id=? OR to_id=?", (nid, nid))

    def delete_edge(self, eid: str):
        with self._conn() as c:
            c.execute("DELETE FROM edges WHERE id=?", (eid,))

    def load_all(self) -> Tuple[List[Node], List[Edge]]:
        with self._conn() as c:
            nrows = c.execute("SELECT * FROM nodes").fetchall()
            erows = c.execute("SELECT * FROM edges").fetchall()
        nodes = [Node(id=r["id"], label=r["label"], node_type=r["node_type"],
                       properties=json.loads(r["properties"]),
                       tags=json.loads(r["tags"]),
                       created_at=r["created_at"]) for r in nrows]
        edges = [Edge(id=r["id"], from_id=r["from_id"], to_id=r["to_id"],
                       relation=r["relation"], weight=r["weight"],
                       properties=json.loads(r["properties"]),
                       created_at=r["created_at"]) for r in erows]
        return nodes, edges

    def stats(self) -> Dict:
        with self._conn() as c:
            nn = c.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            ne = c.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        return {"nodes": nn, "edges": ne}


# Backward compatibility alias
KGStore = GraphStore


class KnowledgeGraph:
    """
    Directed property graph with traversal, path-finding, and subgraph ops.

    Usage:
        kg = KnowledgeGraph()
        kg.add_node("alice", "Alice", node_type="person")
        kg.add_node("bob",   "Bob",   node_type="person")
        kg.add_node("acme",  "Acme Corp", node_type="company")
        kg.add_edge("alice", "bob",  "knows",    weight=0.9)
        kg.add_edge("alice", "acme", "works_at", weight=1.0)

        path = kg.shortest_path("bob", "acme")
        sub  = kg.subgraph("alice", hops=2)
    """
    def __init__(self, db_path: str = "data/kg.db",
                 directed: bool = True):
        self._store = GraphStore(db_path)
        self._nodes: Dict[str, Node] = {}
        self._edges: Dict[str, Edge] = {}
        self._out: Dict[str, List[Edge]] = defaultdict(list)  # from_id -> edges
        self._in:  Dict[str, List[Edge]] = defaultdict(list)  # to_id   -> edges
        self.directed = directed
        # Load from DB
        nodes, edges = self._store.load_all()
        for n in nodes: self._mem_add_node(n)
        for e in edges: self._mem_add_edge(e)

    def _mem_add_node(self, n: Node):
        self._nodes[n.id] = n

    def _mem_add_edge(self, e: Edge):
        self._edges[e.id] = e
        self._out[e.from_id].append(e)
        self._in[e.to_id].append(e)
        if not self.directed:
            self._out[e.to_id].append(e)
            self._in[e.from_id].append(e)

    # ── CRUD ──────────────────────────────────────────────────────────────────
    def add_node(self, node_id: str, label: str,
                  node_type: str = "entity",
                  properties: Dict = None,
                  tags: List[str] = None) -> Node:
        n = Node(id=node_id, label=label, node_type=node_type,
                  properties=dict(properties or {}),
                  tags=list(tags or []))
        self._mem_add_node(n)
        self._store.save_node(n)
        return n

    def add_edge(self, from_id: str, to_id: str, relation: str,
                  weight: float = 1.0, properties: Dict = None,
                  edge_id: str = None) -> Optional[Edge]:
        if from_id not in self._nodes or to_id not in self._nodes:
            return None
        eid = edge_id or str(uuid.uuid4())[:10]
        e = Edge(id=eid, from_id=from_id, to_id=to_id, relation=relation,
                  weight=weight, properties=dict(properties or {}))
        self._mem_add_edge(e)
        self._store.save_edge(e)
        return e

    def get_node(self, node_id: str) -> Optional[Node]:
        return self._nodes.get(node_id)

    def get_edge(self, edge_id: str) -> Optional[Edge]:
        return self._edges.get(edge_id)

    def update_node(self, node_id: str, **props) -> bool:
        n = self._nodes.get(node_id)
        if not n: return False
        n.properties.update(props)
        self._store.save_node(n)
        return True

    def delete_node(self, node_id: str) -> bool:
        if node_id not in self._nodes: return False
        # Remove connected edges from memory
        for e in list(self._out.get(node_id, [])):
            self._edges.pop(e.id, None)
            self._in[e.to_id] = [x for x in self._in[e.to_id] if x.id != e.id]
        for e in list(self._in.get(node_id, [])):
            self._edges.pop(e.id, None)
            self._out[e.from_id] = [x for x in self._out[e.from_id] if x.id != e.id]
        self._out.pop(node_id, None)
        self._in.pop(node_id, None)
        del self._nodes[node_id]
        self._store.delete_node(node_id)
        return True

    def delete_edge(self, edge_id: str) -> bool:
        e = self._edges.pop(edge_id, None)
        if not e: return False
        self._out[e.from_id] = [x for x in self._out[e.from_id] if x.id != edge_id]
        self._in[e.to_id]    = [x for x in self._in[e.to_id]    if x.id != edge_id]
        self._store.delete_edge(edge_id)
        return True

    # ── Query ─────────────────────────────────────────────────────────────────
    def neighbours(self, node_id: str, relation: str = None,
                    direction: str = "out") -> List[Node]:
        if direction == "out":
            edges = self._out.get(node_id, [])
            nids  = [e.to_id for e in edges
                      if not relation or e.relation == relation]
        elif direction == "in":
            edges = self._in.get(node_id, [])
            nids  = [e.from_id for e in edges
                      if not relation or e.relation == relation]
        else:
            out_ids = [e.to_id   for e in self._out.get(node_id, [])
                        if not relation or e.relation == relation]
            in_ids  = [e.from_id for e in self._in.get(node_id, [])
                        if not relation or e.relation == relation]
            nids = list(dict.fromkeys(out_ids + in_ids))
        return [self._nodes[nid] for nid in nids if nid in self._nodes]

    def nodes_by_type(self, node_type: str) -> List[Node]:
        return [n for n in self._nodes.values() if n.node_type == node_type]

    def edges_by_relation(self, relation: str) -> List[Edge]:
        return [e for e in self._edges.values() if e.relation == relation]

    def find_nodes(self, predicate: Callable[[Node], bool]) -> List[Node]:
        return [n for n in self._nodes.values() if predicate(n)]

    # ── Traversal ─────────────────────────────────────────────────────────────
    def bfs(self, start_id: str, max_depth: int = 3,
             relation: str = None) -> List[Tuple[str, int]]:
        """BFS returning (node_id, depth) pairs."""
        if start_id not in self._nodes: return []
        visited = {start_id}; result = [(start_id, 0)]
        queue = deque([(start_id, 0)])
        while queue:
            nid, depth = queue.popleft()
            if depth >= max_depth: continue
            for nb in self.neighbours(nid, relation, "out"):
                if nb.id not in visited:
                    visited.add(nb.id)
                    result.append((nb.id, depth + 1))
                    queue.append((nb.id, depth + 1))
        return result

    def dfs(self, start_id: str, max_depth: int = 3,
             relation: str = None) -> List[str]:
        """DFS returning visited node ids."""
        if start_id not in self._nodes: return []
        visited: Set[str] = set(); result: List[str] = []
        def _dfs(nid, depth):
            if nid in visited or depth > max_depth: return
            visited.add(nid); result.append(nid)
            for nb in self.neighbours(nid, relation, "out"):
                _dfs(nb.id, depth + 1)
        _dfs(start_id, 0); return result

    def shortest_path(self, from_id: str, to_id: str,
                       weighted: bool = False) -> Optional[List[str]]:
        """Dijkstra (weighted) or BFS (unweighted) shortest path."""
        if from_id not in self._nodes or to_id not in self._nodes:
            return None
        if weighted:
            import heapq
            dist  = {from_id: 0.0}
            prev: Dict[str, Optional[str]] = {from_id: None}
            heap  = [(0.0, from_id)]
            while heap:
                d, nid = heapq.heappop(heap)
                if nid == to_id: break
                if d > dist.get(nid, float("inf")): continue
                for e in self._out.get(nid, []):
                    nd = d + e.weight
                    if nd < dist.get(e.to_id, float("inf")):
                        dist[e.to_id] = nd; prev[e.to_id] = nid
                        heapq.heappush(heap, (nd, e.to_id))
            if to_id not in prev: return None
        else:
            prev = {from_id: None}
            queue = deque([from_id])
            while queue:
                nid = queue.popleft()
                if nid == to_id: break
                for nb in self.neighbours(nid, direction="out"):
                    if nb.id not in prev:
                        prev[nb.id] = nid; queue.append(nb.id)
            if to_id not in prev: return None
        # Reconstruct
        path = []
        cur: Optional[str] = to_id
        while cur is not None:
            path.append(cur); cur = prev.get(cur)
        return list(reversed(path))

    def subgraph(self, center_id: str, hops: int = 2) -> "KnowledgeGraph":
        """Extract N-hop neighbourhood as a new KnowledgeGraph."""
        visited = set(n for n, _ in self.bfs(center_id, hops))
        # Also include in-direction
        in_visited = set()
        q = deque([(center_id, 0)])
        seen = {center_id}
        while q:
            nid, d = q.popleft()
            if d >= hops: continue
            for nb in self.neighbours(nid, direction="in"):
                if nb.id not in seen:
                    seen.add(nb.id); in_visited.add(nb.id)
                    q.append((nb.id, d + 1))
        all_ids = visited | in_visited | {center_id}
        sub = KnowledgeGraph(db_path=":memory:", directed=self.directed)
        for nid in all_ids:
            n = self._nodes.get(nid)
            if n: sub._mem_add_node(n)
        for e in self._edges.values():
            if e.from_id in all_ids and e.to_id in all_ids:
                sub._mem_add_edge(e)
        return sub

    # ── Graph analytics ───────────────────────────────────────────────────────
    def degree_centrality(self) -> Dict[str, float]:
        n = max(1, len(self._nodes) - 1)
        return {nid: (len(self._out.get(nid, [])) + len(self._in.get(nid, []))) / n
                for nid in self._nodes}

    def connected_components(self) -> List[Set[str]]:
        """Union-find for undirected connectivity."""
        parent = {nid: nid for nid in self._nodes}
        def find(x):
            while parent[x] != x: parent[x] = parent[parent[x]]; x = parent[x]
            return x
        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb: parent[ra] = rb
        for e in self._edges.values():
            if e.from_id in parent and e.to_id in parent:
                union(e.from_id, e.to_id)
        comps: Dict[str, Set[str]] = defaultdict(set)
        for nid in self._nodes: comps[find(nid)].add(nid)
        return list(comps.values())

    def merge_nodes(self, keep_id: str, remove_id: str) -> bool:
        if keep_id not in self._nodes or remove_id not in self._nodes:
            return False
        # Re-route edges from remove_id to keep_id
        for e in list(self._out.get(remove_id, [])):
            self.add_edge(keep_id, e.to_id, e.relation, e.weight)
        for e in list(self._in.get(remove_id, [])):
            self.add_edge(e.from_id, keep_id, e.relation, e.weight)
        self.delete_node(remove_id)
        return True

    def stats(self) -> Dict:
        s = self._store.stats()
        s["in_memory_nodes"] = len(self._nodes)
        s["in_memory_edges"] = len(self._edges)
        s["directed"] = self.directed
        if self._nodes:
            deg = self.degree_centrality()
            s["avg_degree"] = round(
                sum(v * (len(self._nodes)-1) for v in deg.values())
                / len(self._nodes), 2)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def add_node_ep(req):
            d = await req.json()
            n = self.add_node(d["id"], d["label"], d.get("type","entity"),
                               d.get("properties",{}), d.get("tags",[]))
            return web.json_response(n.to_dict(), status=201)
        async def add_edge_ep(req):
            d = await req.json()
            e = self.add_edge(d["from"], d["to"], d["relation"],
                               d.get("weight",1.0), d.get("properties",{}))
            if not e: return web.json_response({"error":"node not found"},status=404)
            return web.json_response(e.to_dict(), status=201)
        async def query_ep(req):
            q = req.rel_url.query
            ntype = q.get("type")
            nodes = self.nodes_by_type(ntype) if ntype else list(self._nodes.values())
            return web.json_response({"nodes": [n.to_dict() for n in nodes[:50]]})
        async def path_ep(req):
            d = await req.json()
            p = self.shortest_path(d["from"], d["to"], d.get("weighted", False))
            return web.json_response({"path": p})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/kg"
        app.router.add_post(f"{p}/node",  add_node_ep)
        app.router.add_post(f"{p}/edge",  add_edge_ep)
        app.router.add_get( f"{p}/query", query_ep)
        app.router.add_post(f"{p}/path",  path_ep)
        app.router.add_get( f"{p}/stats", stats_ep)
        logger.info(f"Knowledge graph API at {prefix}/kg/")
