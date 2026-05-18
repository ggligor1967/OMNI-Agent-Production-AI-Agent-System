"""OMNI Agent — A/B Testing V2: multi-variant experiments, statistical significance, rollouts."""
from __future__ import annotations
import hashlib, json, math, random, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class ExperimentStatus(str, Enum):
    DRAFT    = "draft"
    RUNNING  = "running"
    PAUSED   = "paused"
    CONCLUDED = "concluded"
    ARCHIVED  = "archived"


class AllocationStrategy(str, Enum):
    RANDOM      = "random"       # pure random
    HASH_USER   = "hash_user"    # deterministic by user_id
    WEIGHTED    = "weighted"     # respects variant weights
    ROLLOUT     = "rollout"      # gradual percentage rollout


@dataclass
class Variant:
    variant_id: str
    name: str
    weight: float = 1.0           # relative weight for allocation
    config: Dict[str, Any] = field(default_factory=dict)
    is_control: bool = False


@dataclass
class Assignment:
    assignment_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    experiment_id: str = ""
    variant_id: str = ""
    user_id: str = ""
    ts: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MetricObservation:
    experiment_id: str
    variant_id: str
    user_id: str
    metric_name: str
    value: float
    ts: float = field(default_factory=time.time)


@dataclass
class VariantStats:
    variant_id: str
    name: str
    assignments: int = 0
    observations: Dict[str, List[float]] = field(default_factory=dict)

    def mean(self, metric: str) -> Optional[float]:
        vals = self.observations.get(metric, [])
        return sum(vals) / len(vals) if vals else None

    def std(self, metric: str) -> float:
        vals = self.observations.get(metric, [])
        if len(vals) < 2:
            return 0.0
        m = sum(vals) / len(vals)
        return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))

    def count(self, metric: str) -> int:
        return len(self.observations.get(metric, []))


def _z_test(mean_a: float, std_a: float, n_a: int,
             mean_b: float, std_b: float, n_b: int) -> Tuple[float, float]:
    """Two-sample z-test. Returns (z_score, p_value)."""
    if n_a < 2 or n_b < 2:
        return 0.0, 1.0
    se = math.sqrt((std_a ** 2 / n_a) + (std_b ** 2 / n_b))
    if se == 0:
        return 0.0, 1.0
    z = (mean_b - mean_a) / se
    # Approximate p-value via error function (two-tailed)
    p = 2 * (1 - _normal_cdf(abs(z)))
    return z, p


def _normal_cdf(z: float) -> float:
    """Approximation of standard normal CDF."""
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


@dataclass
class SignificanceResult:
    metric: str
    control_mean: float
    variant_mean: float
    lift: float              # relative change
    z_score: float
    p_value: float
    significant: bool        # p < alpha
    confidence: float        # 1 - p_value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric": self.metric,
            "control_mean": round(self.control_mean, 6),
            "variant_mean": round(self.variant_mean, 6),
            "lift_pct": round(self.lift * 100, 2),
            "z_score": round(self.z_score, 4),
            "p_value": round(self.p_value, 6),
            "significant": self.significant,
            "confidence_pct": round(self.confidence * 100, 2),
        }


@dataclass
class Experiment:
    experiment_id: str
    name: str
    variants: List[Variant] = field(default_factory=list)
    status: ExperimentStatus = ExperimentStatus.DRAFT
    strategy: AllocationStrategy = AllocationStrategy.HASH_USER
    rollout_pct: float = 100.0      # % of traffic to include
    alpha: float = 0.05             # significance threshold
    metrics: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    concluded_at: Optional[float] = None
    tags: List[str] = field(default_factory=list)
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "name": self.name,
            "status": self.status.value,
            "strategy": self.strategy.value,
            "rollout_pct": self.rollout_pct,
            "variants": [{"id": v.variant_id, "name": v.name,
                          "weight": v.weight, "control": v.is_control}
                         for v in self.variants],
            "metrics": self.metrics,
        }


