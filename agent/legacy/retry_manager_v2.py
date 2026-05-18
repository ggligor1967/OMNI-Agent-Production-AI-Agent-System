"""OMNI Agent — Retry Manager V2: backoff strategies, budgets, jitter and hooks."""
from __future__ import annotations
import math, random, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, Type


class BackoffStrategy(str, Enum):
    FIXED        = "fixed"
    LINEAR       = "linear"
    EXPONENTIAL  = "exponential"
    FIBONACCI    = "fibonacci"
    DECORRELATED = "decorrelated"   # AWS decorrelated jitter
    CUSTOM       = "custom"


class RetryOutcome(str, Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    EXHAUSTED = "exhausted"
    BUDGETED = "budgeted"   # stopped due to budget


@dataclass
class RetryPolicy:
    policy_id: str
    name: str
    max_attempts: int = 3
    strategy: BackoffStrategy = BackoffStrategy.EXPONENTIAL
    base_delay_s: float = 1.0
    max_delay_s: float = 60.0
    jitter: bool = True
    jitter_range: float = 0.5      # ± fraction of computed delay
    multiplier: float = 2.0        # for exponential/linear
    retryable_exceptions: List[Type[Exception]] = field(default_factory=list)
    non_retryable_exceptions: List[Type[Exception]] = field(default_factory=list)
    retry_on: Optional[Callable[[Exception], bool]] = None
    custom_delay_fn: Optional[Callable[[int], float]] = None  # attempt → delay
    timeout_s: Optional[float] = None   # total timeout across all attempts
    enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "name": self.name,
            "max_attempts": self.max_attempts,
            "strategy": self.strategy.value,
            "base_delay_s": self.base_delay_s,
            "max_delay_s": self.max_delay_s,
            "jitter": self.jitter,
        }


@dataclass
class RetryAttempt:
    attempt: int
    exception: Optional[Exception] = None
    result: Any = None
    delay_s: float = 0.0
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "attempt": self.attempt,
            "error": str(self.exception) if self.exception else None,
            "delay_s": self.delay_s,
        }


@dataclass
class RetryRecord:
    record_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    policy_id: str = ""
    operation: str = ""
    outcome: RetryOutcome = RetryOutcome.SUCCESS
    total_attempts: int = 0
    total_duration_s: float = 0.0
    result: Any = None
    final_error: Optional[str] = None
    attempts: List[RetryAttempt] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "record_id": self.record_id,
            "policy_id": self.policy_id,
            "operation": self.operation,
            "outcome": self.outcome.value,
            "total_attempts": self.total_attempts,
            "total_duration_s": round(self.total_duration_s, 3),
            "final_error": self.final_error,
        }


class RetryBudget:
    """Token-bucket retry budget — limits retries across all operations."""
    def __init__(self, tokens: int = 100, refill_rate: float = 1.0):
        self.tokens      = float(tokens)
        self.max_tokens  = float(tokens)
        self.refill_rate = refill_rate   # tokens per second
        self._last_ts    = time.time()

    def _refill(self):
        now   = time.time()
        delta = now - self._last_ts
        self.tokens = min(self.max_tokens,
                          self.tokens + delta * self.refill_rate)
        self._last_ts = now

    def consume(self, cost: float = 1.0) -> bool:
        self._refill()
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False

    @property
    def available(self) -> float:
        self._refill()
        return self.tokens


