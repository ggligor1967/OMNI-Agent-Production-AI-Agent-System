"""OMNI Agent — Batch Processor V3: partitioning, checkpointing, parallel execution."""
from __future__ import annotations
import json, sqlite3, threading, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


class BatchStatus(str, Enum):
    PENDING    = "pending"
    RUNNING    = "running"
    DONE       = "done"
    FAILED     = "failed"
    PARTIAL    = "partial"
    CHECKPOINTED = "checkpointed"


class PartitionStrategy(str, Enum):
    ROUND_ROBIN = "round_robin"
    HASH        = "hash"
    RANGE       = "range"
    CUSTOM      = "custom"


@dataclass
class BatchItem:
    item_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    data: Any = None
    partition_key: Optional[str] = None
    status: str = "pending"
    result: Any = None
    error: Optional[str] = None
    attempts: int = 0
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {"item_id": self.item_id, "status": self.status,
                "attempts": self.attempts}


@dataclass
class BatchJob:
    job_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = ""
    status: BatchStatus = BatchStatus.PENDING
    total: int = 0
    processed: int = 0
    succeeded: int = 0
    failed_count: int = 0
    skipped: int = 0
    checkpointed_at: int = 0   # last checkpointed item index
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    partitions: int = 1
    errors: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at) * 1000
        return 0.0

    @property
    def throughput(self) -> float:
        ms = self.duration_ms
        return self.processed / (ms / 1000) if ms else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id, "name": self.name,
            "status": self.status.value,
            "total": self.total, "processed": self.processed,
            "succeeded": self.succeeded, "failed": self.failed_count,
            "duration_ms": round(self.duration_ms, 2),
            "throughput_per_s": round(self.throughput, 2),
        }


