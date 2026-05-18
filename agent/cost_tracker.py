"""
OMNI AGENT - Cost Tracker
Real-time LLM cost accounting: track every token, enforce budgets,
fire alerts when thresholds are breached, and generate spend reports.

Features:
- Per-model pricing: input/output token costs for 20+ models
- Custom pricing: override or add any model's rates
- Usage recording: log every API call with tokens, cost, metadata
- Budget enforcement: per-user, per-session, per-day, per-month limits
- Spend alerts: callback hooks when budgets are approached/exceeded
- Aggregation: group spend by user, model, session, time period
- Rolling window: track spend over last N hours/days
- Reports: daily/monthly breakdowns, top spenders, model distribution
- SQLite persistence: full usage history queryable at any time
- REST API: record, budgets, reports, alerts
"""
import time
import uuid
import json
import sqlite3
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
from collections import defaultdict

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# MODEL PRICING REGISTRY
# ══════════════════════════════════════════════════════════════════════════════

# USD per 1M tokens (input, output)
_MODEL_PRICING: Dict[str, Tuple[float, float]] = {
    # Anthropic Claude
    "claude-3-5-sonnet":          (3.00,  15.00),
    "claude-3-5-haiku":           (0.80,   4.00),
    "claude-3-opus":             (15.00,  75.00),
    "claude-3-haiku":             (0.25,   1.25),
    "claude-sonnet-4-6":          (3.00,  15.00),
    "claude-opus-4-6":           (15.00,  75.00),
    # OpenAI
    "gpt-4o":                     (2.50,  10.00),
    "gpt-4o-mini":                (0.15,   0.60),
    "gpt-4-turbo":               (10.00,  30.00),
    "gpt-4":                     (30.00,  60.00),
    "gpt-3.5-turbo":              (0.50,   1.50),
    "o1":                        (15.00,  60.00),
    "o1-mini":                    (3.00,  12.00),
    # Google
    "gemini-1.5-pro":             (1.25,   5.00),
    "gemini-1.5-flash":           (0.075,  0.30),
    "gemini-2.0-flash":           (0.10,   0.40),
    # Mistral
    "mistral-large":              (3.00,   9.00),
    "mistral-small":              (0.20,   0.60),
    # DeepSeek
    "deepseek-v3":                (0.27,   1.10),
    "deepseek-chat":              (0.27,   1.10),
    # Groq (free tier estimates)
    "llama-3.1-70b":              (0.59,   0.79),
    "llama-3.1-8b":               (0.05,   0.08),
    # Default fallback
    "default":                    (1.00,   3.00),
}


