"""
OMNI AGENT - A/B Traffic Router
Weighted routing across model configurations with canary deployments,
sticky sessions, real-time metrics, and automatic rollback.

Features:
- Named routing experiments with weighted variant buckets
- Consistent hashing: same user always gets same variant (sticky)
- Canary deployments: gradually ramp traffic to new configs
- Real-time metrics: per-variant latency, error rate, cost tracking
- Automatic rollback: disable variant on error threshold breach
- Override rules: pin specific users/sessions to a variant
- Schedule: activate/deactivate experiments by time window
- Shadow mode: send traffic to variant without returning its result
- REST API: create/update/pause experiments, query metrics
"""
import time
import uuid
import json
import hashlib
import sqlite3
import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from collections import deque, defaultdict

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# VARIANT & EXPERIMENT MODEL
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Variant:
    """A single routing target within an experiment."""
    id: str
    name: str
    weight: float              # relative weight (will be normalized)
    config: Dict[str, Any]     # e.g. {"model": "gpt-4", "temperature": 0.7}
    enabled: bool = True
    shadow: bool = False       # shadow mode: run but don't use result
    description: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "name": self.name,
            "weight": self.weight, "config": self.config,
            "enabled": self.enabled, "shadow": self.shadow,
            "description": self.description,
        }


class ExperimentStatus(str, Enum):
    ACTIVE   = "active"
    PAUSED   = "paused"
    ENDED    = "ended"
    CANARY   = "canary"      # gradual rollout in progress


@dataclass
class Experiment:
    id: str
    name: str
    variants: List[Variant]
    status: ExperimentStatus = ExperimentStatus.ACTIVE
    sticky: bool = True              # same user always gets same variant
    overrides: Dict[str, str] = field(default_factory=dict)   # user_id → variant_id
    starts_at: Optional[float] = None
    ends_at: Optional[float] = None
    canary_target_id: str = ""       # variant being ramped up
    canary_target_pct: float = 0.0   # final target percentage
    canary_step_pct: float = 5.0     # percent increment per step
    description: str = ""
    created_at: float = field(default_factory=time.time)

    @property
    def is_active(self) -> bool:
        now = time.time()
        if self.status == ExperimentStatus.PAUSED:
            return False
        if self.starts_at and now < self.starts_at:
            return False
        if self.ends_at and now > self.ends_at:
            return False
        return True

    def active_variants(self) -> List[Variant]:
        return [v for v in self.variants if v.enabled]

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "name": self.name,
            "status": self.status,
            "variants": [v.to_dict() for v in self.variants],
            "sticky": self.sticky,
            "starts_at": self.starts_at, "ends_at": self.ends_at,
            "canary_target_id": self.canary_target_id,
            "canary_target_pct": self.canary_target_pct,
            "description": self.description,
            "is_active": self.is_active,
        }


# ══════════════════════════════════════════════════════════════════════════════
# ROUTING DECISION
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class RoutingDecision:
    experiment_id: str
    variant: Variant
    user_id: str
    sticky_key: str
    shadow: bool = False
    override: bool = False      # was this from an explicit override rule?
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {
            "experiment_id": self.experiment_id,
            "variant_id": self.variant.id,
            "variant_name": self.variant.name,
            "config": self.variant.config,
            "shadow": self.shadow,
            "override": self.override,
        }


# ══════════════════════════════════════════════════════════════════════════════
# PER-VARIANT METRICS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class VariantMetrics:
    variant_id: str
    requests: int = 0
    successes: int = 0
    errors: int = 0
    total_latency_ms: float = 0.0
    total_cost_usd: float = 0.0
    recent_latencies: deque = field(default_factory=lambda: deque(maxlen=500))
    error_window: deque = field(default_factory=lambda: deque(maxlen=100))  # recent bool outcomes

    def record(self, success: bool, latency_ms: float, cost_usd: float = 0.0):
        self.requests += 1
        if success:
            self.successes += 1
        else:
            self.errors += 1
        self.total_latency_ms += latency_ms
        self.total_cost_usd += cost_usd
        self.recent_latencies.append(latency_ms)
        self.error_window.append(0 if success else 1)

    @property
    def error_rate(self) -> float:
        if not self.error_window:
            return 0.0
        return sum(self.error_window) / len(self.error_window)

    @property
    def avg_latency_ms(self) -> float:
        if not self.recent_latencies:
            return 0.0
        return sum(self.recent_latencies) / len(self.recent_latencies)

    @property
    def p95_latency_ms(self) -> float:
        if not self.recent_latencies:
            return 0.0
        sorted_lats = sorted(self.recent_latencies)
        idx = int(0.95 * len(sorted_lats))
        return sorted_lats[min(idx, len(sorted_lats) - 1)]

    def to_dict(self) -> Dict:
        return {
            "variant_id": self.variant_id,
            "requests": self.requests,
            "successes": self.successes,
            "errors": self.errors,
            "error_rate": round(self.error_rate, 4),
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "p95_latency_ms": round(self.p95_latency_ms, 1),
            "total_cost_usd": round(self.total_cost_usd, 6),
        }


