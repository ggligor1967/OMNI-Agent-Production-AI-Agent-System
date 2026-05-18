"""OMNI Agent — Agent Coordinator: dependency-aware multi-agent task orchestration."""
from __future__ import annotations
import asyncio, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set


class TaskStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    DONE      = "done"
    FAILED    = "failed"
    SKIPPED   = "skipped"
    CANCELLED = "cancelled"


class AgentStatus(str, Enum):
    IDLE    = "idle"
    BUSY    = "busy"
    OFFLINE = "offline"


@dataclass
class AgentSpec:
    agent_id: str
    name: str
    capabilities: List[str] = field(default_factory=list)
    max_concurrent: int = 1
    status: AgentStatus = AgentStatus.IDLE
    _running: int = field(default=0, repr=False)

    def can_accept(self) -> bool:
        return self.status != AgentStatus.OFFLINE and self._running < self.max_concurrent

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "capabilities": self.capabilities,
            "status": self.status.value,
            "running": self._running,
            "max_concurrent": self.max_concurrent,
        }


@dataclass
class CoordTask:
    task_id: str
    name: str
    fn: Callable[..., Coroutine]
    depends_on: List[str] = field(default_factory=list)   # task_id list
    required_capability: Optional[str] = None
    timeout_s: Optional[float] = None
    on_error: str = "fail"           # "fail" | "skip" | "continue"
    status: TaskStatus = TaskStatus.PENDING
    assigned_agent: Optional[str] = None
    result: Any = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    retries: int = 0
    max_retries: int = 0

    @property
    def duration_ms(self) -> Optional[float]:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at) * 1000
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "status": self.status.value,
            "depends_on": self.depends_on,
            "assigned_agent": self.assigned_agent,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "retries": self.retries,
        }


class DependencyCycleError(Exception):
    pass


