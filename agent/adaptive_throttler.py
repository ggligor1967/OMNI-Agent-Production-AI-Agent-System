"""OMNI Agent — Adaptive Throttler: token-bucket + sliding-window per-key rate limiter."""
from __future__ import annotations
import asyncio, sqlite3, threading, time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class ThrottleStrategy(str, Enum):
    TOKEN_BUCKET   = "token_bucket"
    SLIDING_WINDOW = "sliding_window"
    FIXED_WINDOW   = "fixed_window"
    LEAKY_BUCKET   = "leaky_bucket"


class ThrottleExceeded(Exception):
    pass


@dataclass
class ThrottlePolicy:
    name: str
    strategy: ThrottleStrategy = ThrottleStrategy.TOKEN_BUCKET
    # Token bucket
    capacity: float      = 60.0    # max tokens
    refill_rate: float   = 1.0     # tokens/second
    # Sliding/Fixed window
    max_requests: int    = 60
    window_s: float      = 60.0
    # Adaptive
    adaptive: bool       = False
    scale_down_at: float = 0.9    # throttle harder at this error rate
    scale_up_at: float   = 0.1    # relax at this error rate


@dataclass
class BucketState:
    key: str
    tokens: float
    last_refill: float = field(default_factory=time.time)
    total_allowed: int = 0
    total_rejected: int = 0
    window_requests: List[float] = field(default_factory=list)  # timestamps
    leaky_level: float = 0.0
    last_leak: float = field(default_factory=time.time)

    @property
    def rejection_rate(self) -> float:
        total = self.total_allowed + self.total_rejected
        return self.total_rejected / total if total > 0 else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "tokens": round(self.tokens, 2),
            "total_allowed": self.total_allowed,
            "total_rejected": self.total_rejected,
            "rejection_rate": round(self.rejection_rate, 4),
        }


