"""OMNI Agent — Task Queue V2: persistent priority queue with workers, retries, dead-letter."""
from __future__ import annotations
import json, queue, sqlite3, threading, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class TaskStatus(str, Enum):
    QUEUED    = "queued"
    RUNNING   = "running"
    DONE      = "done"
    FAILED    = "failed"
    RETRYING  = "retrying"
    DEAD      = "dead"       # exhausted retries → dead-letter
    CANCELLED = "cancelled"


class TaskPriority(int, Enum):
    CRITICAL = 0
    HIGH     = 1
    NORMAL   = 2
    LOW      = 3
    BULK     = 4


@dataclass
class Task:
    task_id: str
    name: str
    payload: Any
    priority: TaskPriority = TaskPriority.NORMAL
    status: TaskStatus = TaskStatus.QUEUED
    max_retries: int = 3
    retry_delay_s: float = 1.0
    timeout_s: Optional[float] = None
    attempt: int = 0
    result: Any = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    queue_name: str = "default"

    @property
    def duration_ms(self) -> float:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at) * 1000
        return 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "priority": self.priority.value,
            "status": self.status.value,
            "attempt": self.attempt,
            "duration_ms": round(self.duration_ms, 2),
            "error": self.error,
            "queue": self.queue_name,
        }


class TaskQueueV2:
    """
    Persistent priority task queue with:
    - Named queues with independent worker pools
    - Priority ordering (CRITICAL → BULK)
    - Per-task retry with configurable delay
    - Dead-letter queue for exhausted tasks
    - Timeout per task
    - Worker pool (N threads per queue)
    - Pre/post execution hooks
    - Pause/resume per queue
    - Task cancellation
    - SQLite persistence
    - Full task history and stats
    """

    def __init__(self, db_path: str = ":memory:"):
        self._tasks: Dict[str, Task] = {}
        self._queues: Dict[str, queue.PriorityQueue] = {}
        self._workers: Dict[str, List[threading.Thread]] = {}
        self._handlers: Dict[str, Callable] = {}
        self._paused: set = set()
        self._dead_letter: List[Task] = []
        self._pre_hooks:  List[Callable] = []
        self._post_hooks: List[Callable] = []
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._enqueue_count = 0
        self._done_count    = 0
        self._fail_count    = 0

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS tq_tasks (
                task_id TEXT PRIMARY KEY, name TEXT, queue_name TEXT,
                priority INTEGER, status TEXT, payload TEXT,
                attempt INTEGER, result TEXT, error TEXT,
                created_at REAL, started_at REAL, finished_at REAL
            );
        """)
        self._db.commit()

    # ── QUEUE MANAGEMENT ──────────────────────────────────────────────

    def create_queue(self, name: str, handler: Callable,
                     num_workers: int = 2,
                     auto_start: bool = True) -> str:
        if name not in self._queues:
            self._queues[name] = queue.PriorityQueue()
            self._handlers[name] = handler
            self._workers[name] = []
            if auto_start:
                self._start_workers(name, num_workers)
        return name

    def _start_workers(self, queue_name: str, count: int):
        for _ in range(count):
            t = threading.Thread(
                target=self._worker_loop,
                args=(queue_name,), daemon=True)
            t.start()
            self._workers[queue_name].append(t)

    def pause_queue(self, name: str):
        self._paused.add(name)

    def resume_queue(self, name: str):
        self._paused.discard(name)

    def register_handler(self, queue_name: str, fn: Callable):
        self._handlers[queue_name] = fn

    # ── ENQUEUE ───────────────────────────────────────────────────────

    def enqueue(self, name: str, payload: Any,
                queue_name: str = "default",
                priority: TaskPriority = TaskPriority.NORMAL,
                max_retries: int = 3,
                retry_delay_s: float = 1.0,
                timeout_s: Optional[float] = None,
                tags: Optional[List[str]] = None,
                task_id: Optional[str] = None,
                metadata: Optional[Dict] = None) -> Task:
        tid = task_id or str(uuid.uuid4())[:10]
        task = Task(
            task_id=tid, name=name, payload=payload,
            priority=priority, queue_name=queue_name,
            max_retries=max_retries, retry_delay_s=retry_delay_s,
            timeout_s=timeout_s, tags=list(tags or []),
            metadata=metadata or {})
        with self._lock:
            self._tasks[tid] = task
            self._enqueue_count += 1
        if queue_name in self._queues:
            self._queues[queue_name].put((priority.value, time.time(), tid))
        self._persist(task)
        return task

    def enqueue_batch(self, items: List[Dict]) -> List[Task]:
        return [self.enqueue(**item) for item in items]

    def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task and task.status == TaskStatus.QUEUED:
            task.status = TaskStatus.CANCELLED
            self._persist(task)
            return True
        return False

    # ── WORKER ────────────────────────────────────────────────────────

    def _worker_loop(self, queue_name: str):
        q = self._queues.get(queue_name)
        if not q: return
        while True:
            if queue_name in self._paused:
                time.sleep(0.1)
                continue
            try:
                _, _, tid = q.get(timeout=0.5)
            except queue.Empty:
                continue
            task = self._tasks.get(tid)
            if not task or task.status == TaskStatus.CANCELLED:
                q.task_done()
                continue
            self._execute(task, queue_name)
            q.task_done()

    def _execute(self, task: Task, queue_name: str):
        handler = self._handlers.get(queue_name)
        if not handler:
            return
        task.status     = TaskStatus.RUNNING
        task.started_at = time.time()
        task.attempt   += 1
        self._persist(task)

        for fn in self._pre_hooks:
            try: fn(task)
            except Exception: pass

        try:
            if task.timeout_s:
                result = [None]
                exc_box = [None]
                def _run():
                    try: result[0] = handler(task.payload)
                    except Exception as e: exc_box[0] = e
                t = threading.Thread(target=_run, daemon=True)
                t.start(); t.join(task.timeout_s)
                if t.is_alive():
                    raise TimeoutError(f"Task timed out after {task.timeout_s}s")
                if exc_box[0]: raise exc_box[0]
                task.result = result[0]
            else:
                task.result = handler(task.payload)

            task.status      = TaskStatus.DONE
            task.finished_at = time.time()
            self._done_count += 1

        except Exception as exc:
            task.error = str(exc)
            if task.attempt <= task.max_retries:
                task.status = TaskStatus.RETRYING
                self._persist(task)
                time.sleep(task.retry_delay_s)
                self._queues[queue_name].put(
                    (task.priority.value, time.time(), task.task_id))
            else:
                task.status      = TaskStatus.DEAD
                task.finished_at = time.time()
                self._dead_letter.append(task)
                self._fail_count += 1

        self._persist(task)

        for fn in self._post_hooks:
            try: fn(task)
            except Exception: pass

    # ── SYNC EXECUTION (no workers) ───────────────────────────────────

    def run_sync(self, task_id: str,
                 queue_name: str = "default") -> Task:
        """Execute a task synchronously (blocking), with retries."""
        task = self._tasks.get(task_id)
        if not task:
            raise KeyError(f"Task {task_id} not found")
        handler = self._handlers.get(queue_name)
        if not handler:
            raise KeyError(f"No handler for queue '{queue_name}'")
        # Run with retries inline
        task.status     = TaskStatus.QUEUED
        task.attempt    = 0
        while True:
            task.status     = TaskStatus.RUNNING
            task.started_at = task.started_at or time.time()
            task.attempt   += 1
            for fn in self._pre_hooks:
                try: fn(task)
                except Exception: pass
            try:
                task.result      = handler(task.payload)
                task.status      = TaskStatus.DONE
                task.finished_at = time.time()
                self._done_count += 1
                self._persist(task)
                for fn in self._post_hooks:
                    try: fn(task)
                    except Exception: pass
                return task
            except Exception as exc:
                task.error = str(exc)
                if task.attempt <= task.max_retries:
                    task.status = TaskStatus.RETRYING
                    self._persist(task)
                    time.sleep(task.retry_delay_s)
                else:
                    task.status      = TaskStatus.DEAD
                    task.finished_at = time.time()
                    self._dead_letter.append(task)
                    self._fail_count += 1
                    self._persist(task)
                    for fn in self._post_hooks:
                        try: fn(task)
                        except Exception: pass
                    return task

    # ── HOOKS ─────────────────────────────────────────────────────────

    def on_pre_execute(self, fn: Callable):  self._pre_hooks.append(fn)
    def on_post_execute(self, fn: Callable): self._post_hooks.append(fn)

    # ── QUERY ─────────────────────────────────────────────────────────

    def get_task(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def list_tasks(self, status: Optional[TaskStatus] = None,
                   queue_name: Optional[str] = None,
                   tag: Optional[str] = None,
                   limit: int = 50) -> List[Dict]:
        tasks = list(self._tasks.values())
        if status:
            tasks = [t for t in tasks if t.status == status]
        if queue_name:
            tasks = [t for t in tasks if t.queue_name == queue_name]
        if tag:
            tasks = [t for t in tasks if tag in t.tags]
        return [t.to_dict() for t in tasks[-limit:]]

    def dead_letter(self) -> List[Dict]:
        return [t.to_dict() for t in self._dead_letter]

    def queue_depth(self, queue_name: str) -> int:
        q = self._queues.get(queue_name)
        return q.qsize() if q else 0

    def task_history(self, limit: int = 50) -> List[Dict]:
        rows = self._db.execute(
            "SELECT task_id,name,queue_name,status,attempt,"
            "created_at,finished_at FROM tq_tasks "
            "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [{"task_id": r[0], "name": r[1], "queue": r[2],
                 "status": r[3], "attempt": r[4]} for r in rows]

    def _persist(self, task: Task):
        self._db.execute(
            "INSERT OR REPLACE INTO tq_tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (task.task_id, task.name, task.queue_name,
             task.priority.value, task.status.value,
             json.dumps(task.payload, default=str),
             task.attempt,
             json.dumps(task.result, default=str) if task.result is not None else None,
             task.error, task.created_at,
             task.started_at, task.finished_at))
        self._db.commit()

    def stats(self) -> Dict[str, Any]:
        return {
            "queues": len(self._queues),
            "total_tasks": len(self._tasks),
            "enqueued": self._enqueue_count,
            "done": self._done_count,
            "failed": self._fail_count,
            "dead_letter": len(self._dead_letter),
        }
