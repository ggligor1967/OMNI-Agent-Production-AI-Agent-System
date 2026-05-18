"""OMNI Agent — Token Budget V2: multi-model token budgets, priority queues, cost tracking."""
from __future__ import annotations
import sqlite3, threading, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class BudgetPeriod(str, Enum):
    MINUTE  = "minute"
    HOUR    = "hour"
    DAY     = "day"
    MONTH   = "month"
    TOTAL   = "total"       # lifetime


class Priority(str, Enum):
    CRITICAL = "critical"   # never throttled
    HIGH     = "high"
    NORMAL   = "normal"
    LOW      = "low"
    BATCH    = "batch"      # first to be throttled


PERIOD_SECONDS = {
    BudgetPeriod.MINUTE: 60,
    BudgetPeriod.HOUR:   3600,
    BudgetPeriod.DAY:    86400,
    BudgetPeriod.MONTH:  2592000,
    BudgetPeriod.TOTAL:  float("inf"),
}

PRIORITY_WEIGHT = {
    Priority.CRITICAL: 1.0,
    Priority.HIGH:     0.8,
    Priority.NORMAL:   0.5,
    Priority.LOW:      0.3,
    Priority.BATCH:    0.1,
}


@dataclass
class Budget:
    budget_id: str
    name: str
    model_id: str
    max_tokens: int
    period: BudgetPeriod
    used_tokens: int = 0
    cost_per_1k: float = 0.0          # USD
    total_cost: float = 0.0
    window_start: float = field(default_factory=time.time)
    enabled: bool = True
    alert_threshold: float = 0.8       # fire alert at 80% usage
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def remaining(self) -> int:
        return max(0, self.max_tokens - self.used_tokens)

    @property
    def utilization(self) -> float:
        return self.used_tokens / self.max_tokens if self.max_tokens > 0 else 0.0

    @property
    def is_exhausted(self) -> bool:
        return self.used_tokens >= self.max_tokens

    def _window_age(self) -> float:
        return time.time() - self.window_start

    def should_reset(self) -> bool:
        period_s = PERIOD_SECONDS.get(self.period, float("inf"))
        return self._window_age() >= period_s and self.period != BudgetPeriod.TOTAL

    def reset_window(self):
        self.used_tokens = 0
        self.window_start = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "budget_id": self.budget_id,
            "name": self.name,
            "model_id": self.model_id,
            "period": self.period.value,
            "max_tokens": self.max_tokens,
            "used_tokens": self.used_tokens,
            "remaining": self.remaining,
            "utilization": round(self.utilization, 4),
            "total_cost_usd": round(self.total_cost, 6),
            "is_exhausted": self.is_exhausted,
            "enabled": self.enabled,
        }


@dataclass
class TokenRequest:
    request_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    model_id: str = ""
    tokens: int = 0
    priority: Priority = Priority.NORMAL
    user_id: str = "system"
    session_id: str = ""
    ts: float = field(default_factory=time.time)
    granted: bool = False
    denied_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "model_id": self.model_id,
            "tokens": self.tokens,
            "priority": self.priority.value,
            "granted": self.granted,
            "denied_reason": self.denied_reason,
        }


class BudgetExceeded(Exception):
    pass


