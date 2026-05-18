"""OMNI Agent — Pipeline Registry V2: named pipelines with versioning and execution."""
from __future__ import annotations
import json, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class PipelineStatus(str, Enum):
    ACTIVE     = "active"
    DISABLED   = "disabled"
    DEPRECATED = "deprecated"
    DRAFT      = "draft"


class StepType(str, Enum):
    TRANSFORM  = "transform"
    FILTER     = "filter"
    ENRICH     = "enrich"
    VALIDATE   = "validate"
    AGGREGATE  = "aggregate"
    BRANCH     = "branch"
    SINK       = "sink"


@dataclass
class PipelineStep:
    step_id: str
    name: str
    fn: Callable
    step_type: StepType = StepType.TRANSFORM
    enabled: bool = True
    on_error: str = "raise"     # raise | skip | default
    default_value: Any = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "name": self.name,
            "type": self.step_type.value,
            "enabled": self.enabled,
            "on_error": self.on_error,
        }


@dataclass
class PipelineDefinition:
    pipeline_id: str
    name: str
    version: str = "1.0.0"
    status: PipelineStatus = PipelineStatus.DRAFT
    steps: List[PipelineStep] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    description: str = ""
    owner: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    run_count: int = 0
    last_run_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pipeline_id": self.pipeline_id,
            "name": self.name,
            "version": self.version,
            "status": self.status.value,
            "steps": len(self.steps),
            "tags": self.tags,
            "run_count": self.run_count,
        }


@dataclass
class PipelineRun:
    run_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    pipeline_id: str = ""
    pipeline_version: str = ""
    status: str = "pending"
    input_data: Any = None
    output_data: Any = None
    step_results: List[Dict] = field(default_factory=list)
    errors: List[Dict] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    @property
    def duration_ms(self) -> float:
        end = self.finished_at or time.time()
        return (end - self.started_at) * 1000

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "pipeline_id": self.pipeline_id,
            "status": self.status,
            "steps_run": len(self.step_results),
            "errors": len(self.errors),
            "duration_ms": round(self.duration_ms, 2),
        }


