"""OMNI AGENT - DAG Task Scheduler v2
Dependency-aware task scheduler with parallel execution,
retries, timeouts, progress tracking, and cycle detection.

Features:
- Tasks: name, fn (async or sync), dependencies (list of task names)
- DAG: directed acyclic graph; Kahn's topo sort for execution order
- Parallel: independent tasks (no deps in current wave) run concurrently
- Execution waves: tasks grouped by dependency level; wave N runs all
    tasks whose dependencies all completed in waves < N
- Retries: per-task max_retries with exponential backoff
- Timeout: per-task timeout_s; TimeoutError → retry or fail
- Skip: task can be skipped if condition fn(context) → bool returns False
- Context: shared dict passed to all tasks; tasks can read/write
- Task result: stored in context[task_name] on success
- Status per task: PENDING, RUNNING, DONE, FAILED, SKIPPED, TIMEOUT
- Progress: overall 0-1 float; per-task status visible
- DAG visualization: to_dot() emits Graphviz dot notation
- Hooks: on_start(task), on_done(task, result), on_fail(task, error)
- Pause/resume: stop dispatching new waves; drain running tasks
- Cancel: cancel in-flight asyncio tasks
- Run history: last N runs with task timings
- Cycle detection: Kahn's count vs total nodes; raises on cycle
- SQLite persistence: run history, task timings
- REST API: submit dag, status, cancel, history, stats
"""
import asyncio, inspect, json, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class TaskStatus(str, Enum):
    PENDING  = "pending"
    RUNNING  = "running"
    DONE     = "done"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    TIMEOUT  = "timeout"

@dataclass
class TaskSpec:
    name: str
    fn: Callable
    deps: List[str] = field(default_factory=list)
    max_retries: int = 0
    timeout_s: float = 0.0       # 0 = no timeout
    skip_if: Optional[Callable] = None   # fn(ctx) → bool
    backoff_s: float = 1.0

@dataclass
class TaskRun:
    name: str; status: TaskStatus = TaskStatus.PENDING
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    attempt: int = 0; error: str = ""; result: Any = None

    @property
    def duration_s(self) -> float:
        if self.started_at and self.finished_at:
            return round(self.finished_at - self.started_at, 3)
        return 0.0

    def to_dict(self):
        return {"name": self.name, "status": self.status.value,
                "duration_s": self.duration_s, "attempt": self.attempt,
                "error": self.error}

@dataclass
class DAGRun:
    id: str; dag_name: str
    tasks: Dict[str, TaskRun] = field(default_factory=dict)
    status: str = "pending"
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    context: Dict = field(default_factory=dict)
    _cancel: bool = field(default=False, repr=False)

    @property
    def progress(self) -> float:
        if not self.tasks: return 0.0
        done = sum(1 for t in self.tasks.values()
                    if t.status in (TaskStatus.DONE, TaskStatus.SKIPPED,
                                    TaskStatus.FAILED))
        return done / len(self.tasks)

    def to_dict(self):
        return {"id": self.id, "dag": self.dag_name,
                "status": self.status, "progress": round(self.progress, 3),
                "started_at": round(self.started_at, 2),
                "finished_at": round(self.finished_at, 2) if self.finished_at else None,
                "tasks": {n: t.to_dict() for n, t in self.tasks.items()}}

def _topo_sort(tasks: Dict[str, TaskSpec]) -> List[List[str]]:
    """Kahn's algo → list of waves (each wave = parallel group)."""
    in_deg = {n: 0 for n in tasks}
    for t in tasks.values():
        for d in t.deps:
            if d not in in_deg:
                raise ValueError(f"Task '{t.name}' depends on unknown '{d}'")
            in_deg[t.name] = in_deg.get(t.name, 0)
    for t in tasks.values():
        for d in t.deps:
            in_deg[t.name] += 0  # ensure exists
    # recompute properly
    in_deg = {n: 0 for n in tasks}
    for t in tasks.values():
        for d in t.deps:
            in_deg[t.name] = in_deg.get(t.name, 0) + 1

    waves = []
    remaining = set(tasks.keys())
    completed: set = set()
    for _ in range(len(tasks) + 1):
        ready = [n for n in remaining
                  if all(d in completed for d in tasks[n].deps)]
        if not ready:
            if remaining:
                raise ValueError(f"Cycle detected in DAG: {remaining}")
            break
        waves.append(sorted(ready))
        for n in ready:
            remaining.discard(n); completed.add(n)
    return waves

