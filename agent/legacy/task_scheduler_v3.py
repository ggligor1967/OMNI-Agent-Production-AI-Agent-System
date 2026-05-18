"""OMNI Agent — Task Scheduler V3: cron + one-shot tasks, priority, jitter, run history."""
from __future__ import annotations
import asyncio, random, sqlite3, threading, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class ScheduleType(str, Enum):
    CRON     = "cron"       # cron-like: "*/5 * * * *" (minute/hour/dom/month/dow)
    INTERVAL = "interval"   # every N seconds
    ONCE     = "once"       # run once at given timestamp
    IMMEDIATE = "immediate" # run immediately


class TaskStatus(str, Enum):
    SCHEDULED = "scheduled"
    RUNNING   = "running"
    DONE      = "done"
    FAILED    = "failed"
    CANCELLED = "cancelled"
    PAUSED    = "paused"
    SKIPPED   = "skipped"


class TaskPriority(str, Enum):
    CRITICAL = "critical"
    HIGH     = "high"
    NORMAL   = "normal"
    LOW      = "low"


PRIORITY_INT = {
    TaskPriority.CRITICAL: 0,
    TaskPriority.HIGH:     1,
    TaskPriority.NORMAL:   2,
    TaskPriority.LOW:      3,
}


def _cron_matches(expr: str, ts: float) -> bool:
    """Check if timestamp matches a cron expression (simplified 5-field)."""
    import datetime
    dt = datetime.datetime.fromtimestamp(ts)
    parts = expr.strip().split()
    if len(parts) != 5:
        return False
    minute, hour, dom, month, dow = parts

    def _match_field(val: int, field: str, min_v: int, max_v: int) -> bool:
        if field == "*":
            return True
        try:
            if "/" in field:
                base, step = field.split("/")
                step = int(step)
                start = 0 if base == "*" else int(base)
                return (val - start) % step == 0 and val >= start
            if "," in field:
                return val in {int(x) for x in field.split(",")}
            if "-" in field:
                lo, hi = field.split("-")
                return int(lo) <= val <= int(hi)
            return val == int(field)
        except Exception:
            return False

    return (
        _match_field(dt.minute, minute, 0, 59) and
        _match_field(dt.hour,   hour,   0, 23) and
        _match_field(dt.day,    dom,    1, 31) and
        _match_field(dt.month,  month,  1, 12) and
        _match_field(dt.weekday(), dow, 0, 6)
    )


@dataclass
class TaskRun:
    run_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    task_id: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    status: TaskStatus = TaskStatus.RUNNING
    result: Any = None
    error: Optional[str] = None
    attempt: int = 1

    @property
    def duration_ms(self) -> float:
        if self.finished_at:
            return (self.finished_at - self.started_at) * 1000
        return 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "status": self.status.value,
            "duration_ms": round(self.duration_ms, 2),
            "error": self.error,
            "attempt": self.attempt,
        }


@dataclass
class TaskSpec:
    task_id: str
    name: str
    fn: Callable
    schedule_type: ScheduleType
    schedule: Any                      # cron str, interval seconds, or timestamp
    priority: TaskPriority = TaskPriority.NORMAL
    max_retries: int = 0
    retry_delay_s: float = 1.0
    jitter_s: float = 0.0             # random jitter on execution time
    timeout_s: Optional[float] = None
    enabled: bool = True
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    next_run_at: float = 0.0
    last_run_at: Optional[float] = None
    run_count: int = 0
    error_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "schedule_type": self.schedule_type.value,
            "schedule": self.schedule,
            "priority": self.priority.value,
            "enabled": self.enabled,
            "run_count": self.run_count,
            "error_count": self.error_count,
            "next_run_at": self.next_run_at,
        }