class RetryManagerV2:
    """
    Configurable retry manager:
    - Policies: FIXED / LINEAR / EXPONENTIAL / FIBONACCI / DECORRELATED / CUSTOM
    - Per-call jitter (uniform range)
    - Retryable / non-retryable exception filtering
    - Custom predicate for retry decision
    - Total-timeout budget per call
    - Global retry budget (token bucket)
    - Pre/post attempt hooks
    - Decorator factory (@retry)
    - Full attempt history per call
    - SQLite persistence of records
    """

    def __init__(self, db_path: str = ":memory:",
                 global_budget: Optional[RetryBudget] = None):
        self._policies: Dict[str, RetryPolicy] = {}
        self._records:  List[RetryRecord] = []
        self._budget    = global_budget
        self._pre_hooks:  List[Callable] = []
        self._post_hooks: List[Callable] = []
        self._rng = random.Random()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS rm_records (
                record_id TEXT PRIMARY KEY, policy_id TEXT,
                operation TEXT, outcome TEXT, total_attempts INTEGER,
                total_duration_s REAL, final_error TEXT, ts REAL
            );
        """)
        self._db.commit()

    # ── POLICY ───────────────────────────────────────────────────────

    def add_policy(self, name: str,
                   max_attempts: int = 3,
                   strategy: BackoffStrategy = BackoffStrategy.EXPONENTIAL,
                   base_delay_s: float = 1.0,
                   max_delay_s: float = 60.0,
                   jitter: bool = True,
                   multiplier: float = 2.0,
                   retryable_exceptions: Optional[List[Type[Exception]]] = None,
                   non_retryable_exceptions: Optional[List[Type[Exception]]] = None,
                   retry_on: Optional[Callable[[Exception], bool]] = None,
                   custom_delay_fn: Optional[Callable[[int], float]] = None,
                   timeout_s: Optional[float] = None,
                   policy_id: Optional[str] = None) -> RetryPolicy:
        pid = policy_id or str(uuid.uuid4())[:8]
        p = RetryPolicy(
            policy_id=pid, name=name, max_attempts=max_attempts,
            strategy=strategy, base_delay_s=base_delay_s,
            max_delay_s=max_delay_s, jitter=jitter,
            multiplier=multiplier,
            retryable_exceptions=list(retryable_exceptions or []),
            non_retryable_exceptions=list(non_retryable_exceptions or []),
            retry_on=retry_on, custom_delay_fn=custom_delay_fn,
            timeout_s=timeout_s)
        self._policies[pid] = p
        return p

    def disable_policy(self, policy_id: str):
        p = self._policies.get(policy_id)
        if p: p.enabled = False

    def enable_policy(self, policy_id: str):
        p = self._policies.get(policy_id)
        if p: p.enabled = True

    # ── DELAY COMPUTATION ────────────────────────────────────────────

    def _compute_delay(self, policy: RetryPolicy, attempt: int,
                        prev_delay: float = 0.0) -> float:
        s = policy.strategy
        b = policy.base_delay_s
        m = policy.multiplier

        if s == BackoffStrategy.FIXED:
            delay = b
        elif s == BackoffStrategy.LINEAR:
            delay = b * attempt
        elif s == BackoffStrategy.EXPONENTIAL:
            delay = b * (m ** (attempt - 1))
        elif s == BackoffStrategy.FIBONACCI:
            a, bb = 1, 1
            for _ in range(attempt - 1):
                a, bb = bb, a + bb
            delay = b * a
        elif s == BackoffStrategy.DECORRELATED:
            delay = min(policy.max_delay_s,
                        self._rng.uniform(b, prev_delay * 3 or b * 3))
        elif s == BackoffStrategy.CUSTOM and policy.custom_delay_fn:
            delay = policy.custom_delay_fn(attempt)
        else:
            delay = b

        delay = min(delay, policy.max_delay_s)

        if policy.jitter:
            r = policy.jitter_range
            delay = delay * (1 + self._rng.uniform(-r, r))
            delay = max(0.0, delay)

        return delay

    # ── EXECUTE ──────────────────────────────────────────────────────

    def execute(self, fn: Callable[[], Any],
                policy_id: str,
                operation: str = "") -> RetryRecord:
        policy = self._policies.get(policy_id)
        if not policy:
            raise KeyError(f"Policy {policy_id} not found")

        record = RetryRecord(policy_id=policy_id, operation=operation)
        t_start = time.time()
        prev_delay = 0.0

        for attempt in range(1, policy.max_attempts + 1):
            # Budget check
            if self._budget and not self._budget.consume(1.0):
                record.outcome    = RetryOutcome.BUDGETED
                record.final_error = "Global retry budget exhausted"
                break

            # Timeout check
            if policy.timeout_s and (time.time() - t_start) >= policy.timeout_s:
                record.outcome    = RetryOutcome.EXHAUSTED
                record.final_error = "Total timeout exceeded"
                break

            for fn_pre in self._pre_hooks:
                try: fn_pre(attempt, operation)
                except Exception: pass

            att = RetryAttempt(attempt=attempt)
            try:
                att.result    = fn()
                att.exception = None
                record.result = att.result
                record.outcome = RetryOutcome.SUCCESS
                record.attempts.append(att)
                record.total_attempts = attempt
                break

            except Exception as exc:
                att.exception = exc

                # Non-retryable check
                if policy.non_retryable_exceptions:
                    if isinstance(exc, tuple(policy.non_retryable_exceptions)):
                        record.outcome    = RetryOutcome.FAILED
                        record.final_error = str(exc)
                        record.attempts.append(att)
                        record.total_attempts = attempt
                        break

                # Retryable check
                if policy.retryable_exceptions:
                    if not isinstance(exc, tuple(policy.retryable_exceptions)):
                        if not (policy.retry_on and policy.retry_on(exc)):
                            record.outcome    = RetryOutcome.FAILED
                            record.final_error = str(exc)
                            record.attempts.append(att)
                            record.total_attempts = attempt
                            break

                if policy.retry_on and not policy.retry_on(exc):
                    record.outcome    = RetryOutcome.FAILED
                    record.final_error = str(exc)
                    record.attempts.append(att)
                    record.total_attempts = attempt
                    break

                if attempt < policy.max_attempts:
                    delay = self._compute_delay(policy, attempt, prev_delay)
                    att.delay_s = delay
                    prev_delay  = delay
                    record.attempts.append(att)
                    for fn_post in self._post_hooks:
                        try: fn_post(attempt, exc, delay)
                        except Exception: pass
                    if delay > 0: time.sleep(delay)
                else:
                    record.outcome     = RetryOutcome.EXHAUSTED
                    record.final_error = str(exc)
                    record.attempts.append(att)
                    record.total_attempts = attempt

        record.total_duration_s = time.time() - t_start
        if not record.total_attempts:
            record.total_attempts = len(record.attempts)
        self._records.append(record)
        self._persist(record)
        return record

    # ── DECORATOR ────────────────────────────────────────────────────

    def retry(self, policy_id: str, operation: str = ""):
        """Decorator factory: @rm.retry(policy_id)"""
        def decorator(fn: Callable) -> Callable:
            def wrapper(*args, **kwargs):
                record = self.execute(lambda: fn(*args, **kwargs),
                                      policy_id, operation or fn.__name__)
                if record.outcome == RetryOutcome.SUCCESS:
                    return record.result
                raise RuntimeError(record.final_error or "Retry exhausted")
            return wrapper
        return decorator

    # ── HOOKS ────────────────────────────────────────────────────────

    def on_retry(self, fn: Callable): self._post_hooks.append(fn)
    def on_attempt(self, fn: Callable): self._pre_hooks.append(fn)

    # ── QUERY ────────────────────────────────────────────────────────

    def get_record(self, record_id: str) -> Optional[RetryRecord]:
        return next((r for r in self._records
                     if r.record_id == record_id), None)

    def history(self, policy_id: Optional[str] = None,
                limit: int = 50) -> List[Dict]:
        rows = self._db.execute(
            "SELECT record_id,policy_id,operation,outcome,"
            "total_attempts,total_duration_s FROM rm_records "
            "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        results = [{"id": r[0], "policy": r[1], "op": r[2],
                    "outcome": r[3], "attempts": r[4]} for r in rows]
        if policy_id:
            results = [r for r in results if r["policy"] == policy_id]
        return results

    def _persist(self, rec: RetryRecord):
        self._db.execute(
            "INSERT OR REPLACE INTO rm_records VALUES (?,?,?,?,?,?,?,?)",
            (rec.record_id, rec.policy_id, rec.operation,
             rec.outcome.value, rec.total_attempts,
             rec.total_duration_s, rec.final_error, rec.ts))
        self._db.commit()

    def stats(self) -> Dict[str, Any]:
        total    = len(self._records)
        success  = sum(1 for r in self._records if r.outcome == RetryOutcome.SUCCESS)
        exhausted = sum(1 for r in self._records if r.outcome == RetryOutcome.EXHAUSTED)
        return {
            "policies": len(self._policies),
            "total_calls": total,
            "success": success,
            "exhausted": exhausted,
            "success_rate": round(success / total, 4) if total else 0.0,
            "budget_available": self._budget.available if self._budget else None,
        }
