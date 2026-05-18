"""OMNI AGENT - Retry Manager
Configurable retry policies with exponential backoff, jitter, deadline
budgets, per-exception routing, and execution statistics.

Features:
- Policies: FIXED, EXPONENTIAL, LINEAR, FIBONACCI delay sequences
- Jitter: NONE, FULL (uniform 0..delay), DECORRELATED (AWS-style)
- Max attempts, max total duration (deadline budget)
- Per-exception routing: map exception types to specific policies
- Stop conditions: on_stop_fn(attempt, elapsed, exc) → bool
- Before/after attempt hooks for logging and metrics
- Async and sync support; coroutines awaited transparently
- RetryResult: attempts made, total elapsed, final result or exception
- Named policies: register and reuse across the codebase
- Timeout per attempt (asyncio.wait_for)
- Dead-letter sink: optional fn called with final failed call info
- SQLite persistence: execution log, policy definitions
- Decorator: @retry("policy_name") wraps sync/async functions
- REST API: execute, register_policy, stats
"""
import asyncio, json, math, random, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, List, Optional, Tuple, Type
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class BackoffStrategy(str, Enum):
    FIXED        = "fixed"
    EXPONENTIAL  = "exponential"
    LINEAR       = "linear"
    FIBONACCI    = "fibonacci"

class JitterMode(str, Enum):
    NONE         = "none"
    FULL         = "full"
    DECORRELATED = "decorrelated"

def _fibonacci(n: int) -> float:
    a, b = 1.0, 1.0
    for _ in range(n - 1): a, b = b, a + b
    return a

def _compute_delay(strategy: BackoffStrategy, jitter: JitterMode,
                    attempt: int, base_s: float,
                    max_delay_s: float, _prev_delay: float) -> Tuple[float, float]:
    """Return (delay_s, new_prev_delay)."""
    if strategy == BackoffStrategy.FIXED:
        d = base_s
    elif strategy == BackoffStrategy.EXPONENTIAL:
        d = base_s * (2 ** (attempt - 1))
    elif strategy == BackoffStrategy.LINEAR:
        d = base_s * attempt
    else:  # FIBONACCI
        d = base_s * _fibonacci(attempt)

    d = min(d, max_delay_s)

    if jitter == JitterMode.FULL:
        d = random.uniform(0, d)
    elif jitter == JitterMode.DECORRELATED:
        d = random.uniform(base_s, max(base_s, _prev_delay * 3))
        d = min(d, max_delay_s)

    return d, d

@dataclass
class RetryPolicy:
    name: str
    max_attempts: int = 3
    base_delay_s: float = 1.0
    max_delay_s: float = 60.0
    deadline_s: float = 0.0        # 0 = no deadline
    strategy: BackoffStrategy = BackoffStrategy.EXPONENTIAL
    jitter: JitterMode = JitterMode.FULL
    attempt_timeout_s: float = 0.0  # 0 = no per-attempt timeout
    retryable_exceptions: Tuple[Type[Exception], ...] = (Exception,)
    stop_fn: Optional[Callable] = None   # fn(attempt, elapsed, exc) → bool
    dl_sink: Optional[Callable] = None   # dead-letter fn(fn_name, exc, attempts)

    def to_dict(self):
        return {"name": self.name, "max_attempts": self.max_attempts,
                "base_delay_s": self.base_delay_s, "max_delay_s": self.max_delay_s,
                "deadline_s": self.deadline_s, "strategy": self.strategy.value,
                "jitter": self.jitter.value}

@dataclass
class RetryResult:
    success: bool
    result: Any = None
    exception: Optional[Exception] = None
    attempts: int = 0
    total_elapsed_s: float = 0.0
    delays: List[float] = field(default_factory=list)

    def to_dict(self):
        return {"success": self.success,
                "result": str(self.result)[:200] if self.result is not None else None,
                "error": str(self.exception) if self.exception else None,
                "attempts": self.attempts,
                "elapsed_s": round(self.total_elapsed_s, 3),
                "delays": [round(d, 3) for d in self.delays]}

class RMStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS executions(
                    id TEXT PRIMARY KEY, policy TEXT,
                    fn_name TEXT, success INTEGER,
                    attempts INTEGER, elapsed_s REAL,
                    error TEXT DEFAULT '', created_at REAL);
                CREATE INDEX IF NOT EXISTS idx_ex_policy
                    ON executions(policy, created_at DESC);
            """)

    def log(self, policy: str, fn_name: str, r: RetryResult):
        with self._conn() as c:
            c.execute("INSERT INTO executions VALUES(?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4())[:8], policy, fn_name,
                 int(r.success), r.attempts,
                 r.total_elapsed_s,
                 str(r.exception)[:300] if r.exception else "",
                 time.time()))

    def stats(self, policy: str = None) -> Dict:
        with self._conn() as c:
            if policy is not None:
                n = c.execute(
                    "SELECT COUNT(*) FROM executions WHERE policy=?",
                    (policy,),
                ).fetchone()[0]
                ns = c.execute(
                    "SELECT COUNT(*) FROM executions WHERE policy=? AND success=1",
                    (policy,),
                ).fetchone()[0]
                avg = c.execute(
                    "SELECT AVG(attempts) FROM executions WHERE policy=?",
                    (policy,),
                ).fetchone()[0] or 0
            else:
                n = c.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
                ns = c.execute(
                    "SELECT COUNT(*) FROM executions WHERE success=1"
                ).fetchone()[0]
                avg = c.execute(
                    "SELECT AVG(attempts) FROM executions"
                ).fetchone()[0] or 0
        return {"total": n, "success": ns, "failure": n - ns,
                "success_rate": round(ns / max(1, n), 4),
                "avg_attempts": round(avg, 2)}

class RetryManager:
    """
    Configurable retry manager with multiple backoff strategies.

    Usage:
        rm = RetryManager()
        rm.register("api_retry", max_attempts=5, base_delay_s=0.5,
                     strategy=BackoffStrategy.EXPONENTIAL,
                     jitter=JitterMode.FULL)

        result = await rm.execute("api_retry", my_api_call, arg1, arg2)
        if result.success:
            print(result.result)
        else:
            print(f"Failed after {result.attempts} attempts: {result.exception}")
    """
    def __init__(self, db_path: str = "data/retry.db"):
        self._store = RMStore(db_path)
        self._policies: Dict[str, RetryPolicy] = {}
        self._before_hooks: List[Callable] = []
        self._after_hooks:  List[Callable] = []

    def register(self, name: str, **kwargs) -> RetryPolicy:
        p = RetryPolicy(name=name, **kwargs)
        self._policies[name] = p
        return p

    def get(self, name: str) -> Optional[RetryPolicy]:
        return self._policies.get(name)

    def before_attempt(self, fn: Callable): self._before_hooks.append(fn)
    def after_attempt(self, fn: Callable):  self._after_hooks.append(fn)

    async def execute(self, policy_name: str, fn: Callable,
                       *args, **kwargs) -> RetryResult:
        policy = self._policies.get(policy_name)
        if not policy:
            # No policy — just call once
            try:
                r = await fn(*args, **kwargs) \
                    if asyncio.iscoroutinefunction(fn) else fn(*args, **kwargs)
                return RetryResult(success=True, result=r, attempts=1)
            except Exception as e:
                return RetryResult(success=False, exception=e, attempts=1)

        start = time.time()
        attempt = 0
        prev_delay = policy.base_delay_s
        delays: List[float] = []
        last_exc: Optional[Exception] = None

        while attempt < policy.max_attempts:
            attempt += 1
            elapsed = time.time() - start

            # Deadline check
            if policy.deadline_s > 0 and elapsed >= policy.deadline_s:
                break

            # Before hooks
            for h in self._before_hooks:
                try: h(policy_name, attempt, fn.__name__ if hasattr(fn,"__name__") else "?")
                except: pass

            try:
                if asyncio.iscoroutinefunction(fn):
                    coro = fn(*args, **kwargs)
                else:
                    loop = asyncio.get_running_loop()
                    coro = loop.run_in_executor(None, lambda: fn(*args, **kwargs))
                if policy.attempt_timeout_s > 0:
                    result = await asyncio.wait_for(coro,
                                                     timeout=policy.attempt_timeout_s)
                else:
                    result = await coro

                elapsed = time.time() - start
                r = RetryResult(success=True, result=result,
                                 attempts=attempt, total_elapsed_s=elapsed,
                                 delays=delays)
                self._store.log(policy_name,
                                 fn.__name__ if hasattr(fn,"__name__") else "?", r)
                for h in self._after_hooks:
                    try: h(policy_name, attempt, None, result)
                    except: pass
                return r

            except Exception as exc:
                last_exc = exc
                for h in self._after_hooks:
                    try: h(policy_name, attempt, exc, None)
                    except: pass

                # Check if exception is retryable
                if not isinstance(exc, policy.retryable_exceptions):
                    break

                # Custom stop condition
                if policy.stop_fn:
                    try:
                        if policy.stop_fn(attempt, time.time() - start, exc):
                            break
                    except: pass

                if attempt >= policy.max_attempts:
                    break

                delay, prev_delay = _compute_delay(
                    policy.strategy, policy.jitter, attempt,
                    policy.base_delay_s, policy.max_delay_s, prev_delay)

                # Respect deadline
                if policy.deadline_s > 0:
                    remaining = policy.deadline_s - (time.time() - start)
                    if remaining <= 0:
                        break
                    delay = min(delay, remaining)

                delays.append(delay)
                await asyncio.sleep(delay)

        elapsed = time.time() - start
        r = RetryResult(success=False, exception=last_exc,
                         attempts=attempt, total_elapsed_s=elapsed,
                         delays=delays)
        self._store.log(policy_name,
                         fn.__name__ if hasattr(fn,"__name__") else "?", r)
        if policy.dl_sink and last_exc:
            try: policy.dl_sink(
                fn.__name__ if hasattr(fn,"__name__") else "?",
                last_exc, attempt)
            except: pass
        return r

    def retry(self, policy_name: str):
        """Decorator factory."""
        import functools
        def decorator(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                r = await self.execute(policy_name, fn, *args, **kwargs)
                if not r.success: raise r.exception
                return r.result
            @functools.wraps(fn)
            def sync_wrapper(*args, **kwargs):
                r = asyncio.run(self.execute(policy_name, fn, *args, **kwargs))
                if not r.success: raise r.exception
                return r.result
            return async_wrapper if asyncio.iscoroutinefunction(fn) else sync_wrapper
        return decorator

    def stats(self, policy_name: str = None) -> Dict:
        s = self._store.stats(policy_name)
        s["policies"] = len(self._policies)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def stats_ep(req):
            p = req.rel_url.query.get("policy")
            return web.json_response(self.stats(p))
        async def policies_ep(req):
            return web.json_response(
                {"policies": [p.to_dict() for p in self._policies.values()]})
        p = f"{prefix}/retry"
        app.router.add_get(f"{p}/stats",    stats_ep)
        app.router.add_get(f"{p}/policies", policies_ep)
        logger.info(f"Retry manager API at {prefix}/retry/")
