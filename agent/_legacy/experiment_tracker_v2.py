"""OMNI Agent — Experiment Tracker V2: ML runs, metrics, artifacts, comparison."""
from __future__ import annotations
import json, math, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class RunStatus(str, Enum):
    CREATED  = "created"
    RUNNING  = "running"
    FINISHED = "finished"
    FAILED   = "failed"
    KILLED   = "killed"


class MetricDirection(str, Enum):
    MAXIMIZE = "maximize"
    MINIMIZE = "minimize"


@dataclass
class Metric:
    key: str
    value: float
    step: int = 0
    ts: float = field(default_factory=time.time)


@dataclass
class Artifact:
    artifact_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    run_id: str = ""
    name: str = ""
    artifact_type: str = "file"   # file | model | dataset | plot
    path: str = ""
    size_bytes: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {"artifact_id": self.artifact_id, "name": self.name,
                "type": self.artifact_type, "path": self.path,
                "size_bytes": self.size_bytes}


@dataclass
class ExperimentRun:
    run_id: str = field(default_factory=lambda: str(uuid.uuid4())[:10])
    experiment_id: str = ""
    run_name: str = ""
    status: RunStatus = RunStatus.CREATED
    params: Dict[str, Any] = field(default_factory=dict)
    tags: Dict[str, str] = field(default_factory=dict)
    metrics: Dict[str, List[Metric]] = field(default_factory=dict)   # key → history
    artifacts: List[Artifact] = field(default_factory=list)
    notes: str = ""
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    parent_run_id: Optional[str] = None   # nested runs
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration_s(self) -> Optional[float]:
        if self.started_at and self.finished_at:
            return self.finished_at - self.started_at
        return None

    def latest_metric(self, key: str) -> Optional[float]:
        history = self.metrics.get(key, [])
        return history[-1].value if history else None

    def best_metric(self, key: str,
                    direction: MetricDirection = MetricDirection.MAXIMIZE) -> Optional[float]:
        vals = [m.value for m in self.metrics.get(key, [])]
        if not vals: return None
        return max(vals) if direction == MetricDirection.MAXIMIZE else min(vals)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_name": self.run_name,
            "status": self.status.value,
            "params": self.params,
            "tags": self.tags,
            "metrics": {k: round(v[-1].value, 6) for k, v in self.metrics.items() if v},
            "duration_s": round(self.duration_s, 3) if self.duration_s else None,
        }


@dataclass
class Experiment:
    experiment_id: str
    name: str
    description: str = ""
    tags: List[str] = field(default_factory=list)
    metric_keys: List[str] = field(default_factory=list)  # declared metrics
    created_at: float = field(default_factory=time.time)
    artifact_location: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "name": self.name,
            "description": self.description,
            "tags": self.tags,
        }


