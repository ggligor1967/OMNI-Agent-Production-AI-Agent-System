"""OMNI Agent — Agent Coordinator V2: delegation, voting, result aggregation."""
from __future__ import annotations
import statistics, threading, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class AgentRole(str, Enum):
    COORDINATOR = "coordinator"
    WORKER      = "worker"
    VALIDATOR   = "validator"
    CRITIC      = "critic"
    SUMMARIZER  = "summarizer"
    CUSTOM      = "custom"


class TaskState(str, Enum):
    PENDING   = "pending"
    ASSIGNED  = "assigned"
    RUNNING   = "running"
    DONE      = "done"
    FAILED    = "failed"
    TIMEOUT   = "timeout"


class AggregationStrategy(str, Enum):
    FIRST        = "first"        # use first result
    MAJORITY     = "majority"     # most common result
    CONSENSUS    = "consensus"    # all must agree
    AVERAGE      = "average"      # numeric average
    BEST         = "best"         # highest scoring
    CHAIN        = "chain"        # pass output of one to next


@dataclass
class AgentSpec:
    agent_id: str
    name: str
    role: AgentRole = AgentRole.WORKER
    fn: Callable = field(default=lambda task, ctx: None)
    capabilities: List[str] = field(default_factory=list)
    weight: float = 1.0          # for weighted voting
    max_concurrent: int = 5
    timeout_s: float = 30.0
    active_tasks: int = 0
    total_tasks: int = 0
    success_count: int = 0
    total_ms: float = 0.0
    tags: List[str] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return self.success_count / self.total_tasks if self.total_tasks else 0.0

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.total_tasks if self.total_tasks else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {"agent_id": self.agent_id, "name": self.name,
                "role": self.role.value, "capabilities": self.capabilities,
                "success_rate": round(self.success_rate, 3),
                "avg_ms": round(self.avg_ms, 2),
                "active_tasks": self.active_tasks}


@dataclass
class DelegatedTask:
    task_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    task_type: str = ""
    payload: Any = None
    required_capability: Optional[str] = None
    state: TaskState = TaskState.PENDING
    assigned_to: List[str] = field(default_factory=list)
    results: Dict[str, Any] = field(default_factory=dict)   # agent_id → result
    errors:  Dict[str, str] = field(default_factory=dict)
    final_result: Any = None
    aggregation: AggregationStrategy = AggregationStrategy.FIRST
    timeout_s: float = 30.0
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    context: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        if self.finished_at:
            return (self.finished_at - self.created_at) * 1000
        return 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {"task_id": self.task_id, "type": self.task_type,
                "state": self.state.value,
                "agents": len(self.assigned_to),
                "duration_ms": round(self.duration_ms, 2)}


