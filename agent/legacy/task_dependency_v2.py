"""OMNI Agent — Task Dependency V2: DAG tasks with scheduling, critical path, exec."""
from __future__ import annotations
import sqlite3, threading, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set


class TaskState(str, Enum):
    PENDING   = "pending"
    READY     = "ready"       # all deps satisfied
    RUNNING   = "running"
    DONE      = "done"
    FAILED    = "failed"
    SKIPPED   = "skipped"
    BLOCKED   = "blocked"     # dep failed


class ExecStrategy(str, Enum):
    SEQUENTIAL = "sequential"
    PARALLEL   = "parallel"
    WAVE       = "wave"        # execute by depth level


@dataclass
class DependencyTask:
    task_id: str
    name: str
    fn: Callable
    deps: List[str] = field(default_factory=list)   # task_ids
    state: TaskState = TaskState.PENDING
    result: Any = None
    error: Optional[str] = None
    priority: int = 0
    weight: float = 1.0       # for critical path cost
    timeout_s: Optional[float] = None
    max_retries: int = 0
    retries: int = 0
    skip_on_dep_fail: bool = False
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at) * 1000
        return 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "state": self.state.value,
            "deps": self.deps,
            "priority": self.priority,
            "duration_ms": round(self.duration_ms, 2),
            "error": self.error,
        }


@dataclass
class ExecRun:
    run_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    strategy: str = "sequential"
    status: str = "pending"
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    exec_order: List[str] = field(default_factory=list)
    results: Dict[str, Any] = field(default_factory=dict)
    errors: Dict[str, str] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        end = self.finished_at or time.time()
        return (end - self.started_at) * 1000

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "strategy": self.strategy,
            "status": self.status,
            "tasks_run": len(self.exec_order),
            "errors": len(self.errors),
            "duration_ms": round(self.duration_ms, 2),
        }


