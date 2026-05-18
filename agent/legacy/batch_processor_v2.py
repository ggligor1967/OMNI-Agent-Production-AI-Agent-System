"""OMNI Agent — Batch Processor V2: chunking, progress, error handling, checkpointing."""
from __future__ import annotations
import json, sqlite3, threading, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Generator, Iterable, List, Optional, Tuple


class BatchStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    DONE      = "done"
    FAILED    = "failed"
    PAUSED    = "paused"
    CANCELLED = "cancelled"


class ErrorPolicy(str, Enum):
    STOP        = "stop"        # halt on first error
    SKIP        = "skip"        # skip failed items
    RETRY       = "retry"       # retry failed items N times
    COLLECT     = "collect"     # collect errors, continue


@dataclass
class BatchConfig:
    batch_size: int = 100
    max_workers: int = 1         # parallel processing threads
    error_policy: ErrorPolicy = ErrorPolicy.SKIP
    max_retries: int = 2
    retry_delay_s: float = 0.0
    timeout_s: Optional[float] = None
    progress_interval: int = 100  # report progress every N items
    checkpoint_interval: int = 0  # 0 = no checkpointing
    pre_batch_hook: Optional[Callable] = None
    post_batch_hook: Optional[Callable] = None
    on_item_error: Optional[Callable] = None


@dataclass
class BatchRun:
    run_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    job_id: str = ""
    status: BatchStatus = BatchStatus.PENDING
    total_items: int = 0
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    errors: List[Dict] = field(default_factory=list)
    results: List[Any] = field(default_factory=list)
    checkpoint: int = 0           # last checkpointed index
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def progress(self) -> float:
        return self.processed / self.total_items if self.total_items else 0.0

    @property
    def duration_s(self) -> float:
        end = self.finished_at or time.time()
        return end - self.started_at

    @property
    def throughput(self) -> float:
        d = self.duration_s
        return self.processed / d if d > 0 else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status.value,
            "total": self.total_items,
            "processed": self.processed,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "progress": round(self.progress, 4),
            "throughput": round(self.throughput, 2),
            "duration_s": round(self.duration_s, 3),
        }


@dataclass
class BatchJob:
    job_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = ""
    processor: Callable = field(default=lambda item: item)
    config: BatchConfig = field(default_factory=BatchConfig)
    created_at: float = field(default_factory=time.time)
    run_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "name": self.name,
            "batch_size": self.config.batch_size,
            "run_count": self.run_count,
        }


