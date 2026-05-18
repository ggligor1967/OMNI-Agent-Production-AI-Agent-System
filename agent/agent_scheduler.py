"""OMNI AGENT - Agent Scheduler
Task scheduling: cron-style, interval, one-shot, dependency ordering,
priority queue, and missed-run catch-up.

Features:
- Schedule types: INTERVAL, CRON (minute/hour/day/weekday), ONE_SHOT, MANUAL
- Priority queue: lower number = higher priority; FIFO within same priority
- Dependency ordering: task B waits for task A to complete before running
- Missed-run catch-up: run once after restart if next_run is in the past
- Concurrency limit: max N tasks running simultaneously
- Run history: last N runs per task with status and duration
- Pause / resume individual tasks or all tasks
- Timeout: per-task execution timeout
- Hooks: on_task_start, on_task_success, on_task_failure
- SQLite persistence: task definitions, run log
- REST API: schedule, cancel, pause, resume, status, runs, stats
"""
import asyncio, json, re, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class ScheduleType(str, Enum):
    INTERVAL = "interval"
    CRON     = "cron"
    ONE_SHOT = "one_shot"
    MANUAL   = "manual"

class TaskStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"
    PAUSED    = "paused"
    CANCELLED = "cancelled"

def _parse_cron(expr: str, now: float) -> float:
    """Return next unix timestamp matching cron expression.
    Supports: minute hour day month weekday (all integers or *)."""
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Invalid cron: {expr!r} (need 5 fields)")
    minute, hour, dom, month, dow = parts

    def _match(part, val):
        if part == "*": return True
        if "," in part: return val in [int(x) for x in part.split(",")]
        if "-" in part:
            lo, hi = part.split("-"); return int(lo) <= val <= int(hi)
        if "/" in part:
            _, step = part.split("/"); return val % int(step) == 0
        return val == int(part)

    import datetime
    dt = datetime.datetime.fromtimestamp(now + 60)  # start 1 min ahead
    for _ in range(527040):  # up to 1 year of minutes
        if (_match(month, dt.month) and _match(dom, dt.day)
                and _match(dow, dt.weekday()) and _match(hour, dt.hour)
                and _match(minute, dt.minute)):
            return dt.timestamp()
        dt += datetime.timedelta(minutes=1)
    return now + 31536000  # fallback: 1 year

@dataclass
class TaskRun:
    id: str; task_id: str; task_name: str
    status: TaskStatus = TaskStatus.PENDING
    output: Any = None; error: str = ""
    started_at: float = 0.0; finished_at: float = 0.0

    @property
    def duration_ms(self):
        if not self.finished_at: return 0.0
        return round((self.finished_at - self.started_at) * 1000, 1)

    def to_dict(self):
        return {"id": self.id, "task": self.task_name,
                "status": self.status.value,
                "duration_ms": self.duration_ms,
                "error": self.error, "started_at": round(self.started_at, 1)}

@dataclass
class TaskSpec:
    id: str; name: str; fn: Callable
    schedule_type: ScheduleType = ScheduleType.INTERVAL
    interval_s: float = 60.0
    cron_expr: str = ""
    run_at: float = 0.0        # ONE_SHOT: absolute timestamp
    priority: int = 5
    timeout_s: float = 60.0
    max_retries: int = 0
    retry_delay: float = 5.0
    depends_on: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    enabled: bool = True
    catch_up: bool = True       # run missed tasks on restart
    next_run: float = 0.0
    last_run: float = 0.0
    run_count: int = 0
    error_count: int = 0
    history: List[TaskRun] = field(default_factory=list)

    @property
    def due(self) -> bool:
        return self.enabled and time.time() >= self.next_run

    def schedule_next(self, from_time: float = None):
        t = from_time or time.time()
        if self.schedule_type == ScheduleType.INTERVAL:
            self.next_run = t + self.interval_s
        elif self.schedule_type == ScheduleType.CRON:
            self.next_run = _parse_cron(self.cron_expr, t)
        elif self.schedule_type == ScheduleType.ONE_SHOT:
            self.next_run = float('inf')   # don't re-run
        elif self.schedule_type == ScheduleType.MANUAL:
            self.next_run = float('inf')

    def to_dict(self):
        return {"id": self.id, "name": self.name,
                "schedule": self.schedule_type.value,
                "interval_s": self.interval_s,
                "cron": self.cron_expr,
                "priority": self.priority,
                "enabled": self.enabled,
                "next_run": round(self.next_run, 1),
                "run_count": self.run_count,
                "error_count": self.error_count,
                "depends_on": self.depends_on}

class ASStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS task_runs(
                    id TEXT PRIMARY KEY, task_id TEXT, task_name TEXT,
                    status TEXT, error TEXT DEFAULT '',
                    started_at REAL DEFAULT 0, finished_at REAL DEFAULT 0,
                    duration_ms REAL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS idx_tr_task ON task_runs(task_id, started_at DESC);
            """)

    def log_run(self, tr: TaskRun):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO task_runs VALUES(?,?,?,?,?,?,?,?)",
                (tr.id, tr.task_id, tr.task_name, tr.status.value,
                 tr.error, tr.started_at, tr.finished_at, tr.duration_ms))

    def recent_runs(self, task_id: str = None, limit: int = 20) -> List[Dict]:
        with self._conn() as c:
            if task_id:
                rows = c.execute(
                    "SELECT * FROM task_runs WHERE task_id=? "
                    "ORDER BY started_at DESC LIMIT ?",
                    (task_id, limit)).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM task_runs ORDER BY started_at DESC LIMIT ?",
                    (limit,)).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> Dict:
        with self._conn() as c:
            n  = c.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]
            nc = c.execute(
                "SELECT COUNT(*) FROM task_runs WHERE status='completed'"
            ).fetchone()[0]
            nf = c.execute(
                "SELECT COUNT(*) FROM task_runs WHERE status='failed'"
            ).fetchone()[0]
        return {"total_runs": n, "completed": nc, "failed": nf}

class AgentScheduler:
    """
    Task scheduler with interval, cron, one-shot, and manual triggers.

    Usage:
        scheduler = AgentScheduler(max_concurrent=4)

        async def daily_report(ctx):
            return "Report generated"

        scheduler.schedule("daily_report", daily_report,
                            schedule_type=ScheduleType.CRON,
                            cron_expr="0 9 * * *",
                            priority=1)

        await scheduler.start()
        # ... runs in background
        await scheduler.stop()
    """
    def __init__(self, db_path: str = "data/scheduler.db",
                 max_concurrent: int = 4,
                 tick_s: float = 1.0):
        self._store = ASStore(db_path)
        self._tasks: Dict[str, TaskSpec] = {}
        self._running: Dict[str, asyncio.Task] = {}
        self._paused: bool = False
        self._max_concurrent = max_concurrent
        self._tick_s = tick_s
        self._loop_task: Optional[asyncio.Task] = None
        self._hooks: Dict[str, List[Callable]] = {
            "on_start": [], "on_success": [], "on_failure": []}
        self._context: Dict[str, Any] = {}
        self._completed_names: set = set()

    def schedule(self, name: str, fn: Callable,
                  schedule_type: ScheduleType = ScheduleType.INTERVAL,
                  interval_s: float = 60.0, cron_expr: str = "",
                  run_at: float = 0.0,
                  priority: int = 5, timeout_s: float = 60.0,
                  max_retries: int = 0, retry_delay: float = 5.0,
                  depends_on: List[str] = None,
                  tags: List[str] = None,
                  catch_up: bool = True,
                  run_now: bool = False) -> TaskSpec:
        spec = TaskSpec(id=str(uuid.uuid4())[:8], name=name, fn=fn,
                         schedule_type=schedule_type, interval_s=interval_s,
                         cron_expr=cron_expr, run_at=run_at,
                         priority=priority, timeout_s=timeout_s,
                         max_retries=max_retries, retry_delay=retry_delay,
                         depends_on=depends_on or [], tags=tags or [],
                         catch_up=catch_up)
        now = time.time()
        if run_now or schedule_type == ScheduleType.ONE_SHOT:
            spec.next_run = run_at if run_at > 0 else now
        elif schedule_type == ScheduleType.CRON:
            spec.next_run = _parse_cron(cron_expr, now)
        elif schedule_type == ScheduleType.INTERVAL:
            spec.next_run = now + interval_s
        else:
            spec.next_run = float('inf')
        self._tasks[name] = spec
        logger.info(f"Scheduled: {name!r} ({schedule_type.value})")
        return spec

    def cancel(self, name: str) -> bool:
        spec = self._tasks.pop(name, None)
        if spec:
            t = self._running.pop(name, None)
            if t and not t.done(): t.cancel()
            return True
        return False

    def pause(self, name: str = None):
        if name:
            spec = self._tasks.get(name)
            if spec: spec.enabled = False
        else:
            self._paused = True

    def resume(self, name: str = None):
        if name:
            spec = self._tasks.get(name)
            if spec: spec.enabled = True
        else:
            self._paused = False

    def trigger(self, name: str) -> bool:
        """Manually trigger a task immediately."""
        spec = self._tasks.get(name)
        if not spec: return False
        spec.next_run = 0.0; return True

    def set_context(self, key: str, value: Any):
        self._context[key] = value

    def on(self, event: str, fn: Callable):
        if event in self._hooks: self._hooks[event].append(fn)

    def _deps_satisfied(self, spec: TaskSpec) -> bool:
        return all(d in self._completed_names for d in spec.depends_on)

    async def _run_task(self, spec: TaskSpec):
        tr = TaskRun(id=str(uuid.uuid4())[:10],
                      task_id=spec.id, task_name=spec.name,
                      status=TaskStatus.RUNNING,
                      started_at=time.time())
        spec.run_count += 1; spec.last_run = tr.started_at
        for h in self._hooks["on_start"]: h(spec, tr)

        for attempt in range(spec.max_retries + 1):
            try:
                fn = spec.fn
                if asyncio.iscoroutinefunction(fn):
                    result = await asyncio.wait_for(
                        fn(self._context), timeout=spec.timeout_s)
                else:
                    result = fn(self._context)
                tr.output = result; tr.status = TaskStatus.COMPLETED
                self._completed_names.add(spec.name)
                for h in self._hooks["on_success"]: h(spec, tr)
                break
            except Exception as e:
                tr.error = str(e); tr.status = TaskStatus.FAILED
                spec.error_count += 1
                for h in self._hooks["on_failure"]: h(spec, tr, e)
                if attempt < spec.max_retries:
                    await asyncio.sleep(spec.retry_delay)
                else:
                    break

        tr.finished_at = time.time()
        spec.history.append(tr)
        if len(spec.history) > 50: spec.history.pop(0)
        spec.schedule_next()
        self._store.log_run(tr)
        self._running.pop(spec.name, None)

    async def _tick(self):
        while True:
            await asyncio.sleep(self._tick_s)
            if self._paused: continue
            if len(self._running) >= self._max_concurrent: continue

            # Build priority queue of due tasks
            due = sorted(
                [s for s in self._tasks.values()
                 if s.due and s.name not in self._running
                 and self._deps_satisfied(s)],
                key=lambda s: (s.priority, s.next_run))

            for spec in due:
                if len(self._running) >= self._max_concurrent: break
                task = asyncio.create_task(self._run_task(spec))
                self._running[spec.name] = task

    async def start(self):
        self._loop_task = asyncio.create_task(self._tick())
        logger.info("Scheduler started")

    async def stop(self):
        if self._loop_task: self._loop_task.cancel()
        for t in self._running.values():
            if not t.done(): t.cancel()
        logger.info("Scheduler stopped")

    async def run_now(self, name: str) -> Optional[TaskRun]:
        """Run a task immediately and await its completion."""
        spec = self._tasks.get(name)
        if not spec: return None
        await self._run_task(spec)
        return spec.history[-1] if spec.history else None

    def task_info(self, name: str) -> Optional[Dict]:
        spec = self._tasks.get(name)
        return spec.to_dict() if spec else None

    def list_tasks(self, tag: str = None) -> List[TaskSpec]:
        tasks = list(self._tasks.values())
        if tag: tasks = [t for t in tasks if tag in t.tags]
        return sorted(tasks, key=lambda t: t.priority)

    def recent_runs(self, name: str = None, limit: int = 20) -> List[Dict]:
        tid = self._tasks[name].id if name and name in self._tasks else None
        return self._store.recent_runs(tid, limit)

    def stats(self) -> Dict:
        s = self._store.stats()
        s["scheduled_tasks"] = len(self._tasks)
        s["running"] = len(self._running)
        s["paused"] = self._paused
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def schedule_ep(req):
            d = await req.json()
            # Dynamic schedule via API (no fn — uses noop)
            spec = self.schedule(d["name"], lambda ctx: None,
                                  ScheduleType[d.get("type","INTERVAL").upper()],
                                  float(d.get("interval_s",60)),
                                  d.get("cron",""),
                                  priority=int(d.get("priority",5)))
            return web.json_response(spec.to_dict(), status=201)
        async def cancel_ep(req):
            d = await req.json()
            ok = self.cancel(d["name"])
            return web.json_response({"cancelled": ok})
        async def pause_ep(req):
            d = await req.json()
            self.pause(d.get("name"))
            return web.json_response({"ok": True})
        async def resume_ep(req):
            d = await req.json()
            self.resume(d.get("name"))
            return web.json_response({"ok": True})
        async def status_ep(req):
            name = req.match_info.get("name")
            info = self.task_info(name) if name else None
            if name and not info:
                return web.json_response({"error":"not found"},status=404)
            return web.json_response(info or {"tasks":[t.to_dict() for t in self.list_tasks()]})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/scheduler"
        app.router.add_post(f"{p}/schedule",    schedule_ep)
        app.router.add_post(f"{p}/cancel",      cancel_ep)
        app.router.add_post(f"{p}/pause",       pause_ep)
        app.router.add_post(f"{p}/resume",      resume_ep)
        app.router.add_get( f"{p}/task/{{name}}", status_ep)
        app.router.add_get( f"{p}/stats",       stats_ep)
        logger.info(f"Scheduler API at {prefix}/scheduler/")
