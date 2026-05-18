"""OMNI Agent — Rate Limiter V3: sliding window, token bucket, leaky bucket, fixed window."""
from __future__ import annotations
import sqlite3, threading, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class Algorithm(str, Enum):
    TOKEN_BUCKET   = "token_bucket"
    SLIDING_WINDOW = "sliding_window"
    FIXED_WINDOW   = "fixed_window"
    LEAKY_BUCKET   = "leaky_bucket"
    CONCURRENCY    = "concurrency"   # max concurrent requests


class LimitAction(str, Enum):
    REJECT  = "reject"
    QUEUE   = "queue"
    DEGRADE = "degrade"   # allow but mark as degraded


@dataclass
class LimitPolicy:
    policy_id: str
    name: str
    algorithm: Algorithm
    limit: int             # max requests / tokens
    window_s: float = 60.0
    burst: int = 0         # token bucket burst capacity
    refill_rate: float = 1.0  # tokens per second
    leak_rate: float = 1.0    # leaky bucket drain rate per second
    action: LimitAction = LimitAction.REJECT
    cost: int = 1          # cost per request (for weighted limiting)
    enabled: bool = True
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "name": self.name,
            "algorithm": self.algorithm.value,
            "limit": self.limit,
            "window_s": self.window_s,
            "action": self.action.value,
            "enabled": self.enabled,
        }


@dataclass
class LimitDecision:
    allowed: bool
    policy_id: str
    key: str
    remaining: int = 0
    retry_after_s: float = 0.0
    action: LimitAction = LimitAction.REJECT
    degraded: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "policy_id": self.policy_id,
            "key": self.key,
            "remaining": self.remaining,
            "retry_after_s": round(self.retry_after_s, 3),
            "degraded": self.degraded,
        }


# ── PER-KEY STATE ─────────────────────────────────────────────────────────────

@dataclass
class _TokenBucketState:
    tokens: float
    last_refill: float = field(default_factory=time.time)


@dataclass
class _LeakyBucketState:
    queue_size: int = 0
    last_leak: float = field(default_factory=time.time)


@dataclass
class _ConcurrencyState:
    active: int = 0


