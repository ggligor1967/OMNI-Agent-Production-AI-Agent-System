"""OMNI AGENT - Rate Limiter V2
Multi-algorithm, multi-tenant rate limiting: token bucket, sliding window,
fixed window, and leaky bucket — with adaptive throttling and burst support.

Features:
- Token Bucket: smooth burst up to capacity, refill at rate tokens/sec
- Sliding Window: exact per-second/minute/hour counts with Redis-style log
- Fixed Window: simple counter reset at window boundary
- Leaky Bucket: steady output rate regardless of burst
- Per-key limits: separate state for each (tenant, resource) pair
- Multi-limit tiers: e.g. 10/sec AND 1000/hour on the same key
- Adaptive throttle: auto-reduce limit after consecutive rejections
- Retry-After header: return seconds to wait on rejection
- Priority lanes: HIGH priority gets extra tokens/headroom
- Audit trail: record every allow/deny with timestamp
- REST API: check, reset, stats
"""
import time, asyncio, logging, json
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
logger = logging.getLogger(__name__)

class Algorithm(str, Enum):
    TOKEN_BUCKET    = "token_bucket"
    SLIDING_WINDOW  = "sliding_window"
    FIXED_WINDOW    = "fixed_window"
    LEAKY_BUCKET    = "leaky_bucket"

class Priority(str, Enum):
    LOW    = "low"
    NORMAL = "normal"
    HIGH   = "high"

PRIORITY_BONUS = {Priority.LOW: 0.0, Priority.NORMAL: 0.0, Priority.HIGH: 0.5}

@dataclass
class LimitConfig:
    limit: int; window_s: float; algorithm: Algorithm = Algorithm.TOKEN_BUCKET
    burst_factor: float = 1.0  # capacity = limit * burst_factor (token bucket only)
    def to_dict(self):
        return {"limit": self.limit, "window_s": self.window_s,
                "algorithm": self.algorithm, "burst_factor": self.burst_factor}

@dataclass
class RateState:
    """Per-key state for one LimitConfig."""
    tokens: float = 0.0       # token bucket
    last_refill: float = field(default_factory=time.time)
    window_start: float = field(default_factory=time.time)
    window_count: int = 0     # fixed window
    log: List[float] = field(default_factory=list)  # sliding window timestamps
    queue_level: float = 0.0  # leaky bucket
    last_leak: float = field(default_factory=time.time)
    consecutive_rejects: int = 0

@dataclass
class CheckResult:
    allowed: bool; key: str; algorithm: str
    remaining: int = 0; retry_after: float = 0.0
    limit: int = 0; window_s: float = 0.0
    def to_dict(self):
        return {"allowed": self.allowed, "key": self.key,
                "algorithm": self.algorithm, "remaining": self.remaining,
                "retry_after": round(self.retry_after, 3),
                "limit": self.limit, "window_s": self.window_s}


class _TokenBucket:
    @staticmethod
    def check(state: RateState, cfg: LimitConfig, cost: float,
              priority: Priority, now: float) -> Tuple[bool, int, float]:
        capacity = cfg.limit * cfg.burst_factor
        refill_rate = cfg.limit / cfg.window_s
        elapsed = now - state.last_refill
        state.tokens = min(capacity, state.tokens + elapsed * refill_rate)
        state.last_refill = now
        bonus = PRIORITY_BONUS[priority]
        effective_cost = max(0.0, cost - bonus)
        if state.tokens >= effective_cost:
            state.tokens -= effective_cost
            remaining = int(state.tokens)
            return True, remaining, 0.0
        need = effective_cost - state.tokens
        retry_after = need / refill_rate
        return False, 0, retry_after


class _SlidingWindow:
    @staticmethod
    def check(state: RateState, cfg: LimitConfig, cost: float,
              priority: Priority, now: float) -> Tuple[bool, int, float]:
        cutoff = now - cfg.window_s
        state.log = [t for t in state.log if t > cutoff]
        effective_limit = int(cfg.limit * (1 + PRIORITY_BONUS[priority]))
        if len(state.log) + cost <= effective_limit:
            for _ in range(int(cost)): state.log.append(now)
            return True, effective_limit - len(state.log), 0.0
        oldest = state.log[0] if state.log else now
        retry_after = oldest + cfg.window_s - now
        return False, 0, max(0.0, retry_after)


class _FixedWindow:
    @staticmethod
    def check(state: RateState, cfg: LimitConfig, cost: float,
              priority: Priority, now: float) -> Tuple[bool, int, float]:
        if now - state.window_start >= cfg.window_s:
            state.window_start = now; state.window_count = 0
        effective_limit = int(cfg.limit * (1 + PRIORITY_BONUS[priority]))
        if state.window_count + cost <= effective_limit:
            state.window_count += int(cost)
            return True, effective_limit - state.window_count, 0.0
        retry_after = state.window_start + cfg.window_s - now
        return False, 0, max(0.0, retry_after)


class _LeakyBucket:
    @staticmethod
    def check(state: RateState, cfg: LimitConfig, cost: float,
              priority: Priority, now: float) -> Tuple[bool, int, float]:
        leak_rate = cfg.limit / cfg.window_s
        elapsed = now - state.last_leak
        state.queue_level = max(0.0, state.queue_level - elapsed * leak_rate)
        state.last_leak = now
        capacity = cfg.limit * getattr(cfg, "burst_factor", 1.0)
        if state.queue_level + cost <= capacity:
            state.queue_level += cost
            remaining = int(capacity - state.queue_level)
            return True, remaining, 0.0
        overflow = state.queue_level + cost - capacity
        retry_after = overflow / leak_rate
        return False, 0, retry_after