class PipelineRegistryV2:
    """
    Named pipeline registry:
    - Register pipelines with typed, ordered steps
    - Version management (semver strings)
    - Status: DRAFT → ACTIVE → DEPRECATED
    - Tag-based search and filtering
    - Add/remove/reorder steps at runtime
    - Enable/disable individual steps
    - Execute pipelines with full run tracking
    - Step-level error handling (raise/skip/default)
    - Branching (conditional step routing)
    - Clone pipelines with new version
    - Run history with per-step results
    - Pre/post execution hooks
    - SQLite persistence
    """

    def __init__(self, db_path: str = ":memory:"):
        self._pipelines: Dict[str, PipelineDefinition] = {}
        self._runs:      Dict[str, PipelineRun] = {}
        self._pre_hooks:  List[Callable] = []
        self._post_hooks: List[Callable] = []
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS pr_pipelines (
                pipeline_id TEXT PRIMARY KEY, name TEXT, version TEXT,
                status TEXT, tags TEXT, description TEXT,
                owner TEXT, created_at REAL, run_count INTEGER
            );
            CREATE TABLE IF NOT EXISTS pr_runs (
                run_id TEXT PRIMARY KEY, pipeline_id TEXT,
                pipeline_version TEXT, status TEXT,
                steps_run INTEGER, errors INTEGER,
                started_at REAL, finished_at REAL
            );
        """)
        self._db.commit()

    # ── PIPELINE MANAGEMENT ───────────────────────────────────────────

    def register(self, name: str,
                  version: str = "1.0.0",
                  status: PipelineStatus = PipelineStatus.DRAFT,
                  tags: Optional[List[str]] = None,
                  description: str = "",
                  owner: str = "",
                  pipeline_id: Optional[str] = None,
                  metadata: Optional[Dict] = None) -> PipelineDefinition:
        pid = pipeline_id or str(uuid.uuid4())[:8]
        p   = PipelineDefinition(
            pipeline_id=pid, name=name, version=version,
            status=status, tags=list(tags or []),
            description=description, owner=owner,
            metadata=metadata or {})
        self._pipelines[pid] = p
        self._persist(p)
        return p

    def get(self, pipeline_id: str) -> Optional[PipelineDefinition]:
        return self._pipelines.get(pipeline_id)

    def find(self, name: str,
             version: Optional[str] = None) -> Optional[PipelineDefinition]:
        for p in self._pipelines.values():
            if p.name == name:
                if version is None or p.version == version:
                    return p
        return None

    def list(self, tag: Optional[str] = None,
              status: Optional[PipelineStatus] = None) -> List[Dict]:
        pipelines = list(self._pipelines.values())
        if tag:    pipelines = [p for p in pipelines if tag in p.tags]
        if status: pipelines = [p for p in pipelines if p.status == status]
        return [p.to_dict() for p in pipelines]

    def activate(self, pipeline_id: str):
        p = self._pipelines.get(pipeline_id)
        if p:
            p.status = PipelineStatus.ACTIVE
            self._persist(p)

    def disable(self, pipeline_id: str):
        p = self._pipelines.get(pipeline_id)
        if p:
            p.status = PipelineStatus.DISABLED
            self._persist(p)

    def deprecate(self, pipeline_id: str):
        p = self._pipelines.get(pipeline_id)
        if p:
            p.status = PipelineStatus.DEPRECATED
            self._persist(p)

    def delete(self, pipeline_id: str) -> bool:
        p = self._pipelines.pop(pipeline_id, None)
        if p:
            self._db.execute(
                "DELETE FROM pr_pipelines WHERE pipeline_id=?",
                (pipeline_id,))
            self._db.commit()
        return p is not None

    def clone(self, pipeline_id: str,
               new_version: str,
               new_name: Optional[str] = None) -> PipelineDefinition:
        src = self._pipelines.get(pipeline_id)
        if not src: raise KeyError(f"Pipeline {pipeline_id} not found")
        new_p = self.register(
            name=new_name or src.name,
            version=new_version,
            tags=list(src.tags),
            description=src.description,
            owner=src.owner)
        # Clone steps
        for step in src.steps:
            new_p.steps.append(PipelineStep(
                step_id=str(uuid.uuid4())[:8],
                name=step.name, fn=step.fn,
                step_type=step.step_type,
                enabled=step.enabled,
                on_error=step.on_error,
                default_value=step.default_value,
                metadata=dict(step.metadata)))
        return new_p

    # ── STEP MANAGEMENT ──────────────────────────────────────────────

    def add_step(self, pipeline_id: str,
                  name: str,
                  fn: Callable,
                  step_type: StepType = StepType.TRANSFORM,
                  on_error: str = "raise",
                  default_value: Any = None,
                  step_id: Optional[str] = None,
                  position: Optional[int] = None,
                  metadata: Optional[Dict] = None) -> PipelineStep:
        p = self._pipelines.get(pipeline_id)
        if not p: raise KeyError(f"Pipeline {pipeline_id} not found")
        sid  = step_id or str(uuid.uuid4())[:8]
        step = PipelineStep(
            step_id=sid, name=name, fn=fn,
            step_type=step_type, on_error=on_error,
            default_value=default_value,
            metadata=metadata or {})
        if position is not None:
            p.steps.insert(position, step)
        else:
            p.steps.append(step)
        p.updated_at = time.time()
        return step

    def remove_step(self, pipeline_id: str, step_id: str) -> bool:
        p = self._pipelines.get(pipeline_id)
        if not p: return False
        before = len(p.steps)
        p.steps = [s for s in p.steps if s.step_id != step_id]
        return len(p.steps) < before

    def enable_step(self, pipeline_id: str, step_id: str):
        p = self._pipelines.get(pipeline_id)
        if p:
            for s in p.steps:
                if s.step_id == step_id: s.enabled = True

    def disable_step(self, pipeline_id: str, step_id: str):
        p = self._pipelines.get(pipeline_id)
        if p:
            for s in p.steps:
                if s.step_id == step_id: s.enabled = False

    def reorder_steps(self, pipeline_id: str, order: List[str]):
        """Reorder steps by list of step_ids."""
        p = self._pipelines.get(pipeline_id)
        if not p: return
        step_map = {s.step_id: s for s in p.steps}
        p.steps  = [step_map[sid] for sid in order if sid in step_map]

    # ── EXECUTION ────────────────────────────────────────────────────

    def execute(self, pipeline_id: str,
                 data: Any,
                 context: Optional[Dict] = None,
                 run_id: Optional[str] = None) -> PipelineRun:
        p = self._pipelines.get(pipeline_id)
        if not p: raise KeyError(f"Pipeline {pipeline_id} not found")
        if p.status == PipelineStatus.DISABLED:
            raise RuntimeError(f"Pipeline {p.name} is disabled")

        run = PipelineRun(
            run_id=run_id or str(uuid.uuid4())[:8],
            pipeline_id=pipeline_id,
            pipeline_version=p.version,
            input_data=data)
        run.status = "running"
        ctx  = dict(context or {})
        curr = data

        for fn in self._pre_hooks:
            try: fn(p, run)
            except Exception: pass

        for step in p.steps:
            if not step.enabled:
                continue
            t0 = time.time()
            try:
                curr = step.fn(curr, ctx)
                run.step_results.append({
                    "step_id": step.step_id,
                    "name": step.name,
                    "status": "ok",
                    "duration_ms": round((time.time() - t0) * 1000, 2),
                })
            except Exception as exc:
                err = {"step_id": step.step_id, "name": step.name,
                       "error": str(exc)}
                run.errors.append(err)
                run.step_results.append({**err, "status": "error",
                    "duration_ms": round((time.time() - t0) * 1000, 2)})
                if step.on_error == "raise":
                    run.status = "failed"
                    run.output_data  = curr
                    run.finished_at  = time.time()
                    p.run_count     += 1
                    p.last_run_at    = time.time()
                    self._persist_run(run)
                    for fn in self._post_hooks:
                        try: fn(p, run)
                        except Exception: pass
                    return run
                elif step.on_error == "default":
                    curr = step.default_value
                # "skip" → continue with unchanged curr

        run.output_data  = curr
        run.status       = "done"
        run.finished_at  = time.time()
        p.run_count     += 1
        p.last_run_at    = time.time()
        self._runs[run.run_id] = run
        self._persist_run(run)
        self._persist(p)

        for fn in self._post_hooks:
            try: fn(p, run)
            except Exception: pass

        return run

    # ── HOOKS ────────────────────────────────────────────────────────

    def on_before_run(self, fn: Callable): self._pre_hooks.append(fn)
    def on_after_run(self, fn: Callable):  self._post_hooks.append(fn)

    # ── QUERY ────────────────────────────────────────────────────────

    def run_history(self, pipeline_id: Optional[str] = None,
                    limit: int = 50) -> List[Dict]:
        q = ("SELECT run_id,pipeline_id,pipeline_version,status,"
             "steps_run,errors,started_at FROM pr_runs "
             "ORDER BY started_at DESC LIMIT ?")
        rows = self._db.execute(q, (limit,)).fetchall()
        result = [{"id": r[0], "pipeline": r[1], "version": r[2],
                   "status": r[3]} for r in rows]
        if pipeline_id:
            result = [r for r in result if r["pipeline"] == pipeline_id]
        return result

    def get_run(self, run_id: str) -> Optional[PipelineRun]:
        return self._runs.get(run_id)

    def _persist(self, p: PipelineDefinition):
        self._db.execute(
            "INSERT OR REPLACE INTO pr_pipelines VALUES (?,?,?,?,?,?,?,?,?)",
            (p.pipeline_id, p.name, p.version, p.status.value,
             json.dumps(p.tags), p.description, p.owner,
             p.created_at, p.run_count))
        self._db.commit()

    def _persist_run(self, run: PipelineRun):
        self._db.execute(
            "INSERT OR REPLACE INTO pr_runs VALUES (?,?,?,?,?,?,?,?)",
            (run.run_id, run.pipeline_id, run.pipeline_version,
             run.status, len(run.step_results),
             len(run.errors), run.started_at, run.finished_at))
        self._db.commit()

    def stats(self) -> Dict[str, Any]:
        return {
            "pipelines": len(self._pipelines),
            "runs": len(self._runs),
            "active": sum(1 for p in self._pipelines.values()
                          if p.status == PipelineStatus.ACTIVE),
        }
