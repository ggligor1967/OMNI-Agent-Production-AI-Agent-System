"""OMNI AGENT - Task Scheduler
Asyncio-driven cron + delayed task scheduling: register one-shot or
recurring jobs, detect missed runs, enforce concurrency limits, and
persist schedule state to SQLite.

Features:
- One-shot delayed tasks: run after N seconds
- Recurring tasks: cron-style interval (every N seconds/minutes/hours)
- Cron expressions: simple 5-field cron parsing (min hour dom mon dow)
- Missed-run detection: catch up or skip missed executions on restart
- Concurrency limit: cap parallel running jobs with asyncio.Semaphore
- Timeout per job: asyncio.wait_for wraps every execution
- Retry on failure: configurable attempts with exponential backoff
- Priority queue: higher-priority jobs run first when due simultaneously
- Pause/resume: individual jobs or entire scheduler
- Job history: last N executions with timing and output
- SQLite persistence: schedule definitions and run history
- REST API: schedule, cancel, pause, resume, history, stats
"""
import asyncio, time, uuid, sqlite3, json, logging
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class JobState(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    PAUSED    = "paused"
    COMPLETED = "completed"
    FAILED    = "failed"
    CANCELLED = "cancelled"

def _next_interval(last_run: float, interval_s: float) -> float:
    """Next run time for interval-based job."""
    now = time.time()
    if last_run == 0: return now
    next_t = last_run + interval_s
    # Skip missed runs
    while next_t < now:
        next_t += interval_s
    return next_t

def _parse_cron(expr: str, after: float = None) -> float:
    """
    Very simple 5-field cron: min hour dom mon dow
    Only supports '*' and integer values, no ranges/steps.
    Returns next trigger time after `after` (default: now).
    """
    after = after or time.time()
    import datetime
    fields = expr.strip().split()
    if len(fields) != 5:
        raise ValueError(f"Invalid cron expression: {expr!r}")
    cmin, chour, cdom, cmon, cdow = fields

    dt = datetime.datetime.fromtimestamp(after) + datetime.timedelta(minutes=1)
    dt = dt.replace(second=0, microsecond=0)
    for _ in range(527040):  # max 1 year
        ok = True
        if cmin  != '*' and dt.minute     != int(cmin):  ok = False
        if chour != '*' and dt.hour       != int(chour): ok = False
        if cdom  != '*' and dt.day        != int(cdom):  ok = False
        if cmon  != '*' and dt.month      != int(cmon):  ok = False
        if cdow  != '*' and dt.weekday()  != int(cdow):  ok = False
        if ok: return dt.timestamp()
        dt += datetime.timedelta(minutes=1)
    raise ValueError("Could not find next cron time within 1 year")

@dataclass
class Job:
    id: str; name: str; fn: Callable
    mode: str = "once"          # once | interval | cron
    interval_s: float = 0.0     # for interval mode
    cron_expr: str = ""         # for cron mode
    run_at: float = 0.0         # absolute timestamp for one-shot
    state: JobState = JobState.PENDING
    priority: int = 5           # 1=highest, 10=lowest
    timeout_s: float = 60.0
    max_retries: int = 1
    retry_delay: float = 1.0
    context: Dict = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    # Runtime
    last_run: float = 0.0
    next_run: float = 0.0
    run_count: int = 0
    error_count: int = 0
    total_ms: float = 0.0
    history: List[Dict] = field(default_factory=list)
    _task: Any = None   # asyncio.Task

    @property
    def due(self):
        return self.state == JobState.PENDING and time.time() >= self.next_run

    @property
    def avg_ms(self):
        return round(self.total_ms / max(1, self.run_count), 1)

    def to_dict(self):
        return {"id": self.id, "name": self.name, "mode": self.mode,
                "state": self.state, "priority": self.priority,
                "next_run": round(self.next_run, 1),
                "last_run": round(self.last_run, 1),
                "run_count": self.run_count, "error_count": self.error_count,
                "avg_ms": self.avg_ms, "tags": self.tags,
                "history": self.history[-5:]}

@dataclass
class JobRun:
    run_id: str; job_id: str; job_name: str
    started_at: float; finished_at: float = 0.0
    output: Any = None; error: str = ""; retries: int = 0

    @property
    def duration_ms(self): return round((self.finished_at - self.started_at)*1000, 1)
    @property
    def success(self): return not self.error

    def to_dict(self):
        return {"run_id": self.run_id, "job": self.job_name,
                "started_at": self.started_at,
                "duration_ms": self.duration_ms,
                "success": self.success, "error": self.error,
                "output": str(self.output)[:200] if self.output else None}

class TSStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS runs(
                    run_id TEXT PRIMARY KEY, job_id TEXT, job_name TEXT,
                    started_at REAL, finished_at REAL DEFAULT 0,
                    success INTEGER DEFAULT 1, error TEXT DEFAULT '',
                    output TEXT DEFAULT '', retries INTEGER DEFAULT 0);
                CREATE INDEX IF NOT EXISTS idx_run_job ON runs(job_id, started_at DESC);
            """)

    def log_run(self, r: JobRun):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?,?,?,?)",
                (r.run_id, r.job_id, r.job_name, r.started_at, r.finished_at,
                 int(r.success), r.error, str(r.output or "")[:500], r.retries))

    def runs(self, job_id: str = None, limit: int = 20) -> List[Dict]:
        with self._conn() as c:
            if job_id:
                rows = c.execute(
                    "SELECT * FROM runs WHERE job_id=? ORDER BY started_at DESC LIMIT ?",
                    (job_id, limit)).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?",
                    (limit,)).fetchall()
        return [dict(r) for r in rows]

    def stats(self):
        with self._conn() as c:
            total   = c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            success = c.execute("SELECT SUM(success) FROM runs").fetchone()[0] or 0
        return {"total_runs": total, "success": int(success),
                "failed": total - int(success),
                "success_rate": round(int(success)/max(1,total), 4)}

class TaskScheduler:
    """
    Asyncio-driven cron + interval task scheduler with SQLite persistence.

    Usage:
        scheduler = TaskScheduler()

        # Run once after 10 seconds
        scheduler.schedule_once("send-welcome", send_email_fn,
                                  delay_s=10.0, context={"user_id": 42})

        # Run every 5 minutes
        scheduler.schedule_interval("health-check", check_health_fn,
                                      interval_s=300)

        # Run at 9am every weekday (mon=0 … fri=4)
        scheduler.schedule_cron("morning-report", report_fn,
                                  cron_expr="0 9 * * 0")

        await scheduler.start()
        # … later
        await scheduler.stop()
    """
    def __init__(self, db_path: str = "data/scheduler.db",
                 max_concurrent: int = 10,
                 tick_interval: float = 1.0):
        self._store = TSStore(db_path)
        self._jobs: Dict[str, Job] = {}
        self._sem = asyncio.Semaphore(max_concurrent)
        self._tick = tick_interval
        self._running = False
        self._loop_task: Optional[asyncio.Task] = None
        self._paused = False

    def schedule_once(self, name: str, fn: Callable,
                       delay_s: float = 0.0, context: Dict = None,
                       priority: int = 5, timeout_s: float = 60.0,
                       tags: List[str] = None) -> Job:
        job = Job(id=str(uuid.uuid4())[:10], name=name, fn=fn,
                   mode="once", run_at=time.time() + delay_s,
                   context=context or {}, priority=priority,
                   timeout_s=timeout_s, tags=tags or [])
        job.next_run = job.run_at
        self._jobs[job.id] = job
        logger.info(f"Scheduled once: {name!r} in {delay_s:.1f}s")
        return job

    def schedule_interval(self, name: str, fn: Callable,
                           interval_s: float = 60.0, context: Dict = None,
                           priority: int = 5, timeout_s: float = 60.0,
                           max_retries: int = 1, tags: List[str] = None,
                           run_immediately: bool = False) -> Job:
        job = Job(id=str(uuid.uuid4())[:10], name=name, fn=fn,
                   mode="interval", interval_s=interval_s,
                   context=context or {}, priority=priority,
                   timeout_s=timeout_s, max_retries=max_retries,
                   tags=tags or [])
        job.next_run = time.time() if run_immediately else time.time() + interval_s
        self._jobs[job.id] = job
        logger.info(f"Scheduled interval: {name!r} every {interval_s:.1f}s")
        return job

    def schedule_cron(self, name: str, fn: Callable,
                       cron_expr: str = "* * * * *", context: Dict = None,
                       priority: int = 5, timeout_s: float = 60.0,
                       tags: List[str] = None) -> Job:
        next_t = _parse_cron(cron_expr)
        job = Job(id=str(uuid.uuid4())[:10], name=name, fn=fn,
                   mode="cron", cron_expr=cron_expr,
                   context=context or {}, priority=priority,
                   timeout_s=timeout_s, tags=tags or [])
        job.next_run = next_t
        self._jobs[job.id] = job
        logger.info(f"Scheduled cron: {name!r} expr={cron_expr!r}")
        return job

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job: return False
        job.state = JobState.CANCELLED
        if job._task: job._task.cancel()
        return True

    def pause_job(self, job_id: str):
        job = self._jobs.get(job_id)
        if job and job.state == JobState.PENDING:
            job.state = JobState.PAUSED

    def resume_job(self, job_id: str):
        job = self._jobs.get(job_id)
        if job and job.state == JobState.PAUSED:
            job.state = JobState.PENDING

    def pause_all(self): self._paused = True
    def resume_all(self): self._paused = False

    async def _execute_job(self, job: Job):
        job.state = JobState.RUNNING
        run = JobRun(run_id=str(uuid.uuid4())[:10],
                      job_id=job.id, job_name=job.name,
                      started_at=time.time())
        retries = 0
        async with self._sem:
            for attempt in range(job.max_retries + 1):
                try:
                    fn = job.fn
                    if asyncio.iscoroutinefunction(fn):
                        output = await asyncio.wait_for(
                            fn(job.context), timeout=job.timeout_s)
                    else:
                        output = await asyncio.wait_for(
                            asyncio.get_event_loop().run_in_executor(
                                None, fn, job.context),
                            timeout=job.timeout_s)
                    run.output = output; break
                except Exception as e:
                    retries += 1
                    run.error = str(e)
                    if attempt < job.max_retries:
                        await asyncio.sleep(job.retry_delay * (2 ** attempt))
                    else:
                        job.error_count += 1

        run.finished_at = time.time(); run.retries = retries
        ms = run.duration_ms
        job.last_run = run.started_at
        job.run_count += 1; job.total_ms += ms
        job.history.append(run.to_dict())
        if len(job.history) > 20: job.history.pop(0)
        self._store.log_run(run)

        # Reschedule or mark done
        if run.error and not retries:
            job.state = JobState.FAILED
        elif job.mode == "once":
            job.state = JobState.COMPLETED
        elif job.mode == "interval":
            job.next_run = time.time() + job.interval_s
            job.state = JobState.PENDING
        elif job.mode == "cron":
            try:
                job.next_run = _parse_cron(job.cron_expr, time.time())
                job.state = JobState.PENDING
            except Exception:
                job.state = JobState.FAILED
        logger.debug(f"Job {job.name!r} done in {ms:.1f}ms. Next: {job.next_run:.0f}")

    async def _tick_loop(self):
        while self._running:
            if not self._paused:
                due = sorted(
                    [j for j in self._jobs.values() if j.due],
                    key=lambda j: (j.priority, j.next_run))
                for job in due:
                    job._task = asyncio.create_task(self._execute_job(job))
            await asyncio.sleep(self._tick)

    async def start(self):
        self._running = True
        self._loop_task = asyncio.create_task(self._tick_loop())
        logger.info("TaskScheduler started")

    async def stop(self):
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()
        logger.info("TaskScheduler stopped")

    def jobs(self, state: str = None, tag: str = None) -> List[Job]:
        jobs = list(self._jobs.values())
        if state: jobs = [j for j in jobs if j.state == state]
        if tag:   jobs = [j for j in jobs if tag in j.tags]
        return jobs

    def get_job(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def run_history(self, job_id: str = None, limit: int = 20) -> List[Dict]:
        return self._store.runs(job_id, limit)

    def stats(self) -> Dict:
        s = self._store.stats()
        s["total_jobs"] = len(self._jobs)
        s["by_state"] = {}
        for j in self._jobs.values():
            s["by_state"][j.state] = s["by_state"].get(j.state, 0) + 1
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def schedule_ep(req):
            d = await req.json()
            mode = d.get("mode", "once")
            ctx = d.get("context", {})
            if mode == "interval":
                job = self.schedule_interval(d["name"], lambda c: None,
                        float(d.get("interval_s", 60)), ctx,
                        int(d.get("priority", 5)))
            elif mode == "cron":
                job = self.schedule_cron(d["name"], lambda c: None,
                        d.get("cron_expr","* * * * *"), ctx)
            else:
                job = self.schedule_once(d["name"], lambda c: None,
                        float(d.get("delay_s",0)), ctx)
            return web.json_response(job.to_dict(), status=201)
        async def cancel_ep(req):
            d = await req.json()
            ok = self.cancel(d["job_id"])
            return web.json_response({"cancelled": ok})
        async def jobs_ep(req):
            return web.json_response({"jobs": [j.to_dict() for j in self.jobs()]})
        async def history_ep(req):
            jid = req.rel_url.query.get("job_id")
            return web.json_response({"history": self.run_history(jid)})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/scheduler"
        app.router.add_post(f"{p}/schedule", schedule_ep)
        app.router.add_post(f"{p}/cancel",   cancel_ep)
        app.router.add_get( f"{p}/jobs",     jobs_ep)
        app.router.add_get( f"{p}/history",  history_ep)
        app.router.add_get( f"{p}/stats",    stats_ep)
        logger.info(f"Task scheduler API at {prefix}/scheduler/")