class TokenBudgetV2:
    """
    Multi-model token budget manager with:
    - Per-model, per-period budgets
    - Priority-aware allocation
    - Automatic window reset
    - Cost tracking
    - Alert callbacks
    - Request queue with priority ordering
    - SQLite audit log
    """

    def __init__(self, db_path: str = ":memory:"):
        self._budgets: Dict[str, Budget] = {}          # budget_id → Budget
        self._by_model: Dict[str, List[str]] = {}       # model_id → [budget_id]
        self._lock = threading.RLock()
        self._alert_hooks: List[Callable] = []
        self._queue: List[TokenRequest] = []
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._total_granted = 0
        self._total_denied  = 0

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS tb_budgets (
                budget_id TEXT PRIMARY KEY, name TEXT, model_id TEXT,
                period TEXT, max_tokens INTEGER, cost_per_1k REAL, enabled INTEGER
            );
            CREATE TABLE IF NOT EXISTS tb_requests (
                request_id TEXT PRIMARY KEY, model_id TEXT, tokens INTEGER,
                priority TEXT, user_id TEXT, granted INTEGER,
                denied_reason TEXT, ts REAL
            );
        """)
        self._db.commit()

    # ── BUDGET MANAGEMENT ─────────────────────────────────────────────

    def create_budget(self, name: str, model_id: str,
                      max_tokens: int,
                      period: BudgetPeriod = BudgetPeriod.DAY,
                      cost_per_1k: float = 0.0,
                      alert_threshold: float = 0.8,
                      metadata: Optional[Dict] = None,
                      budget_id: Optional[str] = None) -> Budget:
        bid = budget_id or str(uuid.uuid4())[:8]
        b   = Budget(budget_id=bid, name=name, model_id=model_id,
                     max_tokens=max_tokens, period=period,
                     cost_per_1k=cost_per_1k,
                     alert_threshold=alert_threshold,
                     metadata=metadata or {})
        with self._lock:
            self._budgets[bid] = b
            self._by_model.setdefault(model_id, []).append(bid)
        self._db.execute(
            "INSERT OR REPLACE INTO tb_budgets VALUES (?,?,?,?,?,?,?)",
            (bid, name, model_id, period.value, max_tokens, cost_per_1k, 1))
        self._db.commit()
        return b

    def remove_budget(self, budget_id: str):
        with self._lock:
            b = self._budgets.pop(budget_id, None)
            if b:
                self._by_model.get(b.model_id, []).remove(budget_id)

    def enable_budget(self, budget_id: str):
        with self._lock:
            if budget_id in self._budgets:
                self._budgets[budget_id].enabled = True

    def disable_budget(self, budget_id: str):
        with self._lock:
            if budget_id in self._budgets:
                self._budgets[budget_id].enabled = False

    # ── ALLOCATION ────────────────────────────────────────────────────

    def _get_budgets_for_model(self, model_id: str) -> List[Budget]:
        ids = self._by_model.get(model_id, [])
        return [self._budgets[bid] for bid in ids
                if bid in self._budgets and self._budgets[bid].enabled]

    def request(self, model_id: str, tokens: int,
                priority: Priority = Priority.NORMAL,
                user_id: str = "system",
                session_id: str = "",
                raise_on_deny: bool = False) -> TokenRequest:
        req = TokenRequest(model_id=model_id, tokens=tokens,
                           priority=priority, user_id=user_id,
                           session_id=session_id)
        with self._lock:
            budgets = self._get_budgets_for_model(model_id)
            if not budgets:
                # No budget defined → always grant
                req.granted = True
                self._total_granted += tokens
                self._log_request(req)
                return req

            # Auto-reset expired windows
            for b in budgets:
                if b.should_reset():
                    b.reset_window()

            # Check all applicable budgets
            blocking = [b for b in budgets if b.remaining < tokens]
            if blocking and priority != Priority.CRITICAL:
                req.granted = False
                req.denied_reason = (
                    f"Budget exhausted: {blocking[0].name} "
                    f"({blocking[0].used_tokens}/{blocking[0].max_tokens})")
                self._total_denied += 1
                self._log_request(req)
                if raise_on_deny:
                    raise BudgetExceeded(req.denied_reason)
                return req

            # Grant and deduct
            for b in budgets:
                b.used_tokens += tokens
                if b.cost_per_1k > 0:
                    b.total_cost += (tokens / 1000) * b.cost_per_1k
                # Fire alert if threshold crossed
                if b.utilization >= b.alert_threshold:
                    for fn in self._alert_hooks:
                        try: fn(b)
                        except Exception: pass

            req.granted = True
            self._total_granted += tokens
            self._log_request(req)
        return req

    def release(self, model_id: str, tokens: int):
        """Return tokens to budget (e.g., if fewer tokens were actually used)."""
        with self._lock:
            for b in self._get_budgets_for_model(model_id):
                b.used_tokens = max(0, b.used_tokens - tokens)

    def _log_request(self, req: TokenRequest):
        self._db.execute(
            "INSERT INTO tb_requests VALUES (?,?,?,?,?,?,?,?)",
            (req.request_id, req.model_id, req.tokens, req.priority.value,
             req.user_id, int(req.granted), req.denied_reason, req.ts))
        self._db.commit()

    # ── QUEUE ─────────────────────────────────────────────────────────

    def enqueue(self, model_id: str, tokens: int,
                priority: Priority = Priority.NORMAL) -> TokenRequest:
        """Queue a request for later batch processing."""
        req = TokenRequest(model_id=model_id, tokens=tokens, priority=priority)
        self._queue.append(req)
        self._queue.sort(key=lambda r: PRIORITY_WEIGHT[r.priority], reverse=True)
        return req

    def flush_queue(self) -> List[TokenRequest]:
        """Process all queued requests in priority order."""
        results = []
        while self._queue:
            req = self._queue.pop(0)
            processed = self.request(req.model_id, req.tokens, req.priority)
            results.append(processed)
        return results

    def queue_depth(self) -> int:
        return len(self._queue)

    # ── QUERY ─────────────────────────────────────────────────────────

    def get_budget(self, budget_id: str) -> Optional[Budget]:
        return self._budgets.get(budget_id)

    def model_budgets(self, model_id: str) -> List[Dict[str, Any]]:
        return [b.to_dict() for b in self._get_budgets_for_model(model_id)]

    def all_budgets(self) -> List[Dict[str, Any]]:
        return [b.to_dict() for b in self._budgets.values()]

    def request_history(self, model_id: Optional[str] = None,
                        limit: int = 50) -> List[Dict[str, Any]]:
        q = "SELECT * FROM tb_requests"
        params: List[Any] = []
        if model_id:
            q += " WHERE model_id=?"; params.append(model_id)
        q += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        rows = self._db.execute(q, params).fetchall()
        return [{"request_id": r[0], "model_id": r[1], "tokens": r[2],
                 "priority": r[3], "user_id": r[4], "granted": bool(r[5]),
                 "denied_reason": r[6]} for r in rows]

    # ── ALERTS ────────────────────────────────────────────────────────

    def on_alert(self, fn: Callable[[Budget], None]):
        self._alert_hooks.append(fn)

    # ── COST ──────────────────────────────────────────────────────────

    def total_cost(self, model_id: Optional[str] = None) -> float:
        if model_id:
            return sum(b.total_cost for b in self._get_budgets_for_model(model_id))
        return sum(b.total_cost for b in self._budgets.values())

    def cost_breakdown(self) -> Dict[str, float]:
        result: Dict[str, float] = {}
        for b in self._budgets.values():
            result[b.model_id] = result.get(b.model_id, 0.0) + b.total_cost
        return result

    # ── STATS ─────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        exhausted = sum(1 for b in self._budgets.values() if b.is_exhausted)
        return {
            "budgets": len(self._budgets),
            "exhausted": exhausted,
            "total_granted": self._total_granted,
            "total_denied": self._total_denied,
            "total_cost_usd": round(self.total_cost(), 6),
            "queue_depth": self.queue_depth(),
        }
