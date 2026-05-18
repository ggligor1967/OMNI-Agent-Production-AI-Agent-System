"""
OMNI AGENT - Federation (Multi-Agent Orchestration)
Spawn subagents, delegate subtasks, collect results, and aggregate
with fan-out/fan-in patterns, dependency graphs, and timeout management.

Features:
- Agent registry: register named agents with capability tags
- Task graph: define tasks with dependencies (DAG execution)
- Fan-out: broadcast a task to N agents concurrently
- Fan-in: aggregate results with configurable merge strategies
- Delegation: route tasks to the best-fit agent by capability
- Timeout & retry: per-task timeout with configurable retries
- Result streaming: yield partial results as subagents complete
- Federation log: full trace of all delegated tasks
- Local or remote agents (HTTP callback or in-process callable)
"""
import asyncio
import time
import uuid
import json
import logging
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# AGENT DEFINITION
# ══════════════════════════════════════════════════════════════════════════════

class AgentTransport(str, Enum):
    IN_PROCESS = "in_process"   # callable in same process
    HTTP       = "http"         # remote via HTTP POST
    QUEUE      = "queue"        # via job queue


@dataclass
class AgentDef:
    """A registered agent capable of handling tasks."""
    id: str
    name: str
    capabilities: Set[str]               # e.g. {"summarize", "translate", "code"}
    transport: AgentTransport = AgentTransport.IN_PROCESS
    handler: Optional[Callable] = None   # for IN_PROCESS agents
    endpoint: str = ""                   # for HTTP agents
    timeout_s: float = 30.0
    max_concurrent: int = 10
    metadata: Dict = field(default_factory=dict)
    _semaphore: Optional[asyncio.Semaphore] = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        self._semaphore = asyncio.Semaphore(self.max_concurrent)

    def can_handle(self, capability: str) -> bool:
        return capability in self.capabilities or "*" in self.capabilities

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "name": self.name,
            "capabilities": list(self.capabilities),
            "transport": self.transport,
            "endpoint": self.endpoint,
            "timeout_s": self.timeout_s,
            "max_concurrent": self.max_concurrent,
            "metadata": self.metadata,
        }


# ══════════════════════════════════════════════════════════════════════════════
# TASK MODEL
# ══════════════════════════════════════════════════════════════════════════════

class TaskStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"
    SKIPPED   = "skipped"    # dependency failed, skip this task
    TIMEOUT   = "timeout"


@dataclass
class Task:
    """A unit of work to be delegated to an agent."""
    id: str
    name: str
    capability: str           # which capability is required
    payload: Dict[str, Any]
    depends_on: List[str] = field(default_factory=list)   # task IDs
    agent_id: Optional[str] = None   # pin to specific agent, or None for auto
    timeout_s: float = 30.0
    retries: int = 1
    priority: int = 5         # 1 (high) - 10 (low)
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[Any] = None
    error: str = ""
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    assigned_agent: str = ""

    @property
    def duration_ms(self) -> Optional[float]:
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at) * 1000
        return None

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "name": self.name,
            "capability": self.capability,
            "depends_on": self.depends_on,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "assigned_agent": self.assigned_agent,
        }


# ══════════════════════════════════════════════════════════════════════════════
# MERGE STRATEGIES
# ══════════════════════════════════════════════════════════════════════════════

class MergeStrategy(str, Enum):
    FIRST        = "first"        # return first successful result
    ALL          = "all"          # return all results as list
    VOTE         = "vote"         # majority vote on string results
    CONCATENATE  = "concatenate"  # join text results
    BEST_SCORE   = "best_score"   # result with highest .score field
    CUSTOM       = "custom"       # use provided merge_fn


def _merge_results(results: List[Any], strategy: MergeStrategy,
                   merge_fn: Callable = None) -> Any:
    successful = [r for r in results if r is not None]
    if not successful:
        return None
    if strategy == MergeStrategy.FIRST:
        return successful[0]
    elif strategy == MergeStrategy.ALL:
        return successful
    elif strategy == MergeStrategy.CONCATENATE:
        return "\n\n".join(str(r) for r in successful)
    elif strategy == MergeStrategy.VOTE:
        from collections import Counter
        votes = Counter(str(r) for r in successful)
        return votes.most_common(1)[0][0]
    elif strategy == MergeStrategy.BEST_SCORE:
        scored = [(r.get("score", 0) if isinstance(r, dict) else 0, r)
                  for r in successful]
        return max(scored, key=lambda x: x[0])[1]
    elif strategy == MergeStrategy.CUSTOM and merge_fn:
        return merge_fn(successful)
    return successful