class ABTestingV2:
    """
    Full A/B testing framework:
    - Multi-variant experiments
    - Deterministic (hash-based) and random assignment
    - Gradual rollout (% traffic)
    - Metric tracking and z-test significance
    - Control vs treatment comparison
    - SQLite persistence
    """

    def __init__(self, db_path: str = ":memory:"):
        self._experiments: Dict[str, Experiment] = {}
        self._assignments: Dict[str, Dict[str, Assignment]] = {}  # exp_id → user_id → assignment
        self._stats: Dict[str, Dict[str, VariantStats]] = {}       # exp_id → variant_id → stats
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS ab_experiments (
                experiment_id TEXT PRIMARY KEY, name TEXT, status TEXT,
                strategy TEXT, rollout_pct REAL, created_at REAL
            );
            CREATE TABLE IF NOT EXISTS ab_assignments (
                assignment_id TEXT PRIMARY KEY, experiment_id TEXT,
                variant_id TEXT, user_id TEXT, ts REAL
            );
            CREATE TABLE IF NOT EXISTS ab_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_id TEXT, variant_id TEXT, user_id TEXT,
                metric_name TEXT, value REAL, ts REAL
            );
        """)
        self._db.commit()

    # ── EXPERIMENT MANAGEMENT ─────────────────────────────────────────

    def create_experiment(self, name: str,
                           variants: List[Dict[str, Any]],
                           strategy: AllocationStrategy = AllocationStrategy.HASH_USER,
                           rollout_pct: float = 100.0,
                           alpha: float = 0.05,
                           metrics: Optional[List[str]] = None,
                           tags: Optional[List[str]] = None,
                           description: str = "",
                           experiment_id: Optional[str] = None) -> Experiment:
        eid = experiment_id or str(uuid.uuid4())[:8]
        parsed_variants = []
        for i, v in enumerate(variants):
            parsed_variants.append(Variant(
                variant_id=v.get("id", f"v{i}"),
                name=v.get("name", f"Variant {i}"),
                weight=v.get("weight", 1.0),
                config=v.get("config", {}),
                is_control=v.get("is_control", i == 0),
            ))
        exp = Experiment(
            experiment_id=eid, name=name,
            variants=parsed_variants, strategy=strategy,
            rollout_pct=rollout_pct, alpha=alpha,
            metrics=list(metrics or []),
            tags=list(tags or []), description=description)
        self._experiments[eid] = exp
        self._assignments[eid] = {}
        self._stats[eid] = {v.variant_id: VariantStats(v.variant_id, v.name)
                            for v in parsed_variants}
        self._db.execute(
            "INSERT OR REPLACE INTO ab_experiments VALUES (?,?,?,?,?,?)",
            (eid, name, exp.status.value, strategy.value, rollout_pct, exp.created_at))
        self._db.commit()
        return exp

    def start(self, experiment_id: str) -> Experiment:
        exp = self._get(experiment_id)
        exp.status = ExperimentStatus.RUNNING
        exp.started_at = time.time()
        self._update_status(experiment_id, ExperimentStatus.RUNNING)
        return exp

    def pause(self, experiment_id: str) -> Experiment:
        exp = self._get(experiment_id)
        exp.status = ExperimentStatus.PAUSED
        self._update_status(experiment_id, ExperimentStatus.PAUSED)
        return exp

    def conclude(self, experiment_id: str) -> Experiment:
        exp = self._get(experiment_id)
        exp.status = ExperimentStatus.CONCLUDED
        exp.concluded_at = time.time()
        self._update_status(experiment_id, ExperimentStatus.CONCLUDED)
        return exp

    def _update_status(self, eid: str, status: ExperimentStatus):
        self._db.execute("UPDATE ab_experiments SET status=? WHERE experiment_id=?",
                         (status.value, eid))
        self._db.commit()

    def _get(self, experiment_id: str) -> Experiment:
        exp = self._experiments.get(experiment_id)
        if not exp:
            raise KeyError(f"Experiment '{experiment_id}' not found")
        return exp

    # ── ASSIGNMENT ────────────────────────────────────────────────────

    def assign(self, experiment_id: str, user_id: str,
               metadata: Optional[Dict] = None) -> Optional[Assignment]:
        exp = self._get(experiment_id)
        if exp.status != ExperimentStatus.RUNNING:
            return None

        # Return existing assignment (sticky)
        if user_id in self._assignments[experiment_id]:
            return self._assignments[experiment_id][user_id]

        # Rollout gate
        if exp.rollout_pct < 100.0:
            h = int(hashlib.md5(f"{experiment_id}:{user_id}:rollout".encode())
                    .hexdigest(), 16) % 100
            if h >= exp.rollout_pct:
                return None

        # Select variant
        variant = self._select_variant(exp, user_id)
        if not variant:
            return None

        asn = Assignment(experiment_id=experiment_id,
                         variant_id=variant.variant_id,
                         user_id=user_id, metadata=metadata or {})
        self._assignments[experiment_id][user_id] = asn
        self._stats[experiment_id][variant.variant_id].assignments += 1
        self._db.execute(
            "INSERT INTO ab_assignments VALUES (?,?,?,?,?)",
            (asn.assignment_id, experiment_id, variant.variant_id, user_id, asn.ts))
        self._db.commit()
        return asn

    def _select_variant(self, exp: Experiment, user_id: str) -> Optional[Variant]:
        if not exp.variants:
            return None
        if exp.strategy == AllocationStrategy.HASH_USER:
            h = int(hashlib.md5(f"{exp.experiment_id}:{user_id}".encode())
                    .hexdigest(), 16)
            total_w = sum(v.weight for v in exp.variants)
            r = (h % 10000) / 10000 * total_w
            cumulative = 0.0
            for v in exp.variants:
                cumulative += v.weight
                if r < cumulative:
                    return v
            return exp.variants[-1]
        if exp.strategy == AllocationStrategy.RANDOM:
            weights = [v.weight for v in exp.variants]
            return random.choices(exp.variants, weights=weights, k=1)[0]
        if exp.strategy == AllocationStrategy.WEIGHTED:
            weights = [v.weight for v in exp.variants]
            return random.choices(exp.variants, weights=weights, k=1)[0]
        return exp.variants[0]

    def get_variant(self, experiment_id: str,
                    user_id: str) -> Optional[Variant]:
        """Get assigned variant config for a user."""
        asn = self._assignments.get(experiment_id, {}).get(user_id)
        if not asn:
            return None
        exp = self._experiments.get(experiment_id)
        if not exp:
            return None
        for v in exp.variants:
            if v.variant_id == asn.variant_id:
                return v
        return None

    # ── METRICS ───────────────────────────────────────────────────────

    def record(self, experiment_id: str, user_id: str,
               metric_name: str, value: float):
        asn = self._assignments.get(experiment_id, {}).get(user_id)
        if not asn:
            return
        stats = self._stats[experiment_id][asn.variant_id]
        stats.observations.setdefault(metric_name, []).append(value)
        self._db.execute(
            "INSERT INTO ab_observations (experiment_id,variant_id,user_id,metric_name,value,ts) "
            "VALUES (?,?,?,?,?,?)",
            (experiment_id, asn.variant_id, user_id, metric_name, value, time.time()))
        self._db.commit()

    # ── ANALYSIS ──────────────────────────────────────────────────────

    def analyze(self, experiment_id: str,
                metric: str) -> List[SignificanceResult]:
        exp   = self._get(experiment_id)
        stats = self._stats[experiment_id]
        control = next((v for v in exp.variants if v.is_control), exp.variants[0])
        ctrl_stats = stats[control.variant_id]
        results = []
        for v in exp.variants:
            if v.is_control:
                continue
            vs = stats[v.variant_id]
            cm = ctrl_stats.mean(metric)
            vm = vs.mean(metric)
            if cm is None or vm is None:
                continue
            z, p = _z_test(cm, ctrl_stats.std(metric), ctrl_stats.count(metric),
                           vm, vs.std(metric), vs.count(metric))
            lift = (vm - cm) / cm if cm != 0 else 0.0
            results.append(SignificanceResult(
                metric=metric,
                control_mean=cm, variant_mean=vm,
                lift=lift, z_score=z, p_value=p,
                significant=p < exp.alpha,
                confidence=1.0 - p))
        return results

    def summary(self, experiment_id: str) -> Dict[str, Any]:
        exp   = self._get(experiment_id)
        stats = self._stats[experiment_id]
        return {
            "experiment": exp.to_dict(),
            "variants": {
                vid: {
                    "assignments": vs.assignments,
                    "metrics": {m: {"mean": vs.mean(m), "n": vs.count(m)}
                                for m in vs.observations},
                }
                for vid, vs in stats.items()
            }
        }

    # ── QUERY ─────────────────────────────────────────────────────────

    def list_experiments(self, status: Optional[ExperimentStatus] = None) -> List[Dict]:
        exps = list(self._experiments.values())
        if status:
            exps = [e for e in exps if e.status == status]
        return [e.to_dict() for e in exps]

    def stats(self) -> Dict[str, Any]:
        total_assignments = sum(
            len(a) for a in self._assignments.values())
        return {
            "experiments": len(self._experiments),
            "running": sum(1 for e in self._experiments.values()
                           if e.status == ExperimentStatus.RUNNING),
            "total_assignments": total_assignments,
        }