class AgentCoordinator:
    """
    Orchestrates multiple agents running tasks with dependency resolution.
    Tasks execute in topological order; independent tasks run concurrently.
    """

    def __init__(self):
        self._agents: Dict[str, AgentSpec] = {}
        self._tasks: Dict[str, CoordTask] = {}
        self._hooks_on_start: List[Callable] = []
        self._hooks_on_done:  List[Callable] = []
        self._hooks_on_fail:  List[Callable] = []
        self._completed_count = 0
        self._failed_count = 0

    # ── AGENTS ────────────────────────────────────────────────────────

    def register_agent(self, agent_id: str, name: str,
                       capabilities: Optional[List[str]] = None,
                       max_concurrent: int = 1) -> AgentSpec:
        spec = AgentSpec(
            agent_id=agent_id, name=name,
            capabilities=capabilities or [],
            max_concurrent=max_concurrent)
        self._agents[agent_id] = spec
        return spec

    def unregister_agent(self, agent_id: str):
        self._agents.pop(agent_id, None)

    def set_agent_status(self, agent_id: str, status: AgentStatus):
        agent = self._agents.get(agent_id)
        if agent:
            agent.status = status

    def available_agents(self, capability: Optional[str] = None) -> List[AgentSpec]:
        agents = [a for a in self._agents.values() if a.can_accept()]
        if capability:
            agents = [a for a in agents if capability in a.capabilities]
        return agents

    # ── TASKS ─────────────────────────────────────────────────────────

    def add_task(
        self,
        name: str,
        fn: Callable[..., Coroutine],
        depends_on: Optional[List[str]] = None,
        required_capability: Optional[str] = None,
        timeout_s: Optional[float] = None,
        on_error: str = "fail",
        max_retries: int = 0,
        task_id: Optional[str] = None,
    ) -> CoordTask:
        tid = task_id or str(uuid.uuid4())
        task = CoordTask(
            task_id=tid, name=name, fn=fn,
            depends_on=depends_on or [],
            required_capability=required_capability,
            timeout_s=timeout_s,
            on_error=on_error,
            max_retries=max_retries,
        )
        self._tasks[tid] = task
        return task

    def remove_task(self, task_id: str):
        self._tasks.pop(task_id, None)

    def clear_tasks(self):
        self._tasks.clear()

    # ── TOPOLOGY ──────────────────────────────────────────────────────

    def _topo_sort(self) -> List[List[str]]:
        """Kahn's algorithm → list of parallel waves."""
        in_degree: Dict[str, int] = {tid: 0 for tid in self._tasks}
        children: Dict[str, List[str]] = {tid: [] for tid in self._tasks}
        for tid, task in self._tasks.items():
            for dep in task.depends_on:
                if dep not in self._tasks:
                    continue
                in_degree[tid] += 1
                children[dep].append(tid)
        waves: List[List[str]] = []
        ready = [tid for tid, deg in in_degree.items() if deg == 0]
        visited = 0
        while ready:
            waves.append(list(ready))
            visited += len(ready)
            next_ready = []
            for tid in ready:
                for child in children[tid]:
                    in_degree[child] -= 1
                    if in_degree[child] == 0:
                        next_ready.append(child)
            ready = next_ready
        if visited < len(self._tasks):
            raise DependencyCycleError("Cycle detected in task graph")
        return waves

    def has_cycle(self) -> bool:
        try:
            self._topo_sort()
            return False
        except DependencyCycleError:
            return True

    # ── EXECUTION ─────────────────────────────────────────────────────

    async def run(self, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Execute all tasks respecting dependencies. Returns results dict."""
        ctx = dict(context or {})
        waves = self._topo_sort()
        for wave in waves:
            await asyncio.gather(*[self._run_task(tid, ctx) for tid in wave])
        return {tid: task.result for tid, task in self._tasks.items()}

    async def run_task(self, task_id: str,
                       context: Optional[Dict[str, Any]] = None) -> Any:
        """Run a single task directly (ignores dependencies)."""
        ctx = dict(context or {})
        return await self._run_task(task_id, ctx)

    async def _run_task(self, task_id: str, ctx: Dict[str, Any]):
        task = self._tasks[task_id]

        # Skip if a dependency failed with on_error=fail and we should propagate
        dep_failed = any(
            self._tasks[d].status in (TaskStatus.FAILED, TaskStatus.SKIPPED)
            for d in task.depends_on if d in self._tasks
        )
        if dep_failed and task.on_error != "continue":
            task.status = TaskStatus.SKIPPED
            return

        # Assign agent
        agents = self.available_agents(task.required_capability)
        agent = agents[0] if agents else None
        if agent:
            task.assigned_agent = agent.agent_id
            agent._running += 1

        task.status = TaskStatus.RUNNING
        task.started_at = time.time()
        for hook in self._hooks_on_start:
            try: hook(task)
            except Exception: pass

        for attempt in range(task.max_retries + 1):
            try:
                coro = task.fn(ctx)
                if task.timeout_s:
                    task.result = await asyncio.wait_for(coro, timeout=task.timeout_s)
                else:
                    task.result = await coro
                task.status = TaskStatus.DONE
                task.finished_at = time.time()
                ctx[task_id] = task.result
                self._completed_count += 1
                for hook in self._hooks_on_done:
                    try: hook(task)
                    except Exception: pass
                break
            except Exception as exc:
                task.retries = attempt
                if attempt < task.max_retries:
                    await asyncio.sleep(0.01 * (attempt + 1))
                    continue
                task.error = str(exc)
                task.status = (TaskStatus.SKIPPED
                               if task.on_error == "skip" else TaskStatus.FAILED)
                task.finished_at = time.time()
                self._failed_count += 1
                for hook in self._hooks_on_fail:
                    try: hook(task, exc)
                    except Exception: pass

        if agent:
            agent._running = max(0, agent._running - 1)

    # ── HOOKS ─────────────────────────────────────────────────────────

    def on_task_start(self, fn: Callable): self._hooks_on_start.append(fn)
    def on_task_done(self, fn: Callable):  self._hooks_on_done.append(fn)
    def on_task_fail(self, fn: Callable):  self._hooks_on_fail.append(fn)

    # ── INTROSPECTION ─────────────────────────────────────────────────

    def task_status(self, task_id: str) -> Optional[TaskStatus]:
        t = self._tasks.get(task_id)
        return t.status if t else None

    def list_tasks(self) -> List[Dict[str, Any]]:
        return [t.to_dict() for t in self._tasks.values()]

    def list_agents(self) -> List[Dict[str, Any]]:
        return [a.to_dict() for a in self._agents.values()]

    def stats(self) -> Dict[str, Any]:
        statuses: Dict[str, int] = {}
        for t in self._tasks.values():
            statuses[t.status.value] = statuses.get(t.status.value, 0) + 1
        return {
            "agents": len(self._agents),
            "tasks": len(self._tasks),
            "completed": self._completed_count,
            "failed": self._failed_count,
            "by_status": statuses,
        }
