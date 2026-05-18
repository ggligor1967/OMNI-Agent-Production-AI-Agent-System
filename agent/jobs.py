"""
OMNI AGENT - Background Jobs
Persistent SQLite-backed job queue with priority scheduling, automatic retry,
cron-style recurring jobs, and a worker pool.

Features:
- Named job types with registered handler functions
- Priority levels: critical(0) > high(1) > normal(2) > low(3)
- SQLite persistence: jobs survive process restarts
- Automatic retry with configurable backoff and max attempts
- Cron-style recurring jobs using cron expression parsing (5-field)
- Worker pool: configurable concurrency (default 4 workers)
- Job lifecycle: pending → running → completed | failed | retrying
- Dead-letter queue: failed jobs after max_attempts
- Progress tracking: jobs can update their own progress 0-100
- REST API: submit, cancel, get status, list, DLQ inspection
"""
import re
import time
import uuid
import json
import asyncio
import sqlite3
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# JOB MODEL
# ══════════════════════════════════════════════════════════════════════════════

class JobStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"
    RETRYING  = "retrying"
    CANCELLED = "cancelled"
    DEAD      = "dead"       # exceeded max_attempts


class JobPriority(int, Enum):
    CRITICAL = 0
    HIGH     = 1
    NORMAL   = 2
    LOW      = 3


@dataclass
class Job:
    id: str
    job_type: str
    payload: Dict[str, Any]
    priority: int = JobPriority.NORMAL
    status: JobStatus = JobStatus.PENDING
    max_attempts: int = 3
    attempt: int = 0
    progress: int = 0           # 0-100
    result: Optional[Any] = None
    error: str = ""
    created_at: float = field(default_factory=time.time)
    scheduled_at: float = field(default_factory=time.time)  # run_at time
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    recur_cron: str = ""        # cron expression for recurring jobs
    parent_id: str = ""         # for chained/subtask jobs
    tags: List[str] = field(default_factory=list)
    timeout_s: float = 300.0

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "type": self.job_type,
            "priority": self.priority,
            "status": self.status,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "scheduled_at": self.scheduled_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "recur_cron": self.recur_cron,
            "tags": self.tags,
            "payload_keys": list(self.payload.keys()),
        }

    @property
    def duration_s(self) -> Optional[float]:
        if self.started_at and self.completed_at:
            return self.completed_at - self.started_at
        return None


# ══════════════════════════════════════════════════════════════════════════════
# CRON PARSER
# ══════════════════════════════════════════════════════════════════════════════

class CronSchedule:
    """
    Simple 5-field cron parser: minute hour dom month dow
    Supports: * / , -
    Example: "*/5 * * * *" → every 5 minutes
    """

    def __init__(self, expr: str):
        self.expr = expr.strip()
        parts = self.expr.split()
        if len(parts) != 5:
            raise ValueError(f"Cron expression must have 5 fields: '{expr}'")
        self._minute, self._hour, self._dom, self._month, self._dow = parts

    def _matches_field(self, field: str, value: int,
                        min_val: int, max_val: int) -> bool:
        """Check if a single cron field matches a value."""
        if field == "*":
            return True
        for part in field.split(","):
            if "/" in part:
                base, step = part.split("/", 1)
                step = int(step)
                start = min_val if base == "*" else int(base)
                if value >= start and (value - start) % step == 0:
                    return True
            elif "-" in part:
                lo, hi = part.split("-", 1)
                if int(lo) <= value <= int(hi):
                    return True
            elif part == "*":
                return True
            elif int(part) == value:
                return True
        return False

    def matches(self, ts: float = None) -> bool:
        """Return True if the given timestamp matches this cron schedule."""
        import datetime
        t = datetime.datetime.fromtimestamp(ts or time.time())
        return (
            self._matches_field(self._minute, t.minute,  0, 59) and
            self._matches_field(self._hour,   t.hour,    0, 23) and
            self._matches_field(self._dom,    t.day,     1, 31) and
            self._matches_field(self._month,  t.month,   1, 12) and
            self._matches_field(self._dow,    t.weekday(), 0, 6)
        )

    def next_run(self, after: float = None) -> float:
        """Return the next Unix timestamp matching this schedule."""
        import datetime
        t = datetime.datetime.fromtimestamp(after or time.time())
        t = t.replace(second=0, microsecond=0) + datetime.timedelta(minutes=1)
        for _ in range(366 * 24 * 60):  # search up to 1 year
            if self.matches(t.timestamp()):
                return t.timestamp()
            t += datetime.timedelta(minutes=1)
        raise ValueError(f"No next run found for: {self.expr}")