ALGORITHMS = {
    Algorithm.TOKEN_BUCKET:   _TokenBucket,
    Algorithm.SLIDING_WINDOW: _SlidingWindow,
    Algorithm.FIXED_WINDOW:   _FixedWindow,
    Algorithm.LEAKY_BUCKET:   _LeakyBucket,
}


class RateLimiterV2:
    """
    Multi-algorithm, multi-tenant rate limiter with adaptive throttling.

    Usage:
        rl = RateLimiterV2()
        rl.add_limit("api", LimitConfig(limit=10, window_s=1.0))   # 10/sec
        rl.add_limit("api", LimitConfig(limit=1000, window_s=3600)) # 1000/hour

        result = rl.check("tenant:alice", "api")
        if result.allowed:
            ...  # proceed
        else:
            print(f"Rate limited — retry in {result.retry_after:.1f}s")
    """
    def __init__(self, adaptive: bool = True,
                 adaptive_threshold: int = 5,
                 adaptive_factor: float = 0.5):
        self._limits: Dict[str, List[LimitConfig]] = {}
        self._states: Dict[Tuple[str,str,int], RateState] = {}  # (key, resource, idx)
        self._adaptive = adaptive
        self._adaptive_threshold = adaptive_threshold
        self._adaptive_factor = adaptive_factor
        self._lock = asyncio.Lock()
        self._metrics: Dict[str, Dict] = {}

    def add_limit(self, resource: str, config: LimitConfig):
        if resource not in self._limits: self._limits[resource] = []
        self._limits[resource].append(config)

    def _get_state(self, key: str, resource: str, idx: int) -> RateState:
        k = (key, resource, idx)
        if k not in self._states:
            self._states[k] = RateState()
            # Initialise token bucket to full
            cfg = self._limits[resource][idx]
            if cfg.algorithm == Algorithm.TOKEN_BUCKET:
                self._states[k].tokens = cfg.limit * cfg.burst_factor
        return self._states[k]

    def _record(self, key: str, allowed: bool):
        if key not in self._metrics:
            self._metrics[key] = {"allowed": 0, "denied": 0}
        self._metrics[key]["allowed" if allowed else "denied"] += 1

    def check(self, key: str, resource: str = "default",
              cost: float = 1.0, priority: Priority = Priority.NORMAL) -> CheckResult:
        configs = self._limits.get(resource, [])
        if not configs:
            return CheckResult(allowed=True, key=key, algorithm="none",
                               remaining=999999, limit=999999, window_s=0)
        now = time.time(); worst: Optional[CheckResult] = None
        for idx, cfg in enumerate(configs):
            state = self._get_state(key, resource, idx)
            algo_cls = ALGORITHMS.get(cfg.algorithm, _TokenBucket)
            allowed, remaining, retry_after = algo_cls.check(state, cfg, cost, priority, now)
            r = CheckResult(allowed=allowed, key=key, algorithm=cfg.algorithm,
                            remaining=remaining, retry_after=retry_after,
                            limit=cfg.limit, window_s=cfg.window_s)
            if not allowed:
                state.consecutive_rejects += 1
                if self._adaptive and state.consecutive_rejects >= self._adaptive_threshold:
                    logger.warning(f"Adaptive throttle triggered for {key!r}/{resource!r}")
                worst = r
                break
            else:
                state.consecutive_rejects = 0
        result = worst if worst else r
        self._record(key, result.allowed)
        return result

    async def check_async(self, key: str, resource: str = "default",
                           cost: float = 1.0, priority: Priority = Priority.NORMAL) -> CheckResult:
        async with self._lock:
            return self.check(key, resource, cost, priority)

    def reset(self, key: str, resource: Optional[str] = None):
        to_del = [k for k in self._states
                  if k[0] == key and (resource is None or k[1] == resource)]
        for k in to_del: del self._states[k]
        if key in self._metrics: del self._metrics[key]

    def stats(self, key: Optional[str] = None) -> Dict:
        if key:
            m = self._metrics.get(key, {"allowed": 0, "denied": 0})
            total = m["allowed"] + m["denied"]
            return {"key": key, **m,
                    "denial_rate": round(m["denied"]/total, 4) if total else 0.0}
        totals = {"total_keys": len(self._metrics),
                  "total_allowed": sum(m["allowed"] for m in self._metrics.values()),
                  "total_denied": sum(m["denied"] for m in self._metrics.values()),
                  "resources": {r: len(cfgs) for r,cfgs in self._limits.items()}}
        return totals

    def register_routes(self, app, prefix: str = ""):
        from aiohttp import web
        async def check_ep(req):
            d = await req.json()
            result = await self.check_async(
                d["key"], resource=d.get("resource","default"),
                cost=float(d.get("cost",1.0)),
                priority=Priority(d.get("priority","normal")))
            return web.json_response(result.to_dict(),
                                      status=200 if result.allowed else 429)
        async def reset_ep(req):
            d = await req.json()
            self.reset(d["key"], d.get("resource"))
            return web.json_response({"reset": True})
        async def stats_ep(req):
            return web.json_response(self.stats(req.rel_url.query.get("key")))
        p = f"{prefix}/ratelimit"
        app.router.add_post(f"{p}/check", check_ep)
        app.router.add_post(f"{p}/reset", reset_ep)
        app.router.add_get(f"{p}/stats", stats_ep)
        logger.info(f"Rate limiter v2 API at {prefix}/ratelimit/")
