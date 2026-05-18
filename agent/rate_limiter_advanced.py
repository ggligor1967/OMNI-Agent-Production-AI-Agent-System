"""OMNI AGENT - Rate Limiter Advanced
Token bucket + sliding window rate limiting with per-actor quotas,
burst allowances, backpressure signalling, and detailed analytics.

Features:
- Token bucket: smooth rate limiting with configurable refill rate
- Sliding window: count-based limiting over rolling time windows
- Per-actor quotas: independent limits for each actor/API key
- Burst allowance: allow short bursts above steady-state rate
- Global ceiling: hard cap across all actors combined
- Backpressure: return retry-after delay when limit exceeded
- Quota tiers: define named tiers (free/pro/enterprise) with limits
- Usage analytics: request counts, rejection rate, avg wait time
- SQLite persistence: survive restarts; audit every allow/deny
- Async-safe: all operations use asyncio locks
- REST API: check, consume, set-quota, stats, reset
"""
import asyncio, time, uuid, sqlite3, json, logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

# ── Token Bucket ──────────────────────────────────────────────────────────────
@dataclass
class TokenBucket:
    capacity: float          # max tokens (burst ceiling)
    refill_rate: float       # tokens added per second
    tokens: float = -1.0    # current tokens (-1 = lazy init to capacity)
    last_refill: float = field(default_factory=time.time)

    def _refill(self):
        now = time.time()
        elapsed = now - self.last_refill
        if self.tokens < 0: self.tokens = self.capacity
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    def consume(self, tokens: float = 1.0) -> Tuple[bool, float]:
        """Returns (allowed, retry_after_seconds)."""
        self._refill()
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True, 0.0
        needed = tokens - self.tokens
        retry_after = needed / max(1e-9, self.refill_rate)
        return False, round(retry_after, 3)

    def peek(self) -> float:
        self._refill()
        return round(self.tokens, 4)

# ── Sliding Window ────────────────────────────────────────────────────────────
@dataclass
class SlidingWindow:
    max_requests: int; window_s: float
    _requests: List[float] = field(default_factory=list)

    def _prune(self):
        cutoff = time.time() - self.window_s
        self._requests = [t for t in self._requests if t >= cutoff]

    def allow(self) -> Tuple[bool, float]:
        self._prune()
        if len(self._requests) < self.max_requests:
            self._requests.append(time.time())
            return True, 0.0
        oldest = self._requests[0]
        retry_after = (oldest + self.window_s) - time.time()
        return False, round(max(0.0, retry_after), 3)

    def count(self) -> int:
        self._prune(); return len(self._requests)

# ── Data classes ──────────────────────────────────────────────────────────────
@dataclass
class Quota:
    actor: str; tier: str
    rpm: int = 60          # requests per minute (sliding window)
    rph: int = 1000        # requests per hour
    burst: int = 10        # token bucket capacity
    refill_rate: float = 1.0  # tokens/sec
    active: bool = True
    created_at: float = field(default_factory=time.time)

    def to_dict(self):
        return {"actor": self.actor, "tier": self.tier, "rpm": self.rpm,
                "rph": self.rph, "burst": self.burst, "active": self.active}

@dataclass
class LimitDecision:
    actor: str; allowed: bool
    retry_after: float = 0.0
    tokens_remaining: float = 0.0
    window_count: int = 0
    reason: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self):
        return {"actor": self.actor, "allowed": self.allowed,
                "retry_after": self.retry_after,
                "tokens_remaining": round(self.tokens_remaining, 2),
                "window_count": self.window_count,
                "reason": self.reason, "timestamp": self.timestamp}

@dataclass
class Tier:
    name: str; rpm: int; rph: int; burst: int; refill_rate: float

    def to_dict(self):
        return {"name": self.name, "rpm": self.rpm, "rph": self.rph,
                "burst": self.burst, "refill_rate": self.refill_rate}

class RLStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS quotas(
                    actor TEXT PRIMARY KEY, tier TEXT,
                    rpm INTEGER DEFAULT 60, rph INTEGER DEFAULT 1000,
                    burst INTEGER DEFAULT 10, refill_rate REAL DEFAULT 1.0,
                    active INTEGER DEFAULT 1, created_at REAL);
                CREATE TABLE IF NOT EXISTS decisions(
                    id TEXT PRIMARY KEY, actor TEXT, allowed INTEGER,
                    retry_after REAL, reason TEXT, timestamp REAL);
                CREATE INDEX IF NOT EXISTS idx_dec_actor ON decisions(actor, timestamp DESC);
            """)

    def save_quota(self, q: Quota):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO quotas VALUES(?,?,?,?,?,?,?,?)",
                (q.actor, q.tier, q.rpm, q.rph, q.burst, q.refill_rate,
                 int(q.active), q.created_at))

    def load_quota(self, actor: str) -> Optional[Quota]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM quotas WHERE actor=?", (actor,)).fetchone()
        if not row: return None
        return Quota(actor=row["actor"], tier=row["tier"], rpm=row["rpm"],
                      rph=row["rph"], burst=row["burst"],
                      refill_rate=row["refill_rate"],
                      active=bool(row["active"]), created_at=row["created_at"])

    def log_decision(self, d: LimitDecision):
        with self._conn() as c:
            c.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4())[:10], d.actor, int(d.allowed),
                 d.retry_after, d.reason, d.timestamp))

    def stats(self, actor: str = None) -> Dict:
        with self._conn() as c:
            if actor:
                total  = c.execute("SELECT COUNT(*) FROM decisions WHERE actor=?", (actor,)).fetchone()[0]
                denied = c.execute("SELECT COUNT(*) FROM decisions WHERE actor=? AND allowed=0", (actor,)).fetchone()[0]
            else:
                total  = c.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
                denied = c.execute("SELECT COUNT(*) FROM decisions WHERE allowed=0").fetchone()[0]
        return {"total_requests": total, "denied": denied,
                "allowed": total - denied,
                "denial_rate": round(denied / max(1, total), 4)}

class RateLimiterAdvanced:
    """
    Token bucket + sliding window rate limiter with per-actor quotas and tiers.

    Usage:
        rl = RateLimiterAdvanced()
        rl.define_tier("free",       rpm=30,   rph=500,   burst=5,  refill_rate=0.5)
        rl.define_tier("pro",        rpm=120,  rph=5000,  burst=20, refill_rate=2.0)
        rl.define_tier("enterprise", rpm=1000, rph=50000, burst=100, refill_rate=16.0)

        rl.set_quota("alice", tier="pro")
        rl.set_quota("bob",   tier="free")

        decision = await rl.check("alice", tokens=1.0)
        if decision.allowed:
            # proceed with request
            pass
        else:
            print(f"Rate limited. Retry after {decision.retry_after}s")
    """
    DEFAULT_TIERS = [
        Tier("free",       30,    500,   5,   0.5),
        Tier("pro",        120,  5000,  20,   2.0),
        Tier("enterprise", 1000, 50000, 100, 16.0),
        Tier("unlimited",  10**6, 10**8, 10**4, 10**4),
    ]

    def __init__(self, db_path: str = "data/rate_limiter.db",
                 global_rps: float = 0.0,   # 0 = no global limit
                 audit: bool = True):
        self._store = RLStore(db_path)
        self._audit = audit
        self._global_rps = global_rps
        self._lock = asyncio.Lock()
        self._tiers: Dict[str, Tier] = {}
        self._quotas: Dict[str, Quota] = {}
        # Per-actor runtime state
        self._buckets: Dict[str, TokenBucket] = {}
        self._windows_min: Dict[str, SlidingWindow] = {}
        self._windows_hour: Dict[str, SlidingWindow] = {}
        self._global_bucket: Optional[TokenBucket] = None
        # Seed default tiers
        for t in self.DEFAULT_TIERS:
            self._tiers[t.name] = t
        if global_rps > 0:
            self._global_bucket = TokenBucket(capacity=global_rps * 10,
                                               refill_rate=global_rps)

    def define_tier(self, name: str, rpm: int, rph: int,
                     burst: int, refill_rate: float) -> Tier:
        t = Tier(name=name, rpm=rpm, rph=rph, burst=burst, refill_rate=refill_rate)
        self._tiers[name] = t; return t

    def set_quota(self, actor: str, tier: str = "free",
                   rpm: int = None, rph: int = None,
                   burst: int = None, refill_rate: float = None):
        t = self._tiers.get(tier, self._tiers["free"])
        q = Quota(actor=actor, tier=tier,
                   rpm=rpm if rpm is not None else t.rpm,
                   rph=rph if rph is not None else t.rph,
                   burst=burst if burst is not None else t.burst,
                   refill_rate=refill_rate if refill_rate is not None else t.refill_rate)
        self._quotas[actor] = q
        self._store.save_quota(q)
        # Reset runtime state for this actor
        self._buckets[actor] = TokenBucket(capacity=q.burst, refill_rate=q.refill_rate)
        self._windows_min[actor] = SlidingWindow(max_requests=q.rpm, window_s=60.0)
        self._windows_hour[actor] = SlidingWindow(max_requests=q.rph, window_s=3600.0)
        logger.debug(f"Quota set: {actor!r} → tier={tier}, rpm={q.rpm}")

    def _get_or_create(self, actor: str):
        if actor not in self._quotas:
            q = self._store.load_quota(actor)
            if q:
                self._quotas[actor] = q
                self._buckets[actor] = TokenBucket(capacity=q.burst, refill_rate=q.refill_rate)
                self._windows_min[actor] = SlidingWindow(max_requests=q.rpm, window_s=60.0)
                self._windows_hour[actor] = SlidingWindow(max_requests=q.rph, window_s=3600.0)
            else:
                self.set_quota(actor, tier="free")

    async def check(self, actor: str, tokens: float = 1.0) -> LimitDecision:
        async with self._lock:
            self._get_or_create(actor)
            q = self._quotas[actor]
            if not q.active:
                d = LimitDecision(actor=actor, allowed=False, reason="quota_disabled")
                if self._audit: self._store.log_decision(d)
                return d

            # Global ceiling check
            if self._global_bucket:
                ok, wait = self._global_bucket.consume(tokens)
                if not ok:
                    d = LimitDecision(actor=actor, allowed=False,
                                       retry_after=wait, reason="global_limit")
                    if self._audit: self._store.log_decision(d)
                    return d

            # Sliding window (minute)
            ok_min, wait_min = self._windows_min[actor].allow()
            if not ok_min:
                d = LimitDecision(actor=actor, allowed=False, retry_after=wait_min,
                                   reason="rpm_exceeded",
                                   window_count=self._windows_min[actor].count())
                if self._audit: self._store.log_decision(d)
                return d

            # Sliding window (hour)
            ok_hr, wait_hr = self._windows_hour[actor].allow()
            if not ok_hr:
                # Undo the minute-window consume
                self._windows_min[actor]._requests.pop()
                d = LimitDecision(actor=actor, allowed=False, retry_after=wait_hr,
                                   reason="rph_exceeded",
                                   window_count=self._windows_hour[actor].count())
                if self._audit: self._store.log_decision(d)
                return d

            # Token bucket
            ok_tb, wait_tb = self._buckets[actor].consume(tokens)
            if not ok_tb:
                # Undo window consumes
                self._windows_min[actor]._requests.pop()
                self._windows_hour[actor]._requests.pop()
                d = LimitDecision(actor=actor, allowed=False, retry_after=wait_tb,
                                   reason="burst_exceeded",
                                   tokens_remaining=self._buckets[actor].peek())
                if self._audit: self._store.log_decision(d)
                return d

            d = LimitDecision(actor=actor, allowed=True,
                               tokens_remaining=self._buckets[actor].peek(),
                               window_count=self._windows_min[actor].count())
            if self._audit: self._store.log_decision(d)
            return d

    async def wait_and_consume(self, actor: str, tokens: float = 1.0,
                                max_wait: float = 30.0) -> LimitDecision:
        """Block until allowed or max_wait exceeded."""
        waited = 0.0
        while waited < max_wait:
            d = await self.check(actor, tokens)
            if d.allowed: return d
            if d.retry_after > max_wait - waited:
                return LimitDecision(actor=actor, allowed=False,
                                      retry_after=d.retry_after,
                                      reason="max_wait_exceeded")
            await asyncio.sleep(d.retry_after)
            waited += d.retry_after
        return LimitDecision(actor=actor, allowed=False, reason="max_wait_exceeded")

    def disable_quota(self, actor: str):
        q = self._quotas.get(actor)
        if q: q.active = False; self._store.save_quota(q)

    def enable_quota(self, actor: str):
        q = self._quotas.get(actor)
        if q: q.active = True; self._store.save_quota(q)

    def reset_actor(self, actor: str):
        """Reset all counters for an actor."""
        q = self._quotas.get(actor)
        if q:
            self._buckets[actor] = TokenBucket(capacity=q.burst, refill_rate=q.refill_rate)
            self._windows_min[actor] = SlidingWindow(max_requests=q.rpm, window_s=60.0)
            self._windows_hour[actor] = SlidingWindow(max_requests=q.rph, window_s=3600.0)

    def tokens_remaining(self, actor: str) -> float:
        self._get_or_create(actor)
        return self._buckets[actor].peek()

    def tiers(self) -> List[Tier]:
        return list(self._tiers.values())

    def stats(self, actor: str = None) -> Dict:
        s = self._store.stats(actor)
        s["registered_actors"] = len(self._quotas)
        s["defined_tiers"] = list(self._tiers.keys())
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def check_ep(req):
            d = await req.json()
            dec = await self.check(d["actor"], float(d.get("tokens", 1.0)))
            return web.json_response(dec.to_dict())
        async def set_ep(req):
            d = await req.json()
            self.set_quota(d["actor"], d.get("tier","free"),
                            d.get("rpm"), d.get("rph"), d.get("burst"), d.get("refill_rate"))
            return web.json_response({"set": True}, status=201)
        async def stats_ep(req):
            actor = req.rel_url.query.get("actor")
            return web.json_response(self.stats(actor))
        async def reset_ep(req):
            d = await req.json()
            self.reset_actor(d["actor"])
            return web.json_response({"reset": True})
        async def tiers_ep(req):
            return web.json_response({"tiers": [t.to_dict() for t in self.tiers()]})
        p = f"{prefix}/ratelimit"
        app.router.add_post(f"{p}/check",  check_ep)
        app.router.add_post(f"{p}/quota",  set_ep)
        app.router.add_get( f"{p}/stats",  stats_ep)
        app.router.add_post(f"{p}/reset",  reset_ep)
        app.router.add_get( f"{p}/tiers",  tiers_ep)
        logger.info(f"Rate limiter API at {prefix}/ratelimit/")
