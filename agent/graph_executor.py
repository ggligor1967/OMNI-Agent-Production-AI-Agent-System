"""OMNI Agent — Graph Executor: DAG computation graph with lazy eval and parallel exec."""
from __future__ import annotations
import hashlib, json, threading, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


class NodeStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    DONE      = "done"
    FAILED    = "failed"
    SKIPPED   = "skipped"
    CACHED    = "cached"


class ExecMode(str, Enum):
    SEQUENTIAL = "sequential"
    PARALLEL   = "parallel"     # run independent nodes concurrently
    LAZY       = "lazy"         # only compute what is needed


@dataclass
class GraphNode:
    node_id: str
    name: str
    fn: Callable
    inputs: List[str] = field(default_factory=list)    # node_ids
    outputs: List[str] = field(default_factory=list)   # node_ids (consumers)
    status: NodeStatus = NodeStatus.PENDING
    result: Any = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    retries: int = 0
    max_retries: int = 0
    skip_on_fail: bool = False
    cache_key: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at) * 1000
        return 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "status": self.status.value,
            "duration_ms": round(self.duration_ms, 2),
            "error": self.error,
            "inputs": self.inputs,
        }


@dataclass
class GraphRun:
    run_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    graph_id: str = ""
    status: str = "pending"
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    node_results: Dict[str, Any] = field(default_factory=dict)
    errors: Dict[str, str] = field(default_factory=dict)
    exec_order: List[str] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        end = self.finished_at or time.time()
        return (end - self.started_at) * 1000

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "duration_ms": round(self.duration_ms, 2),
            "nodes_run": len(self.exec_order),
            "errors": len(self.errors),
        }


