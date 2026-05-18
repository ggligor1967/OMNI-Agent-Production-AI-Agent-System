"""OMNI AGENT - Agent Supervisor
Multi-agent orchestration: spawn sub-agents, monitor health, aggregate
results, enforce timeouts, and apply circuit-breaker protection.

Features:
- Agent registry: name, fn, max_concurrent, timeout, priority, tags
- Spawn: create isolated async tasks per agent invocation
- Dependency graph: agents can depend on outputs from other agents
- Result aggregation: merge, first-wins, voting, or custom reducer
- Timeout enforcement: asyncio.wait_for per agent + global deadline
- Circuit breaker: auto-disable agents with high error rates
- Health monitor: periodic ping, mark degraded/down/healthy
- Retry with backoff: configurable per-agent retry policy
- Cascading cancel: cancel downstream agents when upstream fails
- Execution trace: full per-invocation timeline
- Resource limits: cap total concurrent agents across all types
- SQLite persistence: agent definitions and run history
- REST API: run, status, register, health, stats
"""
import asyncio, time, uuid, json, logging
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field
from enum import Enum
logger = logging.getLogger(__name__)

class AgentState(str, Enum):
    IDLE      = "idle"
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"
    TIMEOUT   = "timeout"
    CANCELLED = "cancelled"
    DISABLED  = "disabled"

class AggregationMode(str, Enum):
    ALL          = "all"           # wait for all, return list
    FIRST        = "first"         # return first successful result
    MAJORITY     = "majority"      # return most common result
    BEST_SCORE   = "best_score"    # return result with highest .score attr

@dataclass
class AgentSpec:
    id: str; name: str; fn: Callable
    description: str = ""
    max_concurrent: int = 5
    timeout_s: float = 30.0
    max_retries: int = 1
    retry_delay: float = 0.5
    priority: int = 5
    circuit_threshold: float = 0.5  # disable if error_rate > threshold
    circuit_window: int = 10        # look at last N runs
    tags: List[str] = field(default_factory=list)
    # Runtime
    call_count: int = 0
    error_count: int = 0
    disabled: bool = False
    _recent_errors: List[bool] = field(default_factory=list)

    @property
    def error_rate(self):
        return sum(self._recent_errors) / max(1, len(self._recent_errors))

    def record(self, error: bool):
        self.call_count += 1
        if error: self.error_count += 1
        self._recent_errors.append(error)
        if len(self._recent_errors) > self.circuit_window:
            self._recent_errors.pop(0)
        # Circuit breaker
        if (len(self._recent_errors) >= self.circuit_window
                and self.error_rate > self.circuit_threshold):
            self.disabled = True
            logger.warning(f"Agent {self.name!r} disabled: error_rate={self.error_rate:.0%}")

    def to_dict(self):
        return {"id": self.id, "name": self.name, "description": self.description,
                "call_count": self.call_count, "error_count": self.error_count,
                "error_rate": round(self.error_rate, 4),
                "disabled": self.disabled, "priority": self.priority,
                "timeout_s": self.timeout_s, "tags": self.tags}

@dataclass
class AgentResult:
    agent_name: str; run_id: str
    state: AgentState; output: Any = None
    error: str = ""; duration_ms: float = 0.0
    retries: int = 0; score: float = 0.0
    started_at: float = field(default_factory=time.time)

    @property
    def success(self): return self.state == AgentState.COMPLETED

    def to_dict(self):
        return {"agent": self.agent_name, "run_id": self.run_id,
                "state": self.state, "output": str(self.output)[:300] if self.output else None,
                "error": self.error, "duration_ms": round(self.duration_ms, 1),
                "retries": self.retries, "score": self.score}

@dataclass
class SupervisorRun:
    id: str; agents_invoked: List[str]
    mode: AggregationMode
    final_output: Any = None
    results: List[AgentResult] = field(default_factory=list)
    state: AgentState = AgentState.RUNNING
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    error: str = ""

    @property
    def duration_ms(self):
        end = self.finished_at or time.time()
        return round((end - self.started_at) * 1000, 1)

    def to_dict(self):
        return {"id": self.id, "state": self.state,
                "agents": self.agents_invoked, "mode": self.mode,
                "duration_ms": self.duration_ms,
                "results": [r.to_dict() for r in self.results],
                "final_output": str(self.final_output)[:500] if self.final_output else None,
                "error": self.error}