class TaskSchedulerV3:
    """
    Flexible task scheduler:
    - Cron expressions (5-field)
    - Interval-based repeating tasks
    - One-shot at timestamp
    - Priority ordering
    - Jitter to spread load
    - Retry with delay
    - Run history in SQLite
    - Thread-safe tick-based execution
    """

    def __init__(self, db_path: str = ":memory:", tick_s: float = 1.0):
        self.tick_s = tick_s
        self._tasks: Dict[str, TaskSpec] = {}
        self._runs: List[TaskRun] = []
        self._hooks_before: List[Callable] = []
        self._hooks_after:  List[Callable] = []
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._tick_count = 0

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS ts_tasks (
                task_id TEXT PRIMARY KEY, name TEXT, schedule_type TEXT,
                schedule TEXT, priority TEXT, enabled INTEGER,
                run_count INTEGER, error_count INTEGER
            );
            CREATE TABLE IF NOT EXISTS ts_runs (
                run_id TEXT PRIMARY KEY, task_id TEXT, started_at REAL,
                finished_at REAL, status TEXT, error TEXT, attempt INTEGER
            );
        """)
        self._db.commit()

    # ── REGISTRATION ──────────────────────────────────────────────────

    def schedule(self, name: str, fn: Callable,
                 schedule_type: ScheduleType = ScheduleType.INTERVAL,
                 schedule: Any = 60,
                 priority: TaskPriority = TaskPriority.NORMAL,
                 max_retries: int = 0,
                 retry_delay_s: float = 1.0,
                 jitter_s: float = 0.0,
                 timeout_s: Optional[float] = None,
                 tags: Optional[List[str]] = None,
                 task_id: Optional[str] = None,
                 metadata: Optional[Dict] = None) -> TaskSpec:
        tid = task_id or str(uuid.uuid4())[:8]
        spec = TaskSpec(
            task_id=tid, name=name, fn=fn,
            schedule_type=schedule_type, schedule=schedule,
            priority=priority, max_retries=max_retries,
            retry_delay_s=retry_delay_s, jitter_s=jitter_s,
            timeout_s=timeout_s, tags=list(tags or []),
            metadata=metadata or {})
        spec.next_run_at = self._compute_next(spec, time.time())
        with self._lock:
            self._tasks[tid] = spec
        self._db.execute(
            "INSERT OR REPLACE INTO ts_tasks VALUES (?,?,?,?,?,?,?,?)",
            (tid, name, schedule_type.value, str(schedule),
             priority.value, 1, 0, 0))
        self._db.commit()
        return spec

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.enabled = False
                task.schedule_type = ScheduleType.ONCE
                task.next_run_at = float("inf")
                return True
        return False

    def pause(self, task_id: str):
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id].enabled = False

    def resume(self, task_id: str):
        with self._lock:
            if task_id in self._tasks:
                task = self._tasks[task_id]
                task.enabled = True
                task.next_run_at = self._compute_next(task, time.time())

    def remove(self, task_id: str):
        with self._lock:
            self._tasks.pop(task_id, None)

    # ── SCHEDULING MATH ───────────────────────────────────────────────

    def _compute_next(self, spec: TaskSpec, after: float) -> float:
        jitter = random.uniform(0, spec.jitter_s) if spec.jitter_s > 0 else 0.0
        if spec.schedule_type == ScheduleType.IMMEDIATE:
            return after + jitter
        if spec.schedule_type == ScheduleType.ONCE:
            return float(spec.schedule) + jitter
        if spec.schedule_type == ScheduleType.INTERVAL:
            return after + float(spec.schedule) + jitter
        if spec.schedule_type == ScheduleType.CRON:
            # Scan forward minute by minute (max 24h)
            t = after - (after % 60) + 60
            for _ in range(1440):
                if _cron_matches(spec.schedule, t):
                    return t + jitter
                t += 60
            return after + 3600  # fallback
        return after + 60

    # ── TICK / EXECUTION ──────────────────────────────────────────────

    def tick(self) -> List[TaskRun]:
        """Execute all due tasks. Returns list of TaskRuns."""
        now = time.time()
        self._tick_count += 1
        due = []
        with self._lock:
            for spec in self._tasks.values():
                if spec.enabled and spec.next_run_at <= now:
                    due.append(spec)
        due.sort(key=lambda s: PRIORITY_INT[s.priority])
        runs = []
        for spec in due:
            run = self._execute(spec)
            runs.append(run)
        return runs

    def _execute(self, spec: TaskSpec) -> TaskRun:
        run = TaskRun(task_id=spec.task_id)
        for hook in self._hooks_before:
            try: hook(spec)
            except Exception: pass

        attempt = 0
        last_err = ""
        while attempt <= spec.max_retries:
            attempt += 1
            run.attempt = attempt
            try:
                result = spec.fn()
                run.result  = result
                run.status  = TaskStatus.DONE
                run.error   = None
                spec.run_count += 1
                spec.last_run_at = time.time()
                break
            except Exception as exc:
                last_err = str(exc)
                spec.error_count += 1
                if attempt <= spec.max_retries:
                    time.sleep(spec.retry_delay_s)
                else:
                    run.status = TaskStatus.FAILED
                    run.error  = last_err

        run.finished_at = time.time()
        # Schedule next
        if spec.schedule_type not in (ScheduleType.ONCE, ScheduleType.IMMEDIATE):
            spec.next_run_at = self._compute_next(spec, run.finished_at)
        else:
            spec.enabled = False

        self._runs.append(run)
        self._db.execute(
            "INSERT INTO ts_runs VALUES (?,?,?,?,?,?,?)",
            (run.run_id, spec.task_id, run.started_at, run.finished_at,
             run.status.value, run.error, run.attempt))
        self._db.commit()
        for hook in self._hooks_after:
            try: hook(spec, run)
            except Exception: pass
        return run

    # ── BACKGROUND THREAD ─────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            self.tick()
            time.sleep(self.tick_s)

    # ── HOOKS ─────────────────────────────────────────────────────────

    def on_before(self, fn: Callable): self._hooks_before.append(fn)
    def on_after(self, fn: Callable):  self._hooks_after.append(fn)

    # ── QUERY ─────────────────────────────────────────────────────────

    def get_task(self, task_id: str) -> Optional[TaskSpec]:
        return self._tasks.get(task_id)

    def list_tasks(self, enabled_only: bool = False,
                   tag: Optional[str] = None) -> List[Dict[str, Any]]:
        tasks = list(self._tasks.values())
        if enabled_only:
            tasks = [t for t in tasks if t.enabled]
        if tag:
            tasks = [t for t in tasks if tag in t.tags]
        return [t.to_dict() for t in sorted(tasks,
                key=lambda t: PRIORITY_INT[t.priority])]

    def run_history(self, task_id: Optional[str] = None,
                    limit: int = 50) -> List[Dict[str, Any]]:
        runs = self._runs
        if task_id:
            runs = [r for r in runs if r.task_id == task_id]
        return [r.to_dict() for r in runs[-limit:]]

    def stats(self) -> Dict[str, Any]:
        enabled  = sum(1 for t in self._tasks.values() if t.enabled)
        total_ok = sum(1 for r in self._runs if r.status == TaskStatus.DONE)
        total_err = sum(1 for r in self._runs if r.status == TaskStatus.FAILED)
        return {
            "tasks": len(self._tasks),
            "enabled": enabled,
            "ticks": self._tick_count,
            "runs": len(self._runs),
            "successful": total_ok,
            "failed": total_err,
        }
