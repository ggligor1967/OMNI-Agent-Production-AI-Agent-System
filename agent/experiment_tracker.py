"""OMNI AGENT - Experiment Tracker
Log, compare, and analyse ML/LLM experiments: hyperparameters, metrics,
artefacts, and run comparisons with statistical significance testing.

Features:
- Experiment runs: log params + metrics with arbitrary nesting
- Step-level logging: metric curves (loss, accuracy) over training steps
- Artefact references: attach filenames/URLs to any run
- Run comparison: diff params and metrics across N runs
- Best-run selection: find run with best value for a target metric
- Statistical tests: paired t-test for significance (when >1 sample)
- Tags and notes: annotate runs for easy filtering
- Run status: pending → running → completed | failed
- SQLite persistence: all runs and metrics stored
- REST API: create-run, log-metrics, compare, best, list
"""
import json, time, uuid, sqlite3, math, logging
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class RunStatus(str, Enum):
    PENDING="pending"; RUNNING="running"; COMPLETED="completed"; FAILED="failed"

@dataclass
class MetricPoint:
    run_id: str; name: str; value: float
    step: int = 0; timestamp: float = field(default_factory=time.time)
    def to_dict(self):
        return {"name":self.name,"value":self.value,"step":self.step}

@dataclass
class Run:
    id: str; name: str; experiment: str = "default"
    params: Dict = field(default_factory=dict)
    metrics: Dict[str, List[float]] = field(default_factory=dict)  # name → [values]
    summary: Dict[str, float] = field(default_factory=dict)   # name → last value
    artefacts: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    notes: str = ""
    status: RunStatus = RunStatus.PENDING
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    duration_s: float = 0.0

    def log_metric(self, name: str, value: float, step: int = 0):
        self.metrics.setdefault(name, []).append(value)
        self.summary[name] = value
        self.updated_at = time.time()

    def best_metric(self, name: str, mode: str = "max") -> Optional[float]:
        vals = self.metrics.get(name, [])
        if not vals: return None
        return max(vals) if mode == "max" else min(vals)

    def to_dict(self, include_curves: bool = False):
        d = {"id":self.id,"name":self.name,"experiment":self.experiment,
             "params":self.params,"summary":self.summary,
             "artefacts":self.artefacts,"tags":self.tags,"notes":self.notes,
             "status":self.status,"created_at":self.created_at,
             "updated_at":self.updated_at,"duration_s":round(self.duration_s,3)}
        if include_curves:
            d["metrics"] = self.metrics
        return d

class ExpStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path=db_path; self._init()
    def _conn(self):
        c=sqlite3.connect(self.db_path); c.row_factory=sqlite3.Row; return c
    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS runs(
                    id TEXT PRIMARY KEY,name TEXT,experiment TEXT DEFAULT 'default',
                    params TEXT DEFAULT '{}',summary TEXT DEFAULT '{}',
                    artefacts TEXT DEFAULT '[]',tags TEXT DEFAULT '[]',
                    notes TEXT DEFAULT '',status TEXT DEFAULT 'pending',
                    created_at REAL,updated_at REAL,duration_s REAL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS metrics(
                    id TEXT PRIMARY KEY,run_id TEXT,name TEXT,
                    value REAL,step INTEGER DEFAULT 0,timestamp REAL);
                CREATE INDEX IF NOT EXISTS idx_met_run ON metrics(run_id,name,step);
                CREATE INDEX IF NOT EXISTS idx_run_exp ON runs(experiment,created_at DESC);
            """)
    def save_run(self, run):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (run.id,run.name,run.experiment,json.dumps(run.params),
                 json.dumps(run.summary),json.dumps(run.artefacts),
                 json.dumps(run.tags),run.notes,run.status,
                 run.created_at,run.updated_at,run.duration_s))
    def save_metric(self, mp: MetricPoint):
        with self._conn() as c:
            c.execute("INSERT INTO metrics VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4())[:10],mp.run_id,mp.name,mp.value,mp.step,mp.timestamp))
    def load_run(self, run_id):
        with self._conn() as c:
            row=c.execute("SELECT * FROM runs WHERE id=?",(run_id,)).fetchone()
            if not row: return None
            mrows=c.execute("SELECT * FROM metrics WHERE run_id=? ORDER BY step ASC",(run_id,)).fetchall()
        run=Run(id=row["id"],name=row["name"],experiment=row["experiment"],
                params=json.loads(row["params"] or "{}"),
                summary=json.loads(row["summary"] or "{}"),
                artefacts=json.loads(row["artefacts"] or "[]"),
                tags=json.loads(row["tags"] or "[]"),
                notes=row["notes"] or "",status=RunStatus(row["status"]),
                created_at=row["created_at"],updated_at=row["updated_at"],
                duration_s=row["duration_s"])
        for mr in mrows:
            run.metrics.setdefault(mr["name"],[]).append(mr["value"])
        return run
    def list_runs(self, experiment=None, status=None, tags=None, limit=50):
        conds,args=["1=1"],[]
        if experiment: conds.append("experiment=?"); args.append(experiment)
        if status: conds.append("status=?"); args.append(status)
        if tags:
            for tag in tags: conds.append("tags LIKE ?"); args.append(f'%{tag}%')
        args.append(limit)
        with self._conn() as c:
            rows=c.execute(f"SELECT id FROM runs WHERE {' AND '.join(conds)} ORDER BY created_at DESC LIMIT ?",args).fetchall()
        return [r for row in rows if (r:=self.load_run(row["id"]))]
    def stats(self):
        with self._conn() as c:
            nr=c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            nm=c.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
            bs=dict(c.execute("SELECT status,COUNT(*) FROM runs GROUP BY status").fetchall())
        return {"total_runs":nr,"total_metric_points":nm,"by_status":bs}

def _t_test_paired(a: List[float], b: List[float]) -> Tuple[float, float]:
    """Simplified paired t-test; returns (t_stat, p_value_approx)."""
    n = min(len(a), len(b))
    if n < 2: return 0.0, 1.0
    diffs = [a[i] - b[i] for i in range(n)]
    mean_d = sum(diffs) / n
    var_d = sum((d - mean_d)**2 for d in diffs) / (n - 1)
    if var_d == 0: return 0.0, 1.0
    t = mean_d / math.sqrt(var_d / n)
    # Approximate two-tailed p using normal CDF approximation
    abs_t = abs(t)
    p = 2 * (1 - (0.5 * (1 + math.erf(abs_t / math.sqrt(2)))))
    return round(t, 4), round(p, 4)

class ExperimentTracker:
    """
    Log, compare, and analyse LLM/ML experiments.

    Usage:
        tracker = ExperimentTracker()
        run = tracker.create_run("gpt4-baseline", experiment="summarisation",
                                  params={"model":"gpt-4o","temperature":0.7})
        tracker.start_run(run.id)
        for step, loss in enumerate(training_losses):
            tracker.log_metric(run.id, "loss", loss, step=step)
        tracker.finish_run(run.id)

        best = tracker.best_run("summarisation", metric="loss", mode="min")
        print(best.params)
    """
    def __init__(self, db_path: str = "data/experiments.db"):
        self._store = ExpStore(db_path)
        self._active: Dict[str, Run] = {}

    def create_run(self, name: str, experiment: str = "default",
                   params: Dict = None, tags: List[str] = None,
                   notes: str = "") -> Run:
        run = Run(id=str(uuid.uuid4())[:12], name=name, experiment=experiment,
                   params=params or {}, tags=tags or [], notes=notes)
        self._active[run.id] = run
        self._store.save_run(run)
        logger.info(f"Run created: {run.id} '{name}' [{experiment}]")
        return run

    def start_run(self, run_id: str):
        run = self._get(run_id)
        if run: run.status = RunStatus.RUNNING; run.updated_at = time.time(); self._store.save_run(run)

    def finish_run(self, run_id: str):
        run = self._get(run_id)
        if run:
            run.status = RunStatus.COMPLETED
            run.duration_s = time.time() - run.created_at
            run.updated_at = time.time()
            self._store.save_run(run)

    def fail_run(self, run_id: str, reason: str = ""):
        run = self._get(run_id)
        if run:
            run.status = RunStatus.FAILED
            if reason: run.notes += f"\nFAILED: {reason}"
            run.updated_at = time.time()
            self._store.save_run(run)

    def log_metric(self, run_id: str, name: str, value: float, step: int = 0):
        run = self._get(run_id)
        if not run: return
        run.log_metric(name, value, step)
        mp = MetricPoint(run_id=run_id, name=name, value=value, step=step)
        self._store.save_metric(mp)
        self._store.save_run(run)

    def log_params(self, run_id: str, params: Dict):
        run = self._get(run_id)
        if not run: return
        run.params.update(params); run.updated_at = time.time()
        self._store.save_run(run)

    def add_artefact(self, run_id: str, path: str):
        run = self._get(run_id)
        if not run: return
        run.artefacts.append(path); self._store.save_run(run)

    def get_run(self, run_id: str) -> Optional[Run]:
        return self._active.get(run_id) or self._store.load_run(run_id)

    def list_runs(self, experiment: str = None, status: str = None,
                   tags: List[str] = None, limit: int = 50) -> List[Run]:
        return self._store.list_runs(experiment, status, tags, limit)

    def best_run(self, experiment: str, metric: str, mode: str = "max") -> Optional[Run]:
        runs = self.list_runs(experiment=experiment, status="completed")
        if not runs: return None
        scored = [(r.best_metric(metric, mode), r) for r in runs if r.best_metric(metric, mode) is not None]
        if not scored: return None
        scored.sort(key=lambda x: x[0], reverse=(mode == "max"))
        return scored[0][1]

    def compare_runs(self, run_ids: List[str]) -> Dict:
        runs = [self.get_run(rid) for rid in run_ids]
        runs = [r for r in runs if r]
        if len(runs) < 2: return {"error": "Need ≥2 runs to compare"}
        # Param diff
        all_param_keys = set(k for r in runs for k in r.params)
        param_table = {k: {r.id: r.params.get(k) for r in runs} for k in all_param_keys}
        # Metric comparison
        all_metric_keys = set(k for r in runs for k in r.summary)
        metric_table = {k: {r.id: r.summary.get(k) for r in runs} for k in all_metric_keys}
        # T-test for pairs
        sig_tests = {}
        if len(runs) == 2:
            r1, r2 = runs[0], runs[1]
            for metric in set(r1.metrics) & set(r2.metrics):
                t, p = _t_test_paired(r1.metrics[metric], r2.metrics[metric])
                sig_tests[metric] = {"t_stat": t, "p_value": p,
                                       "significant_p05": p < 0.05}
        return {"run_ids":[r.id for r in runs],"params":param_table,
                "metrics":metric_table,"significance_tests":sig_tests}

    def stats(self) -> Dict:
        return self._store.stats()

    def _get(self, run_id: str) -> Optional[Run]:
        return self._active.get(run_id) or self._store.load_run(run_id)

    def register_routes(self, app, prefix: str = ""):
        from aiohttp import web
        async def create_ep(req):
            d = await req.json()
            run = self.create_run(d["name"],d.get("experiment","default"),
                                   d.get("params",{}),d.get("tags",[]),d.get("notes",""))
            return web.json_response(run.to_dict(),status=201)
        async def log_ep(req):
            d = await req.json(); rid = req.match_info["id"]
            for name,value in d.get("metrics",{}).items():
                self.log_metric(rid,name,float(value),int(d.get("step",0)))
            run = self.get_run(rid)
            return web.json_response(run.to_dict() if run else {"error":"not found"})
        async def finish_ep(req):
            self.finish_run(req.match_info["id"])
            return web.json_response({"status":"completed"})
        async def compare_ep(req):
            d = await req.json()
            return web.json_response(self.compare_runs(d.get("run_ids",[])))
        async def best_ep(req):
            q = req.rel_url.query
            run = self.best_run(q.get("experiment","default"),
                                 q.get("metric","loss"),q.get("mode","max"))
            if not run: return web.json_response({"error":"no completed runs"},status=404)
            return web.json_response(run.to_dict())
        async def list_ep(req):
            q = req.rel_url.query
            runs = self.list_runs(q.get("experiment"),q.get("status"),limit=int(q.get("limit",50)))
            return web.json_response({"runs":[r.to_dict() for r in runs]})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/experiments"
        app.router.add_post(p, create_ep); app.router.add_get(p, list_ep)
        app.router.add_post(f"{p}/{{id}}/metrics", log_ep)
        app.router.add_post(f"{p}/{{id}}/finish", finish_ep)
        app.router.add_post(f"{p}/compare", compare_ep)
        app.router.add_get(f"{p}/best", best_ep)
        app.router.add_get(f"{p}/stats", stats_ep)
        logger.info(f"Experiment tracker API at {prefix}/experiments/")
