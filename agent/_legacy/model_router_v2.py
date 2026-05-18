"""OMNI Agent — Model Router V2: latency/cost/capability-aware LLM routing with fallback chains."""
from __future__ import annotations
import asyncio, math, random, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


class RoutingStrategy(str, Enum):
    LOWEST_COST     = "lowest_cost"
    LOWEST_LATENCY  = "lowest_latency"
    HIGHEST_QUALITY = "highest_quality"
    ROUND_ROBIN     = "round_robin"
    WEIGHTED        = "weighted"
    CAPABILITY      = "capability"
    ADAPTIVE        = "adaptive"      # tracks live stats


class ModelStatus(str, Enum):
    ACTIVE    = "active"
    DEGRADED  = "degraded"
    OFFLINE   = "offline"
    RATE_LIMITED = "rate_limited"


@dataclass
class ModelSpec:
    model_id: str
    name: str
    provider: str
    cost_per_1k_tokens: float = 0.0     # USD
    avg_latency_ms: float     = 1000.0
    quality_score: float      = 0.7     # 0–1
    context_window: int       = 4096
    capabilities: Set[str]    = field(default_factory=set)
    weight: float             = 1.0     # for weighted routing
    max_rps: float            = 10.0    # requests/second
    status: ModelStatus       = ModelStatus.ACTIVE
    metadata: Dict[str, Any]  = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "name": self.name,
            "provider": self.provider,
            "cost_per_1k": self.cost_per_1k_tokens,
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "quality_score": self.quality_score,
            "context_window": self.context_window,
            "capabilities": list(self.capabilities),
            "status": self.status.value,
        }


@dataclass
class RouteDecision:
    decision_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    model_id: str = ""
    strategy: RoutingStrategy = RoutingStrategy.ADAPTIVE
    score: float = 0.0
    fallback_chain: List[str] = field(default_factory=list)
    reason: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "model_id": self.model_id,
            "strategy": self.strategy.value,
            "score": round(self.score, 4),
            "fallback_chain": self.fallback_chain,
            "reason": self.reason,
        }


@dataclass
class ModelStats:
    model_id: str
    requests: int = 0
    successes: int = 0
    failures: int = 0
    total_latency_ms: float = 0.0
    total_tokens: int = 0
    total_cost: float = 0.0
    last_used: Optional[float] = None

    @property
    def avg_latency(self) -> float:
        return self.total_latency_ms / self.requests if self.requests > 0 else 0.0

    @property
    def success_rate(self) -> float:
        return self.successes / self.requests if self.requests > 0 else 1.0

    @property
    def error_rate(self) -> float:
        return 1.0 - self.success_rate

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "requests": self.requests,
            "successes": self.successes,
            "failures": self.failures,
            "avg_latency_ms": round(self.avg_latency, 1),
            "success_rate": round(self.success_rate, 4),
            "total_cost": round(self.total_cost, 6),
            "total_tokens": self.total_tokens,
        }


class NoModelAvailable(Exception):
    pass


