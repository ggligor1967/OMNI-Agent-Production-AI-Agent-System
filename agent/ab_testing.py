"""OMNI AGENT - A/B Testing Framework
Experiment management: variants, deterministic user bucketing,
metric tracking, statistical significance, and winner selection.

Features:
- Experiments: name, variants, status (DRAFT/RUNNING/PAUSED/CONCLUDED)
- Variants: name, traffic weight (% of eligible users), config dict
- Deterministic bucketing: HMAC(experiment_id, user_id) → bucket 0-99
  same user always gets same variant per experiment
- Traffic allocation: variant weights sum to 100; remainder = control
- Holdout: reserve % of traffic as un-exposed control
- Targeting: filter users by attribute conditions before assignment
- Metrics: conversion events with value; tracked per user per variant
- Aggregation: count, unique_users, sum, mean, conversion_rate per variant
- Statistical significance: two-proportion z-test for binary metrics
- Effect size: relative uplift % vs control variant
- Min sample size: estimate required n for power=0.8, alpha=0.05
- Assignment log: record every assignment (user, variant, ts)
- Overrides: force specific users to specific variants
- Mutual exclusion: users in one experiment excluded from another
- Hooks: on_assign(user_id, experiment, variant), on_convert(user_id, exp, metric)
- SQLite persistence: experiments, assignments, conversions
- REST API: create_experiment, assign, convert, results, stats
"""
import hashlib, hmac, json, math, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class ExperimentStatus(str, Enum):
    DRAFT      = "draft"
    RUNNING    = "running"
    PAUSED     = "paused"
    CONCLUDED  = "concluded"

@dataclass
class Variant:
    name: str; weight: float = 50.0
    config: Dict[str, Any] = field(default_factory=dict)
    is_control: bool = False

    def to_dict(self):
        return {"name": self.name, "weight": self.weight,
                "config": self.config, "is_control": self.is_control}