# ══════════════════════════════════════════════════════════════════════════════
# AB ROUTER
# ══════════════════════════════════════════════════════════════════════════════

class ABRouter:
    """
    Weighted A/B traffic router with canary support and auto-rollback.

    Usage:
        router = ABRouter()

        exp = router.create_experiment(
            name="model_comparison",
            variants=[
                Variant(id="v_gpt4",  name="GPT-4",      weight=50,
                        config={"model": "gpt-4", "temperature": 0.7}),
                Variant(id="v_claude",name="Claude 3.5",  weight=50,
                        config={"model": "claude-3-5-sonnet", "temperature": 0.7}),
            ],
            sticky=True,
        )

        # Route a user
        decision = router.route(exp.id, user_id="user_123")
        config = decision.variant.config
        # → use config["model"] for this user's request

        # Record outcome
        router.record(exp.id, decision.variant.id,
                      success=True, latency_ms=430, cost_usd=0.002)

        # Canary: gradually roll out v_claude to 100%
        router.start_canary(exp.id, target_variant_id="v_claude",
                            target_pct=100, step_pct=10)

        # Metrics
        metrics = router.metrics(exp.id)
    """

    def __init__(self, db_path: str = "data/ab_router.db",
                 auto_rollback_error_rate: float = 0.2):
        self._experiments: Dict[str, Experiment] = {}
        self._metrics: Dict[str, Dict[str, VariantMetrics]] = defaultdict(dict)
        self._decision_log: deque = deque(maxlen=10000)
        self._auto_rollback_threshold = auto_rollback_error_rate
        self._canary_tasks: Dict[str, asyncio.Task] = {}
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._init_db()
        self._load_experiments()

    def _init_db(self):
        with sqlite3.connect(self._db_path) as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS experiments (
                    id   TEXT PRIMARY KEY,
                    data TEXT NOT NULL
                );
            """)

    def _save_experiment(self, exp: Experiment):
        with sqlite3.connect(self._db_path) as c:
            c.execute("INSERT OR REPLACE INTO experiments (id, data) VALUES (?,?)",
                      (exp.id, json.dumps(exp.to_dict())))

    def _load_experiments(self):
        try:
            with sqlite3.connect(self._db_path) as c:
                rows = c.execute("SELECT id, data FROM experiments").fetchall()
            for row_id, data in rows:
                d = json.loads(data)
                variants = [Variant(**{k: v for k, v in v.items()}) for v in d["variants"]]
                exp = Experiment(
                    id=d["id"], name=d["name"], variants=variants,
                    status=ExperimentStatus(d["status"]),
                    sticky=d.get("sticky", True),
                    overrides=d.get("overrides", {}),
                    starts_at=d.get("starts_at"), ends_at=d.get("ends_at"),
                    canary_target_id=d.get("canary_target_id", ""),
                    canary_target_pct=d.get("canary_target_pct", 0.0),
                    description=d.get("description", ""),
                )
                self._experiments[exp.id] = exp
        except Exception as e:
            logger.debug(f"No saved experiments to load: {e}")

    # ── Experiment Management ─────────────────────────────────────────────────

    def create_experiment(self, name: str, variants: List[Variant],
                          sticky: bool = True,
                          starts_at: float = None,
                          ends_at: float = None,
                          description: str = "") -> Experiment:
        exp = Experiment(
            id=str(uuid.uuid4())[:12],
            name=name, variants=variants, sticky=sticky,
            starts_at=starts_at, ends_at=ends_at,
            description=description,
        )
        self._experiments[exp.id] = exp
        # Init per-variant metrics
        for v in variants:
            self._metrics[exp.id][v.id] = VariantMetrics(v.id)
        self._save_experiment(exp)
        logger.info(f"Experiment created: id={exp.id} name='{name}' "
                   f"variants={len(variants)}")
        return exp

    def get_experiment(self, exp_id: str) -> Optional[Experiment]:
        return self._experiments.get(exp_id)

    def list_experiments(self) -> List[Dict]:
        return [e.to_dict() for e in self._experiments.values()]

    def pause(self, exp_id: str) -> bool:
        exp = self._experiments.get(exp_id)
        if exp:
            exp.status = ExperimentStatus.PAUSED
            self._save_experiment(exp)
        return exp is not None

    def resume(self, exp_id: str) -> bool:
        exp = self._experiments.get(exp_id)
        if exp:
            exp.status = ExperimentStatus.ACTIVE
            self._save_experiment(exp)
        return exp is not None

    def end(self, exp_id: str) -> bool:
        exp = self._experiments.get(exp_id)
        if exp:
            exp.status = ExperimentStatus.ENDED
            self._save_experiment(exp)
        return exp is not None

    def set_override(self, exp_id: str, user_id: str, variant_id: str):
        """Force a specific user to always get a specific variant."""
        exp = self._experiments.get(exp_id)
        if exp:
            exp.overrides[user_id] = variant_id
            self._save_experiment(exp)

    def clear_override(self, exp_id: str, user_id: str):
        exp = self._experiments.get(exp_id)
        if exp and user_id in exp.overrides:
            del exp.overrides[user_id]
            self._save_experiment(exp)

    # ── Routing ───────────────────────────────────────────────────────────────

    def _pick_variant(self, exp: Experiment, sticky_key: str) -> Optional[Variant]:
        """
        Pick a variant using weighted random with consistent hashing for sticky routing.
        """
        active = exp.active_variants()
        if not active:
            return None

        total_weight = sum(v.weight for v in active)
        if total_weight <= 0:
            return active[0]

        if exp.sticky:
            # Deterministic hash → same user always lands in same bucket
            h = int(
                hashlib.md5(  # nosec B324 - sticky rollout bucketing only
                    f"{exp.id}:{sticky_key}".encode(), usedforsecurity=False
                ).hexdigest(),
                16,
            )
            bucket = (h % 10000) / 10000.0 * total_weight
        else:
            import random
            bucket = random.random() * total_weight

        cumulative = 0.0
        for v in active:
            cumulative += v.weight
            if bucket <= cumulative:
                return v
        return active[-1]

    def route(self, exp_id: str, user_id: str = "",
              sticky_key: str = None) -> Optional[RoutingDecision]:
        """
        Route a request to a variant.

        Args:
            exp_id:     Experiment to route in
            user_id:    User identifier for sticky routing
            sticky_key: Override key for sticky hash (defaults to user_id)

        Returns:
            RoutingDecision or None if experiment is inactive/has no variants
        """
        exp = self._experiments.get(exp_id)
        if not exp or not exp.is_active:
            return None

        key = sticky_key or user_id or str(uuid.uuid4())

        # Check override
        if user_id and user_id in exp.overrides:
            variant_id = exp.overrides[user_id]
            variant = next((v for v in exp.variants if v.id == variant_id), None)
            if variant and variant.enabled:
                decision = RoutingDecision(
                    experiment_id=exp_id, variant=variant,
                    user_id=user_id, sticky_key=key,
                    shadow=variant.shadow, override=True,
                )
                self._decision_log.append(decision)
                return decision

        variant = self._pick_variant(exp, key)
        if not variant:
            return None

        decision = RoutingDecision(
            experiment_id=exp_id, variant=variant,
            user_id=user_id, sticky_key=key,
            shadow=variant.shadow,
        )
        self._decision_log.append(decision)
        return decision

    # ── Metrics Recording ─────────────────────────────────────────────────────

    def record(self, exp_id: str, variant_id: str,
               success: bool, latency_ms: float,
               cost_usd: float = 0.0):
        """Record the outcome of a routed request."""
        if exp_id not in self._metrics:
            self._metrics[exp_id] = {}
        if variant_id not in self._metrics[exp_id]:
            self._metrics[exp_id][variant_id] = VariantMetrics(variant_id)

        vm = self._metrics[exp_id][variant_id]
        vm.record(success, latency_ms, cost_usd)

        # Auto-rollback if error rate exceeds threshold
        if not success and vm.requests >= 20:
            if vm.error_rate > self._auto_rollback_threshold:
                exp = self._experiments.get(exp_id)
                if exp:
                    variant = next((v for v in exp.variants if v.id == variant_id), None)
                    if variant and variant.enabled:
                        variant.enabled = False
                        self._save_experiment(exp)
                        logger.warning(
                            f"Auto-rollback: variant {variant_id} disabled "
                            f"(error_rate={vm.error_rate:.1%})"
                        )

    def metrics(self, exp_id: str) -> Dict:
        """Get per-variant metrics for an experiment."""
        exp = self._experiments.get(exp_id)
        variant_metrics = self._metrics.get(exp_id, {})
        return {
            "experiment_id": exp_id,
            "experiment_name": exp.name if exp else "",
            "variants": {vid: vm.to_dict() for vid, vm in variant_metrics.items()},
        }

    def all_metrics(self) -> Dict:
        return {eid: self.metrics(eid) for eid in self._experiments}

    # ── Canary Deployment ─────────────────────────────────────────────────────

    def start_canary(self, exp_id: str, target_variant_id: str,
                     target_pct: float = 100.0,
                     step_pct: float = 5.0,
                     step_interval_s: float = 300.0) -> bool:
        """
        Gradually increase traffic to target_variant from current weight
        to target_pct over time.
        """
        exp = self._experiments.get(exp_id)
        if not exp:
            return False
        target = next((v for v in exp.variants if v.id == target_variant_id), None)
        if not target:
            return False

        exp.status = ExperimentStatus.CANARY
        exp.canary_target_id = target_variant_id
        exp.canary_target_pct = target_pct
        exp.canary_step_pct = step_pct
        self._save_experiment(exp)

        async def _ramp():
            total_weight = sum(v.weight for v in exp.variants)
            while target.weight < target_pct:
                await asyncio.sleep(step_interval_s)
                increment = min(step_pct, target_pct - target.weight)
                target.weight += increment
                # Reduce other variants proportionally
                others = [v for v in exp.variants if v.id != target_variant_id]
                if others:
                    reduction_each = increment / len(others)
                    for v in others:
                        v.weight = max(0, v.weight - reduction_each)
                self._save_experiment(exp)
                logger.info(f"Canary step: {target.name} → {target.weight:.1f}%")

            exp.status = ExperimentStatus.ACTIVE
            self._save_experiment(exp)
            logger.info(f"Canary complete: {target.name} at {target.weight:.1f}%")

        task = asyncio.create_task(_ramp())
        self._canary_tasks[exp_id] = task
        return True

    def stop_canary(self, exp_id: str) -> bool:
        task = self._canary_tasks.pop(exp_id, None)
        if task:
            task.cancel()
        exp = self._experiments.get(exp_id)
        if exp:
            exp.status = ExperimentStatus.ACTIVE
            self._save_experiment(exp)
        return task is not None

    # ── REST API ──────────────────────────────────────────────────────────────

    def register_routes(self, app, prefix: str = ""):
        from aiohttp import web

        async def list_exps(request):
            return web.json_response({"experiments": self.list_experiments()})

        async def create_exp(request):
            data = await request.json()
            variants = [
                Variant(
                    id=v.get("id", str(uuid.uuid4())[:8]),
                    name=v["name"], weight=float(v["weight"]),
                    config=v.get("config", {}),
                    enabled=v.get("enabled", True),
                    shadow=v.get("shadow", False),
                    description=v.get("description", ""),
                )
                for v in data["variants"]
            ]
            exp = self.create_experiment(
                name=data["name"], variants=variants,
                sticky=data.get("sticky", True),
                starts_at=data.get("starts_at"),
                ends_at=data.get("ends_at"),
                description=data.get("description", ""),
            )
            return web.json_response(exp.to_dict(), status=201)

        async def get_exp(request):
            exp = self.get_experiment(request.match_info["id"])
            if not exp:
                return web.json_response({"error": "not found"}, status=404)
            return web.json_response(exp.to_dict())

        async def route_ep(request):
            data = await request.json()
            exp_id = data.get("experiment_id") or request.match_info.get("id")
            decision = self.route(exp_id, user_id=data.get("user_id", ""))
            if not decision:
                return web.json_response({"error": "no route"}, status=404)
            return web.json_response(decision.to_dict())

        async def record_ep(request):
            data = await request.json()
            self.record(
                exp_id=data["experiment_id"],
                variant_id=data["variant_id"],
                success=data.get("success", True),
                latency_ms=float(data.get("latency_ms", 0)),
                cost_usd=float(data.get("cost_usd", 0)),
            )
            return web.json_response({"recorded": True})

        async def metrics_ep(request):
            exp_id = request.match_info.get("id")
            if exp_id:
                return web.json_response(self.metrics(exp_id))
            return web.json_response(self.all_metrics())

        async def control_ep(request):
            exp_id = request.match_info["id"]
            action = request.match_info["action"]
            if action == "pause":
                ok = self.pause(exp_id)
            elif action == "resume":
                ok = self.resume(exp_id)
            elif action == "end":
                ok = self.end(exp_id)
            else:
                return web.json_response({"error": "unknown action"}, status=400)
            return web.json_response({"ok": ok})

        app.router.add_get( f"{prefix}/ab/experiments",                       list_exps)
        app.router.add_post(f"{prefix}/ab/experiments",                       create_exp)
        app.router.add_get( f"{prefix}/ab/experiments/{{id}}",                get_exp)
        app.router.add_post(f"{prefix}/ab/experiments/{{id}}/route",          route_ep)
        app.router.add_post(f"{prefix}/ab/record",                            record_ep)
        app.router.add_get( f"{prefix}/ab/experiments/{{id}}/metrics",        metrics_ep)
        app.router.add_get( f"{prefix}/ab/metrics",                           metrics_ep)
        app.router.add_post(f"{prefix}/ab/experiments/{{id}}/{{action}}",     control_ep)
        logger.info(f"A/B Router API routes registered at {prefix}/ab/")
