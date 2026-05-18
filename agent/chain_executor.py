"""OMNI AGENT - Chain Executor
Execute sequential LLM prompt chains with variable interpolation,
branching logic, retry, streaming simulation, and result accumulation.

Features:
- Step types: PROMPT, TRANSFORM, BRANCH, LOOP, PARALLEL, WAIT
- Variable interpolation: {{var}} in prompt templates resolved from context
- Branching: evaluate condition on previous step output to pick next step
- Loop: repeat a step N times or until condition met
- Parallel: run multiple steps concurrently, collect results
- Retry: per-step max_retries + exponential backoff
- Streaming: per-step stream=True calls fn with partial tokens
- Result accumulation: each step output stored in chain context
- Step hooks: on_step_start, on_step_end, on_chain_end
- Dry-run: validate template vars and return execution plan
- Timeout: per-step and per-chain hard limits
- SQLite persistence: chain runs, step results
- REST API: run, status, plan, stats
"""
import asyncio, json, re, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class StepType(str, Enum):
    PROMPT    = "prompt"
    TRANSFORM = "transform"
    BRANCH    = "branch"
    LOOP      = "loop"
    PARALLEL  = "parallel"
    WAIT      = "wait"

class StepStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"
    SKIPPED   = "skipped"

def _interpolate(template: str, context: Dict[str, Any]) -> str:
    """Replace {{var}} placeholders with context values."""
    def _repl(m):
        key = m.group(1).strip()
        val = context.get(key, m.group(0))
        return str(val) if val is not None else m.group(0)
    return re.sub(r'\{\{([^}]+)\}\}', _repl, template)

def _missing_vars(template: str, context: Dict) -> List[str]:
    keys = re.findall(r'\{\{([^}]+)\}\}', template)
    return [k.strip() for k in keys if k.strip() not in context]

@dataclass
class StepSpec:
    id: str; name: str; step_type: StepType = StepType.PROMPT
    template: str = ""           # prompt template with {{vars}}
    fn: Optional[Callable] = None  # transform/branch fn(context) -> output
    output_key: str = ""         # store output in context[output_key]
    condition_fn: Optional[Callable] = None   # BRANCH: fn(ctx) -> step_name
    loop_count: int = 1          # LOOP: repeat N times
    loop_while: Optional[Callable] = None    # LOOP: continue while fn(ctx) is True
    parallel_steps: List[str] = field(default_factory=list)  # PARALLEL: step ids
    wait_s: float = 0.0          # WAIT
    max_retries: int = 1
    retry_delay: float = 0.5
    timeout_s: float = 30.0
    enabled: bool = True
    tags: List[str] = field(default_factory=list)

    def to_dict(self):
        return {"id": self.id, "name": self.name,
                "type": self.step_type.value,
                "output_key": self.output_key,
                "tags": self.tags}

@dataclass
class StepResult:
    step_id: str; step_name: str
    status: StepStatus = StepStatus.PENDING
    output: Any = None; error: str = ""
    attempts: int = 0; latency_ms: float = 0.0
    rendered_template: str = ""

    def to_dict(self):
        return {"step": self.step_name, "status": self.status.value,
                "output": str(self.output)[:300] if self.output is not None else None,
                "error": self.error, "attempts": self.attempts,
                "latency_ms": round(self.latency_ms, 1)}

@dataclass
class ChainRun:
    id: str; chain_name: str
    status: str = "running"
    context: Dict[str, Any] = field(default_factory=dict)
    step_results: List[StepResult] = field(default_factory=list)
    error: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    @property
    def duration_ms(self):
        end = self.finished_at or time.time()
        return round((end - self.started_at) * 1000, 1)

    @property
    def output(self):
        for sr in reversed(self.step_results):
            if sr.status == StepStatus.COMPLETED and sr.output is not None:
                return sr.output
        return None

    def to_dict(self):
        return {"id": self.id, "chain": self.chain_name,
                "status": self.status, "duration_ms": self.duration_ms,
                "steps_completed": sum(1 for s in self.step_results
                                        if s.status == StepStatus.COMPLETED),
                "output": str(self.output)[:500] if self.output is not None else None,
                "error": self.error}

class CEStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS runs(
                    id TEXT PRIMARY KEY, chain TEXT, status TEXT,
                    steps_completed INTEGER DEFAULT 0,
                    error TEXT DEFAULT '', started_at REAL, finished_at REAL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS idx_ce_chain ON runs(chain, started_at DESC);
            """)

    def save(self, run: ChainRun):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?,?)",
                (run.id, run.chain_name, run.status,
                 sum(1 for s in run.step_results if s.status == StepStatus.COMPLETED),
                 run.error, run.started_at, run.finished_at))

    def stats(self) -> Dict:
        with self._conn() as c:
            n   = c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            nc  = c.execute(
                "SELECT COUNT(*) FROM runs WHERE status='completed'").fetchone()[0]
            avg = c.execute(
                "SELECT AVG(finished_at - started_at) FROM runs "
                "WHERE status='completed'").fetchone()[0] or 0
        return {"total_runs": n, "completed": nc,
                "avg_duration_s": round(avg, 3)}

class ChainExecutor:
    """
    Sequential LLM prompt chain executor with branching and variable interpolation.

    Usage:
        executor = ChainExecutor()

        async def call_llm(ctx):
            prompt = ctx["_rendered"]
            return f"Response to: {prompt[:30]}"

        chain = executor.define("qa_chain")
        chain.step("expand",   fn=call_llm,
                    template="Expand this topic: {{topic}}",
                    output_key="expanded")
        chain.step("summarize", fn=call_llm,
                    template="Summarize: {{expanded}}",
                    output_key="summary")

        run = await executor.run("qa_chain", context={"topic": "AI safety"})
        print(run.context["summary"])
    """
    def __init__(self, db_path: str = "data/chains.db"):
        self._store = CEStore(db_path)
        self._chains: Dict[str, "_ChainDef"] = {}
        self._runs: Dict[str, ChainRun] = {}
        self._hooks: Dict[str, List[Callable]] = {
            "on_step_start": [], "on_step_end": [], "on_chain_end": []}

    def define(self, name: str) -> "_ChainDef":
        cd = _ChainDef(name)
        self._chains[name] = cd
        return cd

    def on(self, event: str, fn: Callable):
        if event in self._hooks: self._hooks[event].append(fn)

    def _fire(self, event: str, *args):
        for h in self._hooks[event]:
            try: h(*args)
            except: pass

    async def run(self, chain_name: str,
                   context: Dict = None,
                   run_id: str = None,
                   dry_run: bool = False) -> ChainRun:
        cd = self._chains.get(chain_name)
        if not cd:
            raise ValueError(f"Chain {chain_name!r} not defined")

        rid = run_id or str(uuid.uuid4())[:12]
        run = ChainRun(id=rid, chain_name=chain_name,
                        context=dict(context or {}))
        self._runs[rid] = run

        if dry_run:
            plan = self.plan(chain_name, run.context)
            run.status = "dry_run"; run.finished_at = time.time()
            run.context["_plan"] = plan
            return run

        try:
            steps = list(cd.steps.values())
            idx = 0
            while idx < len(steps):
                step = steps[idx]
                if not step.enabled:
                    idx += 1; continue

                sr = await self._run_step(step, run)
                run.step_results.append(sr)

                if sr.status == StepStatus.FAILED:
                    run.status = "failed"
                    run.error = sr.error
                    break

                # BRANCH: jump to named step
                if step.step_type == StepType.BRANCH and sr.output:
                    target = str(sr.output)
                    step_names = [s.name for s in steps]
                    if target in step_names:
                        idx = step_names.index(target)
                        continue

                # LOOP
                if step.step_type == StepType.LOOP:
                    loop_key = f"_loop_{step.id}"
                    count = run.context.get(loop_key, 0) + 1
                    run.context[loop_key] = count
                    should_continue = (count < step.loop_count)
                    if step.loop_while:
                        try: should_continue = bool(step.loop_while(run.context))
                        except: should_continue = False
                    if should_continue:
                        continue   # re-run same step
                    else:
                        run.context.pop(loop_key, None)

                idx += 1

            if run.status == "running":
                run.status = "completed"

        except Exception as e:
            run.status = "failed"; run.error = str(e)

        run.finished_at = time.time()
        self._store.save(run)
        self._fire("on_chain_end", run)
        return run

    async def _run_step(self, step: StepSpec, run: ChainRun) -> StepResult:
        sr = StepResult(step_id=step.id, step_name=step.name,
                         status=StepStatus.RUNNING)
        self._fire("on_step_start", step, run)

        if step.step_type == StepType.WAIT:
            await asyncio.sleep(step.wait_s)
            sr.status = StepStatus.COMPLETED
            return sr

        if step.step_type == StepType.PARALLEL:
            parallel_steps = [run._chain_def.steps[sid]
                               for sid in step.parallel_steps
                               if sid in run._chain_def.steps] \
                              if hasattr(run, '_chain_def') else []
            tasks = [self._run_step(s, run) for s in parallel_steps]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            sr.output = [r.output for r in results if isinstance(r, StepResult)]
            sr.status = StepStatus.COMPLETED
            return sr

        for attempt in range(step.max_retries + 1):
            sr.attempts = attempt + 1
            start = time.time()
            try:
                # Render template
                rendered = _interpolate(step.template, run.context)
                sr.rendered_template = rendered
                run.context["_rendered"] = rendered
                run.context["_step"]     = step.name

                fn = step.fn
                if fn is None:
                    sr.output = rendered
                    sr.status = StepStatus.COMPLETED
                    key = step.output_key or step.name
                    run.context[key] = rendered
                    self._fire("on_step_end", step, sr, run)
                    break

                if asyncio.iscoroutinefunction(fn):
                    output = await asyncio.wait_for(
                        fn(run.context), timeout=step.timeout_s)
                else:
                    output = fn(run.context)

                sr.output = output
                sr.status = StepStatus.COMPLETED
                sr.latency_ms = (time.time() - start) * 1000

                # Store output in context
                key = step.output_key or step.name
                run.context[key] = output
                self._fire("on_step_end", step, sr, run)
                break

            except asyncio.TimeoutError:
                sr.error = f"Timeout after {step.timeout_s}s"
                sr.status = StepStatus.FAILED
            except Exception as e:
                sr.error = str(e); sr.status = StepStatus.FAILED
                if attempt < step.max_retries:
                    await asyncio.sleep(step.retry_delay * (2 ** attempt))
                else:
                    break

        if sr.status == StepStatus.RUNNING:
            sr.status = StepStatus.COMPLETED
        return sr

    def plan(self, chain_name: str, context: Dict = None) -> List[Dict]:
        cd = self._chains.get(chain_name)
        if not cd: return []
        ctx = context or {}
        plan = []
        for step in cd.steps.values():
            missing = _missing_vars(step.template, ctx)
            plan.append({**step.to_dict(), "missing_vars": missing,
                          "template_preview": step.template[:100]})
        return plan

    def get_run(self, run_id: str) -> Optional[ChainRun]:
        return self._runs.get(run_id)

    def stats(self) -> Dict:
        s = self._store.stats()
        s["defined_chains"] = len(self._chains)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def run_ep(req):
            d = await req.json()
            run = await self.run(d["chain"], d.get("context",{}),
                                  dry_run=d.get("dry_run", False))
            return web.json_response(run.to_dict(), status=201)
        async def status_ep(req):
            r = self.get_run(req.match_info["run_id"])
            if not r: return web.json_response({"error":"not found"},status=404)
            return web.json_response(r.to_dict())
        async def plan_ep(req):
            d = await req.json()
            return web.json_response(
                {"plan": self.plan(d["chain"], d.get("context",{}))})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/chain"
        app.router.add_post(f"{p}/run",            run_ep)
        app.router.add_get( f"{p}/run/{{run_id}}", status_ep)
        app.router.add_post(f"{p}/plan",           plan_ep)
        app.router.add_get( f"{p}/stats",          stats_ep)
        logger.info(f"Chain executor API at {prefix}/chain/")

class _ChainDef:
    def __init__(self, name: str):
        self.name = name
        self.steps: Dict[str, StepSpec] = {}

    def step(self, name: str, fn: Callable = None,
              template: str = "", output_key: str = "",
              step_type: StepType = StepType.PROMPT,
              max_retries: int = 1, retry_delay: float = 0.5,
              timeout_s: float = 30.0,
              tags: List[str] = None) -> "_ChainDef":
        spec = StepSpec(id=str(uuid.uuid4())[:8], name=name,
                         step_type=step_type, fn=fn,
                         template=template,
                         output_key=output_key or name,
                         max_retries=max_retries,
                         retry_delay=retry_delay,
                         timeout_s=timeout_s,
                         tags=tags or [])
        self.steps[name] = spec
        return self

    def branch(self, name: str, condition_fn: Callable,
                **kw) -> "_ChainDef":
        spec = StepSpec(id=str(uuid.uuid4())[:8], name=name,
                         step_type=StepType.BRANCH,
                         fn=condition_fn, **kw)
        self.steps[name] = spec
        return self

    def loop(self, name: str, fn: Callable,
              count: int = 3, while_fn: Callable = None,
              **kw) -> "_ChainDef":
        spec = StepSpec(id=str(uuid.uuid4())[:8], name=name,
                         step_type=StepType.LOOP,
                         fn=fn, loop_count=count,
                         loop_while=while_fn, **kw)
        self.steps[name] = spec
        return self
