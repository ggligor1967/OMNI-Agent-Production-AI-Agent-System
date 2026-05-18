"""OMNI Agent — Model Fine-Tuner: fine-tuning job lifecycle, hyperparameter management, metrics."""
from __future__ import annotations
import json, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class JobStatus(str, Enum):
    QUEUED     = "queued"
    PREPARING  = "preparing"
    TRAINING   = "training"
    EVALUATING = "evaluating"
    DONE       = "done"
    FAILED     = "failed"
    CANCELLED  = "cancelled"
    PAUSED     = "paused"


class TrainingObjective(str, Enum):
    INSTRUCTION_FOLLOWING = "instruction_following"
    CLASSIFICATION        = "classification"
    SUMMARIZATION         = "summarization"
    CODE_GENERATION       = "code_generation"
    CONVERSATION          = "conversation"
    CUSTOM                = "custom"


@dataclass
class Hyperparams:
    learning_rate: float  = 2e-5
    epochs: int           = 3
    batch_size: int       = 8
    max_seq_length: int   = 512
    warmup_ratio: float   = 0.1
    weight_decay: float   = 0.01
    grad_accumulation: int = 4
    lr_scheduler: str     = "cosine"     # cosine|linear|constant
    optimizer: str        = "adamw"
    dropout: float        = 0.1
    lora_r: int           = 8            # LoRA rank (if applicable)
    lora_alpha: float     = 16.0
    lora_dropout: float   = 0.05
    extras: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "learning_rate": self.learning_rate,
            "epochs": self.epochs,
            "batch_size": self.batch_size,
            "max_seq_length": self.max_seq_length,
            "warmup_ratio": self.warmup_ratio,
            "weight_decay": self.weight_decay,
            "optimizer": self.optimizer,
            "lr_scheduler": self.lr_scheduler,
            "lora_r": self.lora_r,
            "lora_alpha": self.lora_alpha,
        }


@dataclass
class TrainingMetrics:
    step: int
    epoch: float
    loss: float
    eval_loss: Optional[float] = None
    accuracy: Optional[float] = None
    perplexity: Optional[float] = None
    lr: Optional[float] = None
    ts: float = field(default_factory=time.time)
    extras: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step": self.step,
            "epoch": round(self.epoch, 3),
            "loss": round(self.loss, 6),
            "eval_loss": round(self.eval_loss, 6) if self.eval_loss is not None else None,
            "accuracy": round(self.accuracy, 4) if self.accuracy is not None else None,
            "perplexity": round(self.perplexity, 4) if self.perplexity is not None else None,
        }


@dataclass
class FineTuneJob:
    job_id: str
    name: str
    base_model: str
    objective: TrainingObjective
    hyperparams: Hyperparams = field(default_factory=Hyperparams)
    status: JobStatus = JobStatus.QUEUED
    dataset_id: str = ""
    output_model_id: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    metrics: List[TrainingMetrics] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    notes: str = ""
    priority: int = 0
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration_s(self) -> Optional[float]:
        if self.started_at and self.finished_at:
            return self.finished_at - self.started_at
        return None

    @property
    def best_loss(self) -> Optional[float]:
        losses = [m.loss for m in self.metrics]
        return min(losses) if losses else None

    @property
    def best_eval_loss(self) -> Optional[float]:
        losses = [m.eval_loss for m in self.metrics if m.eval_loss is not None]
        return min(losses) if losses else None

    @property
    def current_epoch(self) -> float:
        return self.metrics[-1].epoch if self.metrics else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "name": self.name,
            "base_model": self.base_model,
            "objective": self.objective.value,
            "status": self.status.value,
            "priority": self.priority,
            "best_loss": round(self.best_loss, 6) if self.best_loss else None,
            "best_eval_loss": round(self.best_eval_loss, 6) if self.best_eval_loss else None,
            "current_epoch": self.current_epoch,
            "duration_s": round(self.duration_s, 1) if self.duration_s else None,
            "steps": len(self.metrics),
            "tags": self.tags,
        }


class EarlyStopping:
    """Monitors eval_loss and signals stop when no improvement."""

    def __init__(self, patience: int = 3, min_delta: float = 1e-4):
        self.patience   = patience
        self.min_delta  = min_delta
        self._best      = float("inf")
        self._bad_count = 0

    def step(self, eval_loss: float) -> bool:
        """Returns True if training should stop."""
        if eval_loss < self._best - self.min_delta:
            self._best = eval_loss
            self._bad_count = 0
        else:
            self._bad_count += 1
        return self._bad_count >= self.patience

    def reset(self):
        self._best = float("inf")
        self._bad_count = 0