class TaskDependencyV2:
    """
    Task dependency graph executor:
    - Add tasks with explicit dep lists
    - Topological sort with cycle detection
    - Three execution strategies: sequential / parallel / wave (by depth)
    - Dynamic dep resolution (results passed to dependents)
    - Critical path analysis (longest weighted path)
    - Blocking: skip downstream if dep failed (or use skip_on_dep_fail)
    - Retry per task
    - Timeout per task (thread-based)
    - Priority ordering within same depth level
    - Tag-based task groups
    - Pre/post task hooks
    - Run history
    - SQLite persistence
    """

    def __init__(self, db_path: str = ":memory:"):
        self._tasks: Dict[str, DependencyTask] = {}
        self._runs:  List[ExecRun] = []
        self._pre_hooks:  List[Callable] = []
        self._post_hooks: List[Callable] = []
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS td_tasks (
                task_id TEXT PRIMARY KEY, name TEXT, state TEXT,
                priority INTEGER, duration_ms REAL, error TEXT
            );
            CREATE TABLE IF NOT EXISTS td_runs (
                run_id TEXT PRIMARY KEY, strategy TEXT, status TEXT,
                tasks_run INTEGER, errors INTEGER,
                started_at REAL, finished_at REAL
            );
        """)
        self._db.commit()

    # ── TASK MANAGEMENT ───────────────────────────────────────────────

    def add_task(self, name: str,
                  fn: Callable,
                  deps: Optional[List[str]] = None,
                  priority: int = 0,
                  weight: float = 1.0,
                  timeout_s: Optional[float] = None,
                  max_retries: int = 0,
                  skip_on_dep_fail: bool = False,
                  tags: Optional[List[str]] = None,
                  task_id: Optional[str] = None,
                  metadata: Optional[Dict] = None) -> DependencyTask:
        tid = task_id or str(uuid.uuid4())[:8]
        t   = DependencyTask(
            task_id=tid, name=name, fn=fn,
            deps=list(deps or []),
            priority=priority, weight=weight,
            timeout_s=timeout_s, max_retries=max_retries,
            skip_on_dep_fail=skip_on_dep_fail,
            tags=list(tags or []),
            metadata=metadata or {})
        self._tasks[tid] = t
        return t

    def remove_task(self, task_id: str) -> bool:
        return self._tasks.pop(task_id, None) is not None

    def add_dep(self, task_id: str, dep_id: str):
        t = self._tasks.get(task_id)
        if t and dep_id not in t.deps:
            t.deps.append(dep_id)

    # ── TOPOLOGY ─────────────────────────────────────────────────────

    def _topo_sort(self) -> List[str]:
        in_deg = {tid: 0 for tid in self._tasks}
        for t in self._tasks.values():
            for dep in t.deps:
                if dep in in_deg:
                    in_deg[t.task_id] += 1
        queue = sorted([tid for tid, d in in_deg.items() if d == 0],
                        key=lambda tid: -self._tasks[tid].priority)
        order: List[str] = []
        while queue:
            tid = queue.pop(0)
            order.append(tid)
            for other_tid, t in self._tasks.items():
                if tid in t.deps:
                    in_deg[other_tid] -= 1
                    if in_deg[other_tid] == 0:
                        queue.append(other_tid)
                        queue.sort(key=lambda x: -self._tasks[x].priority)
        if len(order) != len(self._tasks):
            raise ValueError("Cycle detected in task graph")
        return order

    def detect_cycle(self) -> bool:
        try: self._topo_sort(); return False
        except ValueError: return True

    def _depth_levels(self) -> List[List[str]]:
        order  = self._topo_sort()
        depths: Dict[str, int] = {tid: 0 for tid in order}
        for tid in order:
            t = self._tasks[tid]
            for dep in t.deps:
                if dep in depths:
                    depths[tid] = max(depths[tid], depths[dep] + 1)
        max_d = max(depths.values(), default=0)
        levels = [[] for _ in range(max_d + 1)]
        for tid in order:
            levels[depths[tid]].append(tid)
        return levels

    def critical_path(self) -> List[str]:
        order = self._topo_sort()
        cost:  Dict[str, float] = {}
        prev:  Dict[str, Optional[str]] = {}
        for tid in order:
            t     = self._tasks[tid]
            best  = max((cost[dep] for dep in t.deps if dep in cost), default=0.0)
            cost[tid] = best + t.weight
            best_dep = max((dep for dep in t.deps if dep in cost),
                            key=lambda d: cost[d], default=None)
            prev[tid] = best_dep
        end   = max(cost, key=lambda k: cost[k]) if cost else None
        if not end: return []
        path: List[str] = []
        cur: Optional[str] = end
        while cur:
            path.append(cur)
            cur = prev[cur]
        return list(reversed(path))

    # ── EXECUTION ─────────────────────────────────────────────────────

    def _reset(self):
        for t in self._tasks.values():
            t.state       = TaskState.PENDING
            t.result      = None
            t.error       = None
            t.started_at  = None
            t.finished_at = None
            t.retries     = 0

    def run(self, strategy: ExecStrategy = ExecStrategy.SEQUENTIAL,
             context: Optional[Dict] = None) -> ExecRun:
        self._reset()
        ctx = dict(context or {})
        run = ExecRun(strategy=strategy.value, status="running")

        try:
            if strategy == ExecStrategy.PARALLEL:
                self._run_parallel(ctx, run)
            elif strategy == ExecStrategy.WAVE:
                self._run_wave(ctx, run)
            else:
                order = self._topo_sort()
                self._run_sequential(order, ctx, run)
        except ValueError as e:
            run.status = "error"
            run.errors["__cycle__"] = str(e)
            run.finished_at = time.time()
            return run

        run.status      = "done" if not run.errors else "partial"
        run.finished_at = time.time()
        self._runs.append(run)
        self._persist_run(run)
        return run

    def _run_sequential(self, order: List[str],
                         ctx: Dict, run: ExecRun):
        for tid in order:
            self._exec_task(tid, ctx, run)

    def _run_wave(self, ctx: Dict, run: ExecRun):
        for level in self._depth_levels():
            level_sorted = sorted(level,
                                   key=lambda tid: -self._tasks[tid].priority)
            for tid in level_sorted:
                self._exec_task(tid, ctx, run)

    def _run_parallel(self, ctx: Dict, run: ExecRun):
        for level in self._depth_levels():
            threads = [threading.Thread(
                target=self._exec_task,
                args=(tid, ctx, run), daemon=True)
                for tid in level]
            for th in threads: th.start()
            for th in threads: th.join()

    def _exec_task(self, task_id: str, ctx: Dict, run: ExecRun):
        t = self._tasks[task_id]

        # Check dep states
        for dep_id in t.deps:
            dep = self._tasks.get(dep_id)
            if not dep: continue
            if dep.state == TaskState.FAILED:
                if t.skip_on_dep_fail:
                    t.state = TaskState.SKIPPED
                else:
                    t.state = TaskState.BLOCKED
                run.exec_order.append(task_id)
                return
            if dep.state == TaskState.BLOCKED:
                t.state = TaskState.BLOCKED
                run.exec_order.append(task_id)
                return

        for fn in self._pre_hooks:
            try: fn(t)
            except Exception: pass

        t.state      = TaskState.RUNNING
        t.started_at = time.time()

        dep_results = {dep_id: self._tasks[dep_id].result
                       for dep_id in t.deps if dep_id in self._tasks}

        attempt = 0
        while True:
            attempt += 1
            result_box: List[Any] = [None]
            exc_box:    List[Optional[Exception]] = [None]

            def _run():
                try:
                    result_box[0] = t.fn(dep_results, ctx)
                except Exception as e:
                    exc_box[0] = e

            thread = threading.Thread(target=_run, daemon=True)
            thread.start()
            thread.join(timeout=t.timeout_s)

            if thread.is_alive():
                t.state = TaskState.FAILED
                t.error = f"Timeout after {t.timeout_s}s"
                run.errors[task_id] = t.error
                break
            elif exc_box[0]:
                if attempt <= t.max_retries:
                    t.retries = attempt
                    continue
                t.state = TaskState.FAILED
                t.error = str(exc_box[0])
                run.errors[task_id] = t.error
                break
            else:
                t.state  = TaskState.DONE
                t.result = result_box[0]
                run.results[task_id] = t.result
                break

        t.finished_at = time.time()
        run.exec_order.append(task_id)

        for fn in self._post_hooks:
            try: fn(t)
            except Exception: pass

        self._persist_task(t)

    # ── HOOKS ─────────────────────────────────────────────────────────

    def on_task_start(self, fn: Callable): self._pre_hooks.append(fn)
    def on_task_done(self, fn: Callable):  self._post_hooks.append(fn)

    # ── QUERY ─────────────────────────────────────────────────────────

    def get_task(self, task_id: str) -> Optional[DependencyTask]:
        return self._tasks.get(task_id)

    def list_tasks(self, tag: Optional[str] = None,
                    state: Optional[TaskState] = None) -> List[Dict]:
        tasks = list(self._tasks.values())
        if tag:   tasks = [t for t in tasks if tag in t.tags]
        if state: tasks = [t for t in tasks if t.state == state]
        return [t.to_dict() for t in tasks]

    def run_history(self, limit: int = 20) -> List[Dict]:
        return [r.to_dict() for r in self._runs[-limit:]]

    def _persist_task(self, t: DependencyTask):
        self._db.execute(
            "INSERT OR REPLACE INTO td_tasks VALUES (?,?,?,?,?,?)",
            (t.task_id, t.name, t.state.value,
             t.priority, t.duration_ms, t.error))
        self._db.commit()

    def _persist_run(self, r: ExecRun):
        self._db.execute(
            "INSERT OR REPLACE INTO td_runs VALUES (?,?,?,?,?,?,?)",
            (r.run_id, r.strategy, r.status, len(r.exec_order),
             len(r.errors), r.started_at, r.finished_at))
        self._db.commit()

    def stats(self) -> Dict[str, Any]:
        return {
            "tasks": len(self._tasks),
            "runs": len(self._runs),
            "has_cycle": self.detect_cycle(),
        }