def get_model_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Calculate USD cost for a model call."""
    pricing = None
    for key, rates in _MODEL_PRICING.items():
        if key in model:
            pricing = rates
            break
    if pricing is None:
        pricing = _MODEL_PRICING["default"]
    input_cost  = (input_tokens  / 1_000_000) * pricing[0]
    output_cost = (output_tokens / 1_000_000) * pricing[1]
    return input_cost + output_cost


def add_model_pricing(model: str, input_per_1m: float, output_per_1m: float):
    """Register custom pricing for a model."""
    _MODEL_PRICING[model] = (input_per_1m, output_per_1m)


# ══════════════════════════════════════════════════════════════════════════════
# USAGE RECORD
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class UsageRecord:
    id: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    user_id: str = ""
    session_id: str = ""
    request_id: str = ""
    operation: str = ""   # e.g. "chat", "summarize", "embed"
    latency_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)
    metadata: Dict = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 8),
            "user_id": self.user_id, "session_id": self.session_id,
            "operation": self.operation, "latency_ms": self.latency_ms,
            "timestamp": self.timestamp,
        }


# ══════════════════════════════════════════════════════════════════════════════
# BUDGET
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Budget:
    id: str
    scope: str          # "user", "session", "global", "model"
    scope_id: str       # user_id, session_id, model name, or "global"
    limit_usd: float
    period: str         # "day", "month", "session", "all_time"
    alert_pct: float = 80.0   # alert when X% of budget consumed
    hard_limit: bool = False  # if True, block calls that exceed budget
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "scope": self.scope, "scope_id": self.scope_id,
            "limit_usd": self.limit_usd, "period": self.period,
            "alert_pct": self.alert_pct, "hard_limit": self.hard_limit,
        }


@dataclass
class BudgetStatus:
    budget: Budget
    spent_usd: float
    remaining_usd: float
    utilization_pct: float
    exceeded: bool
    alert_triggered: bool

    def to_dict(self) -> Dict:
        return {
            **self.budget.to_dict(),
            "spent_usd": round(self.spent_usd, 6),
            "remaining_usd": round(self.remaining_usd, 6),
            "utilization_pct": round(self.utilization_pct, 2),
            "exceeded": self.exceeded,
            "alert_triggered": self.alert_triggered,
        }


# ══════════════════════════════════════════════════════════════════════════════
# STORE
# ══════════════════════════════════════════════════════════════════════════════

class CostStore:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS usage (
                    id           TEXT PRIMARY KEY,
                    model        TEXT NOT NULL,
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    cost_usd     REAL DEFAULT 0,
                    user_id      TEXT DEFAULT '',
                    session_id   TEXT DEFAULT '',
                    request_id   TEXT DEFAULT '',
                    operation    TEXT DEFAULT '',
                    latency_ms   REAL DEFAULT 0,
                    timestamp    REAL,
                    metadata     TEXT DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS budgets (
                    id          TEXT PRIMARY KEY,
                    scope       TEXT NOT NULL,
                    scope_id    TEXT NOT NULL,
                    limit_usd   REAL NOT NULL,
                    period      TEXT NOT NULL,
                    alert_pct   REAL DEFAULT 80,
                    hard_limit  INTEGER DEFAULT 0,
                    created_at  REAL
                );
                CREATE INDEX IF NOT EXISTS idx_usage_user ON usage(user_id, timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_usage_session ON usage(session_id);
                CREATE INDEX IF NOT EXISTS idx_usage_model ON usage(model, timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_budget_scope ON budgets(scope, scope_id);
            """)

    def record(self, rec: UsageRecord):
        with self._conn() as c:
            c.execute("""
                INSERT INTO usage
                (id,model,input_tokens,output_tokens,cost_usd,user_id,session_id,
                 request_id,operation,latency_ms,timestamp,metadata)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                rec.id, rec.model, rec.input_tokens, rec.output_tokens,
                rec.cost_usd, rec.user_id, rec.session_id, rec.request_id,
                rec.operation, rec.latency_ms, rec.timestamp,
                json.dumps(rec.metadata),
            ))

    def query(self, user_id: str = None, session_id: str = None,
              model: str = None, after: float = None,
              before: float = None, limit: int = 100) -> List[UsageRecord]:
        conds, params = [], []
        if user_id:
            conds.append("user_id=?"); params.append(user_id)
        if session_id:
            conds.append("session_id=?"); params.append(session_id)
        if model:
            conds.append("model LIKE ?"); params.append(f"%{model}%")
        if after:
            conds.append("timestamp>=?"); params.append(after)
        if before:
            conds.append("timestamp<=?"); params.append(before)
        q = "SELECT * FROM usage"
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        with self._conn() as c:
            rows = c.execute(q, params).fetchall()
        return [self._row(r) for r in rows]

    def aggregate(self, group_by: str, after: float = None,
                  before: float = None) -> List[Dict]:
        """Aggregate spend grouped by user/model/session/operation."""
        valid = {"user_id", "model", "session_id", "operation"}
        if group_by not in valid:
            group_by = "model"
        conds, params = [], []
        if after:
            conds.append("timestamp>=?"); params.append(after)
        if before:
            conds.append("timestamp<=?"); params.append(before)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        q = f"""
            SELECT {group_by} as group_key,
                   COUNT(*) as calls,
                   SUM(input_tokens) as input_tokens,
                   SUM(output_tokens) as output_tokens,
                   SUM(cost_usd) as cost_usd,
                   AVG(latency_ms) as avg_latency_ms
            FROM usage{where}
            GROUP BY {group_by}
            ORDER BY cost_usd DESC
        """
        with self._conn() as c:
            rows = c.execute(q, params).fetchall()
        return [
            {
                "key": r["group_key"], "calls": r["calls"],
                "input_tokens": r["input_tokens"],
                "output_tokens": r["output_tokens"],
                "cost_usd": round(r["cost_usd"], 6),
                "avg_latency_ms": round(r["avg_latency_ms"] or 0, 1),
            }
            for r in rows
        ]

    def total_spend(self, user_id: str = None, session_id: str = None,
                    after: float = None) -> float:
        conds, params = [], []
        if user_id:
            conds.append("user_id=?"); params.append(user_id)
        if session_id:
            conds.append("session_id=?"); params.append(session_id)
        if after:
            conds.append("timestamp>=?"); params.append(after)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        with self._conn() as c:
            row = c.execute(f"SELECT SUM(cost_usd) FROM usage{where}",
                            params).fetchone()
        return row[0] or 0.0

    def save_budget(self, budget: Budget):
        with self._conn() as c:
            c.execute("""
                INSERT OR REPLACE INTO budgets
                (id,scope,scope_id,limit_usd,period,alert_pct,hard_limit,created_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (budget.id, budget.scope, budget.scope_id, budget.limit_usd,
                  budget.period, budget.alert_pct, int(budget.hard_limit),
                  budget.created_at))

    def get_budgets(self, scope: str = None,
                    scope_id: str = None) -> List[Budget]:
        conds, params = [], []
        if scope:
            conds.append("scope=?"); params.append(scope)
        if scope_id:
            conds.append("scope_id=?"); params.append(scope_id)
        q = "SELECT * FROM budgets"
        if conds:
            q += " WHERE " + " AND ".join(conds)
        with self._conn() as c:
            rows = c.execute(q, params).fetchall()
        return [Budget(id=r["id"], scope=r["scope"], scope_id=r["scope_id"],
                       limit_usd=r["limit_usd"], period=r["period"],
                       alert_pct=r["alert_pct"],
                       hard_limit=bool(r["hard_limit"]),
                       created_at=r["created_at"]) for r in rows]

    def delete_budget(self, budget_id: str) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM budgets WHERE id=?", (budget_id,))
        return cur.rowcount > 0

    def _row(self, row) -> UsageRecord:
        return UsageRecord(
            id=row["id"], model=row["model"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            cost_usd=row["cost_usd"],
            user_id=row["user_id"] or "",
            session_id=row["session_id"] or "",
            request_id=row["request_id"] or "",
            operation=row["operation"] or "",
            latency_ms=row["latency_ms"] or 0.0,
            timestamp=row["timestamp"],
            metadata=json.loads(row["metadata"] or "{}"),
        )


# ══════════════════════════════════════════════════════════════════════════════
# COST TRACKER
# ══════════════════════════════════════════════════════════════════════════════

class CostTracker:
    """
    LLM cost accounting with budgets and spend alerts.

    Usage:
        tracker = CostTracker()

        # Record a call
        record = tracker.record_call(
            model="claude-3-5-sonnet",
            input_tokens=1500, output_tokens=300,
            user_id="user_123", session_id="sess_456",
            operation="chat",
        )
        print(f"Cost: ${record.cost_usd:.6f}")

        # Set budgets
        tracker.set_budget("user", "user_123", limit_usd=5.00, period="month",
                           alert_pct=80)

        # Check before calling
        ok, status = tracker.check_budget("user", "user_123")
        if not ok:
            raise Exception("Budget exceeded!")

        # Reports
        report = tracker.daily_report()
        print(report)
    """

    def __init__(self, db_path: str = "data/cost_tracker.db",
                 alert_callback: Callable = None):
        self._store = CostStore(db_path)
        self._alert_cb = alert_callback
        self._period_start: Dict[str, float] = {}   # cache period start times

    # ── Recording ─────────────────────────────────────────────────────────────

    def record_call(self, model: str, input_tokens: int, output_tokens: int,
                    user_id: str = "", session_id: str = "",
                    request_id: str = "", operation: str = "",
                    latency_ms: float = 0.0,
                    custom_cost_usd: float = None,
                    metadata: Dict = None) -> UsageRecord:
        cost = (custom_cost_usd if custom_cost_usd is not None
                else get_model_cost(model, input_tokens, output_tokens))
        rec = UsageRecord(
            id=str(uuid.uuid4())[:14],
            model=model, input_tokens=input_tokens, output_tokens=output_tokens,
            cost_usd=cost, user_id=user_id, session_id=session_id,
            request_id=request_id, operation=operation, latency_ms=latency_ms,
            metadata=metadata or {},
        )
        self._store.record(rec)
        self._check_alerts(user_id=user_id, session_id=session_id)
        return rec

    # ── Budget management ─────────────────────────────────────────────────────

    def set_budget(self, scope: str, scope_id: str,
                   limit_usd: float, period: str = "month",
                   alert_pct: float = 80.0,
                   hard_limit: bool = False) -> Budget:
        budget = Budget(
            id=str(uuid.uuid4())[:12],
            scope=scope, scope_id=scope_id, limit_usd=limit_usd,
            period=period, alert_pct=alert_pct, hard_limit=hard_limit,
        )
        self._store.save_budget(budget)
        logger.info(f"Budget set: {scope}={scope_id} "
                   f"${limit_usd:.2f}/{period}")
        return budget

    def get_budgets(self, scope: str = None,
                    scope_id: str = None) -> List[Budget]:
        return self._store.get_budgets(scope, scope_id)

    def delete_budget(self, budget_id: str) -> bool:
        return self._store.delete_budget(budget_id)

    def _period_after(self, period: str) -> Optional[float]:
        """Return Unix timestamp of start of current period."""
        import datetime
        now = datetime.datetime.utcnow()
        if period == "day":
            start = datetime.datetime(now.year, now.month, now.day)
            return start.timestamp()
        elif period == "month":
            start = datetime.datetime(now.year, now.month, 1)
            return start.timestamp()
        elif period == "session":
            return None   # handled differently
        return None   # "all_time"

    def check_budget(self, scope: str, scope_id: str) -> Tuple[bool, List[BudgetStatus]]:
        """
        Check all budgets for a scope.
        Returns (all_ok, list_of_statuses).
        """
        budgets = self._store.get_budgets(scope, scope_id)
        statuses = []
        all_ok = True

        for b in budgets:
            after = self._period_after(b.period)
            if scope == "user":
                spent = self._store.total_spend(user_id=scope_id, after=after)
            elif scope == "session":
                spent = self._store.total_spend(session_id=scope_id, after=after)
            else:
                spent = self._store.total_spend(after=after)

            remaining = max(0.0, b.limit_usd - spent)
            util_pct = (spent / b.limit_usd * 100) if b.limit_usd else 0.0
            exceeded = spent >= b.limit_usd
            alert = util_pct >= b.alert_pct

            status = BudgetStatus(
                budget=b, spent_usd=spent, remaining_usd=remaining,
                utilization_pct=util_pct, exceeded=exceeded,
                alert_triggered=alert,
            )
            statuses.append(status)
            if exceeded and b.hard_limit:
                all_ok = False

        return all_ok, statuses

    def _check_alerts(self, user_id: str = "", session_id: str = ""):
        """Fire alert callbacks if any budget threshold is breached."""
        if not self._alert_cb:
            return
        for scope, sid in [("user", user_id), ("session", session_id)]:
            if not sid:
                continue
            _, statuses = self.check_budget(scope, sid)
            for status in statuses:
                if status.alert_triggered:
                    try:
                        self._alert_cb(status)
                    except Exception as e:
                        logger.warning(f"Alert callback error: {e}")

    # ── Reporting ─────────────────────────────────────────────────────────────

    def total_spend(self, user_id: str = None, session_id: str = None,
                    after: float = None) -> float:
        return self._store.total_spend(user_id=user_id,
                                       session_id=session_id, after=after)

    def spend_by_model(self, after: float = None) -> List[Dict]:
        return self._store.aggregate("model", after=after)

    def spend_by_user(self, after: float = None, limit: int = 20) -> List[Dict]:
        return self._store.aggregate("user_id", after=after)[:limit]

    def spend_by_operation(self, after: float = None) -> List[Dict]:
        return self._store.aggregate("operation", after=after)

    def daily_report(self, days: int = 7) -> Dict:
        """Spend breakdown for the last N days."""
        import datetime
        cutoff = (datetime.datetime.utcnow() -
                  datetime.timedelta(days=days)).timestamp()
        return {
            "period_days": days,
            "total_spend_usd": round(self.total_spend(after=cutoff), 6),
            "by_model": self.spend_by_model(after=cutoff),
            "by_user": self.spend_by_user(after=cutoff),
            "by_operation": self.spend_by_operation(after=cutoff),
        }

    def usage_history(self, **kwargs) -> List[UsageRecord]:
        return self._store.query(**kwargs)

    def stats(self) -> Dict:
        total = self._store.total_spend()
        by_model = self._store.aggregate("model")
        return {
            "total_spend_usd": round(total, 6),
            "by_model": by_model[:5],
            "pricing_models": len(_MODEL_PRICING),
        }

    def register_routes(self, app, prefix: str = ""):
        from aiohttp import web

        async def record_ep(request):
            data = await request.json()
            rec = self.record_call(
                model=data["model"],
                input_tokens=int(data.get("input_tokens", 0)),
                output_tokens=int(data.get("output_tokens", 0)),
                user_id=data.get("user_id", ""),
                session_id=data.get("session_id", ""),
                operation=data.get("operation", ""),
                latency_ms=float(data.get("latency_ms", 0)),
                metadata=data.get("metadata", {}),
            )
            return web.json_response(rec.to_dict(), status=201)

        async def report_ep(request):
            days = int(request.rel_url.query.get("days", 7))
            return web.json_response(self.daily_report(days))

        async def budget_ep(request):
            data = await request.json()
            budget = self.set_budget(
                scope=data["scope"], scope_id=data["scope_id"],
                limit_usd=float(data["limit_usd"]),
                period=data.get("period", "month"),
                alert_pct=float(data.get("alert_pct", 80)),
                hard_limit=bool(data.get("hard_limit", False)),
            )
            return web.json_response(budget.to_dict(), status=201)

        async def check_budget_ep(request):
            scope = request.rel_url.query.get("scope", "user")
            scope_id = request.rel_url.query.get("scope_id", "")
            ok, statuses = self.check_budget(scope, scope_id)
            return web.json_response({
                "ok": ok,
                "statuses": [s.to_dict() for s in statuses],
            })

        async def stats_ep(request):
            return web.json_response(self.stats())

        async def pricing_ep(request):
            return web.json_response({
                "pricing": {k: {"input_per_1m": v[0], "output_per_1m": v[1]}
                            for k, v in _MODEL_PRICING.items()}
            })

        p = f"{prefix}/cost"
        app.router.add_post(p + "/record",    record_ep)
        app.router.add_get( p + "/report",    report_ep)
        app.router.add_post(p + "/budget",    budget_ep)
        app.router.add_get( p + "/budget",    check_budget_ep)
        app.router.add_get( p + "/stats",     stats_ep)
        app.router.add_get( p + "/pricing",   pricing_ep)
        logger.info(f"Cost tracker API registered at {prefix}/cost/")
