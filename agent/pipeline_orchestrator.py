"""OMNI Agent — Pipeline Orchestrator: DAG-based data pipeline with stages, retries, metrics."""
from __future__ import annotations
import asyncio, threading, time, uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


class StageStatus(str, Enum):
    PENDING  = "pending"
    RUNNING  = "running"
    DONE     = "done"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    RETRYING = "retrying"


@dataclass
class StageSpec:
    stage_id: str
    name: str
    fn: Callable
    dependencies: List[str] = field(default_factory=list)
    max_retries: int = 0
    retry_delay_s: float = 0.5
    timeout_s: Optional[float] = None
    skip_on_fail: bool = False         # mark SKIPPED instead of propagating failure
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "name": self.name,
            "dependencies": self.dependencies,
            "max_retries": self.max_retries,
            "tags": self.tags,
        }


@dataclass
class StageRun:
    stage_id: str
    attempt: int = 1
    status: StageStatus = StageStatus.PENDING
    input_data: Any = None
    output: Any = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    @property
    def duration_ms(self) -> float:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at) * 1000
        return 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "attempt": self.attempt,
            "status": self.status.value,
            "duration_ms": round(self.duration_ms, 2),
            "error": self.error,
        }


@dataclass
class PipelineRun:
    run_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    pipeline_name: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    stage_runs: Dict[str, StageRun] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)
    status: StageStatus = StageStatus.PENDING

    @property
    def duration_ms(self) -> float:
        if self.finished_at:
            return (self.finished_at - self.started_at) * 1000
        return 0.0

    @property
    def failed_stages(self) -> List[str]:
        return [sid for sid, sr in self.stage_runs.items()
                if sr.status == StageStatus.FAILED]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "pipeline": self.pipeline_name,
            "status": self.status.value,
            "duration_ms": round(self.duration_ms, 2),
            "stages": {sid: sr.to_dict() for sid, sr in self.stage_runs.items()},
            "failed": self.failed_stages,
        }


class CyclicDependencyError(Exception):
    pass