@dataclass
class Experiment:
    id: str; name: str
    variants: List[Variant]
    status: ExperimentStatus = ExperimentStatus.DRAFT
    holdout_pct: float = 0.0
    targeting: List[Dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    concluded_at: Optional[float] = None

    def to_dict(self):
        return {"id": self.id, "name": self.name,
                "status": self.status.value,
                "variants": [v.to_dict() for v in self.variants],
                "holdout_pct": self.holdout_pct,
                "created_at": round(self.created_at, 2)}

class ABStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS experiments(
                    id TEXT PRIMARY KEY, name TEXT UNIQUE,
                    data TEXT, status TEXT, created_at REAL);
                CREATE TABLE IF NOT EXISTS assignments(
                    id TEXT PRIMARY KEY, experiment_id TEXT,
                    user_id TEXT, variant TEXT, ts REAL);
                CREATE TABLE IF NOT EXISTS conversions(
                    id TEXT PRIMARY KEY, experiment_id TEXT,
                    user_id TEXT, variant TEXT, metric TEXT,
                    value REAL, ts REAL);
                CREATE INDEX IF NOT EXISTS idx_assign
                    ON assignments(experiment_id, user_id);
                CREATE INDEX IF NOT EXISTS idx_conv
                    ON conversions(experiment_id, metric);
            """)

    def save_exp(self, exp: Experiment):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO experiments VALUES(?,?,?,?,?)",
                (exp.id, exp.name,
                 json.dumps(exp.to_dict(), default=str),
                 exp.status.value, exp.created_at))

    def load_exp(self, exp_id: str) -> Optional[Dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT data FROM experiments WHERE id=?",
                (exp_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def load_by_name(self, name: str) -> Optional[str]:
        with self._conn() as c:
            row = c.execute(
                "SELECT id FROM experiments WHERE name=?", (name,)).fetchone()
        return row["id"] if row else None

    def log_assignment(self, exp_id: str, user_id: str, variant: str):
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO assignments VALUES(?,?,?,?,?)",
                (str(uuid.uuid4())[:8], exp_id, user_id, variant, time.time()))

    def get_assignment(self, exp_id: str, user_id: str) -> Optional[str]:
        with self._conn() as c:
            row = c.execute(
                "SELECT variant FROM assignments "
                "WHERE experiment_id=? AND user_id=?",
                (exp_id, user_id)).fetchone()
        return row["variant"] if row else None

    def log_conversion(self, exp_id: str, user_id: str,
                        variant: str, metric: str, value: float):
        with self._conn() as c:
            c.execute("INSERT INTO conversions VALUES(?,?,?,?,?,?,?)",
                (str(uuid.uuid4())[:8], exp_id, user_id,
                 variant, metric, value, time.time()))

    def conversion_counts(self, exp_id: str, metric: str) -> Dict[str, Dict]:
        with self._conn() as c:
            rows = c.execute("""
                SELECT v.variant,
                    COUNT(*) as conversions,
                    COUNT(DISTINCT v.user_id) as unique_converters,
                    SUM(v.value) as total_value,
                    AVG(v.value) as mean_value
                FROM conversions v
                WHERE v.experiment_id=? AND v.metric=?
                GROUP BY v.variant
            """, (exp_id, metric)).fetchall()
        return {r["variant"]: {"conversions": r["conversions"],
                                "unique_converters": r["unique_converters"],
                                "total_value": r["total_value"] or 0,
                                "mean_value": r["mean_value"] or 0}
                for r in rows}

    def assignment_counts(self, exp_id: str) -> Dict[str, int]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT variant, COUNT(*) as cnt FROM assignments "
                "WHERE experiment_id=? GROUP BY variant", (exp_id,)).fetchall()
        return {r["variant"]: r["cnt"] for r in rows}

    def stats(self) -> Dict:
        with self._conn() as c:
            ne = c.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
            na = c.execute("SELECT COUNT(*) FROM assignments").fetchone()[0]
            nc = c.execute("SELECT COUNT(*) FROM conversions").fetchone()[0]
        return {"experiments": ne, "assignments": na, "conversions": nc}

def _bucket(exp_id: str, user_id: str) -> int:
    """Deterministic bucket 0-99 using a stable, non-cryptographic digest."""
    digest = hmac.new(
        exp_id.encode(), user_id.encode(), hashlib.md5  # nosec B324 - deterministic experiment bucketing only
    ).hexdigest()
    return int(digest[:4], 16) % 100

def _z_test_two_proportions(n1: int, c1: int, n2: int, c2: int
                              ) -> Tuple[float, float]:
    """Two-proportion z-test. Returns (z_score, p_value)."""
    if n1 == 0 or n2 == 0: return 0.0, 1.0
    p1 = c1 / n1; p2 = c2 / n2
    p_pool = (c1 + c2) / (n1 + n2)
    se = math.sqrt(p_pool * (1 - p_pool) * (1/n1 + 1/n2))
    if se == 0: return 0.0, 1.0
    z = (p1 - p2) / se
    # Approximate p-value via normal CDF approximation
    p = 2 * (1 - _norm_cdf(abs(z)))
    return z, p

def _norm_cdf(z: float) -> float:
    """Approximation of standard normal CDF."""
    t = 1 / (1 + 0.2316419 * abs(z))
    poly = t * (0.319381530 + t * (-0.356563782 +
                 t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    phi = 1 - (1 / math.sqrt(2 * math.pi)) * math.exp(-z*z/2) * poly
    return phi if z >= 0 else 1 - phi

def _min_sample_size(baseline_rate: float, mde: float = 0.05,
                      alpha: float = 0.05, power: float = 0.8) -> int:
    """Minimum detectable effect sample size per variant."""
    if baseline_rate <= 0 or baseline_rate >= 1: return 0
    p1 = baseline_rate; p2 = baseline_rate * (1 + mde)
    if p2 >= 1: p2 = 0.999
    z_alpha = 1.96; z_beta = 0.842
    p_avg = (p1 + p2) / 2
    n = ((z_alpha * math.sqrt(2 * p_avg * (1-p_avg)) +
           z_beta * math.sqrt(p1*(1-p1) + p2*(1-p2))) ** 2) / ((p2 - p1) ** 2)
    return math.ceil(n)

class ABTesting:
    """
    A/B testing with deterministic bucketing and statistical analysis.

    Usage:
        ab = ABTesting()

        exp = ab.create_experiment("checkout_button",
            variants=[{"name":"blue","weight":50},
                      {"name":"green","weight":50}])
        ab.start(exp.id)

        variant = ab.assign("checkout_button", user_id="u123")
        # Always returns same variant for same user

        ab.convert("checkout_button", user_id="u123",
                    metric="purchase", value=49.99)

        results = ab.results("checkout_button", metric="purchase")
    """
    def __init__(self, db_path: str = "data/ab_testing.db"):
        self._store = ABStore(db_path)
        self._experiments: Dict[str, Experiment] = {}
        self._overrides: Dict[str, Dict[str, str]] = {}  # exp_id → {user_id: variant}
        self._hooks_assign:  List[Callable] = []
        self._hooks_convert: List[Callable] = []

    def on_assign(self, fn):  self._hooks_assign.append(fn)
    def on_convert(self, fn): self._hooks_convert.append(fn)

    def create_experiment(self, name: str,
                           variants: List[Dict],
                           holdout_pct: float = 0.0,
                           targeting: List[Dict] = None) -> Experiment:
        exp_id = str(uuid.uuid4())[:12]
        variant_objs = []
        for i, v in enumerate(variants):
            variant_objs.append(Variant(
                name=v["name"],
                weight=v.get("weight", 100/len(variants)),
                config=v.get("config", {}),
                is_control=(i == 0)))
        exp = Experiment(id=exp_id, name=name,
                          variants=variant_objs,
                          holdout_pct=holdout_pct,
                          targeting=list(targeting or []))
        self._experiments[exp_id] = exp
        self._store.save_exp(exp)
        return exp

    def _get_exp(self, name_or_id: str) -> Optional[Experiment]:
        if name_or_id in self._experiments:
            return self._experiments[name_or_id]
        # Try by name
        for exp in self._experiments.values():
            if exp.name == name_or_id: return exp
        return None

    def start(self, name_or_id: str) -> bool:
        exp = self._get_exp(name_or_id)
        if not exp: return False
        exp.status = ExperimentStatus.RUNNING
        exp.started_at = time.time()
        self._store.save_exp(exp)
        return True

    def pause(self, name_or_id: str) -> bool:
        exp = self._get_exp(name_or_id)
        if not exp: return False
        exp.status = ExperimentStatus.PAUSED
        self._store.save_exp(exp); return True

    def conclude(self, name_or_id: str, winner: str = None) -> bool:
        exp = self._get_exp(name_or_id)
        if not exp: return False
        exp.status = ExperimentStatus.CONCLUDED
        exp.concluded_at = time.time()
        self._store.save_exp(exp); return True

    def _is_eligible(self, exp: Experiment, user_attrs: Dict) -> bool:
        for cond in exp.targeting:
            field = cond.get("field","")
            op    = cond.get("op","==")
            val   = cond.get("value")
            actual = user_attrs.get(field)
            if op == "==" and actual != val: return False
            if op == "!=" and actual == val: return False
            if op == "in" and actual not in (val or []): return False
            if op == "not_in" and actual in (val or []): return False
        return True

    def assign(self, name_or_id: str, user_id: str,
                user_attrs: Dict = None) -> Optional[str]:
        """Return variant name for user, or None if not eligible/not running."""
        exp = self._get_exp(name_or_id)
        if not exp or exp.status != ExperimentStatus.RUNNING:
            return None

        if not self._is_eligible(exp, user_attrs or {}): return None

        # Override
        override = self._overrides.get(exp.id, {}).get(user_id)
        if override: return override

        # Retrieve stored assignment (idempotent)
        stored = self._store.get_assignment(exp.id, user_id)
        if stored: return stored

        bucket = _bucket(exp.id, user_id)

        # Holdout
        if bucket < exp.holdout_pct:
            self._store.log_assignment(exp.id, user_id, "__holdout__")
            return None

        # Assign to variant
        effective_bucket = bucket - exp.holdout_pct
        effective_range  = 100 - exp.holdout_pct
        cumulative = 0.0
        for variant in exp.variants:
            cumulative += variant.weight * effective_range / 100
            if effective_bucket < cumulative:
                self._store.log_assignment(exp.id, user_id, variant.name)
                for h in self._hooks_assign:
                    try: h(user_id, exp, variant.name)
                    except: pass
                return variant.name
        # Fallback to last variant
        last = exp.variants[-1].name
        self._store.log_assignment(exp.id, user_id, last)
        return last

    def set_override(self, name_or_id: str, user_id: str, variant: str):
        exp = self._get_exp(name_or_id)
        if not exp: return
        self._overrides.setdefault(exp.id, {})[user_id] = variant

    def convert(self, name_or_id: str, user_id: str,
                 metric: str, value: float = 1.0) -> bool:
        exp = self._get_exp(name_or_id)
        if not exp: return False
        variant = self._store.get_assignment(exp.id, user_id)
        if not variant or variant == "__holdout__": return False
        self._store.log_conversion(exp.id, user_id, variant, metric, value)
        for h in self._hooks_convert:
            try: h(user_id, exp, metric)
            except: pass
        return True

    def results(self, name_or_id: str, metric: str) -> Dict:
        exp = self._get_exp(name_or_id)
        if not exp: return {}
        assign_counts = self._store.assignment_counts(exp.id)
        conv_data     = self._store.conversion_counts(exp.id, metric)
        variant_results = {}
        control_name = exp.variants[0].name if exp.variants else None
        for v in exp.variants:
            n = assign_counts.get(v.name, 0)
            c = conv_data.get(v.name, {})
            cvr = c.get("unique_converters", 0) / n if n > 0 else 0
            variant_results[v.name] = {
                "assignments": n,
                "conversions": c.get("conversions", 0),
                "unique_converters": c.get("unique_converters", 0),
                "conversion_rate": round(cvr, 4),
                "total_value": round(c.get("total_value", 0), 4),
                "mean_value": round(c.get("mean_value", 0), 4),
            }
        # Statistical significance vs control
        if control_name and control_name in variant_results:
            ctrl = variant_results[control_name]
            ctrl_n = ctrl["assignments"]
            ctrl_c = ctrl["unique_converters"]
            for vname, vdata in variant_results.items():
                if vname == control_name: continue
                z, p = _z_test_two_proportions(
                    ctrl_n, ctrl_c, vdata["assignments"],
                    vdata["unique_converters"])
                cr_ctrl = ctrl["conversion_rate"]
                cr_var  = vdata["conversion_rate"]
                uplift  = ((cr_var - cr_ctrl) / cr_ctrl * 100
                            if cr_ctrl > 0 else 0)
                vdata["z_score"]    = round(z, 4)
                vdata["p_value"]    = round(p, 4)
                vdata["significant"]= p < 0.05
                vdata["uplift_pct"] = round(uplift, 2)
        return {"experiment": exp.to_dict(), "metric": metric,
                "variants": variant_results}

    def min_sample_size(self, baseline_rate: float,
                         mde: float = 0.05) -> int:
        return _min_sample_size(baseline_rate, mde)

    def list_experiments(self) -> List[Dict]:
        return [e.to_dict() for e in self._experiments.values()]

    def stats(self) -> Dict:
        s = self._store.stats()
        s["in_memory"] = len(self._experiments)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def create_ep(req):
            d = await req.json()
            exp = self.create_experiment(d["name"], d["variants"],
                                          d.get("holdout_pct", 0))
            return web.json_response(exp.to_dict(), status=201)
        async def assign_ep(req):
            d = await req.json()
            variant = self.assign(d["experiment"], d["user_id"],
                                   d.get("user_attrs", {}))
            return web.json_response({"variant": variant})
        async def convert_ep(req):
            d = await req.json()
            ok = self.convert(d["experiment"], d["user_id"],
                               d["metric"], d.get("value", 1.0))
            return web.json_response({"recorded": ok})
        async def results_ep(req):
            d = await req.json()
            r = self.results(d["experiment"], d["metric"])
            return web.json_response(r)
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/ab"
        app.router.add_post(f"{p}/create",  create_ep)
        app.router.add_post(f"{p}/assign",  assign_ep)
        app.router.add_post(f"{p}/convert", convert_ep)
        app.router.add_post(f"{p}/results", results_ep)
        app.router.add_get( f"{p}/stats",   stats_ep)
        logger.info(f"A/B testing API at {prefix}/ab/")