class ModelRouterV2:
    """
    Routes LLM calls to the best available model based on strategy.
    Tracks live performance, supports fallback chains, and adapts routing.
    """

    def __init__(self, strategy: RoutingStrategy = RoutingStrategy.ADAPTIVE,
                 fallback_enabled: bool = True):
        self.strategy        = strategy
        self.fallback_enabled = fallback_enabled
        self._models: Dict[str, ModelSpec]  = {}
        self._stats:  Dict[str, ModelStats] = {}
        self._rr_index = 0
        self._decisions: List[RouteDecision] = []
        self._filters: List[Callable[[ModelSpec], bool]] = []

    # ── MODEL MANAGEMENT ──────────────────────────────────────────────

    def register(self, spec: ModelSpec):
        self._models[spec.model_id] = spec
        self._stats[spec.model_id]  = ModelStats(model_id=spec.model_id)

    def unregister(self, model_id: str):
        self._models.pop(model_id, None)
        self._stats.pop(model_id, None)

    def set_status(self, model_id: str, status: ModelStatus):
        if model_id in self._models:
            self._models[model_id].status = status

    def add_filter(self, fn: Callable[[ModelSpec], bool]):
        """Add a global filter (e.g. context_window >= N)."""
        self._filters.append(fn)

    def clear_filters(self):
        self._filters.clear()

    # ── CANDIDATES ────────────────────────────────────────────────────

    def _active_models(self,
                       required_capabilities: Optional[Set[str]] = None,
                       min_context: int = 0) -> List[ModelSpec]:
        candidates = [m for m in self._models.values()
                      if m.status == ModelStatus.ACTIVE]
        if required_capabilities:
            candidates = [m for m in candidates
                          if required_capabilities.issubset(m.capabilities)]
        if min_context > 0:
            candidates = [m for m in candidates
                          if m.context_window >= min_context]
        for fn in self._filters:
            candidates = [m for m in candidates if fn(m)]
        return candidates

    # ── SCORING ───────────────────────────────────────────────────────

    def _score(self, model: ModelSpec, strategy: RoutingStrategy) -> float:
        stats = self._stats.get(model.model_id)
        if strategy == RoutingStrategy.LOWEST_COST:
            # Lower cost → higher score
            max_cost = max((m.cost_per_1k_tokens for m in self._models.values()), default=1.0)
            return 1.0 - (model.cost_per_1k_tokens / (max_cost + 1e-9))
        if strategy == RoutingStrategy.LOWEST_LATENCY:
            lat = stats.avg_latency if stats and stats.requests > 0 else model.avg_latency_ms
            max_lat = max((m.avg_latency_ms for m in self._models.values()), default=1000.0)
            return 1.0 - (lat / (max_lat + 1.0))
        if strategy == RoutingStrategy.HIGHEST_QUALITY:
            return model.quality_score
        if strategy == RoutingStrategy.WEIGHTED:
            total_w = sum(m.weight for m in self._models.values())
            return model.weight / (total_w + 1e-9)
        if strategy == RoutingStrategy.ADAPTIVE:
            # Composite: success_rate * quality - latency_penalty - cost_penalty
            sr  = stats.success_rate if stats else 1.0
            lat = (stats.avg_latency if stats and stats.requests > 0
                   else model.avg_latency_ms)
            max_lat  = max((m.avg_latency_ms for m in self._models.values()), default=1000.0)
            max_cost = max((m.cost_per_1k_tokens for m in self._models.values()), default=1.0)
            lat_pen  = lat / (max_lat + 1.0)
            cost_pen = model.cost_per_1k_tokens / (max_cost + 1e-9)
            return sr * model.quality_score - 0.3 * lat_pen - 0.2 * cost_pen
        return 0.0

    # ── ROUTING ───────────────────────────────────────────────────────

    def route(self, required_capabilities: Optional[Set[str]] = None,
              min_context: int = 0,
              strategy: Optional[RoutingStrategy] = None) -> RouteDecision:
        strat      = strategy or self.strategy
        candidates = self._active_models(required_capabilities, min_context)
        if not candidates:
            raise NoModelAvailable("No active models match the requirements")

        if strat == RoutingStrategy.ROUND_ROBIN:
            model = candidates[self._rr_index % len(candidates)]
            self._rr_index += 1
            score = 1.0
        elif strat == RoutingStrategy.CAPABILITY:
            model = random.choice(candidates)
            score = 1.0
        else:
            scored    = [(m, self._score(m, strat)) for m in candidates]
            scored.sort(key=lambda x: x[1], reverse=True)
            model, score = scored[0]

        fallback = ([m.model_id for m, _ in scored[1:3]]
                    if strat not in (RoutingStrategy.ROUND_ROBIN,
                                     RoutingStrategy.CAPABILITY)
                    else [m.model_id for m in candidates[1:3]])

        decision = RouteDecision(
            model_id=model.model_id,
            strategy=strat,
            score=score,
            fallback_chain=fallback,
            reason=f"{strat.value} selected '{model.name}'",
        )
        self._decisions.append(decision)
        return decision

    async def route_async(self, **kwargs) -> RouteDecision:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self.route(**kwargs))

    def route_with_fallback(self, fn: Callable[[str], Any],
                             required_capabilities: Optional[Set[str]] = None,
                             min_context: int = 0) -> Tuple[Any, str]:
        """Try primary model, fall back through chain on failure."""
        decision = self.route(required_capabilities, min_context)
        chain    = [decision.model_id] + decision.fallback_chain
        for model_id in chain:
            t0 = time.time()
            try:
                result = fn(model_id)
                self._record(model_id, True, (time.time() - t0) * 1000)
                return result, model_id
            except Exception:
                self._record(model_id, False, (time.time() - t0) * 1000)
                if not self.fallback_enabled:
                    raise
        raise NoModelAvailable("All models in fallback chain failed")

    # ── STATS TRACKING ────────────────────────────────────────────────

    def _record(self, model_id: str, success: bool, latency_ms: float,
                tokens: int = 0):
        s = self._stats.get(model_id)
        if not s:
            return
        s.requests += 1
        s.total_latency_ms += latency_ms
        s.last_used = time.time()
        if success:
            s.successes += 1
        else:
            s.failures += 1
        model = self._models.get(model_id)
        if model and tokens:
            s.total_tokens += tokens
            s.total_cost += (tokens / 1000) * model.cost_per_1k_tokens

    def record_outcome(self, model_id: str, success: bool,
                       latency_ms: float, tokens: int = 0):
        self._record(model_id, success, latency_ms, tokens)
        # Auto-degrade if error rate too high
        s = self._stats.get(model_id)
        if s and s.requests >= 5 and s.error_rate > 0.7:
            self.set_status(model_id, ModelStatus.DEGRADED)

    # ── INTROSPECTION ─────────────────────────────────────────────────

    def get_stats(self, model_id: str) -> Optional[Dict[str, Any]]:
        s = self._stats.get(model_id)
        return s.to_dict() if s else None

    def all_stats(self) -> Dict[str, Any]:
        return {mid: s.to_dict() for mid, s in self._stats.items()}

    def list_models(self) -> List[Dict[str, Any]]:
        return [m.to_dict() for m in self._models.values()]

    def recent_decisions(self, n: int = 10) -> List[Dict[str, Any]]:
        return [d.to_dict() for d in self._decisions[-n:]]

    def stats(self) -> Dict[str, Any]:
        total_req  = sum(s.requests  for s in self._stats.values())
        total_cost = sum(s.total_cost for s in self._stats.values())
        active     = sum(1 for m in self._models.values()
                         if m.status == ModelStatus.ACTIVE)
        return {
            "models": len(self._models),
            "active_models": active,
            "total_requests": total_req,
            "total_cost_usd": round(total_cost, 6),
            "decisions": len(self._decisions),
            "strategy": self.strategy.value,
        }