class ExperimentTrackerV2:
    """
    ML Experiment tracking:
    - Named experiments with runs
    - Params, tags, metrics per run
    - Metric step history (for learning curves)
    - Artifact logging per run
    - Run status lifecycle (created→running→finished/failed)
    - Nested runs (parent_run_id)
    - Run comparison: side-by-side metrics
    - Best-run selection per metric + direction
    - Parameter sweep analysis (group by param)
    - Run search by tag/status/metric range
    - SQLite persistence
    """

    def __init__(self, db_path: str = ":memory:"):
        self._experiments: Dict[str, Experiment] = {}
        self._runs:        Dict[str, ExperimentRun] = {}
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS et_experiments (
                experiment_id TEXT PRIMARY KEY, name TEXT,
                description TEXT, tags TEXT, created_at REAL
            );
            CREATE TABLE IF NOT EXISTS et_runs (
                run_id TEXT PRIMARY KEY, experiment_id TEXT,
                run_name TEXT, status TEXT, params TEXT, tags TEXT,
                notes TEXT, started_at REAL, finished_at REAL,
                parent_run_id TEXT
            );
            CREATE TABLE IF NOT EXISTS et_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT, key TEXT, value REAL, step INTEGER, ts REAL
            );
            CREATE TABLE IF NOT EXISTS et_artifacts (
                artifact_id TEXT PRIMARY KEY, run_id TEXT, name TEXT,
                artifact_type TEXT, path TEXT, size_bytes INTEGER, created_at REAL
            );
        """)
        self._db.commit()

    # ── EXPERIMENTS ──────────────────────────────────────────────────

    def create_experiment(self, name: str,
                           description: str = "",
                           tags: Optional[List[str]] = None,
                           experiment_id: Optional[str] = None) -> Experiment:
        eid = experiment_id or str(uuid.uuid4())[:8]
        exp = Experiment(experiment_id=eid, name=name,
                          description=description, tags=list(tags or []))
        self._experiments[eid] = exp
        self._db.execute(
            "INSERT OR REPLACE INTO et_experiments VALUES (?,?,?,?,?)",
            (eid, name, description, json.dumps(tags or []), exp.created_at))
        self._db.commit()
        return exp

    def get_experiment(self, experiment_id: str) -> Optional[Experiment]:
        return self._experiments.get(experiment_id)

    def find_experiment(self, name: str) -> Optional[Experiment]:
        return next((e for e in self._experiments.values()
                     if e.name == name), None)

    def list_experiments(self) -> List[Dict]:
        return [e.to_dict() for e in self._experiments.values()]

    # ── RUNS ─────────────────────────────────────────────────────────

    def start_run(self, experiment_id: str,
                   run_name: str = "",
                   params: Optional[Dict] = None,
                   tags: Optional[Dict[str, str]] = None,
                   parent_run_id: Optional[str] = None,
                   run_id: Optional[str] = None,
                   notes: str = "") -> ExperimentRun:
        rid  = run_id or str(uuid.uuid4())[:10]
        run  = ExperimentRun(
            run_id=rid, experiment_id=experiment_id,
            run_name=run_name or rid,
            status=RunStatus.RUNNING,
            params=dict(params or {}),
            tags=dict(tags or {}),
            parent_run_id=parent_run_id,
            notes=notes,
            started_at=time.time())
        self._runs[rid] = run
        self._persist_run(run)
        return run

    def end_run(self, run_id: str,
                status: RunStatus = RunStatus.FINISHED) -> bool:
        run = self._runs.get(run_id)
        if not run: return False
        run.status      = status
        run.finished_at = time.time()
        self._persist_run(run)
        return True

    def fail_run(self, run_id: str) -> bool:
        return self.end_run(run_id, RunStatus.FAILED)

    def get_run(self, run_id: str) -> Optional[ExperimentRun]:
        return self._runs.get(run_id)

    # ── LOGGING ──────────────────────────────────────────────────────

    def log_param(self, run_id: str, key: str, value: Any):
        run = self._runs.get(run_id)
        if run: run.params[key] = value

    def log_params(self, run_id: str, params: Dict[str, Any]):
        run = self._runs.get(run_id)
        if run: run.params.update(params)

    def log_metric(self, run_id: str, key: str,
                    value: float, step: int = 0):
        run = self._runs.get(run_id)
        if not run: return
        m = Metric(key=key, value=value, step=step)
        run.metrics.setdefault(key, []).append(m)
        self._db.execute(
            "INSERT INTO et_metrics (run_id,key,value,step,ts) VALUES (?,?,?,?,?)",
            (run_id, key, value, step, m.ts))
        self._db.commit()

    def log_metrics(self, run_id: str,
                    metrics: Dict[str, float], step: int = 0):
        for k, v in metrics.items():
            self.log_metric(run_id, k, v, step)

    def log_tag(self, run_id: str, key: str, value: str):
        run = self._runs.get(run_id)
        if run: run.tags[key] = value

    def log_artifact(self, run_id: str,
                      name: str, path: str,
                      artifact_type: str = "file",
                      size_bytes: int = 0,
                      metadata: Optional[Dict] = None) -> Artifact:
        run = self._runs.get(run_id)
        if not run: raise KeyError(f"Run {run_id} not found")
        a = Artifact(run_id=run_id, name=name, path=path,
                     artifact_type=artifact_type,
                     size_bytes=size_bytes,
                     metadata=metadata or {})
        run.artifacts.append(a)
        self._db.execute(
            "INSERT INTO et_artifacts VALUES (?,?,?,?,?,?,?)",
            (a.artifact_id, run_id, name, artifact_type,
             path, size_bytes, a.created_at))
        self._db.commit()
        return a

    def set_notes(self, run_id: str, notes: str):
        run = self._runs.get(run_id)
        if run: run.notes = notes

    # ── QUERY ────────────────────────────────────────────────────────

    def list_runs(self, experiment_id: Optional[str] = None,
                  status: Optional[RunStatus] = None,
                  tag_filter: Optional[Dict[str, str]] = None,
                  limit: int = 50) -> List[Dict]:
        runs = list(self._runs.values())
        if experiment_id:
            runs = [r for r in runs if r.experiment_id == experiment_id]
        if status:
            runs = [r for r in runs if r.status == status]
        if tag_filter:
            runs = [r for r in runs
                    if all(r.tags.get(k) == v
                           for k, v in tag_filter.items())]
        return [r.to_dict() for r in runs[-limit:]]

    def get_metric_history(self, run_id: str,
                            key: str) -> List[Tuple[int, float]]:
        rows = self._db.execute(
            "SELECT step, value FROM et_metrics "
            "WHERE run_id=? AND key=? ORDER BY step",
            (run_id, key)).fetchall()
        return [(r[0], r[1]) for r in rows]

    # ── COMPARISON ───────────────────────────────────────────────────

    def compare_runs(self, run_ids: List[str],
                     metrics: Optional[List[str]] = None) -> List[Dict]:
        result = []
        for rid in run_ids:
            run = self._runs.get(rid)
            if not run: continue
            row: Dict[str, Any] = {
                "run_id": rid, "run_name": run.run_name,
                "params": run.params}
            for key, hist in run.metrics.items():
                if metrics and key not in metrics:
                    continue
                if hist:
                    row[key] = round(hist[-1].value, 6)
            result.append(row)
        return result

    def best_run(self, experiment_id: str,
                  metric: str,
                  direction: MetricDirection = MetricDirection.MAXIMIZE
                  ) -> Optional[ExperimentRun]:
        runs = [r for r in self._runs.values()
                if r.experiment_id == experiment_id
                and metric in r.metrics
                and r.metrics[metric]]
        if not runs: return None
        if direction == MetricDirection.MAXIMIZE:
            key_fn = lambda r: r.latest_metric(metric) or float("-inf")
        else:
            key_fn = lambda r: -(r.latest_metric(metric) or float("inf"))
        return max(runs, key=key_fn)

    def group_by_param(self, experiment_id: str,
                        param: str,
                        metric: str) -> Dict[Any, List[float]]:
        """Group latest metric values by a parameter value."""
        groups: Dict[Any, List[float]] = {}
        for run in self._runs.values():
            if run.experiment_id != experiment_id: continue
            pval = run.params.get(param)
            mval = run.latest_metric(metric)
            if pval is not None and mval is not None:
                groups.setdefault(pval, []).append(mval)
        return groups

    def search_runs(self, experiment_id: Optional[str] = None,
                    metric_filters: Optional[Dict[str, Tuple[float, float]]] = None,
                    limit: int = 50) -> List[Dict]:
        """Search with metric range filters: {metric: (min, max)}."""
        runs = list(self._runs.values())
        if experiment_id:
            runs = [r for r in runs if r.experiment_id == experiment_id]
        if metric_filters:
            filtered = []
            for run in runs:
                ok = True
                for key, (lo, hi) in metric_filters.items():
                    val = run.latest_metric(key)
                    if val is None or not (lo <= val <= hi):
                        ok = False; break
                if ok: filtered.append(run)
            runs = filtered
        return [r.to_dict() for r in runs[-limit:]]

    def _persist_run(self, run: ExperimentRun):
        self._db.execute(
            "INSERT OR REPLACE INTO et_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run.run_id, run.experiment_id, run.run_name,
             run.status.value, json.dumps(run.params),
             json.dumps(run.tags), run.notes,
             run.started_at, run.finished_at, run.parent_run_id))
        self._db.commit()

    def stats(self) -> Dict[str, Any]:
        return {
            "experiments": len(self._experiments),
            "runs": len(self._runs),
            "running": sum(1 for r in self._runs.values()
                           if r.status == RunStatus.RUNNING),
            "finished": sum(1 for r in self._runs.values()
                            if r.status == RunStatus.FINISHED),
        }