class RateLimiterV3:
    """
    Multi-algorithm rate limiter with per-key state.

    Supports:
    - Token Bucket (with burst)
    - Sliding Window (per-request timestamps)
    - Fixed Window (reset at intervals)
    - Leaky Bucket (steady drain rate)
    - Concurrency Limiter (max simultaneous)
    - Multiple policies per key
    - On-limit hooks
    - SQLite audit log
    """

    def __init__(self, db_path: str = ":memory:"):
        self._policies: Dict[str, LimitPolicy] = {}
        # State: policy_id → key → state object
        self._tb_state:   Dict[str, Dict[str, _TokenBucketState]] = {}
        self._sw_log:     Dict[str, Dict[str, List[float]]] = {}   # timestamps
        self._fw_state:   Dict[str, Dict[str, Tuple[int, float]]] = {}  # (count, window_start)
        self._lb_state:   Dict[str, Dict[str, _LeakyBucketState]] = {}
        self._cc_state:   Dict[str, Dict[str, _ConcurrencyState]] = {}
        self._lock = threading.RLock()
        self._hooks: List[Callable] = []
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._allow_count = 0
        self._deny_count  = 0

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS rl_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                policy_id TEXT, key TEXT, allowed INTEGER,
                remaining INTEGER, ts REAL
            );
        """)
        self._db.commit()

    # ── POLICY MANAGEMENT ─────────────────────────────────────────────

    def add_policy(self, name: str, algorithm: Algorithm,
                   limit: int, window_s: float = 60.0,
                   burst: int = 0, refill_rate: float = 1.0,
                   leak_rate: float = 1.0,
                   action: LimitAction = LimitAction.REJECT,
                   cost: int = 1,
                   tags: Optional[List[str]] = None,
                   policy_id: Optional[str] = None) -> LimitPolicy:
        pid = policy_id or str(uuid.uuid4())[:8]
        p = LimitPolicy(
            policy_id=pid, name=name, algorithm=algorithm,
            limit=limit, window_s=window_s,
            burst=burst or limit, refill_rate=refill_rate,
            leak_rate=leak_rate, action=action, cost=cost,
            tags=list(tags or []))
        self._policies[pid] = p
        return p

    def remove_policy(self, policy_id: str):
        self._policies.pop(policy_id, None)

    def enable_policy(self, policy_id: str):
        if policy_id in self._policies:
            self._policies[policy_id].enabled = True

    def disable_policy(self, policy_id: str):
        if policy_id in self._policies:
            self._policies[policy_id].enabled = False

    # ── CHECK / ACQUIRE ───────────────────────────────────────────────

    def check(self, policy_id: str, key: str,
              cost: Optional[int] = None) -> LimitDecision:
        """Check (and consume) rate limit for a key. Thread-safe."""
        policy = self._policies.get(policy_id)
        if not policy or not policy.enabled:
            return LimitDecision(allowed=True, policy_id=policy_id, key=key,
                                 remaining=-1)
        actual_cost = cost if cost is not None else policy.cost
        with self._lock:
            decision = self._dispatch(policy, key, actual_cost)
        self._log(decision)
        if not decision.allowed:
            self._deny_count += 1
            for fn in self._hooks:
                try: fn(decision)
                except Exception: pass
        else:
            self._allow_count += 1
        return decision

    def _dispatch(self, p: LimitPolicy, key: str,
                   cost: int) -> LimitDecision:
        if p.algorithm == Algorithm.TOKEN_BUCKET:
            return self._token_bucket(p, key, cost)
        if p.algorithm == Algorithm.SLIDING_WINDOW:
            return self._sliding_window(p, key, cost)
        if p.algorithm == Algorithm.FIXED_WINDOW:
            return self._fixed_window(p, key, cost)
        if p.algorithm == Algorithm.LEAKY_BUCKET:
            return self._leaky_bucket(p, key, cost)
        if p.algorithm == Algorithm.CONCURRENCY:
            return self._concurrency(p, key)
        return LimitDecision(allowed=True, policy_id=p.policy_id, key=key)

    # ── ALGORITHMS ────────────────────────────────────────────────────

    def _token_bucket(self, p: LimitPolicy, key: str,
                       cost: int) -> LimitDecision:
        states = self._tb_state.setdefault(p.policy_id, {})
        now = time.time()
        if key not in states:
            states[key] = _TokenBucketState(tokens=float(p.burst))
        state = states[key]
        # Refill
        elapsed = now - state.last_refill
        state.tokens = min(p.burst, state.tokens + elapsed * p.refill_rate)
        state.last_refill = now
        if state.tokens >= cost:
            state.tokens -= cost
            return LimitDecision(allowed=True, policy_id=p.policy_id, key=key,
                                 remaining=int(state.tokens), action=p.action)
        retry = (cost - state.tokens) / p.refill_rate if p.refill_rate > 0 else 0
        return LimitDecision(allowed=False, policy_id=p.policy_id, key=key,
                             remaining=0, retry_after_s=retry, action=p.action)

    def _sliding_window(self, p: LimitPolicy, key: str,
                         cost: int) -> LimitDecision:
        logs = self._sw_log.setdefault(p.policy_id, {})
        if key not in logs:
            logs[key] = []
        now = time.time()
        cutoff = now - p.window_s
        logs[key] = [t for t in logs[key] if t > cutoff]
        used = len(logs[key])
        if used + cost <= p.limit:
            for _ in range(cost):
                logs[key].append(now)
            return LimitDecision(allowed=True, policy_id=p.policy_id, key=key,
                                 remaining=p.limit - used - cost, action=p.action)
        oldest = logs[key][0] if logs[key] else now
        retry  = oldest + p.window_s - now
        return LimitDecision(allowed=False, policy_id=p.policy_id, key=key,
                             remaining=0, retry_after_s=max(0, retry),
                             action=p.action)

    def _fixed_window(self, p: LimitPolicy, key: str,
                       cost: int) -> LimitDecision:
        states = self._fw_state.setdefault(p.policy_id, {})
        now = time.time()
        if key not in states:
            states[key] = (0, now)
        count, win_start = states[key]
        if now - win_start >= p.window_s:
            count, win_start = 0, now
        if count + cost <= p.limit:
            states[key] = (count + cost, win_start)
            return LimitDecision(allowed=True, policy_id=p.policy_id, key=key,
                                 remaining=p.limit - count - cost, action=p.action)
        retry = win_start + p.window_s - now
        return LimitDecision(allowed=False, policy_id=p.policy_id, key=key,
                             remaining=0, retry_after_s=max(0, retry),
                             action=p.action)

    def _leaky_bucket(self, p: LimitPolicy, key: str,
                       cost: int) -> LimitDecision:
        states = self._lb_state.setdefault(p.policy_id, {})
        if key not in states:
            states[key] = _LeakyBucketState()
        state = states[key]
        now = time.time()
        # Drain
        elapsed = now - state.last_leak
        drained = int(elapsed * p.leak_rate)
        state.queue_size = max(0, state.queue_size - drained)
        state.last_leak  = now
        if state.queue_size + cost <= p.limit:
            state.queue_size += cost
            return LimitDecision(allowed=True, policy_id=p.policy_id, key=key,
                                 remaining=p.limit - state.queue_size, action=p.action)
        retry = cost / p.leak_rate if p.leak_rate > 0 else 0
        return LimitDecision(allowed=False, policy_id=p.policy_id, key=key,
                             remaining=0, retry_after_s=retry, action=p.action)

    def _concurrency(self, p: LimitPolicy, key: str) -> LimitDecision:
        states = self._cc_state.setdefault(p.policy_id, {})
        if key not in states:
            states[key] = _ConcurrencyState()
        state = states[key]
        if state.active < p.limit:
            state.active += 1
            return LimitDecision(allowed=True, policy_id=p.policy_id, key=key,
                                 remaining=p.limit - state.active, action=p.action)
        return LimitDecision(allowed=False, policy_id=p.policy_id, key=key,
                             remaining=0, action=p.action)

    def release(self, policy_id: str, key: str):
        """Release a concurrency slot."""
        states = self._cc_state.get(policy_id, {})
        if key in states:
            states[key].active = max(0, states[key].active - 1)

    # ── RESET ─────────────────────────────────────────────────────────

    def reset_key(self, policy_id: str, key: str):
        """Clear all state for a specific key."""
        for store in (self._tb_state, self._sw_log,
                      self._fw_state, self._lb_state, self._cc_state):
            store.get(policy_id, {}).pop(key, None)

    def reset_policy(self, policy_id: str):
        for store in (self._tb_state, self._sw_log,
                      self._fw_state, self._lb_state, self._cc_state):
            store.pop(policy_id, None)

    # ── HOOKS & LOGGING ───────────────────────────────────────────────

    def on_limit(self, fn: Callable[[LimitDecision], None]):
        self._hooks.append(fn)

    def _log(self, d: LimitDecision):
        self._db.execute(
            "INSERT INTO rl_events (policy_id,key,allowed,remaining,ts) "
            "VALUES (?,?,?,?,?)",
            (d.policy_id, d.key, int(d.allowed), d.remaining, time.time()))
        self._db.commit()

    def event_log(self, policy_id: Optional[str] = None,
                  limit: int = 50) -> List[Dict[str, Any]]:
        q = "SELECT policy_id,key,allowed,remaining,ts FROM rl_events"
        params: List[Any] = []
        if policy_id:
            q += " WHERE policy_id=?"; params.append(policy_id)
        q += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        rows = self._db.execute(q, params).fetchall()
        return [{"policy_id": r[0], "key": r[1], "allowed": bool(r[2]),
                 "remaining": r[3], "ts": r[4]} for r in rows]

    # ── STATS ─────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        total = self._allow_count + self._deny_count
        return {
            "policies": len(self._policies),
            "allowed": self._allow_count,
            "denied": self._deny_count,
            "deny_rate": round(self._deny_count / total, 4) if total > 0 else 0.0,
        }
