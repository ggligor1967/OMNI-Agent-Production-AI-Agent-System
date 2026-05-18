"""
OMNI AGENT - Scheduler & Heartbeat
Cron-based job scheduling + periodic health monitoring.
"""
import asyncio
import logging
import time
import aiohttp
from typing import Callable, Dict, Optional
from dataclasses import dataclass, field
try:
    from croniter import croniter as _croniter
    _CRONITER_AVAILABLE = True
except ImportError:  # pragma: no cover
    _croniter = None
    _CRONITER_AVAILABLE = False
    logging.getLogger(__name__).warning(
        "croniter not installed; cron scheduling degraded to 15-min intervals."
    )
from agent.hooks import hooks, Event, EventType
from config import CONFIG

logger = logging.getLogger(__name__)


@dataclass
class Job:
    name: str
    cron_expr: str
    handler: Callable
    enabled: bool = True
    last_run: Optional[float] = None
    run_count: int = 0
    metadata: dict = field(default_factory=dict)

    def next_run_time(self) -> float:
        base = self.last_run or time.time()
        if _croniter is None:
            return base + 900  # 15-min fallback when croniter unavailable
        return _croniter(self.cron_expr, base).get_next(float)


class Scheduler:
    """Async cron scheduler with hook integration."""

    def __init__(self):
        self._jobs: Dict[str, Job] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None

    def add_job(self, name: str, cron_expr: str, handler: Callable,
                metadata: dict = None, enabled: bool = True) -> Job:
        if _croniter is None:
            logger.warning("croniter not installed; skipping cron expression validation")
        elif not _croniter.is_valid(cron_expr):
            raise ValueError(f"Invalid cron expression: {cron_expr}")
        job = Job(name=name, cron_expr=cron_expr, handler=handler,
                  enabled=enabled, metadata=metadata or {})
        self._jobs[name] = job
        logger.info(f"Job registered: '{name}' [{cron_expr}]")
        return job

    def remove_job(self, name: str):
        self._jobs.pop(name, None)

    def enable_job(self, name: str):
        if name in self._jobs:
            self._jobs[name].enabled = True

    def disable_job(self, name: str):
        if name in self._jobs:
            self._jobs[name].enabled = False

    async def _run_job(self, job: Job):
        logger.info(f"Running job: {job.name}")
        await hooks.emit(Event(EventType.JOB_STARTED, {"job": job.name}))
        try:
            if asyncio.iscoroutinefunction(job.handler):
                await job.handler()
            else:
                job.handler()
            job.last_run = time.time()
            job.run_count += 1
            await hooks.emit(Event(EventType.JOB_COMPLETED, {
                "job": job.name, "run_count": job.run_count
            }))
        except Exception as e:
            logger.error(f"Job '{job.name}' failed: {e}")
            await hooks.emit(Event(EventType.JOB_FAILED, {
                "job": job.name, "error": str(e)
            }))

    async def _loop(self):
        logger.info("Scheduler started.")
        while self._running:
            now = time.time()
            for job in list(self._jobs.values()):
                if not job.enabled:
                    continue
                next_t = job.next_run_time()
                if next_t <= now:
                    asyncio.create_task(self._run_job(job))
            await asyncio.sleep(10)  # tick every 10s

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    def list_jobs(self) -> list:
        return [
            {
                "name": j.name,
                "cron": j.cron_expr,
                "enabled": j.enabled,
                "last_run": j.last_run,
                "run_count": j.run_count,
                "next_run": j.next_run_time() if j.enabled else None
            }
            for j in self._jobs.values()
        ]


class HeartbeatMonitor:
    """Periodic health check with optional webhook ping."""

    def __init__(self, interval: int = None, webhook_url: str = None):
        self.interval = interval or CONFIG.HEARTBEAT_INTERVAL
        self.webhook_url = webhook_url or CONFIG.HEARTBEAT_WEBHOOK
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._checks: Dict[str, Callable] = {}
        self.last_status: Dict = {}

    def register_check(self, name: str, fn: Callable):
        """Register a health check function returning True/False."""
        self._checks[name] = fn

    async def _run_checks(self) -> Dict:
        status = {"ts": time.time(), "checks": {}, "healthy": True}
        for name, fn in self._checks.items():
            try:
                if asyncio.iscoroutinefunction(fn):
                    ok = await fn()
                else:
                    ok = fn()
                status["checks"][name] = "ok" if ok else "fail"
                if not ok:
                    status["healthy"] = False
            except Exception as e:
                status["checks"][name] = f"error: {e}"
                status["healthy"] = False
        return status

    async def _ping_webhook(self, status: Dict):
        if not self.webhook_url:
            return
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(self.webhook_url, json=status, timeout=aiohttp.ClientTimeout(total=5))
        except Exception as e:
            logger.warning(f"Heartbeat webhook failed: {e}")

    async def _loop(self):
        logger.info(f"Heartbeat started (interval={self.interval}s)")
        while self._running:
            status = await self._run_checks()
            self.last_status = status
            level = logging.INFO if status["healthy"] else logging.WARNING
            logger.log(level, f"Heartbeat: {'✓' if status['healthy'] else '✗'} {status['checks']}")
            await self._ping_webhook(status)
            await asyncio.sleep(self.interval)

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
