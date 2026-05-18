"""OMNI AGENT - Job Scheduler
Cron-style and interval-based job scheduler with dependency chains,
missed-run catch-up, concurrency limits, and execution history.

Features:
- Job: name, fn, schedule (cron expression or interval_s), max_runs
- Cron parser: 5-field (min hour dom mon dow); * , - / operators
- Interval scheduling: run every N seconds from last completion
- One-shot: run exactly once at a specific timestamp
- Dependency chains: job A runs only after job B's last run succeeded
- Missed run catch-up: on startup, fire jobs missed since last run
- Concurrency limit: max_concurrent cap; queue or skip excess
- Timeout: per-job timeout; mark timed-out jobs as FAILED
- Retry: max_retries with exponential backoff delay
- Priority: higher priority jobs preempt lower in queue
- Result: return value stored in execution record
- Jitter: random offset to spread load (jitter_s)
- Execution history: last N results per job
- Pause/resume: temporarily stop a job
- SQLite persistence: jobs, executions, next_run times
- REST API: schedule, unschedule, run_now, pause, resume, history, stats
"""
import asyncio, json, random, re, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class JobStatus(str, Enum):
    PENDING  = "pending";  RUNNING  = "running"
    SUCCESS  = "success";  FAILED   = "failed"
    SKIPPED  = "skipped";  TIMEOUT  = "timeout"
    PAUSED   = "paused"

# ── Cron parser ───────────────────────────────────────────────────────────────
def _expand_field(expr: str, lo: int, hi: int) -> List[int]:
    result = set()
    for part in expr.split(","):
        if part == "*":
            result.update(range(lo, hi + 1))
        elif "/" in part:
            base, step = part.split("/", 1)
            start = lo if base == "*" else int(base)
            result.update(range(start, hi + 1, int(step)))
        elif "-" in part:
            a, b = part.split("-", 1)
            result.update(range(int(a), int(b) + 1))
        else:
            result.add(int(part))
    return sorted(result)

def _parse_cron(expr: str) -> Tuple[List[int], ...]:
    """Parse '*/5 * * * *' → (minutes, hours, doms, months, dows)."""
    fields = expr.strip().split()
    if len(fields) != 5:
        raise ValueError(f"Cron expression must have 5 fields: {expr!r}")
    mins  = _expand_field(fields[0], 0, 59)
    hours = _expand_field(fields[1], 0, 23)
    doms  = _expand_field(fields[2], 1, 31)
    mons  = _expand_field(fields[3], 1, 12)
    dows  = _expand_field(fields[4], 0, 6)
    return mins, hours, doms, mons, dows

def _next_cron(cron_fields: Tuple, after_ts: float) -> float:
    """Compute next cron fire time after after_ts."""
    mins, hours, doms, mons, dows = cron_fields
    import datetime
    dt = datetime.datetime.fromtimestamp(after_ts + 60)
    dt = dt.replace(second=0, microsecond=0)
    for _ in range(366 * 24 * 60):   # max 1 year search
        if (dt.month  in mons and dt.day     in doms and
                dt.hour in hours and dt.minute in mins and
                dt.weekday() in [d % 7 for d in dows]):
            return dt.timestamp()
        dt += datetime.timedelta(minutes=1)
    return after_ts + 365 * 86400   # fallback

@dataclass
class Execution:
    id: str; job_name: str
    status: JobStatus = JobStatus.PENDING
    started_at: float = 0.0; finished_at: float = 0.0
    result: Any = None; error: str = ""
    retry_count: int = 0

    @property
    def duration_s(self) -> float:
        if self.started_at and self.finished_at:
            return round(self.finished_at - self.started_at, 3)
        return 0.0

    def to_dict(self):
        return {"id": self.id, "job": self.job_name,
                "status": self.status.value,
                "started_at": round(self.started_at, 2),
                "duration_s": self.duration_s,
                "result": str(self.result)[:200] if self.result else None,
                "error": self.error, "retry_count": self.retry_count}