class BatchProcessorV3:
    """
    Advanced batch processor:
    - Submit items individually or in bulk
    - Partition items by strategy: round-robin / hash / range / custom
    - Parallel partition execution (thread per partition)
    - Per-item retry with max attempts
    - Error isolation: one item failure doesn't stop the batch
    - Checkpointing: save progress every N items (resume on failure)
    - Pre/post batch hooks
    - Pre/post item hooks
    - Transform pipeline per item before processing
    - Dead-letter queue for permanently failed items
    - Result collection and aggregation
    - Progress callbacks
    - Named jobs with history
    - SQLite persistence for job metadata and checkpoints
    """

    def __init__(self, db_path: str = ":memory:",
                 default_workers: int = 4,
                 checkpoint_every: int = 100,
                 max_retries: int = 2):
        self._jobs:     Dict[str, BatchJob] = {}
        self._dlq:      List[BatchItem] = []
        self._pre_batch_hooks:  List[Callable] = []
        self._post_batch_hooks: List[Callable] = []
        self._pre_item_hooks:   List[Callable] = []
        self._post_item_hooks:  List[Callable] = []
        self._transforms:       List[Callable] = []
        self._progress_cb:      Optional[Callable] = None
        self._default_workers   = default_workers
        self._checkpoint_every  = checkpoint_every
        self._max_retries       = max_retries
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS bp_jobs (
                job_id TEXT PRIMARY KEY, name TEXT, status TEXT,
                total INTEGER, processed INTEGER, succeeded INTEGER,
                failed INTEGER, started_at REAL, finished_at REAL
            );
            CREATE TABLE IF NOT EXISTS bp_checkpoints (
                job_id TEXT, checkpoint_index INTEGER,
                ts REAL, PRIMARY KEY (job_id, checkpoint_index)
            );
        """)
        self._db.commit()

    # ── JOB SETUP ────────────────────────────────────────────────────

    def create_job(self, name: str,
                    partitions: int = 1,
                    metadata: Optional[Dict] = None,
                    job_id: Optional[str] = None) -> BatchJob:
        jid = job_id or str(uuid.uuid4())[:8]
        j   = BatchJob(job_id=jid, name=name,
                        partitions=partitions,
                        metadata=dict(metadata or {}))
        self._jobs[jid] = j
        return j

    # ── HOOKS & TRANSFORMS ───────────────────────────────────────────

    def on_before_batch(self, fn: Callable): self._pre_batch_hooks.append(fn)
    def on_after_batch(self, fn: Callable):  self._post_batch_hooks.append(fn)
    def on_before_item(self, fn: Callable):  self._pre_item_hooks.append(fn)
    def on_after_item(self, fn: Callable):   self._post_item_hooks.append(fn)
    def add_transform(self, fn: Callable):   self._transforms.append(fn)
    def on_progress(self, fn: Callable):     self._progress_cb = fn

    # ── PARTITIONING ─────────────────────────────────────────────────

    def _partition(self, items: List[BatchItem],
                    n: int,
                    strategy: PartitionStrategy,
                    key_fn: Optional[Callable] = None) -> List[List[BatchItem]]:
        partitions: List[List[BatchItem]] = [[] for _ in range(n)]
        for i, item in enumerate(items):
            if strategy == PartitionStrategy.ROUND_ROBIN:
                partitions[i % n].append(item)
            elif strategy == PartitionStrategy.HASH:
                key = item.partition_key or str(i)
                partitions[hash(key) % n].append(item)
            elif strategy == PartitionStrategy.CUSTOM and key_fn:
                idx = int(key_fn(item)) % n
                partitions[idx].append(item)
            else:
                # RANGE: divide equally
                chunk = max(1, len(items) // n)
                idx = min(i // chunk, n - 1)
                partitions[idx].append(item)
        return partitions

    # ── PROCESSING ───────────────────────────────────────────────────

    def run(self, items: Iterable,
             processor: Callable[[Any], Any],
             job_name: str = "batch",
             partitions: int = 1,
             workers: Optional[int] = None,
             strategy: PartitionStrategy = PartitionStrategy.ROUND_ROBIN,
             key_fn: Optional[Callable] = None,
             resume_from: int = 0) -> BatchJob:
        job = self.create_job(job_name, partitions=partitions)
        batch_items = [BatchItem(data=d) for d in items]

        # Resume
        if resume_from > 0:
            batch_items = batch_items[resume_from:]
            job.checkpointed_at = resume_from

        job.total      = len(batch_items) + resume_from
        job.processed  = resume_from
        job.status     = BatchStatus.RUNNING
        job.started_at = time.time()

        for fn in self._pre_batch_hooks:
            try: fn(job)
            except Exception: pass

        # Partition
        n_parts = max(1, partitions)
        parts   = self._partition(batch_items, n_parts, strategy, key_fn)
        n_work  = workers or min(self._default_workers, n_parts)

        lock = threading.Lock()

        def _process_partition(part: List[BatchItem]):
            for item in part:
                self._process_item(item, processor, job, lock)

        if n_work > 1 and n_parts > 1:
            threads = [threading.Thread(
                target=_process_partition, args=(p,), daemon=True)
                for p in parts if p]
            for t in threads: t.start()
            for t in threads: t.join()
        else:
            for part in parts:
                _process_partition(part)

        job.status      = (BatchStatus.DONE if job.failed_count == 0
                           else BatchStatus.PARTIAL)
        job.finished_at = time.time()

        for fn in self._post_batch_hooks:
            try: fn(job)
            except Exception: pass

        self._persist_job(job)
        return job

    def _process_item(self, item: BatchItem,
                       processor: Callable,
                       job: BatchJob,
                       lock: threading.Lock):
        for fn in self._pre_item_hooks:
            try: fn(item)
            except Exception: pass

        # Apply transforms
        data = item.data
        for t in self._transforms:
            try: data = t(data)
            except Exception: pass

        item.attempts = 0
        while item.attempts <= self._max_retries:
            item.attempts += 1
            try:
                item.result = processor(data)
                item.status = "done"
                with lock:
                    job.succeeded += 1
                break
            except Exception as exc:
                item.error = str(exc)
                if item.attempts > self._max_retries:
                    item.status = "failed"
                    with lock:
                        job.failed_count += 1
                        job.errors.append(f"item {item.item_id}: {exc}")
                    self._dlq.append(item)

        with lock:
            job.processed += 1
            # Checkpoint
            if (job.processed % self._checkpoint_every == 0):
                job.checkpointed_at = job.processed
                job.status          = BatchStatus.CHECKPOINTED
                self._save_checkpoint(job)

        for fn in self._post_item_hooks:
            try: fn(item)
            except Exception: pass

        if self._progress_cb:
            try: self._progress_cb(job.processed, job.total)
            except Exception: pass

    def _save_checkpoint(self, job: BatchJob):
        self._db.execute(
            "INSERT OR REPLACE INTO bp_checkpoints VALUES (?,?,?)",
            (job.job_id, job.checkpointed_at, time.time()))
        self._db.commit()

    def _persist_job(self, job: BatchJob):
        self._db.execute(
            "INSERT OR REPLACE INTO bp_jobs VALUES (?,?,?,?,?,?,?,?,?)",
            (job.job_id, job.name, job.status.value,
             job.total, job.processed, job.succeeded,
             job.failed_count, job.started_at, job.finished_at))
        self._db.commit()

    # ── MAP / FILTER SHORTCUTS ────────────────────────────────────────

    def map(self, items: Iterable,
             fn: Callable,
             **kwargs) -> Tuple[BatchJob, List[Any]]:
        import threading as _th
        items_list = list(items)
        ordered: List[Any] = []
        lock = _th.Lock()

        def proc(data):
            result = fn(data)
            with lock:
                ordered.append(result)
            return result

        job = self.run(items_list, proc, **kwargs)
        return job, ordered

    def filter(self, items: Iterable,
               predicate: Callable,
               **kwargs) -> Tuple[BatchJob, List[Any]]:
        items_list = list(items)
        passed: List[Any] = []
        lock = threading.Lock()

        def proc(data):
            if predicate(data):
                with lock:
                    passed.append(data)
            return predicate(data)

        job = self.run(items_list, proc, **kwargs)
        return job, passed

    # ── QUERY ─────────────────────────────────────────────────────────

    def get_job(self, job_id: str) -> Optional[BatchJob]:
        return self._jobs.get(job_id)

    def job_history(self, limit: int = 20) -> List[Dict]:
        jobs = sorted(self._jobs.values(),
                       key=lambda j: j.started_at or 0, reverse=True)
        return [j.to_dict() for j in jobs[:limit]]

    def dlq(self) -> List[Dict]:
        return [i.to_dict() for i in self._dlq]

    def flush_dlq(self) -> int:
        n = len(self._dlq)
        self._dlq.clear()
        return n

    def stats(self) -> Dict[str, Any]:
        jobs = list(self._jobs.values())
        return {
            "total_jobs": len(jobs),
            "done": sum(1 for j in jobs
                        if j.status in (BatchStatus.DONE, BatchStatus.PARTIAL)),
            "dlq_size": len(self._dlq),
        }