class ModelFineTuner:
    """
    Manages fine-tuning job lifecycle, hyperparameter experiments,
    metric tracking, early stopping, and checkpoint management.
    Does NOT call real training APIs — manages the job metadata layer.
    """

    def __init__(self, db_path: str = ":memory:"):
        self._jobs: Dict[str, FineTuneJob] = {}
        self._checkpoints: Dict[str, List[Dict]] = {}  # job_id → [checkpoint]
        self._hooks: Dict[str, List[Callable]] = {}
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS ft_jobs (
                job_id TEXT PRIMARY KEY, name TEXT, base_model TEXT,
                objective TEXT, status TEXT, created_at REAL,
                started_at REAL, finished_at REAL, hyperparams TEXT,
                dataset_id TEXT, tags TEXT, notes TEXT, error TEXT
            );
            CREATE TABLE IF NOT EXISTS ft_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT, step INTEGER, epoch REAL, loss REAL,
                eval_loss REAL, accuracy REAL, ts REAL
            );
            CREATE TABLE IF NOT EXISTS ft_checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT, step INTEGER, path TEXT, loss REAL, ts REAL
            );
        """)
        self._db.commit()

    # ── JOB MANAGEMENT ────────────────────────────────────────────────

    def create_job(self, name: str, base_model: str,
                   objective: TrainingObjective = TrainingObjective.INSTRUCTION_FOLLOWING,
                   hyperparams: Optional[Hyperparams] = None,
                   dataset_id: str = "",
                   tags: Optional[List[str]] = None,
                   priority: int = 0,
                   notes: str = "",
                   metadata: Optional[Dict] = None,
                   job_id: Optional[str] = None) -> FineTuneJob:
        jid = job_id or str(uuid.uuid4())[:12]
        hp  = hyperparams or Hyperparams()
        job = FineTuneJob(
            job_id=jid, name=name, base_model=base_model,
            objective=objective, hyperparams=hp,
            dataset_id=dataset_id, tags=list(tags or []),
            priority=priority, notes=notes, metadata=metadata or {})
        self._jobs[jid] = job
        self._db.execute(
            "INSERT INTO ft_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (jid, name, base_model, objective.value, JobStatus.QUEUED.value,
             job.created_at, None, None, json.dumps(hp.to_dict()),
             dataset_id, json.dumps(tags or []), notes, None))
        self._db.commit()
        return job

    def start_job(self, job_id: str) -> FineTuneJob:
        job = self._get(job_id)
        job.status     = JobStatus.TRAINING
        job.started_at = time.time()
        self._update_status(job_id, JobStatus.TRAINING, started_at=job.started_at)
        self._fire("on_start", job)
        return job

    def complete_job(self, job_id: str,
                     output_model_id: str = "") -> FineTuneJob:
        job = self._get(job_id)
        job.status          = JobStatus.DONE
        job.finished_at     = time.time()
        job.output_model_id = output_model_id
        self._update_status(job_id, JobStatus.DONE, finished_at=job.finished_at)
        self._fire("on_complete", job)
        return job

    def fail_job(self, job_id: str, error: str) -> FineTuneJob:
        job = self._get(job_id)
        job.status      = JobStatus.FAILED
        job.finished_at = time.time()
        job.error       = error
        self._db.execute(
            "UPDATE ft_jobs SET status=?, finished_at=?, error=? WHERE job_id=?",
            (JobStatus.FAILED.value, job.finished_at, error, job_id))
        self._db.commit()
        self._fire("on_fail", job)
        return job

    def cancel_job(self, job_id: str) -> FineTuneJob:
        job = self._get(job_id)
        job.status = JobStatus.CANCELLED
        self._update_status(job_id, JobStatus.CANCELLED)
        return job

    def pause_job(self, job_id: str) -> FineTuneJob:
        job = self._get(job_id)
        job.status = JobStatus.PAUSED
        self._update_status(job_id, JobStatus.PAUSED)
        return job

    def resume_job(self, job_id: str) -> FineTuneJob:
        job = self._get(job_id)
        job.status = JobStatus.TRAINING
        self._update_status(job_id, JobStatus.TRAINING)
        return job

    def _update_status(self, job_id: str, status: JobStatus,
                       started_at: Optional[float] = None,
                       finished_at: Optional[float] = None):
        self._db.execute(
            "UPDATE ft_jobs SET status=?, started_at=COALESCE(?,started_at),"
            "finished_at=COALESCE(?,finished_at) WHERE job_id=?",
            (status.value, started_at, finished_at, job_id))
        self._db.commit()

    def _get(self, job_id: str) -> FineTuneJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"Job '{job_id}' not found")
        return job

    # ── METRICS ───────────────────────────────────────────────────────

    def log_metrics(self, job_id: str, step: int, epoch: float,
                    loss: float, eval_loss: Optional[float] = None,
                    accuracy: Optional[float] = None,
                    perplexity: Optional[float] = None,
                    extras: Optional[Dict[str, float]] = None) -> TrainingMetrics:
        job = self._get(job_id)
        m = TrainingMetrics(step=step, epoch=epoch, loss=loss,
                            eval_loss=eval_loss, accuracy=accuracy,
                            perplexity=perplexity, extras=extras or {})
        job.metrics.append(m)
        self._db.execute(
            "INSERT INTO ft_metrics (job_id,step,epoch,loss,eval_loss,accuracy,ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (job_id, step, epoch, loss, eval_loss, accuracy, m.ts))
        self._db.commit()
        self._fire("on_metrics", job, m)
        return m

    def get_metrics(self, job_id: str) -> List[Dict[str, Any]]:
        return [m.to_dict() for m in self._get(job_id).metrics]

    # ── CHECKPOINTS ───────────────────────────────────────────────────

    def save_checkpoint(self, job_id: str, step: int,
                        path: str, loss: float) -> Dict[str, Any]:
        cp = {"step": step, "path": path, "loss": loss, "ts": time.time()}
        self._checkpoints.setdefault(job_id, []).append(cp)
        self._db.execute(
            "INSERT INTO ft_checkpoints (job_id,step,path,loss,ts) VALUES (?,?,?,?,?)",
            (job_id, step, path, loss, cp["ts"]))
        self._db.commit()
        return cp

    def best_checkpoint(self, job_id: str) -> Optional[Dict[str, Any]]:
        cps = self._checkpoints.get(job_id, [])
        return min(cps, key=lambda c: c["loss"]) if cps else None

    def list_checkpoints(self, job_id: str) -> List[Dict[str, Any]]:
        return self._checkpoints.get(job_id, [])

    # ── HYPERPARAMETER SEARCH ─────────────────────────────────────────

    def grid_search(self, base_config: Dict[str, Any],
                    param_grid: Dict[str, List[Any]]) -> List[Dict[str, Any]]:
        """Generate all combinations of hyperparameter values."""
        import itertools
        keys   = list(param_grid.keys())
        values = list(param_grid.values())
        combos = []
        for combo in itertools.product(*values):
            cfg = dict(base_config)
            cfg.update(dict(zip(keys, combo)))
            combos.append(cfg)
        return combos

    def compare_jobs(self, job_ids: List[str]) -> List[Dict[str, Any]]:
        return [self._get(jid).to_dict() for jid in job_ids if jid in self._jobs]

    # ── HOOKS ─────────────────────────────────────────────────────────

    def on(self, event: str, fn: Callable):
        self._hooks.setdefault(event, []).append(fn)

    def _fire(self, event: str, *args):
        for fn in self._hooks.get(event, []):
            try: fn(*args)
            except Exception: pass

    # ── QUERY ─────────────────────────────────────────────────────────

    def get_job(self, job_id: str) -> FineTuneJob:
        return self._get(job_id)

    def list_jobs(self, status: Optional[JobStatus] = None,
                  tag: Optional[str] = None) -> List[Dict[str, Any]]:
        jobs = list(self._jobs.values())
        if status:
            jobs = [j for j in jobs if j.status == status]
        if tag:
            jobs = [j for j in jobs if tag in j.tags]
        return [j.to_dict() for j in sorted(jobs, key=lambda j: j.priority, reverse=True)]

    def stats(self) -> Dict[str, Any]:
        by_status: Dict[str, int] = {}
        for j in self._jobs.values():
            by_status[j.status.value] = by_status.get(j.status.value, 0) + 1
        done_jobs = [j for j in self._jobs.values() if j.best_loss is not None]
        avg_best  = (sum(j.best_loss for j in done_jobs) / len(done_jobs)
                     if done_jobs else None)
        return {
            "total_jobs": len(self._jobs),
            "by_status": by_status,
            "avg_best_loss": round(avg_best, 6) if avg_best else None,
        }