@dataclass
class Job:
    name: str; fn: Callable
    cron: str = ""              # cron expression
    interval_s: float = 0.0    # run every N seconds
    run_at: float = 0.0        # one-shot timestamp
    max_runs: int = 0          # 0 = unlimited
    timeout_s: float = 0.0    # 0 = no timeout
    max_retries: int = 0
    retry_delay_s: float = 1.0
    priority: int = 5          # lower = higher priority
    depends_on: List[str] = field(default_factory=list)
    jitter_s: float = 0.0
    enabled: bool = True
    paused: bool = False
    tags: List[str] = field(default_factory=list)
    run_count: int = 0
    _cron_fields: Any = None   # parsed cron tuple
    next_run: float = 0.0
    last_run: float = 0.0
    last_status: JobStatus = JobStatus.PENDING

    def __post_init__(self):
        if self.cron:
            self._cron_fields = _parse_cron(self.cron)
            self.next_run = _next_cron(self._cron_fields, time.time())
        elif self.interval_s > 0:
            self.next_run = time.time() + self.interval_s
        elif self.run_at > 0:
            self.next_run = self.run_at

    def compute_next(self, from_ts: float = None):
        from_ts = from_ts or time.time()
        if self._cron_fields:
            self.next_run = _next_cron(self._cron_fields, from_ts)
        elif self.interval_s > 0:
            self.next_run = from_ts + self.interval_s
        else:
            self.next_run = float("inf")  # one-shot done
        if self.jitter_s > 0:
            self.next_run += random.uniform(0, self.jitter_s)

    def to_dict(self):
        return {"name": self.name, "cron": self.cron,
                "interval_s": self.interval_s, "run_at": self.run_at,
                "next_run": round(self.next_run, 2),
                "last_run": round(self.last_run, 2),
                "run_count": self.run_count,
                "last_status": self.last_status.value,
                "paused": self.paused, "enabled": self.enabled,
                "priority": self.priority, "tags": self.tags}

class JSStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS executions(
                    id TEXT PRIMARY KEY, job_name TEXT,
                    status TEXT, started_at REAL, finished_at REAL,
                    result TEXT DEFAULT '', error TEXT DEFAULT '',
                    retry_count INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS job_state(
                    name TEXT PRIMARY KEY, next_run REAL,
                    last_run REAL, run_count INTEGER, last_status TEXT);
                CREATE INDEX IF NOT EXISTS idx_exec_job
                    ON executions(job_name, started_at DESC);
            """)

    def save_execution(self, ex: Execution):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO executions VALUES(?,?,?,?,?,?,?,?)",
                (ex.id, ex.job_name, ex.status.value, ex.started_at,
                 ex.finished_at, json.dumps(ex.result, default=str)[:500],
                 ex.error[:300], ex.retry_count))

    def save_job_state(self, job: Job):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO job_state VALUES(?,?,?,?,?)",
                (job.name, job.next_run, job.last_run,
                 job.run_count, job.last_status.value))

    def load_job_state(self, name: str) -> Optional[Dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM job_state WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def history(self, job_name: str, limit: int = 20) -> List[Dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM executions WHERE job_name=? "
                "ORDER BY started_at DESC LIMIT ?", (job_name, limit)).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> Dict:
        with self._conn() as c:
            nt = c.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
            ns = c.execute(
                "SELECT COUNT(*) FROM executions WHERE status='success'").fetchone()[0]
            nf = c.execute(
                "SELECT COUNT(*) FROM executions WHERE status='failed'").fetchone()[0]
        return {"total_executions": nt, "success": ns, "failed": nf,
                "success_rate": round(ns / max(1, nt), 4)}

class JobScheduler:
    """
    Cron + interval job scheduler with dependencies and retry.

    Usage:
        scheduler = JobScheduler(max_concurrent=4)

        async def daily_report():
            return "report generated"

        scheduler.schedule("daily_report", daily_report,
                            cron="0 9 * * *")   # 9 AM daily

        scheduler.schedule("hourly_sync", sync_fn,
                            interval_s=3600, max_retries=2)

        # Run the scheduler loop
        await scheduler.run_forever()
    """
    def __init__(self, db_path: str = "data/scheduler.db",
                 max_concurrent: int = 5,
                 catch_up: bool = True):
        self._store = JSStore(db_path)
        self._jobs: Dict[str, Job] = {}
        self._running: Dict[str, asyncio.Task] = {}
        self._history: Dict[str, List[Execution]] = {}
        self.max_concurrent = max_concurrent
        self.catch_up = catch_up
        self._tick_s = 1.0
        self._running_flag = False

    def schedule(self, name: str, fn: Callable,
                  cron: str = "", interval_s: float = 0,
                  run_at: float = 0, max_runs: int = 0,
                  timeout_s: float = 0, max_retries: int = 0,
                  retry_delay_s: float = 1.0,
                  priority: int = 5, depends_on: List[str] = None,
                  jitter_s: float = 0, tags: List[str] = None) -> Job:
        job = Job(name=name, fn=fn, cron=cron, interval_s=interval_s,
                   run_at=run_at, max_runs=max_runs, timeout_s=timeout_s,
                   max_retries=max_retries, retry_delay_s=retry_delay_s,
                   priority=priority, depends_on=list(depends_on or []),
                   jitter_s=jitter_s, tags=list(tags or []))
        # Restore state from DB
        saved = self._store.load_job_state(name)
        if saved:
            job.last_run   = saved["last_run"]
            job.run_count  = saved["run_count"]
            job.last_status = JobStatus(saved["last_status"])
            if self.catch_up and saved["next_run"] < time.time():
                job.next_run = time.time()  # catch-up: run immediately
            else:
                job.next_run = saved["next_run"]
        self._jobs[name] = job
        self._history[name] = []
        return job

    def unschedule(self, name: str) -> bool:
        if name in self._running:
            self._running[name].cancel()
        return bool(self._jobs.pop(name, None))

    def pause(self, name: str):
        if name in self._jobs: self._jobs[name].paused = True

    def resume(self, name: str):
        if name in self._jobs: self._jobs[name].paused = False

    def _deps_satisfied(self, job: Job) -> bool:
        for dep in job.depends_on:
            dep_job = self._jobs.get(dep)
            if not dep_job: return False
            if dep_job.last_status != JobStatus.SUCCESS: return False
        return True

    def _due_jobs(self) -> List[Job]:
        now = time.time()
        due = [j for j in self._jobs.values()
                if (not j.paused and j.enabled and
                    j.next_run <= now and
                    (j.max_runs == 0 or j.run_count < j.max_runs) and
                    self._deps_satisfied(j) and
                    j.name not in self._running)]
        return sorted(due, key=lambda j: j.priority)

    async def _execute(self, job: Job) -> Execution:
        ex = Execution(id=str(uuid.uuid4())[:10], job_name=job.name,
                        status=JobStatus.RUNNING, started_at=time.time())
        self._store.save_execution(ex)
        retries = 0
        while True:
            try:
                coro = job.fn() if asyncio.iscoroutinefunction(job.fn) \
                    else asyncio.get_event_loop().run_in_executor(None, job.fn)
                if job.timeout_s > 0:
                    result = await asyncio.wait_for(coro, timeout=job.timeout_s)
                else:
                    result = await coro
                ex.result = result
                ex.status = JobStatus.SUCCESS
                break
            except asyncio.TimeoutError:
                ex.status = JobStatus.TIMEOUT
                ex.error  = f"Timeout after {job.timeout_s}s"
                break
            except Exception as e:
                ex.error = str(e)
                if retries < job.max_retries:
                    retries += 1; ex.retry_count = retries
                    delay = job.retry_delay_s * (2 ** (retries - 1))
                    await asyncio.sleep(delay)
                else:
                    ex.status = JobStatus.FAILED; break
        ex.finished_at = time.time()
        job.run_count += 1; job.last_run = ex.started_at
        job.last_status = ex.status
        job.compute_next(ex.finished_at)
        self._store.save_execution(ex)
        self._store.save_job_state(job)
        self._history[job.name].append(ex)
        if len(self._history[job.name]) > 50:
            self._history[job.name].pop(0)
        return ex

    async def run_now(self, name: str) -> Optional[Execution]:
        job = self._jobs.get(name)
        if not job: return None
        return await self._execute(job)

    async def tick(self):
        due = self._due_jobs()
        slots = self.max_concurrent - len(self._running)
        for job in due[:slots]:
            task = asyncio.ensure_future(self._execute(job))
            self._running[job.name] = task

            async def _cleanup(t=task, n=job.name):
                try: await t
                except: pass
                finally: self._running.pop(n, None)

            asyncio.ensure_future(_cleanup())

    async def run_forever(self):
        self._running_flag = True
        while self._running_flag:
            await self.tick()
            await asyncio.sleep(self._tick_s)

    def stop(self):
        self._running_flag = False

    def history(self, job_name: str, limit: int = 20) -> List[Dict]:
        return self._store.history(job_name, limit)

    def next_jobs(self, n: int = 5) -> List[Dict]:
        due = sorted(self._jobs.values(), key=lambda j: j.next_run)[:n]
        return [{"name": j.name, "next_run": round(j.next_run, 2),
                  "in_s": round(j.next_run - time.time(), 1)}
                 for j in due]

    def stats(self) -> Dict:
        s = self._store.stats()
        s["scheduled_jobs"] = len(self._jobs)
        s["running"] = len(self._running)
        s["paused"] = sum(1 for j in self._jobs.values() if j.paused)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def list_ep(req):
            return web.json_response(
                {"jobs": [j.to_dict() for j in self._jobs.values()]})
        async def run_now_ep(req):
            d = await req.json()
            ex = await self.run_now(d["name"])
            if not ex: return web.json_response({"error":"not found"},status=404)
            return web.json_response(ex.to_dict())
        async def pause_ep(req):
            d = await req.json()
            self.pause(d["name"]); return web.json_response({"paused": True})
        async def resume_ep(req):
            d = await req.json()
            self.resume(d["name"]); return web.json_response({"resumed": True})
        async def history_ep(req):
            name = req.match_info["name"]
            return web.json_response({"history": self.history(name)})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/jobs"
        app.router.add_get( f"{p}/list",          list_ep)
        app.router.add_post(f"{p}/run",            run_now_ep)
        app.router.add_post(f"{p}/pause",          pause_ep)
        app.router.add_post(f"{p}/resume",         resume_ep)
        app.router.add_get( f"{p}/{{name}}/history", history_ep)
        app.router.add_get( f"{p}/stats",          stats_ep)
        logger.info(f"Job scheduler API at {prefix}/jobs/")