# ══════════════════════════════════════════════════════════════════════════════
# JOB STORE (SQLite)
# ══════════════════════════════════════════════════════════════════════════════

class JobStore:
    """SQLite-backed persistent job store."""

    def __init__(self, db_path: str = "data/jobs.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id           TEXT PRIMARY KEY,
                    job_type     TEXT NOT NULL,
                    payload      TEXT NOT NULL,
                    priority     INTEGER DEFAULT 2,
                    status       TEXT DEFAULT 'pending',
                    max_attempts INTEGER DEFAULT 3,
                    attempt      INTEGER DEFAULT 0,
                    progress     INTEGER DEFAULT 0,
                    result       TEXT,
                    error        TEXT DEFAULT '',
                    created_at   REAL,
                    scheduled_at REAL,
                    started_at   REAL,
                    completed_at REAL,
                    recur_cron   TEXT DEFAULT '',
                    parent_id    TEXT DEFAULT '',
                    tags         TEXT DEFAULT '[]',
                    timeout_s    REAL DEFAULT 300
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, priority, scheduled_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_type ON jobs(job_type, status);
            """)

    def save(self, job: Job):
        with self._conn() as c:
            c.execute("""
                INSERT OR REPLACE INTO jobs
                (id,job_type,payload,priority,status,max_attempts,attempt,
                 progress,result,error,created_at,scheduled_at,started_at,
                 completed_at,recur_cron,parent_id,tags,timeout_s)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                job.id, job.job_type, json.dumps(job.payload),
                job.priority, job.status, job.max_attempts, job.attempt,
                job.progress, json.dumps(job.result) if job.result is not None else None,
                job.error, job.created_at, job.scheduled_at, job.started_at,
                job.completed_at, job.recur_cron, job.parent_id,
                json.dumps(job.tags), job.timeout_s,
            ))

    def get(self, job_id: str) -> Optional[Job]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def claim_next(self, worker_id: str) -> Optional[Job]:
        """Atomically claim the next available pending job."""
        with self._conn() as c:
            row = c.execute("""
                SELECT * FROM jobs
                WHERE status IN ('pending','retrying') AND scheduled_at <= ?
                ORDER BY priority ASC, scheduled_at ASC
                LIMIT 1
            """, (time.time(),)).fetchone()
            if not row:
                return None
            job_id = row["id"]
            cur = c.execute("""
                UPDATE jobs SET status='running', started_at=?, attempt=attempt+1
                WHERE id=? AND status IN ('pending','retrying')
            """, (time.time(), job_id))
            if cur.rowcount == 0:
                return None  # Race condition — another worker got it
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def update_status(self, job_id: str, status: JobStatus,
                      result: Any = None, error: str = "",
                      progress: int = None,
                      scheduled_at: float = None):
        now = time.time()
        updates = ["status=?", "error=?"]
        params: List[Any] = [status, error]
        if result is not None:
            updates.append("result=?")
            params.append(json.dumps(result))
        if progress is not None:
            updates.append("progress=?")
            params.append(progress)
        if status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.DEAD):
            updates.append("completed_at=?")
            params.append(now)
        if scheduled_at is not None:
            updates.append("scheduled_at=?")
            params.append(scheduled_at)
        params.append(job_id)
        with self._conn() as c:
            c.execute(f"UPDATE jobs SET {', '.join(updates)} WHERE id=?", params)

    def list_jobs(self, status: JobStatus = None,
                  job_type: str = None,
                  limit: int = 50) -> List[Job]:
        with self._conn() as c:
            q, params = "SELECT * FROM jobs", []
            conditions = []
            if status:
                conditions.append("status=?"); params.append(status)
            if job_type:
                conditions.append("job_type=?"); params.append(job_type)
            if conditions:
                q += " WHERE " + " AND ".join(conditions)
            q += " ORDER BY scheduled_at DESC LIMIT ?"
            params.append(limit)
            rows = c.execute(q, params).fetchall()
        return [self._row_to_job(r) for r in rows]

    def cancel(self, job_id: str) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE jobs SET status='cancelled' WHERE id=? AND status IN ('pending','retrying')",
                (job_id,)
            )
        return cur.rowcount > 0

    def stats(self) -> Dict:
        with self._conn() as c:
            by_status = dict(c.execute(
                "SELECT status, COUNT(*) FROM jobs GROUP BY status"
            ).fetchall())
            by_type = dict(c.execute(
                "SELECT job_type, COUNT(*) FROM jobs WHERE status='pending' GROUP BY job_type"
            ).fetchall())
        return {"by_status": by_status, "pending_by_type": by_type}

    def _row_to_job(self, row) -> Job:
        return Job(
            id=row["id"], job_type=row["job_type"],
            payload=json.loads(row["payload"]),
            priority=row["priority"],
            status=JobStatus(row["status"]),
            max_attempts=row["max_attempts"],
            attempt=row["attempt"],
            progress=row["progress"],
            result=json.loads(row["result"]) if row["result"] else None,
            error=row["error"] or "",
            created_at=row["created_at"],
            scheduled_at=row["scheduled_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            recur_cron=row["recur_cron"] or "",
            parent_id=row["parent_id"] or "",
            tags=json.loads(row["tags"] or "[]"),
            timeout_s=row["timeout_s"],
        )


# ══════════════════════════════════════════════════════════════════════════════
# JOB CONTEXT (passed to handlers)
# ══════════════════════════════════════════════════════════════════════════════

class JobContext:
    """Passed to job handlers so they can update progress and log."""

    def __init__(self, job: Job, store: JobStore):
        self._job = job
        self._store = store
        self.job_id = job.id
        self.payload = job.payload
        self.attempt = job.attempt

    def update_progress(self, pct: int, message: str = ""):
        pct = max(0, min(100, pct))
        self._store.update_status(self.job_id, JobStatus.RUNNING, progress=pct)
        if message:
            logger.info(f"Job {self.job_id}: {pct}% — {message}")

    def log(self, message: str):
        logger.info(f"Job {self.job_id} (attempt {self.attempt}): {message}")


# ══════════════════════════════════════════════════════════════════════════════
# JOB QUEUE (main class)
# ══════════════════════════════════════════════════════════════════════════════

HandlerFn = Callable[[JobContext], Any]   # async or sync


class JobQueue:
    """
    Background job queue with persistent storage and worker pool.

    Usage:
        queue = JobQueue()

        # Register handlers
        @queue.handler("send_email")
        async def handle_email(ctx: JobContext):
            ctx.update_progress(50, "Connecting to SMTP...")
            await send_smtp(ctx.payload["to"], ctx.payload["subject"])
            ctx.update_progress(100)
            return {"sent": True}

        # Submit jobs
        job_id = await queue.submit("send_email",
                                     {"to": "user@example.com", "subject": "Hi"},
                                     priority=JobPriority.HIGH)

        # Schedule a recurring job (every hour)
        queue.schedule_cron("cleanup", {"scope": "tmp"}, cron="0 * * * *")

        # Start worker pool
        await queue.start(workers=4)

        # Status
        job = queue.get_job(job_id)
        print(job.status, job.progress)
    """

    def __init__(self, db_path: str = "data/jobs.db"):
        self.store = JobStore(db_path)
        self._handlers: Dict[str, HandlerFn] = {}
        self._workers: List[asyncio.Task] = []
        self._cron_task: Optional[asyncio.Task] = None
        self._running = False
        self._poll_interval = 1.0  # seconds between queue polls

    # ── Handler Registration ──────────────────────────────────────────────────

    def handler(self, job_type: str):
        """Decorator to register a job handler."""
        def decorator(fn: HandlerFn) -> HandlerFn:
            self.register_handler(job_type, fn)
            return fn
        return decorator

    def register_handler(self, job_type: str, fn: HandlerFn):
        self._handlers[job_type] = fn
        logger.info(f"Job handler registered: '{job_type}'")

    # ── Submission ────────────────────────────────────────────────────────────

    async def submit(self, job_type: str, payload: Dict[str, Any],
                     priority: JobPriority = JobPriority.NORMAL,
                     delay_s: float = 0.0,
                     max_attempts: int = 3,
                     timeout_s: float = 300.0,
                     tags: List[str] = None,
                     parent_id: str = "") -> str:
        """Submit a job to the queue. Returns job ID."""
        job = Job(
            id=str(uuid.uuid4())[:12],
            job_type=job_type,
            payload=payload,
            priority=int(priority),
            max_attempts=max_attempts,
            scheduled_at=time.time() + delay_s,
            timeout_s=timeout_s,
            tags=tags or [],
            parent_id=parent_id,
        )
        self.store.save(job)
        logger.info(f"Job submitted: id={job.id} type={job_type} "
                   f"priority={priority} delay={delay_s}s")
        return job.id

    def schedule_cron(self, job_type: str, payload: Dict[str, Any],
                      cron: str,
                      max_attempts: int = 3,
                      tags: List[str] = None) -> str:
        """Register a recurring cron job. Returns a stable cron job ID."""
        schedule = CronSchedule(cron)
        next_run = schedule.next_run()
        job_id = f"cron:{job_type}:{hashlib.cron(cron)}" if False else f"cron:{job_type}"
        job = Job(
            id=f"cron:{job_type}:{abs(hash(cron)) % 100000:05d}",
            job_type=job_type,
            payload={**payload, "_cron": cron},
            priority=int(JobPriority.LOW),
            max_attempts=max_attempts,
            scheduled_at=next_run,
            recur_cron=cron,
            tags=tags or ["cron"],
        )
        self.store.save(job)
        logger.info(f"Cron job scheduled: type={job_type} cron='{cron}' "
                   f"next_run={next_run:.0f}")
        return job.id

    # ── Worker Pool ───────────────────────────────────────────────────────────

    async def start(self, workers: int = 4, poll_interval: float = 1.0):
        """Start the worker pool."""
        self._running = True
        self._poll_interval = poll_interval
        self._workers = [
            asyncio.create_task(self._worker_loop(f"worker-{i}"))
            for i in range(workers)
        ]
        self._cron_task = asyncio.create_task(self._cron_loop())
        logger.info(f"JobQueue started: {workers} workers, poll={poll_interval}s")

    async def stop(self):
        """Gracefully stop all workers."""
        self._running = False
        for w in self._workers:
            w.cancel()
        if self._cron_task:
            self._cron_task.cancel()
        await asyncio.gather(*self._workers, self._cron_task or asyncio.sleep(0),
                            return_exceptions=True)
        logger.info("JobQueue stopped")

    async def _worker_loop(self, worker_id: str):
        while self._running:
            job = self.store.claim_next(worker_id)
            if not job:
                await asyncio.sleep(self._poll_interval)
                continue
            await self._execute(job)

    async def _cron_loop(self):
        """Reschedule completed cron jobs."""
        while self._running:
            await asyncio.sleep(30)
            try:
                completed_crons = [
                    j for j in self.store.list_jobs(status=JobStatus.COMPLETED, limit=1000)
                    if j.recur_cron
                ]
                for job in completed_crons:
                    try:
                        schedule = CronSchedule(job.recur_cron)
                        next_run = schedule.next_run()
                        # Re-queue as new job
                        new_job = Job(
                            id=f"cron:{job.job_type}:{abs(hash(job.recur_cron)) % 100000:05d}",
                            job_type=job.job_type,
                            payload=job.payload,
                            priority=job.priority,
                            max_attempts=job.max_attempts,
                            scheduled_at=next_run,
                            recur_cron=job.recur_cron,
                            tags=job.tags,
                        )
                        self.store.save(new_job)
                    except Exception as e:
                        logger.warning(f"Cron reschedule error for {job.id}: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cron loop error: {e}")

    async def _execute(self, job: Job):
        """Execute a single job with timeout, error handling, and retry."""
        handler = self._handlers.get(job.job_type)
        if not handler:
            logger.error(f"No handler for job type: '{job.job_type}'")
            self.store.update_status(
                job.id, JobStatus.DEAD,
                error=f"No handler registered for job type: {job.job_type}"
            )
            return

        ctx = JobContext(job, self.store)
        logger.info(f"Executing job: id={job.id} type={job.job_type} attempt={job.attempt}")

        try:
            if asyncio.iscoroutinefunction(handler):
                result = await asyncio.wait_for(handler(ctx), timeout=job.timeout_s)
            else:
                result = handler(ctx)

            # Reschedule if recurring
            if job.recur_cron:
                self.store.update_status(
                    job.id, JobStatus.COMPLETED,
                    result=result, progress=100,
                )
            else:
                self.store.update_status(
                    job.id, JobStatus.COMPLETED,
                    result=result, progress=100,
                )
            logger.info(f"Job completed: id={job.id} type={job.job_type}")

        except asyncio.TimeoutError:
            error = f"Timed out after {job.timeout_s}s"
            self._handle_failure(job, error)
        except Exception as e:
            self._handle_failure(job, str(e))

    def _handle_failure(self, job: Job, error: str):
        logger.warning(f"Job failed: id={job.id} attempt={job.attempt}/{job.max_attempts} error={error}")
        if job.attempt >= job.max_attempts:
            self.store.update_status(job.id, JobStatus.DEAD, error=error)
            logger.error(f"Job moved to DLQ: id={job.id} type={job.job_type}")
        else:
            # Retry with exponential backoff
            delay = 2 ** job.attempt
            next_attempt_at = time.time() + delay
            self.store.update_status(
                job.id, JobStatus.RETRYING,
                error=error, scheduled_at=next_attempt_at
            )

    # ── Query API ─────────────────────────────────────────────────────────────

    def get_job(self, job_id: str) -> Optional[Job]:
        return self.store.get(job_id)

    def list_jobs(self, status: JobStatus = None,
                  job_type: str = None, limit: int = 50) -> List[Job]:
        return self.store.list_jobs(status, job_type, limit)

    def cancel(self, job_id: str) -> bool:
        return self.store.cancel(job_id)

    def stats(self) -> Dict:
        return self.store.stats()

    def dead_letter_queue(self) -> List[Job]:
        return self.store.list_jobs(status=JobStatus.DEAD, limit=100)

    # ── REST API ──────────────────────────────────────────────────────────────

    def register_routes(self, app, prefix: str = ""):
        from aiohttp import web

        async def submit_job(request):
            data = await request.json()
            job_id = await self.submit(
                job_type=data["type"],
                payload=data.get("payload", {}),
                priority=JobPriority(int(data.get("priority", JobPriority.NORMAL))),
                delay_s=float(data.get("delay_s", 0)),
                max_attempts=int(data.get("max_attempts", 3)),
                timeout_s=float(data.get("timeout_s", 300)),
                tags=data.get("tags", []),
            )
            return web.json_response({"job_id": job_id}, status=201)

        async def get_job_status(request):
            job_id = request.match_info["id"]
            job = self.get_job(job_id)
            if not job:
                return web.json_response({"error": "not found"}, status=404)
            return web.json_response(job.to_dict())

        async def cancel_job(request):
            job_id = request.match_info["id"]
            ok = self.cancel(job_id)
            return web.json_response({"cancelled": ok})

        async def list_jobs_endpoint(request):
            status = request.rel_url.query.get("status")
            jtype  = request.rel_url.query.get("type")
            limit  = int(request.rel_url.query.get("limit", 50))
            if status:
                status = JobStatus(status)
            jobs = self.list_jobs(status, jtype, limit)
            return web.json_response({"jobs": [j.to_dict() for j in jobs]})

        async def dlq_endpoint(request):
            return web.json_response({"dead": [j.to_dict() for j in self.dead_letter_queue()]})

        async def stats_endpoint(request):
            return web.json_response(self.stats())

        app.router.add_post(f"{prefix}/jobs",             submit_job)
        app.router.add_get( f"{prefix}/jobs",             list_jobs_endpoint)
        app.router.add_get( f"{prefix}/jobs/{{id}}",      get_job_status)
        app.router.add_post(f"{prefix}/jobs/{{id}}/cancel", cancel_job)
        app.router.add_get( f"{prefix}/jobs/dlq",         dlq_endpoint)
        app.router.add_get( f"{prefix}/jobs/stats",       stats_endpoint)
        logger.info(f"Jobs API routes registered at {prefix}/jobs")


# Convenience import
import hashlib