class AdaptiveThrottler:
    """
    Per-key rate limiter supporting four strategies with optional adaptive scaling.
    Thread-safe. Supports sync and async check methods.
    """

    def __init__(self, policy: ThrottlePolicy,
                 db_path: str = ":memory:"):
        self.policy = policy
        self._states: Dict[str, BucketState] = {}
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._on_throttle_hooks: List[Callable] = []

    def _init_db(self):
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS throttle_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, key TEXT, allowed INTEGER, strategy TEXT
            )""")
        self._db.commit()

    def _get_or_create(self, key: str) -> BucketState:
        if key not in self._states:
            self._states[key] = BucketState(
                key=key,
                tokens=self.policy.capacity,
            )
        return self._states[key]

    # ── TOKEN BUCKET ──────────────────────────────────────────────────

    def _token_bucket_check(self, state: BucketState, cost: float = 1.0) -> bool:
        now = time.time()
        elapsed = now - state.last_refill
        refill = elapsed * self.policy.refill_rate
        # Adaptive: slow refill if rejection rate is high
        if self.policy.adaptive and state.rejection_rate >= self.policy.scale_down_at:
            refill *= 0.5
        state.tokens = min(self.policy.capacity, state.tokens + refill)
        state.last_refill = now
        if state.tokens >= cost:
            state.tokens -= cost
            return True
        return False

    # ── SLIDING WINDOW ────────────────────────────────────────────────

    def _sliding_window_check(self, state: BucketState) -> bool:
        now = time.time()
        cutoff = now - self.policy.window_s
        state.window_requests = [t for t in state.window_requests if t > cutoff]
        if len(state.window_requests) < self.policy.max_requests:
            state.window_requests.append(now)
            return True
        return False

    # ── FIXED WINDOW ──────────────────────────────────────────────────

    def _fixed_window_check(self, state: BucketState) -> bool:
        now = time.time()
        window_start = now - (now % self.policy.window_s)
        # Prune requests outside current window
        state.window_requests = [t for t in state.window_requests if t >= window_start]
        if len(state.window_requests) < self.policy.max_requests:
            state.window_requests.append(now)
            return True
        return False

    # ── LEAKY BUCKET ──────────────────────────────────────────────────

    def _leaky_bucket_check(self, state: BucketState, cost: float = 1.0) -> bool:
        now = time.time()
        elapsed = now - state.last_leak
        leaked = elapsed * self.policy.refill_rate
        state.leaky_level = max(0.0, state.leaky_level - leaked)
        state.last_leak = now
        if state.leaky_level + cost <= self.policy.capacity:
            state.leaky_level += cost
            return True
        return False

    # ── PUBLIC API ────────────────────────────────────────────────────

    def check(self, key: str, cost: float = 1.0) -> bool:
        """Check if request is allowed. Returns True/False (does NOT raise)."""
        with self._lock:
            state = self._get_or_create(key)
            strategy = self.policy.strategy
            if strategy == ThrottleStrategy.TOKEN_BUCKET:
                allowed = self._token_bucket_check(state, cost)
            elif strategy == ThrottleStrategy.SLIDING_WINDOW:
                allowed = self._sliding_window_check(state)
            elif strategy == ThrottleStrategy.FIXED_WINDOW:
                allowed = self._fixed_window_check(state)
            else:  # LEAKY_BUCKET
                allowed = self._leaky_bucket_check(state, cost)
            if allowed:
                state.total_allowed += 1
            else:
                state.total_rejected += 1
                for hook in self._on_throttle_hooks:
                    try: hook(key, state)
                    except Exception: pass
            self._db.execute(
                "INSERT INTO throttle_events (ts,key,allowed,strategy) VALUES (?,?,?,?)",
                (time.time(), key, int(allowed), self.policy.strategy.value))
            self._db.commit()
            return allowed

    def require(self, key: str, cost: float = 1.0):
        """Like check() but raises ThrottleExceeded if denied."""
        if not self.check(key, cost):
            raise ThrottleExceeded(f"Rate limit exceeded for key '{key}'")

    async def check_async(self, key: str, cost: float = 1.0) -> bool:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.check, key, cost)

    async def require_async(self, key: str, cost: float = 1.0):
        if not await self.check_async(key, cost):
            raise ThrottleExceeded(f"Rate limit exceeded for key '{key}'")

    def wait_time(self, key: str) -> float:
        """Estimate seconds until next token is available (token bucket only)."""
        with self._lock:
            state = self._get_or_create(key)
            if self.policy.strategy != ThrottleStrategy.TOKEN_BUCKET:
                return 0.0
            deficit = 1.0 - state.tokens
            if deficit <= 0:
                return 0.0
            return deficit / self.policy.refill_rate

    def reset(self, key: str):
        """Reset state for a key."""
        with self._lock:
            self._states.pop(key, None)

    def reset_all(self):
        with self._lock:
            self._states.clear()

    def on_throttle(self, fn: Callable):
        """Hook called when a request is denied."""
        self._on_throttle_hooks.append(fn)

    def get_state(self, key: str) -> Optional[Dict[str, Any]]:
        state = self._states.get(key)
        return state.to_dict() if state else None

    def event_log(self, key: Optional[str] = None, limit: int = 50) -> List[Dict]:
        if key:
            rows = self._db.execute(
                "SELECT ts,key,allowed,strategy FROM throttle_events "
                "WHERE key=? ORDER BY ts DESC LIMIT ?", (key, limit)).fetchall()
        else:
            rows = self._db.execute(
                "SELECT ts,key,allowed,strategy FROM throttle_events "
                "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [{"ts": r[0], "key": r[1], "allowed": bool(r[2]), "strategy": r[3]}
                for r in rows]

    def stats(self) -> Dict[str, Any]:
        total_allowed  = sum(s.total_allowed  for s in self._states.values())
        total_rejected = sum(s.total_rejected for s in self._states.values())
        return {
            "policy": self.policy.name,
            "strategy": self.policy.strategy.value,
            "tracked_keys": len(self._states),
            "total_allowed": total_allowed,
            "total_rejected": total_rejected,
            "overall_rejection_rate": total_rejected / (total_allowed + total_rejected)
            if (total_allowed + total_rejected) > 0 else 0.0,
        }


# ── THROTTLER REGISTRY ────────────────────────────────────────────────────────

class ThrottlerRegistry:
    """Multiple named throttlers with different policies."""

    def __init__(self):
        self._throttlers: Dict[str, AdaptiveThrottler] = {}

    def register(self, name: str, policy: ThrottlePolicy,
                 db_path: str = ":memory:") -> AdaptiveThrottler:
        t = AdaptiveThrottler(policy, db_path=db_path)
        self._throttlers[name] = t
        return t

    def get(self, name: str) -> Optional[AdaptiveThrottler]:
        return self._throttlers.get(name)

    def check(self, throttler_name: str, key: str, cost: float = 1.0) -> bool:
        t = self._throttlers.get(throttler_name)
        if t is None:
            return True  # no throttler = allow by default
        return t.check(key, cost)

    def stats_all(self) -> Dict[str, Any]:
        return {name: t.stats() for name, t in self._throttlers.items()}

    def list_throttlers(self) -> List[str]:
        return list(self._throttlers.keys())