class BatchProcessorV2:
    """
    Configurable batch processing engine:
    - Chunked processing (configurable batch_size)
    - Parallel processing (N worker threads)
    - Error policies: STOP / SKIP / RETRY / COLLECT
    - Per-item retry with delay
    - Progress tracking and callbacks
    - Checkpointing (resume from last saved index)
    - Pre/post batch hooks
    - Job registry (reuse same config for multiple runs)
    - Generator input support (lazy evaluation)
    - Result aggregation and error collection
    - Run history with metrics (throughput, duration)
    - SQLite persistence
    """

    def __init__(self, db_path: str = ":memory:"):
        self._jobs:  Dict[str, BatchJob] = {}
        self._runs:  Dict[str, BatchRun] = {}
        self._active: Dict[str, bool] = {}   # run_id → running
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS bp_runs (
                run_id TEXT PRIMARY KEY, job_id TEXT,
                status TEXT, total INTEGER, processed INTEGER,
                succeeded INTEGER, failed INTEGER,
                started_at REAL, finished_at REAL
            );
        """)
        self._db.commit()

    # ── JOB MANAGEMENT ────────────────────────────────────────────────

    def create_job(self, name: str,
                   processor: Callable[[Any], Any],
                   config: Optional[BatchConfig] = None,
                   job_id: Optional[str] = None) -> BatchJob:
        jid = job_id or str(uuid.uuid4())[:8]
        job = BatchJob(job_id=jid, name=name,
                       processor=processor,
                       config=config or BatchConfig())
        self._jobs[jid] = job
        return job

    def get_job(self, job_id: str) -> Optional[BatchJob]:
        return self._jobs.get(job_id)

    # ── PROCESS ──────────────────────────────────────────────────────

    def process(self, items: Iterable[Any],
                processor: Callable[[Any], Any],
                config: Optional[BatchConfig] = None,
                run_id: Optional[str] = None,
                resume_from: int = 0) -> BatchRun:
        """Process items directly without creating a job."""
        cfg = config or BatchConfig()
        job = BatchJob(processor=processor, config=cfg)
        return self._run(job, items, run_id, resume_from)

    def run_job(self, job_id: str,
                items: Iterable[Any],
                run_id: Optional[str] = None,
                resume_from: int = 0) -> BatchRun:
        job = self._jobs.get(job_id)
        if not job:
            raise KeyError(f"Job {job_id} not found")
        job.run_count += 1
        return self._run(job, items, run_id, resume_from)

    def _run(self, job: BatchJob,
             items: Iterable[Any],
             run_id: Optional[str],
             resume_from: int) -> BatchRun:
        run = BatchRun(run_id=run_id or str(uuid.uuid4())[:8],
                       job_id=job.job_id)
        cfg = job.config
        self._runs[run.run_id]  = run
        self._active[run.run_id] = True
        run.status = BatchStatus.RUNNING

        # Materialise (needed for total count + checkpointing)
        all_items = list(items)
        run.total_items = len(all_items)
        start_idx = max(0, resume_from)
        run.checkpoint = start_idx

        # Chunk into batches
        batches = self._chunk(all_items[start_idx:], cfg.batch_size)

        global_idx = start_idx
        for batch_idx, batch in enumerate(batches):
            if not self._active.get(run.run_id, False):
                run.status = BatchStatus.CANCELLED
                break

            if cfg.pre_batch_hook:
                try: cfg.pre_batch_hook(batch_idx, batch)
                except Exception: pass

            if cfg.max_workers > 1:
                self._process_parallel(batch, job, run, cfg, global_idx)
            else:
                self._process_sequential(batch, job, run, cfg, global_idx)

            global_idx += len(batch)

            if cfg.post_batch_hook:
                try: cfg.post_batch_hook(batch_idx, batch, run)
                except Exception: pass

            # Checkpoint
            if (cfg.checkpoint_interval > 0 and
                    global_idx % cfg.checkpoint_interval == 0):
                run.checkpoint = global_idx
                self._persist(run)

            # Stop on STOP policy with errors
            if (cfg.error_policy == ErrorPolicy.STOP
                    and run.failed > 0):
                run.status = BatchStatus.FAILED
                break

        if run.status == BatchStatus.RUNNING:
            run.status = BatchStatus.DONE
        run.finished_at = time.time()
        self._persist(run)
        return run

    def _process_sequential(self, batch: List,
                              job: BatchJob, run: BatchRun,
                              cfg: BatchConfig, start_idx: int):
        for i, item in enumerate(batch):
            self._process_item(item, i + start_idx, job, run, cfg)

    def _process_parallel(self, batch: List,
                           job: BatchJob, run: BatchRun,
                           cfg: BatchConfig, start_idx: int):
        lock = threading.Lock()
        chunk_size = max(1, len(batch) // cfg.max_workers)
        sub_batches = self._chunk(batch, chunk_size)
        threads = []
        for si, sub in enumerate(sub_batches):
            base = start_idx + si * chunk_size
            t = threading.Thread(
                target=self._process_sequential_locked,
                args=(sub, job, run, cfg, base, lock))
            threads.append(t); t.start()
        for t in threads: t.join()

    def _process_sequential_locked(self, batch, job, run, cfg,
                                    start_idx, lock):
        for i, item in enumerate(batch):
            self._process_item(item, i + start_idx, job, run, cfg, lock)

    def _process_item(self, item: Any, idx: int,
                       job: BatchJob, run: BatchRun,
                       cfg: BatchConfig,
                       lock: Optional[threading.Lock] = None):
        attempts = 0
        while True:
            attempts += 1
            try:
                result = job.processor(item)
                if lock:
                    with lock:
                        run.results.append(result)
                        run.processed += 1
                        run.succeeded += 1
                else:
                    run.results.append(result)
                    run.processed += 1
                    run.succeeded += 1
                break
            except Exception as exc:
                if (cfg.error_policy == ErrorPolicy.RETRY
                        and attempts <= cfg.max_retries):
                    if cfg.retry_delay_s > 0:
                        time.sleep(cfg.retry_delay_s)
                    continue

                err_entry = {"index": idx, "item": str(item)[:200],
                             "error": str(exc), "attempts": attempts}
                if lock:
                    with lock:
                        run.errors.append(err_entry)
                        run.processed += 1
                        run.failed    += 1
                else:
                    run.errors.append(err_entry)
                    run.processed += 1
                    run.failed    += 1

                if cfg.on_item_error:
                    try: cfg.on_item_error(idx, item, exc)
                    except Exception: pass
                break

    # ── CONTROL ──────────────────────────────────────────────────────

    def cancel(self, run_id: str):
        self._active[run_id] = False
        run = self._runs.get(run_id)
        if run: run.status = BatchStatus.CANCELLED

    def get_run(self, run_id: str) -> Optional[BatchRun]:
        return self._runs.get(run_id)

    def list_runs(self, job_id: Optional[str] = None) -> List[Dict]:
        runs = list(self._runs.values())
        if job_id:
            runs = [r for r in runs if r.job_id == job_id]
        return [r.to_dict() for r in runs]

    @staticmethod
    def _chunk(items: List, size: int) -> List[List]:
        return [items[i:i + size] for i in range(0, len(items), size)]

    def _persist(self, run: BatchRun):
        self._db.execute(
            "INSERT OR REPLACE INTO bp_runs VALUES (?,?,?,?,?,?,?,?,?)",
            (run.run_id, run.job_id, run.status.value,
             run.total_items, run.processed,
             run.succeeded, run.failed,
             run.started_at, run.finished_at))
        self._db.commit()

    def stats(self) -> Dict[str, Any]:
        return {
            "jobs": len(self._jobs),
            "runs": len(self._runs),
            "total_processed": sum(r.processed for r in self._runs.values()),
            "total_succeeded": sum(r.succeeded for r in self._runs.values()),
            "total_failed":    sum(r.failed    for r in self._runs.values()),
        }