class GraphExecutor:
    """
    DAG computation graph executor:
    - Nodes are callables with explicit input dependencies
    - Topological sort with cycle detection
    - Sequential, parallel (threaded), and lazy execution modes
    - Result caching per node (hash of inputs)
    - Retry per node
    - skip_on_fail: downstream nodes receive None instead of raising
    - Pre/post node hooks
    - Critical path analysis
    - Sub-graph execution (only up to requested output nodes)
    - Context dict passed to all node functions
    - Full run history
    """

    def __init__(self):
        self._nodes:   Dict[str, GraphNode] = {}
        self._cache:   Dict[str, Any] = {}     # cache_key → result
        self._runs:    List[GraphRun] = []
        self._pre_hooks:  List[Callable] = []
        self._post_hooks: List[Callable] = []
        self._graph_id = str(uuid.uuid4())[:8]

    # ── NODE MANAGEMENT ───────────────────────────────────────────────

    def add_node(self, name: str,
                 fn: Callable,
                 inputs: Optional[List[str]] = None,
                 max_retries: int = 0,
                 skip_on_fail: bool = False,
                 node_id: Optional[str] = None,
                 metadata: Optional[Dict] = None) -> GraphNode:
        nid  = node_id or str(uuid.uuid4())[:8]
        node = GraphNode(
            node_id=nid, name=name, fn=fn,
            inputs=list(inputs or []),
            max_retries=max_retries,
            skip_on_fail=skip_on_fail,
            metadata=metadata or {})
        self._nodes[nid] = node
        # Register this node as output of its inputs
        for inp_id in node.inputs:
            inp = self._nodes.get(inp_id)
            if inp and nid not in inp.outputs:
                inp.outputs.append(nid)
        return node

    def remove_node(self, node_id: str):
        node = self._nodes.pop(node_id, None)
        if node:
            for inp_id in node.inputs:
                inp = self._nodes.get(inp_id)
                if inp and node_id in inp.outputs:
                    inp.outputs.remove(node_id)

    def connect(self, from_id: str, to_id: str):
        """Add edge from_id → to_id."""
        src = self._nodes.get(from_id)
        dst = self._nodes.get(to_id)
        if src and dst:
            if to_id not in src.outputs:  src.outputs.append(to_id)
            if from_id not in dst.inputs: dst.inputs.append(from_id)

    # ── TOPOLOGY ─────────────────────────────────────────────────────

    def _topo_sort(self, target_ids: Optional[Set[str]] = None) -> List[str]:
        """Kahn's algorithm. Returns topological order."""
        nodes = self._nodes
        if target_ids:
            nodes = {nid: n for nid, n in nodes.items()
                     if nid in self._ancestors(target_ids) | target_ids}

        in_degree: Dict[str, int] = {nid: 0 for nid in nodes}
        for nid, node in nodes.items():
            for inp in node.inputs:
                if inp in nodes:
                    in_degree[nid] += 1

        queue = [nid for nid, deg in in_degree.items() if deg == 0]
        order: List[str] = []
        while queue:
            nid = queue.pop(0)
            order.append(nid)
            for out_id in nodes[nid].outputs:
                if out_id in in_degree:
                    in_degree[out_id] -= 1
                    if in_degree[out_id] == 0:
                        queue.append(out_id)

        if len(order) != len(nodes):
            raise ValueError("Cycle detected in graph")
        return order

    def _ancestors(self, node_ids: Set[str]) -> Set[str]:
        visited: Set[str] = set()
        stack = list(node_ids)
        while stack:
            nid = stack.pop()
            for inp in self._nodes.get(nid, GraphNode("", "", lambda: None)).inputs:
                if inp not in visited:
                    visited.add(inp)
                    stack.append(inp)
        return visited

    def detect_cycles(self) -> bool:
        try:
            self._topo_sort()
            return False
        except ValueError:
            return True

    def critical_path(self) -> List[str]:
        """Return the longest dependency chain (by node count)."""
        order = self._topo_sort()
        dist: Dict[str, int] = {nid: 0 for nid in order}
        prev: Dict[str, Optional[str]] = {nid: None for nid in order}
        for nid in order:
            node = self._nodes[nid]
            for inp in node.inputs:
                if inp in dist and dist[inp] + 1 > dist[nid]:
                    dist[nid] = dist[inp] + 1
                    prev[nid] = inp
        end = max(dist, key=lambda k: dist[k])
        path = []
        cur: Optional[str] = end
        while cur:
            path.append(cur)
            cur = prev[cur]
        return list(reversed(path))

    # ── EXECUTION ─────────────────────────────────────────────────────

    def _reset(self):
        for node in self._nodes.values():
            node.status       = NodeStatus.PENDING
            node.result       = None
            node.error        = None
            node.started_at   = None
            node.finished_at  = None
            node.retries      = 0

    def run(self, context: Optional[Dict] = None,
            mode: ExecMode = ExecMode.SEQUENTIAL,
            output_ids: Optional[List[str]] = None) -> GraphRun:
        ctx  = dict(context or {})
        run  = GraphRun(graph_id=self._graph_id)
        self._reset()

        try:
            target = set(output_ids) if output_ids else None
            order  = self._topo_sort(target)
        except ValueError as e:
            run.status = "error"
            run.errors["__cycle__"] = str(e)
            return run

        if mode == ExecMode.PARALLEL:
            self._run_parallel(order, ctx, run)
        elif mode == ExecMode.LAZY and output_ids:
            needed = self._ancestors(set(output_ids)) | set(output_ids)
            order  = [nid for nid in order if nid in needed]
            self._run_sequential(order, ctx, run)
        else:
            self._run_sequential(order, ctx, run)

        run.finished_at = time.time()
        run.status = "done" if not run.errors else "partial"
        self._runs.append(run)
        return run

    def _run_sequential(self, order: List[str],
                         ctx: Dict, run: GraphRun):
        for nid in order:
            self._exec_node(nid, ctx, run)

    def _run_parallel(self, order: List[str],
                       ctx: Dict, run: GraphRun):
        """Execute nodes in parallel waves (nodes with same depth)."""
        depths: Dict[str, int] = {}
        for nid in order:
            node = self._nodes[nid]
            depths[nid] = max((depths.get(inp, 0) + 1
                               for inp in node.inputs
                               if inp in depths), default=0)
        max_depth = max(depths.values(), default=0)
        for d in range(max_depth + 1):
            wave = [nid for nid in order if depths[nid] == d]
            threads = []
            for nid in wave:
                t = threading.Thread(
                    target=self._exec_node, args=(nid, ctx, run))
                threads.append(t); t.start()
            for t in threads: t.join()

    def _exec_node(self, node_id: str, ctx: Dict, run: GraphRun):
        node = self._nodes[node_id]

        # Check if any required input failed (and not skip_on_fail)
        for inp_id in node.inputs:
            inp_node = self._nodes.get(inp_id)
            if inp_node and inp_node.status == NodeStatus.FAILED:
                if not node.skip_on_fail:
                    node.status = NodeStatus.SKIPPED
                    run.exec_order.append(node_id)
                    return

        # Cache check
        cache_key = self._make_cache_key(node, ctx)
        if cache_key and cache_key in self._cache:
            node.result = self._cache[cache_key]
            node.status = NodeStatus.CACHED
            run.node_results[node_id] = node.result
            run.exec_order.append(node_id)
            return

        for fn in self._pre_hooks:
            try: fn(node)
            except Exception: pass

        node.status     = NodeStatus.RUNNING
        node.started_at = time.time()

        # Build kwargs from input results
        input_results = {inp_id: self._nodes[inp_id].result
                         for inp_id in node.inputs
                         if inp_id in self._nodes}

        attempt = 0
        while True:
            attempt += 1
            try:
                node.result = node.fn(input_results, ctx)
                node.status = NodeStatus.DONE
                node.finished_at = time.time()
                if cache_key:
                    self._cache[cache_key] = node.result
                run.node_results[node_id] = node.result
                break
            except Exception as exc:
                node.error = str(exc)
                if attempt <= node.max_retries:
                    node.retries = attempt
                    continue
                node.status = NodeStatus.FAILED
                node.finished_at = time.time()
                run.errors[node_id] = node.error
                run.node_results[node_id] = None
                break

        run.exec_order.append(node_id)
        for fn in self._post_hooks:
            try: fn(node)
            except Exception: pass

    def _make_cache_key(self, node: GraphNode, ctx: Dict) -> Optional[str]:
        if node.cache_key is False:
            return None
        input_vals = {inp_id: str(self._nodes[inp_id].result)
                      for inp_id in node.inputs if inp_id in self._nodes}
        raw = json.dumps({"node": node.node_id,
                          "inputs": input_vals}, sort_keys=True)
        return hashlib.md5(raw.encode()).hexdigest()

    # ── HOOKS ─────────────────────────────────────────────────────────

    def on_node_start(self, fn: Callable): self._pre_hooks.append(fn)
    def on_node_done(self, fn: Callable):  self._post_hooks.append(fn)

    # ── CACHE ─────────────────────────────────────────────────────────

    def clear_cache(self):       self._cache.clear()
    def cache_size(self) -> int: return len(self._cache)

    # ── QUERY ─────────────────────────────────────────────────────────

    def get_node(self, node_id: str) -> Optional[GraphNode]:
        return self._nodes.get(node_id)

    def get_result(self, node_id: str) -> Any:
        node = self._nodes.get(node_id)
        return node.result if node else None

    def run_history(self, limit: int = 20) -> List[Dict]:
        return [r.to_dict() for r in self._runs[-limit:]]

    def stats(self) -> Dict[str, Any]:
        return {
            "nodes": len(self._nodes),
            "runs": len(self._runs),
            "cache_size": self.cache_size(),
            "has_cycle": self.detect_cycles(),
        }
