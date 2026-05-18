"""OMNI Agent — Task Dependency Graph: priority DAG scheduler with cancellation & checkpoints."""
from __future__ import annotations
import asyncio, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set


class NodeState(str, Enum):
    PENDING    = "pending"
    READY      = "ready"
    RUNNING    = "running"
    DONE       = "done"
    FAILED     = "failed"
    CANCELLED  = "cancelled"
    SKIPPED    = "skipped"


@dataclass
class DagNode:
    node_id: str
    name: str
    fn: Callable[..., Coroutine]
    deps: Set[str] = field(default_factory=set)        # node_ids this waits for
    priority: int = 0                                   # higher = runs first in wave
    timeout_s: Optional[float] = None
    on_failure: str = "fail"                            # "fail"|"skip"|"continue"
    max_retries: int = 0
    state: NodeState = NodeState.PENDING
    result: Any = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    retries_done: int = 0
    checkpoint: bool = False                            # save result as checkpoint

    @property
    def duration_ms(self) -> Optional[float]:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at) * 1000
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "state": self.state.value,
            "priority": self.priority,
            "deps": list(self.deps),
            "duration_ms": self.duration_ms,
            "error": self.error,
            "retries_done": self.retries_done,
        }


class CycleError(Exception):
    pass


class TaskDependencyGraph:
    """
    DAG-based task scheduler. Supports:
    - Arbitrary dependency depth
    - Priority ordering within each wave
    - Async parallel execution within a wave
    - Cancellation propagation
    - Checkpointing results
    - Dynamic node addition before run
    """

    def __init__(self):
        self._nodes: Dict[str, DagNode] = {}
        self._checkpoints: Dict[str, Any] = {}
        self._cancel_event = asyncio.Event()
        self._hooks_pre:  List[Callable] = []
        self._hooks_post: List[Callable] = []
        self._hooks_fail: List[Callable] = []
        self._run_count = 0
        self._total_nodes_run = 0

    # ── NODE MANAGEMENT ───────────────────────────────────────────────

    def add(self, name: str, fn: Callable[..., Coroutine],
            deps: Optional[List[str]] = None,
            priority: int = 0,
            timeout_s: Optional[float] = None,
            on_failure: str = "fail",
            max_retries: int = 0,
            checkpoint: bool = False,
            node_id: Optional[str] = None) -> DagNode:
        nid = node_id or str(uuid.uuid4())
        node = DagNode(
            node_id=nid, name=name, fn=fn,
            deps=set(deps or []),
            priority=priority,
            timeout_s=timeout_s,
            on_failure=on_failure,
            max_retries=max_retries,
            checkpoint=checkpoint,
        )
        self._nodes[nid] = node
        return node

    def remove(self, node_id: str):
        self._nodes.pop(node_id, None)

    def add_dependency(self, node_id: str, dep_id: str):
        if node_id in self._nodes:
            self._nodes[node_id].deps.add(dep_id)

    def remove_dependency(self, node_id: str, dep_id: str):
        if node_id in self._nodes:
            self._nodes[node_id].deps.discard(dep_id)

    # ── TOPOLOGY ──────────────────────────────────────────────────────

    def validate(self) -> bool:
        """Returns True if DAG has no cycles."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {nid: WHITE for nid in self._nodes}
        def dfs(u: str) -> bool:
            color[u] = GRAY
            for dep in self._nodes[u].deps:
                if dep not in self._nodes:
                    continue
                if color[dep] == GRAY:
                    return True
                if color[dep] == WHITE and dfs(dep):
                    return True
            color[u] = BLACK
            return False
        return not any(dfs(n) for n in self._nodes if color[n] == WHITE)

    def topological_waves(self) -> List[List[DagNode]]:
        """Kahn's algorithm with priority ordering within waves."""
        if not self.validate():
            raise CycleError("Cycle detected in task graph")
        in_degree: Dict[str, int] = {nid: 0 for nid in self._nodes}
        children: Dict[str, List[str]] = {nid: [] for nid in self._nodes}
        for nid, node in self._nodes.items():
            for dep in node.deps:
                if dep in self._nodes:
                    in_degree[nid] += 1
                    children[dep].append(nid)
        waves = []
        ready = [nid for nid, deg in in_degree.items() if deg == 0]
        while ready:
            # Sort by priority desc within wave
            wave_nodes = sorted(
                [self._nodes[nid] for nid in ready],
                key=lambda n: n.priority, reverse=True)
            waves.append(wave_nodes)
            next_ready = []
            for node in wave_nodes:
                for child in children[node.node_id]:
                    in_degree[child] -= 1
                    if in_degree[child] == 0:
                        next_ready.append(child)
            ready = next_ready
        return waves

    def critical_path(self) -> List[str]:
        """Return node names along the longest dependency chain."""
        waves = self.topological_waves()
        if not waves:
            return []
        # Simple longest-chain by wave depth
        depth: Dict[str, int] = {}
        parent: Dict[str, Optional[str]] = {}
        for wave in waves:
            for node in wave:
                d = 0
                best_dep = None
                for dep in node.deps:
                    if dep in depth and depth[dep] + 1 > d:
                        d = depth[dep] + 1
                        best_dep = dep
                depth[node.node_id] = d
                parent[node.node_id] = best_dep
        # Find end of longest path
        sink = max(depth, key=depth.get)
        path = []
        cur: Optional[str] = sink
        while cur:
            path.append(self._nodes[cur].name)
            cur = parent.get(cur)
        return list(reversed(path))

    # ── EXECUTION ─────────────────────────────────────────────────────

    def cancel(self):
        self._cancel_event.set()

    async def run(self, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        ctx = dict(context or {})
        pre_cancelled = self._cancel_event.is_set()
        self._cancel_event.clear()
        if pre_cancelled:
            for node in self._nodes.values():
                node.state = NodeState.CANCELLED
            return {nid: None for nid in self._nodes}
        # Reset states
        for node in self._nodes.values():
            node.state = NodeState.PENDING
            node.result = None
            node.error = None
            node.started_at = None
            node.finished_at = None
            node.retries_done = 0
        # Restore checkpoints — mark DONE so _execute skips them
        for nid, val in self._checkpoints.items():
            if nid in self._nodes:
                self._nodes[nid].result = val
                self._nodes[nid].state = NodeState.DONE
                ctx[nid] = val
        waves = self.topological_waves()
        self._run_count += 1
        for wave in waves:
            if self._cancel_event.is_set():
                for node in wave:
                    node.state = NodeState.CANCELLED
                continue
            await asyncio.gather(*[self._execute(node, ctx) for node in wave])
        return {nid: node.result for nid, node in self._nodes.items()}

    async def _execute(self, node: DagNode, ctx: Dict[str, Any]):
        # Skip already-done (restored from checkpoint)
        if node.state == NodeState.DONE:
            return
        if self._cancel_event.is_set():
            node.state = NodeState.CANCELLED
            return
        # Skip if a dep failed/cancelled with propagation
        for dep_id in node.deps:
            dep = self._nodes.get(dep_id)
            if dep and dep.state in (NodeState.FAILED, NodeState.CANCELLED):
                if node.on_failure != "continue":
                    node.state = NodeState.SKIPPED
                    return
        for hook in self._hooks_pre:
            try: hook(node)
            except Exception: pass
        node.state = NodeState.RUNNING
        node.started_at = time.time()
        for attempt in range(node.max_retries + 1):
            try:
                coro = node.fn(ctx)
                if node.timeout_s:
                    node.result = await asyncio.wait_for(coro, timeout=node.timeout_s)
                else:
                    node.result = await coro
                node.state = NodeState.DONE
                node.finished_at = time.time()
                ctx[node.node_id] = node.result
                if node.checkpoint:
                    self._checkpoints[node.node_id] = node.result
                self._total_nodes_run += 1
                for hook in self._hooks_post:
                    try: hook(node)
                    except Exception: pass
                return
            except Exception as exc:
                node.retries_done = attempt
                if attempt < node.max_retries:
                    await asyncio.sleep(0.01 * (attempt + 1))
                    continue
                node.error = str(exc)
                node.finished_at = time.time()
                node.state = (NodeState.SKIPPED
                              if node.on_failure == "skip" else NodeState.FAILED)
                for hook in self._hooks_fail:
                    try: hook(node, exc)
                    except Exception: pass
                return

    # ── HOOKS ─────────────────────────────────────────────────────────

    def on_node_start(self, fn: Callable): self._hooks_pre.append(fn)
    def on_node_done(self, fn: Callable):  self._hooks_post.append(fn)
    def on_node_fail(self, fn: Callable):  self._hooks_fail.append(fn)

    # ── CHECKPOINTS ───────────────────────────────────────────────────

    def clear_checkpoints(self): self._checkpoints.clear()

    def get_checkpoint(self, node_id: str) -> Optional[Any]:
        return self._checkpoints.get(node_id)

    # ── INTROSPECTION ─────────────────────────────────────────────────

    def get_node(self, node_id: str) -> Optional[DagNode]:
        return self._nodes.get(node_id)

    def list_nodes(self) -> List[Dict[str, Any]]:
        return [n.to_dict() for n in self._nodes.values()]

    def stats(self) -> Dict[str, Any]:
        by_state: Dict[str, int] = {}
        for n in self._nodes.values():
            by_state[n.state.value] = by_state.get(n.state.value, 0) + 1
        return {
            "nodes": len(self._nodes),
            "run_count": self._run_count,
            "total_nodes_run": self._total_nodes_run,
            "checkpoints": len(self._checkpoints),
            "by_state": by_state,
        }