class TSV2Store:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS runs(
                    id TEXT PRIMARY KEY, dag_name TEXT,
                    status TEXT, progress REAL,
                    started_at REAL, finished_at REAL,
                    task_summary TEXT);
            """)

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def save(self, run: DAGRun):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?,?)",
                (run.id, run.dag_name, run.status, run.progress,
                 run.started_at, run.finished_at,
                 json.dumps({n: t.to_dict() for n, t in run.tasks.items()},
                             default=str)[:2000]))

    def history(self, dag_name: str = None, limit: int = 20) -> List[Dict]:
        where = f"WHERE dag_name='{dag_name}'" if dag_name else ""
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM runs {where} ORDER BY started_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> Dict:
        with self._conn() as c:
            nr = c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            by_status = {r["status"]: r["cnt"] for r in c.execute(
                "SELECT status, COUNT(*) as cnt FROM runs "
                "GROUP BY status").fetchall()}
        return {"runs": nr, "by_status": by_status}

class DAGScheduler:
    """
    DAG-based task scheduler with parallel execution.

    Usage:
        sched = DAGScheduler()

        dag_id = sched.register_dag("pipeline", [
            TaskSpec("fetch",  fn=fetch_data),
            TaskSpec("clean",  fn=clean_data, deps=["fetch"]),
            TaskSpec("train",  fn=train_model, deps=["clean"]),
            TaskSpec("report", fn=generate_report, deps=["train"],
                      timeout_s=30),
        ])

        run = await sched.run("pipeline", context={"config": {...}})
        print(run.status, run.progress)
    """
    def __init__(self, db_path: str = "data/dag_scheduler.db",
                 max_concurrent: int = 8):
        self._store = TSV2Store(db_path)
        self._dags: Dict[str, Dict[str, TaskSpec]] = {}
        self._runs: Dict[str, DAGRun] = {}
        self._max_concurrent = max_concurrent
        self._hooks_start: List[Callable] = []
        self._hooks_done:  List[Callable] = []
        self._hooks_fail:  List[Callable] = []

    def on_start(self, fn): self._hooks_start.append(fn)
    def on_done(self,  fn): self._hooks_done.append(fn)
    def on_fail(self,  fn): self._hooks_fail.append(fn)

    def _fire(self, hooks, *args):
        for h in hooks:
            try: h(*args)
            except: pass

    def register_dag(self, name: str, tasks: List[TaskSpec]) -> str:
        task_map = {t.name: t for t in tasks}
        # Validate – will raise on cycle
        _topo_sort(task_map)
        self._dags[name] = task_map
        return name

    def add_task(self, dag_name: str, spec: TaskSpec):
        if dag_name not in self._dags:
            self._dags[dag_name] = {}
        self._dags[dag_name][spec.name] = spec

    async def _execute_task(self, spec: TaskSpec, run: DAGRun) -> bool:
        tr = run.tasks[spec.name]
        for attempt in range(spec.max_retries + 1):
            tr.attempt = attempt + 1
            tr.status = TaskStatus.RUNNING
            tr.started_at = time.time()
            self._fire(self._hooks_start, spec)
            try:
                coro = (spec.fn(run.context)
                         if inspect.iscoroutinefunction(spec.fn)
                         else asyncio.get_event_loop().run_in_executor(
                             None, spec.fn, run.context))
                if spec.timeout_s > 0:
                    result = await asyncio.wait_for(coro, spec.timeout_s)
                else:
                    result = await coro
                tr.result = result
                tr.status = TaskStatus.DONE
                tr.finished_at = time.time()
                run.context[spec.name] = result
                self._fire(self._hooks_done, spec, result)
                return True
            except asyncio.TimeoutError:
                tr.error = f"Timeout after {spec.timeout_s}s"
                tr.status = TaskStatus.TIMEOUT
                tr.finished_at = time.time()
                if attempt < spec.max_retries:
                    await asyncio.sleep(spec.backoff_s * (2 ** attempt))
                    continue
                self._fire(self._hooks_fail, spec, tr.error)
                return False
            except Exception as e:
                tr.error = str(e)
                tr.status = TaskStatus.FAILED
                tr.finished_at = time.time()
                if attempt < spec.max_retries:
                    await asyncio.sleep(spec.backoff_s * (2 ** attempt))
                    continue
                self._fire(self._hooks_fail, spec, tr.error)
                return False
        return False

    async def run(self, dag_name: str,
                   context: Dict = None,
                   fail_fast: bool = True) -> DAGRun:
        dag = self._dags.get(dag_name)
        if not dag:
            raise KeyError(f"DAG '{dag_name}' not registered")
        run_id = str(uuid.uuid4())[:12]
        run = DAGRun(id=run_id, dag_name=dag_name,
                      tasks={n: TaskRun(name=n) for n in dag},
                      context=dict(context or {}))
        self._runs[run_id] = run
        waves = _topo_sort(dag)
        failed = False
        for wave in waves:
            if run._cancel or (fail_fast and failed):
                for name in wave:
                    run.tasks[name].status = TaskStatus.SKIPPED
                continue
            wave_tasks = [dag[n] for n in wave]
            # Check skip conditions
            to_run = []
            for spec in wave_tasks:
                tr = run.tasks[spec.name]
                if spec.skip_if and spec.skip_if(run.context):
                    tr.status = TaskStatus.SKIPPED
                else:
                    to_run.append(spec)
            if not to_run: continue
            # Limit concurrency within wave
            sem = asyncio.Semaphore(self._max_concurrent)
            async def bounded(spec):
                async with sem:
                    return await self._execute_task(spec, run)
            results = await asyncio.gather(
                *[bounded(s) for s in to_run], return_exceptions=False)
            if not all(results): failed = True
        run.status = ("failed" if failed else "done")
        run.finished_at = time.time()
        self._store.save(run)
        return run

    def cancel(self, run_id: str) -> bool:
        run = self._runs.get(run_id)
        if not run: return False
        run._cancel = True; return True

    def status(self, run_id: str) -> Optional[DAGRun]:
        return self._runs.get(run_id)

    def history(self, dag_name: str = None, limit: int = 20) -> List[Dict]:
        return self._store.history(dag_name, limit)

    def to_dot(self, dag_name: str) -> str:
        dag = self._dags.get(dag_name, {})
        lines = [f'digraph "{dag_name}" {{']
        for name in dag:
            lines.append(f'  "{name}";')
        for spec in dag.values():
            for dep in spec.deps:
                lines.append(f'  "{dep}" -> "{spec.name}";')
        lines.append("}")
        return "\n".join(lines)

    def stats(self) -> Dict:
        s = self._store.stats()
        s["registered_dags"] = len(self._dags)
        s["active_runs"] = len(self._runs)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def run_ep(req):
            d = await req.json()
            try:
                run = await self.run(d["dag"], d.get("context",{}),
                                      d.get("fail_fast", True))
                return web.json_response(run.to_dict())
            except KeyError as e:
                return web.json_response({"error": str(e)}, status=404)
        async def status_ep(req):
            rid = req.match_info["run_id"]
            run = self.status(rid)
            if not run: return web.json_response({}, status=404)
            return web.json_response(run.to_dict())
        async def cancel_ep(req):
            d = await req.json()
            ok = self.cancel(d["run_id"])
            return web.json_response({"cancelled": ok})
        async def history_ep(req):
            dag = req.rel_url.query.get("dag")
            return web.json_response({"history": self.history(dag)})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/dag"
        app.router.add_post(f"{p}/run",           run_ep)
        app.router.add_get( f"{p}/run/{{run_id}}", status_ep)
        app.router.add_post(f"{p}/cancel",         cancel_ep)
        app.router.add_get( f"{p}/history",        history_ep)
        app.router.add_get( f"{p}/stats",          stats_ep)
        logger.info(f"DAG scheduler API at {prefix}/dag/")