class PipelineOrchestrator:
    """
    DAG-based pipeline orchestrator:
    - Topological stage scheduling
    - Parallel execution of independent stages
    - Retry with delay
    - Context passing between stages
    - Hooks: on_stage_start/end, on_run_complete
    - Metrics collection
    """

    def __init__(self):
        self._stages: Dict[str, StageSpec] = {}
        self._runs: List[PipelineRun] = []
        self._hooks_stage_start: List[Callable] = []
        self._hooks_stage_end:   List[Callable] = []
        self._hooks_run_done:    List[Callable] = []
        self._total_runs = 0

    # ── REGISTRATION ──────────────────────────────────────────────────

    def add_stage(self, name: str, fn: Callable,
                  dependencies: Optional[List[str]] = None,
                  max_retries: int = 0,
                  retry_delay_s: float = 0.5,
                  timeout_s: Optional[float] = None,
                  skip_on_fail: bool = False,
                  tags: Optional[List[str]] = None,
                  stage_id: Optional[str] = None,
                  metadata: Optional[Dict] = None) -> StageSpec:
        sid = stage_id or name.lower().replace(" ", "_")
        spec = StageSpec(
            stage_id=sid, name=name, fn=fn,
            dependencies=list(dependencies or []),
            max_retries=max_retries, retry_delay_s=retry_delay_s,
            timeout_s=timeout_s, skip_on_fail=skip_on_fail,
            tags=list(tags or []), metadata=metadata or {})
        self._stages[sid] = spec
        return spec

    def remove_stage(self, stage_id: str):
        self._stages.pop(stage_id, None)

    # ── TOPOLOGICAL SORT ──────────────────────────────────────────────

    def _topo_sort(self) -> List[List[str]]:
        """Return stages as waves (parallel batches). Raises on cycles."""
        in_degree: Dict[str, int] = {sid: 0 for sid in self._stages}
        for spec in self._stages.values():
            for dep in spec.dependencies:
                if dep in in_degree:
                    in_degree[spec.stage_id] += 1

        waves: List[List[str]] = []
        completed: Set[str] = set()
        while len(completed) < len(self._stages):
            wave = [sid for sid, deg in in_degree.items()
                    if deg == 0 and sid not in completed]
            if not wave:
                raise CyclicDependencyError("Cycle detected in pipeline DAG")
            waves.append(wave)
            for sid in wave:
                completed.add(sid)
                for spec in self._stages.values():
                    if sid in spec.dependencies:
                        in_degree[spec.stage_id] -= 1
        return waves

    # ── EXECUTION ─────────────────────────────────────────────────────

    def run(self, initial_context: Optional[Dict[str, Any]] = None,
            pipeline_name: str = "pipeline") -> PipelineRun:
        pr = PipelineRun(pipeline_name=pipeline_name,
                         context=dict(initial_context or {}))
        self._total_runs += 1
        pr.status = StageStatus.RUNNING

        try:
            waves = self._topo_sort()
        except CyclicDependencyError as e:
            pr.status = StageStatus.FAILED
            pr.finished_at = time.time()
            self._runs.append(pr)
            return pr

        overall_ok = True
        for wave in waves:
            threads: List[threading.Thread] = []
            results: Dict[str, StageRun] = {}
            lock = threading.Lock()

            def run_stage(sid: str, ctx: Dict[str, Any]):
                spec = self._stages[sid]
                sr   = StageRun(stage_id=sid)
                attempt = 0
                while attempt <= spec.max_retries:
                    attempt += 1
                    sr.attempt = attempt
                    sr.status  = StageStatus.RUNNING
                    sr.started_at = time.time()
                    for hook in self._hooks_stage_start:
                        try: hook(spec, sr)
                        except Exception: pass
                    try:
                        # Gather dependency outputs into input
                        dep_outputs = {d: ctx.get(f"__out_{d}") for d in spec.dependencies}
                        sr.input_data = dep_outputs
                        out = spec.fn(ctx, dep_outputs)
                        sr.output = out
                        sr.status = StageStatus.DONE
                        sr.finished_at = time.time()
                        with lock:
                            ctx[f"__out_{sid}"] = out
                        break
                    except Exception as exc:
                        sr.error = str(exc)
                        sr.finished_at = time.time()
                        if attempt <= spec.max_retries:
                            sr.status = StageStatus.RETRYING
                            time.sleep(spec.retry_delay_s)
                        else:
                            sr.status = (StageStatus.SKIPPED if spec.skip_on_fail
                                         else StageStatus.FAILED)
                for hook in self._hooks_stage_end:
                    try: hook(spec, sr)
                    except Exception: pass
                with lock:
                    results[sid] = sr

            for sid in wave:
                t = threading.Thread(target=run_stage,
                                     args=(sid, pr.context), daemon=True)
                threads.append(t)
                t.start()
            for t in threads:
                t.join()

            pr.stage_runs.update(results)
            if any(sr.status == StageStatus.FAILED for sr in results.values()):
                overall_ok = False
                break

        pr.status = StageStatus.DONE if overall_ok else StageStatus.FAILED
        pr.finished_at = time.time()
        self._runs.append(pr)
        for fn in self._hooks_run_done:
            try: fn(pr)
            except Exception: pass
        return pr

    async def run_async(self, **kwargs) -> PipelineRun:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self.run(**kwargs))

    # ── HOOKS ─────────────────────────────────────────────────────────

    def on_stage_start(self, fn: Callable): self._hooks_stage_start.append(fn)
    def on_stage_end(self, fn: Callable):   self._hooks_stage_end.append(fn)
    def on_run_done(self, fn: Callable):    self._hooks_run_done.append(fn)

    # ── INTROSPECTION ─────────────────────────────────────────────────

    def get_run(self, run_id: str) -> Optional[PipelineRun]:
        for r in self._runs:
            if r.run_id == run_id:
                return r
        return None

    def recent_runs(self, n: int = 10) -> List[Dict[str, Any]]:
        return [r.to_dict() for r in self._runs[-n:]]

    def list_stages(self) -> List[Dict[str, Any]]:
        return [s.to_dict() for s in self._stages.values()]

    def validate(self) -> List[str]:
        """Check for broken dependencies and cycles."""
        errors = []
        for spec in self._stages.values():
            for dep in spec.dependencies:
                if dep not in self._stages:
                    errors.append(f"Stage '{spec.stage_id}' depends on unknown '{dep}'")
        try:
            self._topo_sort()
        except CyclicDependencyError as e:
            errors.append(str(e))
        return errors

    def stats(self) -> Dict[str, Any]:
        done   = sum(1 for r in self._runs if r.status == StageStatus.DONE)
        failed = sum(1 for r in self._runs if r.status == StageStatus.FAILED)
        return {
            "stages": len(self._stages),
            "total_runs": self._total_runs,
            "done": done,
            "failed": failed,
        }