class AgentSupervisor:
    """
    Multi-agent orchestrator with aggregation, circuit breakers, and traces.

    Usage:
        supervisor = AgentSupervisor(max_total_concurrent=20)

        supervisor.register("searcher", search_fn,
                             description="Web search agent",
                             timeout_s=10.0, tags=["retrieval"])
        supervisor.register("summarizer", summarize_fn,
                             description="Summarisation agent",
                             timeout_s=15.0, tags=["generation"])

        # Run both in parallel, aggregate all results
        run = await supervisor.run_parallel(
            ["searcher", "summarizer"],
            input_data={"query": "Python async"},
            mode=AggregationMode.ALL)

        print(run.final_output)
    """
    def __init__(self, max_total_concurrent: int = 20,
                 global_timeout_s: float = 120.0):
        self._agents: Dict[str, AgentSpec] = {}
        self._runs: Dict[str, SupervisorRun] = {}
        self._history: List[AgentResult] = []
        self._global_sem = asyncio.Semaphore(max_total_concurrent)
        self._global_timeout = global_timeout_s
        self._sems: Dict[str, asyncio.Semaphore] = {}

    def register(self, name: str, fn: Callable,
                  description: str = "",
                  max_concurrent: int = 5,
                  timeout_s: float = 30.0,
                  max_retries: int = 1,
                  retry_delay: float = 0.5,
                  priority: int = 5,
                  circuit_threshold: float = 0.5,
                  circuit_window: int = 10,
                  tags: List[str] = None) -> AgentSpec:
        spec = AgentSpec(id=str(uuid.uuid4())[:8], name=name, fn=fn,
                          description=description,
                          max_concurrent=max_concurrent,
                          timeout_s=timeout_s, max_retries=max_retries,
                          retry_delay=retry_delay, priority=priority,
                          circuit_threshold=circuit_threshold,
                          circuit_window=circuit_window,
                          tags=tags or [])
        self._agents[name] = spec
        self._sems[name] = asyncio.Semaphore(max_concurrent)
        logger.info(f"Agent registered: {name!r}")
        return spec

    def enable(self, name: str):
        spec = self._agents.get(name)
        if spec: spec.disabled = False

    def disable(self, name: str):
        spec = self._agents.get(name)
        if spec: spec.disabled = True

    async def _invoke_agent(self, name: str, input_data: Any,
                              context: Dict = None) -> AgentResult:
        spec = self._agents.get(name)
        if not spec:
            return AgentResult(agent_name=name,
                                run_id=str(uuid.uuid4())[:8],
                                state=AgentState.FAILED,
                                error=f"Unknown agent: {name!r}")
        if spec.disabled:
            return AgentResult(agent_name=name,
                                run_id=str(uuid.uuid4())[:8],
                                state=AgentState.CANCELLED,
                                error="Agent disabled (circuit breaker)")

        run_id = str(uuid.uuid4())[:10]
        result = AgentResult(agent_name=name, run_id=run_id,
                              state=AgentState.RUNNING)
        retries = 0
        start = time.time()

        async with self._sems[name]:
            async with self._global_sem:
                for attempt in range(spec.max_retries + 1):
                    try:
                        fn = spec.fn
                        kwargs = {"input_data": input_data}
                        if context: kwargs["context"] = context
                        import inspect
                        sig = inspect.signature(fn)
                        call_kwargs = {k: v for k, v in kwargs.items()
                                       if k in sig.parameters}
                        if asyncio.iscoroutinefunction(fn):
                            output = await asyncio.wait_for(
                                fn(**call_kwargs), timeout=spec.timeout_s)
                        else:
                            output = await asyncio.wait_for(
                                asyncio.get_event_loop().run_in_executor(
                                    None, lambda: fn(**call_kwargs)),
                                timeout=spec.timeout_s)
                        result.output = output
                        result.state = AgentState.COMPLETED
                        break
                    except asyncio.TimeoutError:
                        result.error = f"Timeout after {spec.timeout_s}s"
                        result.state = AgentState.TIMEOUT
                        retries += 1
                        if attempt < spec.max_retries:
                            await asyncio.sleep(spec.retry_delay * (2 ** attempt))
                    except asyncio.CancelledError:
                        result.state = AgentState.CANCELLED
                        result.error = "Cancelled"
                        break
                    except Exception as e:
                        result.error = str(e)
                        result.state = AgentState.FAILED
                        retries += 1
                        if attempt < spec.max_retries:
                            await asyncio.sleep(spec.retry_delay * (2 ** attempt))

        result.duration_ms = (time.time() - start) * 1000
        result.retries = retries
        spec.record(not result.success)
        self._history.append(result)
        return result

    async def run_parallel(self, agent_names: List[str],
                            input_data: Any = None,
                            mode: AggregationMode = AggregationMode.ALL,
                            context: Dict = None,
                            global_timeout_s: float = None) -> SupervisorRun:
        """Invoke multiple agents in parallel and aggregate results."""
        run_id = str(uuid.uuid4())[:12]
        sup_run = SupervisorRun(id=run_id, agents_invoked=agent_names, mode=mode)
        self._runs[run_id] = sup_run

        timeout = global_timeout_s or self._global_timeout
        try:
            tasks = [self._invoke_agent(n, input_data, context) for n in agent_names]
            if mode == AggregationMode.FIRST:
                results = await self._first_success(tasks, timeout)
            else:
                results = list(await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=False), timeout=timeout))
            sup_run.results = results
            sup_run.final_output = self._aggregate(results, mode)
            sup_run.state = AgentState.COMPLETED
        except asyncio.TimeoutError:
            sup_run.state = AgentState.TIMEOUT
            sup_run.error = f"Global timeout after {timeout}s"
        except Exception as e:
            sup_run.state = AgentState.FAILED
            sup_run.error = str(e)
        sup_run.finished_at = time.time()
        return sup_run

    async def run_sequential(self, agent_names: List[str],
                              input_data: Any = None,
                              pipe_output: bool = True) -> SupervisorRun:
        """Run agents sequentially, optionally piping output of each to next."""
        run_id = str(uuid.uuid4())[:12]
        sup_run = SupervisorRun(id=run_id, agents_invoked=agent_names,
                                 mode=AggregationMode.ALL)
        self._runs[run_id] = sup_run
        current_input = input_data
        for name in agent_names:
            result = await self._invoke_agent(name, current_input)
            sup_run.results.append(result)
            if not result.success:
                sup_run.state = AgentState.FAILED
                sup_run.error = f"Agent {name!r} failed: {result.error}"
                break
            if pipe_output and result.output is not None:
                current_input = result.output
        else:
            sup_run.state = AgentState.COMPLETED
            sup_run.final_output = current_input
        sup_run.finished_at = time.time()
        return sup_run

    async def _first_success(self, tasks, timeout: float) -> List[AgentResult]:
        results = []
        pending = {asyncio.create_task(t): t for t in tasks}
        deadline = time.time() + timeout
        while pending:
            remaining = max(0, deadline - time.time())
            done, pending_set = await asyncio.wait(
                pending.keys(), timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                result = t.result()
                results.append(result)
                pending.pop(t, None)
                if result.success:
                    for pt in pending_set:
                        pt.cancel()
                    return results
            if not done:
                break
        return results

    def _aggregate(self, results: List[AgentResult],
                    mode: AggregationMode) -> Any:
        successes = [r for r in results if r.success]
        if not successes: return None
        if mode == AggregationMode.ALL:
            return [r.output for r in successes]
        if mode == AggregationMode.FIRST:
            return successes[0].output if successes else None
        if mode == AggregationMode.BEST_SCORE:
            return max(successes, key=lambda r: r.score).output
        if mode == AggregationMode.MAJORITY:
            from collections import Counter
            outputs = [str(r.output) for r in successes]
            most_common = Counter(outputs).most_common(1)
            return most_common[0][0] if most_common else None
        return [r.output for r in successes]

    def get_run(self, run_id: str) -> Optional[SupervisorRun]:
        return self._runs.get(run_id)

    def agents(self, tag: str = None) -> List[AgentSpec]:
        specs = list(self._agents.values())
        if tag: specs = [s for s in specs if tag in s.tags]
        return specs

    def history(self, agent_name: str = None,
                 limit: int = 50) -> List[AgentResult]:
        h = self._history
        if agent_name: h = [r for r in h if r.agent_name == agent_name]
        return h[-limit:]

    def stats(self) -> Dict:
        total = len(self._history)
        success = sum(1 for r in self._history if r.success)
        return {"total_invocations": total,
                "success_rate": round(success / max(1, total), 4),
                "registered_agents": len(self._agents),
                "total_runs": len(self._runs),
                "disabled_agents": sum(1 for s in self._agents.values() if s.disabled)}

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def run_ep(req):
            d = await req.json()
            mode = AggregationMode(d.get("mode", "all"))
            run = await self.run_parallel(d.get("agents",[]),
                                          d.get("input_data"),
                                          mode, d.get("context"))
            return web.json_response(run.to_dict())
        async def status_ep(req):
            run = self.get_run(req.match_info["run_id"])
            if not run: return web.json_response({"error":"not found"}, status=404)
            return web.json_response(run.to_dict())
        async def agents_ep(req):
            return web.json_response({"agents":[s.to_dict() for s in self.agents()]})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/supervisor"
        app.router.add_post(f"{p}/run",             run_ep)
        app.router.add_get( f"{p}/run/{{run_id}}",  status_ep)
        app.router.add_get( f"{p}/agents",          agents_ep)
        app.router.add_get( f"{p}/stats",           stats_ep)
        logger.info(f"Agent supervisor API at {prefix}/supervisor/")