# ══════════════════════════════════════════════════════════════════════════════
# FEDERATION ENGINE
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class FederationResult:
    plan_id: str
    tasks: List[Task]
    merged: Any
    success: bool
    duration_ms: float
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "plan_id": self.plan_id,
            "tasks": [t.to_dict() for t in self.tasks],
            "merged": self.merged,
            "success": self.success,
            "duration_ms": round(self.duration_ms, 1),
            "errors": self.errors,
            "task_count": len(self.tasks),
            "completed": sum(1 for t in self.tasks if t.status == TaskStatus.COMPLETED),
            "failed": sum(1 for t in self.tasks if t.status == TaskStatus.FAILED),
        }


class FederationEngine:
    """
    Multi-agent task orchestrator.

    Usage:
        fed = FederationEngine()

        # Register agents
        fed.register(AgentDef(id="summarizer", name="Summarizer",
                              capabilities={"summarize"},
                              handler=my_summarize_fn))
        fed.register(AgentDef(id="translator", name="Translator",
                              capabilities={"translate"},
                              handler=my_translate_fn))

        # Fan-out: run same task on multiple agents
        results = await fed.fan_out("summarize", {"text": long_doc},
                                    agent_ids=["summarizer"],
                                    merge=MergeStrategy.FIRST)

        # DAG execution: tasks with dependencies
        plan = [
            Task(id="t1", name="Summarize", capability="summarize",
                 payload={"text": doc}),
            Task(id="t2", name="Translate", capability="translate",
                 payload={"lang": "es"}, depends_on=["t1"]),
        ]
        result = await fed.execute_plan(plan)
    """

    def __init__(self):
        self._agents: Dict[str, AgentDef] = {}
        self._history: List[FederationResult] = []
        self._stats: Dict[str, int] = defaultdict(int)

    # ── Agent Registry ────────────────────────────────────────────────────────

    def register(self, agent: AgentDef):
        self._agents[agent.id] = agent
        logger.info(f"Agent registered: id={agent.id} name='{agent.name}' "
                   f"caps={agent.capabilities}")

    def unregister(self, agent_id: str) -> bool:
        return bool(self._agents.pop(agent_id, None))

    def get_agent(self, agent_id: str) -> Optional[AgentDef]:
        return self._agents.get(agent_id)

    def find_agents(self, capability: str) -> List[AgentDef]:
        """Find all agents that can handle a given capability."""
        return [a for a in self._agents.values() if a.can_handle(capability)]

    def list_agents(self) -> List[Dict]:
        return [a.to_dict() for a in self._agents.values()]

    # ── Task Execution ────────────────────────────────────────────────────────

    async def _execute_task(self, task: Task, context: Dict = None) -> Task:
        """Execute a single task on its assigned agent."""
        agent = self._agents.get(task.assigned_agent)
        if not agent:
            # Auto-assign
            candidates = self.find_agents(task.capability)
            if not candidates:
                task.status = TaskStatus.FAILED
                task.error = f"No agent found for capability: {task.capability}"
                return task
            agent = candidates[0]
            task.assigned_agent = agent.id

        task.status = TaskStatus.RUNNING
        task.started_at = time.time()

        for attempt in range(max(1, task.retries)):
            try:
                async with agent._semaphore:
                    payload = {**task.payload}
                    if context:
                        payload["_context"] = context

                    if agent.transport == AgentTransport.IN_PROCESS:
                        if not agent.handler:
                            raise ValueError(f"Agent {agent.id} has no handler")
                        if asyncio.iscoroutinefunction(agent.handler):
                            result = await asyncio.wait_for(
                                agent.handler(payload),
                                timeout=task.timeout_s
                            )
                        else:
                            result = agent.handler(payload)

                    elif agent.transport == AgentTransport.HTTP:
                        result = await self._http_call(agent, task, payload)

                    else:
                        raise NotImplementedError(f"Transport {agent.transport} not supported")

                task.result = result
                task.status = TaskStatus.COMPLETED
                task.completed_at = time.time()
                self._stats["tasks_completed"] += 1
                return task

            except asyncio.TimeoutError:
                task.error = f"Timed out after {task.timeout_s}s (attempt {attempt+1})"
                if attempt == task.retries - 1:
                    task.status = TaskStatus.TIMEOUT
                    self._stats["tasks_timeout"] += 1

            except Exception as e:
                task.error = str(e)[:300]
                if attempt == task.retries - 1:
                    task.status = TaskStatus.FAILED
                    self._stats["tasks_failed"] += 1
                else:
                    await asyncio.sleep(0.5 * (attempt + 1))

        task.completed_at = time.time()
        return task

    async def _http_call(self, agent: AgentDef, task: Task, payload: Dict) -> Any:
        """Deliver task to a remote HTTP agent."""
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    agent.endpoint,
                    json={"task_id": task.id, "capability": task.capability,
                          "payload": payload},
                    timeout=aiohttp.ClientTimeout(total=task.timeout_s)
                ) as resp:
                    resp.raise_for_status()
                    return await resp.json()
        except ImportError:
            raise RuntimeError("aiohttp required for HTTP transport")

    # ── Fan-out ───────────────────────────────────────────────────────────────

    async def fan_out(self, capability: str, payload: Dict,
                      agent_ids: List[str] = None,
                      merge: MergeStrategy = MergeStrategy.ALL,
                      merge_fn: Callable = None,
                      timeout_s: float = 30.0) -> FederationResult:
        """
        Send the same task to multiple agents concurrently, then merge results.

        Args:
            capability: Task capability required
            payload:    Task payload sent to all agents
            agent_ids:  Specific agents to target (None = all capable agents)
            merge:      How to combine results
            merge_fn:   Custom merge function (used when merge=CUSTOM)
            timeout_s:  Per-task timeout
        """
        plan_id = str(uuid.uuid4())[:10]
        start = time.time()

        if agent_ids:
            agents = [self._agents[aid] for aid in agent_ids if aid in self._agents]
        else:
            agents = self.find_agents(capability)

        if not agents:
            return FederationResult(
                plan_id=plan_id, tasks=[], merged=None, success=False,
                duration_ms=0, errors=[f"No agents for capability: {capability}"]
            )

        tasks = [
            Task(id=f"{plan_id}:{i}", name=f"{capability}@{a.id}",
                 capability=capability, payload=payload,
                 assigned_agent=a.id, timeout_s=timeout_s)
            for i, a in enumerate(agents)
        ]

        coros = [self._execute_task(t) for t in tasks]
        completed = await asyncio.gather(*coros, return_exceptions=False)

        results = [t.result for t in completed if t.status == TaskStatus.COMPLETED]
        errors = [t.error for t in completed if t.status != TaskStatus.COMPLETED]
        merged = _merge_results(results, merge, merge_fn)

        fed_result = FederationResult(
            plan_id=plan_id, tasks=list(completed),
            merged=merged, success=len(results) > 0,
            duration_ms=(time.time() - start) * 1000,
            errors=errors,
        )
        self._history.append(fed_result)
        self._stats["fan_outs"] += 1
        return fed_result

    # ── DAG Execution ─────────────────────────────────────────────────────────

    async def execute_plan(self, tasks: List[Task],
                           merge: MergeStrategy = MergeStrategy.ALL,
                           inject_results: bool = True) -> FederationResult:
        """
        Execute a list of tasks respecting their dependency graph (DAG).

        If inject_results=True, completed task results are added to
        downstream task payloads as _dep_{task_id}.
        """
        plan_id = str(uuid.uuid4())[:10]
        start = time.time()
        task_map: Dict[str, Task] = {t.id: t for t in tasks}
        completed_ids: Set[str] = set()
        errors: List[str] = []

        # Topological execution: repeatedly execute tasks whose deps are satisfied
        max_rounds = len(tasks) + 1
        for _ in range(max_rounds):
            ready = [
                t for t in task_map.values()
                if t.status == TaskStatus.PENDING
                and all(task_map[d].status == TaskStatus.COMPLETED
                        for d in t.depends_on
                        if d in task_map)
            ]

            # Skip tasks whose dependencies failed
            for t in task_map.values():
                if t.status == TaskStatus.PENDING:
                    failed_deps = [
                        d for d in t.depends_on
                        if d in task_map and
                        task_map[d].status in (TaskStatus.FAILED, TaskStatus.TIMEOUT, TaskStatus.SKIPPED)
                    ]
                    if failed_deps:
                        t.status = TaskStatus.SKIPPED
                        t.error = f"Dependency failed: {failed_deps}"

            if not ready:
                # Check if all done
                pending = [t for t in task_map.values() if t.status == TaskStatus.PENDING]
                if not pending:
                    break
                # Circular dependency or deadlock
                for t in pending:
                    t.status = TaskStatus.FAILED
                    t.error = "Circular dependency or unresolvable dependency"
                break

            # Inject dep results into payload
            if inject_results:
                for t in ready:
                    for dep_id in t.depends_on:
                        dep = task_map.get(dep_id)
                        if dep and dep.result is not None:
                            t.payload[f"_dep_{dep_id}"] = dep.result

            # Execute ready tasks concurrently
            coros = [self._execute_task(t) for t in ready]
            await asyncio.gather(*coros)
            completed_ids.update(t.id for t in ready)

        all_tasks = list(task_map.values())
        results = [t.result for t in all_tasks if t.status == TaskStatus.COMPLETED]
        errors = [f"{t.name}: {t.error}" for t in all_tasks
                  if t.status in (TaskStatus.FAILED, TaskStatus.TIMEOUT)]
        merged = _merge_results(results, merge)

        fed_result = FederationResult(
            plan_id=plan_id, tasks=all_tasks,
            merged=merged, success=len(errors) == 0,
            duration_ms=(time.time() - start) * 1000,
            errors=errors,
        )
        self._history.append(fed_result)
        self._stats["plans_executed"] += 1
        return fed_result

    # ── Simple Delegation ─────────────────────────────────────────────────────

    async def delegate(self, capability: str, payload: Dict,
                       agent_id: str = None,
                       timeout_s: float = 30.0) -> Task:
        """Delegate a single task to the best available agent."""
        task = Task(
            id=str(uuid.uuid4())[:10],
            name=capability,
            capability=capability,
            payload=payload,
            assigned_agent=agent_id or "",
            timeout_s=timeout_s,
        )
        return await self._execute_task(task)

    # ── Analytics ─────────────────────────────────────────────────────────────

    def stats(self) -> Dict:
        return {
            **dict(self._stats),
            "agents_registered": len(self._agents),
            "plans_in_history": len(self._history),
        }

    def history(self, limit: int = 20) -> List[Dict]:
        return [r.to_dict() for r in reversed(self._history[-limit:])]

    def register_routes(self, app, prefix: str = ""):
        from aiohttp import web

        async def list_agents_ep(request):
            return web.json_response({"agents": self.list_agents()})

        async def register_agent_ep(request):
            data = await request.json()
            agent = AgentDef(
                id=data.get("id", str(uuid.uuid4())[:10]),
                name=data["name"],
                capabilities=set(data.get("capabilities", [])),
                transport=AgentTransport(data.get("transport", "in_process")),
                endpoint=data.get("endpoint", ""),
                timeout_s=float(data.get("timeout_s", 30)),
                metadata=data.get("metadata", {}),
            )
            self.register(agent)
            return web.json_response(agent.to_dict(), status=201)

        async def delegate_ep(request):
            data = await request.json()
            task = await self.delegate(
                capability=data["capability"],
                payload=data.get("payload", {}),
                agent_id=data.get("agent_id"),
                timeout_s=float(data.get("timeout_s", 30)),
            )
            return web.json_response(task.to_dict())

        async def fan_out_ep(request):
            data = await request.json()
            result = await self.fan_out(
                capability=data["capability"],
                payload=data.get("payload", {}),
                agent_ids=data.get("agent_ids"),
                merge=MergeStrategy(data.get("merge", "all")),
                timeout_s=float(data.get("timeout_s", 30)),
            )
            return web.json_response(result.to_dict())

        async def stats_ep(request):
            return web.json_response(self.stats())

        async def history_ep(request):
            limit = int(request.rel_url.query.get("limit", 20))
            return web.json_response({"history": self.history(limit)})

        app.router.add_get( f"{prefix}/federation/agents",      list_agents_ep)
        app.router.add_post(f"{prefix}/federation/agents",      register_agent_ep)
        app.router.add_post(f"{prefix}/federation/delegate",    delegate_ep)
        app.router.add_post(f"{prefix}/federation/fan_out",     fan_out_ep)
        app.router.add_get( f"{prefix}/federation/stats",       stats_ep)
        app.router.add_get( f"{prefix}/federation/history",     history_ep)
        logger.info(f"Federation API routes registered at {prefix}/federation/")
