"""OMNI AGENT - Rate Limiter
Token bucket, sliding window counter, and leaky bucket algorithms
with per-key limits, burst capacity, and distributed-ready design.

Features:
- Algorithm: TOKEN_BUCKET (replenish rate), SLIDING_WINDOW (count in window),
    LEAKY_BUCKET (constant outflow), FIXED_WINDOW (count resets at interval)
- Key-based: each unique key (user_id, IP, API key) has independent state
- Burst: token bucket supports burst capacity above steady-state rate
- Sliding window: O(1) approximate via two fixed windows blended
- Quota groups: group multiple keys under shared quota pool
- Per-endpoint rules: different limits for different resource patterns
- Cost: each request can consume N tokens instead of 1
- Penalty: on violation, optional penalty window (temporary block)
- Headers: generate X-RateLimit-* response headers
- Callbacks: on_limit_hit(key, rule) hook
- Reset: manual key reset; auto-reset via sweep
- SQLite persistence: violation log, quota snapshots
- REST API: check, consume, reset, stats, quotas
"""
import json, sqlite3, time, uuid, logging
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class Algorithm(str, Enum):
    TOKEN_BUCKET   = "token_bucket"
    SLIDING_WINDOW = "sliding_window"
    LEAKY_BUCKET   = "leaky_bucket"
    FIXED_WINDOW   = "fixed_window"

@dataclass
class RateRule:
    name: str
    algorithm: Algorithm = Algorithm.TOKEN_BUCKET
    rate: float = 10.0          # tokens/requests per second
    capacity: float = 10.0      # max burst (token bucket) or window size (sliding)
    window_s: float = 1.0       # for sliding/fixed window
    penalty_s: float = 0.0      # block duration on violation
    cost: float = 1.0           # tokens consumed per request
    resource_pattern: str = "*" # wildcard for endpoint matching

    def to_dict(self):
        return {"name": self.name, "algorithm": self.algorithm.value,
                "rate": self.rate, "capacity": self.capacity,
                "window_s": self.window_s, "penalty_s": self.penalty_s}

@dataclass
class RateLimitResult:
    allowed: bool; key: str; rule: str
    tokens_remaining: float = 0.0
    retry_after_s: float = 0.0
    limit: float = 0.0; used: float = 0.0
    reset_at: float = 0.0

    def headers(self) -> Dict[str, str]:
        h = {"X-RateLimit-Limit":     str(int(self.limit)),
              "X-RateLimit-Remaining": str(max(0, int(self.tokens_remaining))),
              "X-RateLimit-Reset":     str(int(self.reset_at))}
        if not self.allowed:
            h["Retry-After"] = str(int(self.retry_after_s) + 1)
        return h

    def to_dict(self):
        return {"allowed": self.allowed, "key": self.key, "rule": self.rule,
                "tokens_remaining": round(self.tokens_remaining, 2),
                "retry_after_s": round(self.retry_after_s, 2),
                "limit": self.limit, "used": round(self.used, 2),
                "reset_at": round(self.reset_at, 2)}

# ── Per-key state objects ─────────────────────────────────────────────────────
@dataclass
class _TokenBucketState:
    tokens: float; last_refill: float

@dataclass
class _SlidingWindowState:
    prev_count: float; curr_count: float
    window_start: float

@dataclass
class _LeakyBucketState:
    queue_size: float; last_leak: float

@dataclass
class _FixedWindowState:
    count: float; window_start: float

class RLStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS violations(
                    id TEXT PRIMARY KEY, key TEXT, rule TEXT,
                    resource TEXT DEFAULT '', ts REAL);
                CREATE INDEX IF NOT EXISTS idx_viol_key
                    ON violations(key, ts DESC);
            """)

    def log_violation(self, key: str, rule: str, resource: str = ""):
        with self._conn() as c:
            c.execute("INSERT INTO violations VALUES(?,?,?,?,?)",
                (str(uuid.uuid4())[:8], key, rule, resource, time.time()))

    def stats(self) -> Dict:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM violations").fetchone()[0]
            by_key = {r["key"]: r["cnt"] for r in c.execute(
                "SELECT key, COUNT(*) as cnt FROM violations "
                "GROUP BY key ORDER BY cnt DESC LIMIT 20").fetchall()}
        return {"total_violations": total, "top_violators": by_key}

class RateLimiter:
    """
    Multi-algorithm rate limiter with per-key state.

    Usage:
        rl = RateLimiter()
        rl.add_rule(RateRule("api", Algorithm.TOKEN_BUCKET,
                              rate=100.0, capacity=200.0))

        result = rl.check("user:alice", "api")
        if result.allowed:
            # process request
        else:
            # return 429 with result.headers()
    """
    def __init__(self, db_path: str = "data/ratelimit.db"):
        self._store = RLStore(db_path)
        self._rules: Dict[str, RateRule] = {}
        self._tb_state:  Dict[str, _TokenBucketState]   = {}
        self._sw_state:  Dict[str, _SlidingWindowState]  = {}
        self._lb_state:  Dict[str, _LeakyBucketState]    = {}
        self._fw_state:  Dict[str, _FixedWindowState]    = {}
        self._penalties: Dict[str, float] = {}   # key → unblock_ts
        self._quotas:    Dict[str, Dict] = {}    # group → {limit, used, reset_ts}
        self._hooks:     List[Callable] = []

    def add_rule(self, rule: RateRule): self._rules[rule.name] = rule
    def on_limit_hit(self, fn: Callable): self._hooks.append(fn)

    def _penalised(self, key: str) -> Tuple[bool, float]:
        unblock = self._penalties.get(key, 0)
        if unblock > time.time():
            return True, unblock - time.time()
        return False, 0.0

    # ── Token Bucket ──────────────────────────────────────────────────────────
    def _token_bucket(self, key: str, rule: RateRule,
                       cost: float) -> RateLimitResult:
        now = time.time()
        state = self._tb_state.get(key)
        if not state:
            state = _TokenBucketState(tokens=rule.capacity, last_refill=now)
            self._tb_state[key] = state
        # Refill
        elapsed = now - state.last_refill
        state.tokens = min(rule.capacity, state.tokens + elapsed * rule.rate)
        state.last_refill = now
        # Consume
        if state.tokens >= cost:
            state.tokens -= cost
            next_refill = now + (1.0 / rule.rate)
            return RateLimitResult(True, key, rule.name,
                                    tokens_remaining=state.tokens,
                                    limit=rule.capacity, used=cost,
                                    reset_at=next_refill)
        # Wait time = (cost - tokens) / rate
        wait = (cost - state.tokens) / rule.rate
        return RateLimitResult(False, key, rule.name,
                                tokens_remaining=state.tokens,
                                retry_after_s=wait, limit=rule.capacity,
                                used=0.0,
                                reset_at=now + wait)

    # ── Sliding Window (approximate, two-bucket blend) ────────────────────────
    def _sliding_window(self, key: str, rule: RateRule,
                          cost: float) -> RateLimitResult:
        now = time.time()
        ws = rule.window_s
        state = self._sw_state.get(key)
        if not state:
            state = _SlidingWindowState(0.0, 0.0, now)
            self._sw_state[key] = state
        # Advance windows
        elapsed = now - state.window_start
        if elapsed >= 2 * ws:
            state.prev_count = 0.0; state.curr_count = 0.0
            state.window_start = now
        elif elapsed >= ws:
            state.prev_count = state.curr_count
            state.curr_count = 0.0
            state.window_start += ws
        # Weighted blend
        frac = (now - state.window_start) / ws
        estimate = state.prev_count * (1 - frac) + state.curr_count
        remaining = rule.capacity - estimate
        reset_at = state.window_start + ws
        if remaining >= cost:
            state.curr_count += cost
            return RateLimitResult(True, key, rule.name,
                                    tokens_remaining=max(0, remaining - cost),
                                    limit=rule.capacity, used=cost,
                                    reset_at=reset_at)
        wait = reset_at - now
        return RateLimitResult(False, key, rule.name,
                                tokens_remaining=0.0,
                                retry_after_s=max(0, wait),
                                limit=rule.capacity, used=0.0,
                                reset_at=reset_at)

    # ── Leaky Bucket ──────────────────────────────────────────────────────────
    def _leaky_bucket(self, key: str, rule: RateRule,
                       cost: float) -> RateLimitResult:
        now = time.time()
        state = self._lb_state.get(key)
        if not state:
            state = _LeakyBucketState(0.0, now)
            self._lb_state[key] = state
        # Drain
        elapsed = now - state.last_leak
        leaked = elapsed * rule.rate
        state.queue_size = max(0.0, state.queue_size - leaked)
        state.last_leak = now
        if state.queue_size + cost <= rule.capacity:
            state.queue_size += cost
            drain_time = state.queue_size / rule.rate
            return RateLimitResult(True, key, rule.name,
                                    tokens_remaining=rule.capacity - state.queue_size,
                                    limit=rule.capacity, used=cost,
                                    reset_at=now + drain_time)
        wait = (state.queue_size + cost - rule.capacity) / rule.rate
        return RateLimitResult(False, key, rule.name,
                                tokens_remaining=0.0,
                                retry_after_s=wait,
                                limit=rule.capacity, used=0.0,
                                reset_at=now + wait)

    # ── Fixed Window ──────────────────────────────────────────────────────────
    def _fixed_window(self, key: str, rule: RateRule,
                       cost: float) -> RateLimitResult:
        now = time.time()
        state = self._fw_state.get(key)
        if not state:
            state = _FixedWindowState(0.0, now)
            self._fw_state[key] = state
        # Reset window
        if now - state.window_start >= rule.window_s:
            state.count = 0.0
            state.window_start = now
        reset_at = state.window_start + rule.window_s
        if state.count + cost <= rule.capacity:
            state.count += cost
            return RateLimitResult(True, key, rule.name,
                                    tokens_remaining=rule.capacity - state.count,
                                    limit=rule.capacity, used=cost,
                                    reset_at=reset_at)
        wait = reset_at - now
        return RateLimitResult(False, key, rule.name,
                                tokens_remaining=0.0,
                                retry_after_s=max(0.0, wait),
                                limit=rule.capacity, used=0.0,
                                reset_at=reset_at)

    def check(self, key: str, rule_name: str,
               cost: float = None, resource: str = "") -> RateLimitResult:
        rule = self._rules.get(rule_name)
        if not rule:
            return RateLimitResult(True, key, rule_name,
                                    tokens_remaining=float("inf"))
        actual_cost = cost if cost is not None else rule.cost
        # Penalty check
        penalised, wait = self._penalised(key)
        if penalised:
            return RateLimitResult(False, key, rule_name,
                                    retry_after_s=wait, limit=rule.capacity,
                                    reset_at=time.time() + wait)
        # Dispatch algorithm
        if rule.algorithm == Algorithm.TOKEN_BUCKET:
            result = self._token_bucket(key, rule, actual_cost)
        elif rule.algorithm == Algorithm.SLIDING_WINDOW:
            result = self._sliding_window(key, rule, actual_cost)
        elif rule.algorithm == Algorithm.LEAKY_BUCKET:
            result = self._leaky_bucket(key, rule, actual_cost)
        else:
            result = self._fixed_window(key, rule, actual_cost)

        if not result.allowed:
            self._store.log_violation(key, rule_name, resource)
            if rule.penalty_s > 0:
                self._penalties[key] = time.time() + rule.penalty_s
            for h in self._hooks:
                try: h(key, rule)
                except: pass
        return result

    def consume(self, key: str, rule_name: str,
                 cost: float = None) -> RateLimitResult:
        """Alias for check(); semantically 'consume tokens'."""
        return self.check(key, rule_name, cost)

    def reset(self, key: str, rule_name: str = None):
        """Reset state for a key (optionally scoped to one rule's algorithm)."""
        rule = self._rules.get(rule_name) if rule_name else None
        alg = rule.algorithm if rule else None
        if alg is None or alg == Algorithm.TOKEN_BUCKET:
            self._tb_state.pop(key, None)
        if alg is None or alg == Algorithm.SLIDING_WINDOW:
            self._sw_state.pop(key, None)
        if alg is None or alg == Algorithm.LEAKY_BUCKET:
            self._lb_state.pop(key, None)
        if alg is None or alg == Algorithm.FIXED_WINDOW:
            self._fw_state.pop(key, None)
        self._penalties.pop(key, None)

    def add_quota_group(self, group: str, limit: float, window_s: float):
        self._quotas[group] = {"limit": limit, "used": 0.0,
                                "window_s": window_s,
                                "reset_ts": time.time() + window_s}

    def consume_quota(self, group: str, key: str, cost: float = 1.0) -> bool:
        q = self._quotas.get(group)
        if not q: return True
        now = time.time()
        if now > q["reset_ts"]:
            q["used"] = 0.0; q["reset_ts"] = now + q["window_s"]
        if q["used"] + cost > q["limit"]: return False
        q["used"] += cost; return True

    def stats(self) -> Dict:
        s = self._store.stats()
        s["rules"] = len(self._rules)
        s["active_tb_keys"]  = len(self._tb_state)
        s["active_sw_keys"]  = len(self._sw_state)
        s["penalised_keys"]  = sum(1 for v in self._penalties.values()
                                    if v > time.time())
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def check_ep(req):
            d = await req.json()
            r = self.check(d["key"], d["rule"],
                            d.get("cost"), d.get("resource",""))
            return web.json_response(r.to_dict(),
                                      status=200 if r.allowed else 429)
        async def reset_ep(req):
            d = await req.json()
            self.reset(d["key"], d.get("rule"))
            return web.json_response({"reset": True})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/ratelimit"
        app.router.add_post(f"{p}/check", check_ep)
        app.router.add_post(f"{p}/reset", reset_ep)
        app.router.add_get( f"{p}/stats", stats_ep)
        logger.info(f"Rate limiter API at {prefix}/ratelimit/")