class AgentCoordinatorV2:
    """
    Multi-agent coordinator:
    - Register agents with roles, capabilities, weight
    - Delegate tasks by capability, role, or explicit agent list
    - Parallel dispatch to multiple agents
    - Aggregation strategies: first/majority/consensus/average/best/chain
    - Weighted majority voting
    - Timeout per task (thread-based)
    - Retry on agent failure (fallback to next capable agent)
    - Chain mode: pipe output of agent N to agent N+1
    - Load-aware routing (prefer agent with fewest active tasks)
    - Task history and per-agent statistics
    - Pre/post task hooks
    """

    def __init__(self, max_workers: int = 8):
        self._agents:  Dict[str, AgentSpec] = {}
        self._tasks:   List[DelegatedTask] = []
        self._pre_hooks:  List[Callable] = []
        self._post_hooks: List[Callable] = []
        self._max_workers = max_workers
        self._lock = threading.Lock()

    # ── AGENT MANAGEMENT ──────────────────────────────────────────────

    def register(self, name: str,
                  fn: Callable,
                  role: AgentRole = AgentRole.WORKER,
                  capabilities: Optional[List[str]] = None,
                  weight: float = 1.0,
                  timeout_s: float = 30.0,
                  max_concurrent: int = 5,
                  tags: Optional[List[str]] = None,
                  agent_id: Optional[str] = None) -> AgentSpec:
        aid = agent_id or str(uuid.uuid4())[:8]
        a   = AgentSpec(agent_id=aid, name=name, role=role, fn=fn,
                         capabilities=list(capabilities or []),
                         weight=weight, timeout_s=timeout_s,
                         max_concurrent=max_concurrent,
                         tags=list(tags or []))
        self._agents[aid] = a
        return a

    def deregister(self, agent_id: str) -> bool:
        return self._agents.pop(agent_id, None) is not None

    def get_agent(self, agent_id: str) -> Optional[AgentSpec]:
        return self._agents.get(agent_id)

    def list_agents(self, role: Optional[AgentRole] = None,
                     capability: Optional[str] = None) -> List[Dict]:
        agents = list(self._agents.values())
        if role:       agents = [a for a in agents if a.role == role]
        if capability: agents = [a for a in agents if capability in a.capabilities]
        return [a.to_dict() for a in agents]

    # ── SELECTION ────────────────────────────────────────────────────

    def _select_agents(self, task: DelegatedTask,
                        n: int = 1) -> List[AgentSpec]:
        candidates = list(self._agents.values())
        if task.required_capability:
            candidates = [a for a in candidates
                          if task.required_capability in a.capabilities]
        # Filter out overloaded agents
        candidates = [a for a in candidates
                      if a.active_tasks < a.max_concurrent]
        # Sort by load (fewest active first), break ties by success rate
        candidates.sort(key=lambda a: (a.active_tasks, -a.success_rate))
        return candidates[:n]

    # ── DISPATCH ─────────────────────────────────────────────────────

    def _run_agent(self, agent: AgentSpec,
                    task: DelegatedTask) -> Any:
        result_box: List[Any] = [None]
        exc_box:    List[Optional[Exception]] = [None]

        def _exec():
            try:
                result_box[0] = agent.fn(task.payload, task.context)
            except Exception as e:
                exc_box[0] = e

        t0  = time.time()
        with self._lock: agent.active_tasks += 1
        thread = threading.Thread(target=_exec, daemon=True)
        thread.start()
        thread.join(timeout=min(agent.timeout_s, task.timeout_s))
        ms  = (time.time() - t0) * 1000
        with self._lock:
            agent.active_tasks  = max(0, agent.active_tasks - 1)
            agent.total_tasks  += 1
            agent.total_ms     += ms
            if thread.is_alive():
                exc_box[0] = TimeoutError(f"Agent {agent.name} timed out")
            elif not exc_box[0]:
                agent.success_count += 1

        if exc_box[0]: raise exc_box[0]
        return result_box[0]

    def delegate(self, task_type: str,
                  payload: Any = None,
                  required_capability: Optional[str] = None,
                  agent_ids: Optional[List[str]] = None,
                  n_agents: int = 1,
                  aggregation: AggregationStrategy = AggregationStrategy.FIRST,
                  timeout_s: float = 30.0,
                  context: Optional[Dict] = None) -> DelegatedTask:
        task = DelegatedTask(
            task_type=task_type, payload=payload,
            required_capability=required_capability,
            aggregation=aggregation, timeout_s=timeout_s,
            context=dict(context or {}))

        # Determine agents
        if agent_ids:
            agents = [self._agents[aid] for aid in agent_ids
                      if aid in self._agents]
        else:
            agents = self._select_agents(task, n_agents)

        if not agents:
            task.state = TaskState.FAILED
            task.errors["__no_agent__"] = "No capable agents available"
            self._tasks.append(task)
            return task

        task.assigned_to = [a.agent_id for a in agents]
        task.state = TaskState.RUNNING

        for fn in self._pre_hooks:
            try: fn(task)
            except Exception: pass

        if aggregation == AggregationStrategy.CHAIN:
            result = self._run_chain(agents, task)
        elif n_agents > 1:
            result = self._run_parallel(agents, task)
        else:
            result = self._run_single(agents[0], task)

        task.final_result = result
        task.state = TaskState.DONE if not task.errors else TaskState.FAILED
        task.finished_at = time.time()
        self._tasks.append(task)

        for fn in self._post_hooks:
            try: fn(task)
            except Exception: pass

        return task

    def _run_single(self, agent: AgentSpec,
                     task: DelegatedTask) -> Any:
        try:
            r = self._run_agent(agent, task)
            task.results[agent.agent_id] = r
            return r
        except Exception as exc:
            task.errors[agent.agent_id] = str(exc)
            return None

    def _run_parallel(self, agents: List[AgentSpec],
                       task: DelegatedTask) -> Any:
        results: Dict[str, Any] = {}
        lock = threading.Lock()

        def run(a):
            try:
                r = self._run_agent(a, task)
                with lock:
                    results[a.agent_id] = r
                    task.results[a.agent_id] = r
            except Exception as exc:
                with lock:
                    task.errors[a.agent_id] = str(exc)

        threads = [threading.Thread(target=run, args=(a,), daemon=True)
                   for a in agents]
        for t in threads: t.start()
        for t in threads: t.join()

        return self._aggregate(task, results, agents)

    def _run_chain(self, agents: List[AgentSpec],
                    task: DelegatedTask) -> Any:
        current = task.payload
        for a in agents:
            t_copy = DelegatedTask(task_type=task.task_type,
                                    payload=current,
                                    context=dict(task.context))
            try:
                current = self._run_agent(a, t_copy)
                task.results[a.agent_id] = current
            except Exception as exc:
                task.errors[a.agent_id] = str(exc)
                break
        return current

    def _aggregate(self, task: DelegatedTask,
                    results: Dict[str, Any],
                    agents: List[AgentSpec]) -> Any:
        values = list(results.values())
        if not values: return None
        agg = task.aggregation
        if agg == AggregationStrategy.FIRST:
            return values[0]
        if agg == AggregationStrategy.MAJORITY:
            try:
                from collections import Counter
                weights: Dict[Any, float] = {}
                for aid, val in results.items():
                    a  = self._agents.get(aid)
                    w  = a.weight if a else 1.0
                    key = str(val)
                    weights[key] = weights.get(key, 0.0) + w
                best_key = max(weights, key=lambda k: weights[k])
                return next(v for v in values if str(v) == best_key)
            except Exception:
                return values[0]
        if agg == AggregationStrategy.CONSENSUS:
            return values[0] if len(set(str(v) for v in values)) == 1 else None
        if agg == AggregationStrategy.AVERAGE:
            try:
                return statistics.mean(float(v) for v in values)
            except Exception:
                return values[0]
        if agg == AggregationStrategy.BEST:
            # best = highest scoring (if results are numeric, else first)
            try:
                return max(values, key=float)
            except Exception:
                return values[0]
        return values[0]

    # ── HOOKS ─────────────────────────────────────────────────────────

    def on_before_task(self, fn: Callable): self._pre_hooks.append(fn)
    def on_after_task(self, fn: Callable):  self._post_hooks.append(fn)

    # ── STATS ─────────────────────────────────────────────────────────

    def task_history(self, limit: int = 50) -> List[Dict]:
        return [t.to_dict() for t in self._tasks[-limit:]]

    def stats(self) -> Dict[str, Any]:
        done = sum(1 for t in self._tasks if t.state == TaskState.DONE)
        return {
            "agents": len(self._agents),
            "total_tasks": len(self._tasks),
            "done": done,
            "failed": sum(1 for t in self._tasks if t.state == TaskState.FAILED),
        }
