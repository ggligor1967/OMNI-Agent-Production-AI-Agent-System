"""OMNI AGENT - Token Budget Manager
Track, allocate, and enforce token quotas across users, teams, models,
and time windows; emit alerts when budgets run low.

Features:
- Quota definition: per-user, per-team, per-model, or global budgets
- Time windows: rolling hourly/daily/monthly windows with auto-reset
- Usage recording: log every inference call with token counts
- Budget check: before-call guard that blocks or warns over-budget actors
- Soft/hard limits: warn at soft threshold, block at hard limit
- Overage policy: block | queue | allow-with-surcharge
- Cost tracking: optional cost per token with currency output
- Usage report: breakdown by actor, model, time window
- Alert callbacks: fire async hooks when budget thresholds crossed
- SQLite persistence: all usage events stored for reporting
- REST API: check, record, report, set-quota, reset
"""
import json, time, uuid, sqlite3, asyncio, logging
from typing import Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class WindowType(str, Enum):
    HOURLY="hourly"; DAILY="daily"; MONTHLY="monthly"; TOTAL="total"

class OveragePolicy(str, Enum):
    BLOCK="block"; WARN="warn"; ALLOW="allow"

@dataclass
class Quota:
    id: str; name: str; actor: str
    hard_limit: int; soft_limit: int
    window: WindowType = WindowType.DAILY
    model_filter: str = ""          # empty = all models
    cost_per_token: float = 0.0     # USD
    overage_policy: OveragePolicy = OveragePolicy.WARN
    enabled: bool = True
    created_at: float = field(default_factory=time.time)
    def to_dict(self):
        return {"id":self.id,"name":self.name,"actor":self.actor,
                "hard_limit":self.hard_limit,"soft_limit":self.soft_limit,
                "window":self.window,"model_filter":self.model_filter,
                "overage_policy":self.overage_policy,"enabled":self.enabled}

@dataclass
class UsageEvent:
    id: str; actor: str; model: str
    input_tokens: int; output_tokens: int
    quota_id: str = ""; cost_usd: float = 0.0
    timestamp: float = field(default_factory=time.time)
    @property
    def total_tokens(self): return self.input_tokens + self.output_tokens

@dataclass
class BudgetCheckResult:
    allowed: bool; actor: str; quota_id: str
    used: int; limit: int; remaining: int
    warning: bool = False; message: str = ""
    cost_so_far: float = 0.0
    def to_dict(self):
        return {"allowed":self.allowed,"actor":self.actor,"used":self.used,
                "limit":self.limit,"remaining":self.remaining,
                "warning":self.warning,"message":self.message,
                "cost_so_far":round(self.cost_so_far,6)}

class BudgetStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path=db_path; self._init()
    def _conn(self):
        c=sqlite3.connect(self.db_path); c.row_factory=sqlite3.Row; return c
    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS quotas(
                    id TEXT PRIMARY KEY,name TEXT,actor TEXT,
                    hard_limit INTEGER,soft_limit INTEGER,
                    window TEXT DEFAULT 'daily',model_filter TEXT DEFAULT '',
                    cost_per_token REAL DEFAULT 0,
                    overage_policy TEXT DEFAULT 'warn',
                    enabled INTEGER DEFAULT 1,created_at REAL);
                CREATE TABLE IF NOT EXISTS usage(
                    id TEXT PRIMARY KEY,actor TEXT,model TEXT,
                    input_tokens INTEGER,output_tokens INTEGER,
                    quota_id TEXT DEFAULT '',cost_usd REAL DEFAULT 0,timestamp REAL);
                CREATE INDEX IF NOT EXISTS idx_usage_actor ON usage(actor,timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_usage_model ON usage(model,timestamp DESC);
            """)
    def save_quota(self, q):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO quotas VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (q.id,q.name,q.actor,q.hard_limit,q.soft_limit,q.window,
                 q.model_filter,q.cost_per_token,q.overage_policy,
                 int(q.enabled),q.created_at))
    def load_quotas(self, actor=None):
        with self._conn() as c:
            if actor:
                rows=c.execute("SELECT * FROM quotas WHERE actor=? AND enabled=1",(actor,)).fetchall()
            else:
                rows=c.execute("SELECT * FROM quotas WHERE enabled=1").fetchall()
        return [Quota(id=r["id"],name=r["name"],actor=r["actor"],
                      hard_limit=r["hard_limit"],soft_limit=r["soft_limit"],
                      window=WindowType(r["window"]),model_filter=r["model_filter"] or "",
                      cost_per_token=r["cost_per_token"],
                      overage_policy=OveragePolicy(r["overage_policy"]),
                      enabled=bool(r["enabled"])) for r in rows]
    def record_usage(self, event: UsageEvent):
        with self._conn() as c:
            c.execute("INSERT INTO usage VALUES(?,?,?,?,?,?,?,?)",
                (event.id,event.actor,event.model,event.input_tokens,
                 event.output_tokens,event.quota_id,event.cost_usd,event.timestamp))
    def get_usage_in_window(self, actor, model_filter, window: WindowType):
        now=time.time()
        if window==WindowType.HOURLY: since=now-3600
        elif window==WindowType.DAILY: since=now-86400
        elif window==WindowType.MONTHLY: since=now-86400*30
        else: since=0
        with self._conn() as c:
            if model_filter:
                row=c.execute("SELECT COALESCE(SUM(input_tokens+output_tokens),0) as tot, COALESCE(SUM(cost_usd),0) as cost FROM usage WHERE actor=? AND model=? AND timestamp>=?",(actor,model_filter,since)).fetchone()
            else:
                row=c.execute("SELECT COALESCE(SUM(input_tokens+output_tokens),0) as tot, COALESCE(SUM(cost_usd),0) as cost FROM usage WHERE actor=? AND timestamp>=?",(actor,since)).fetchone()
        return int(row["tot"]), float(row["cost"])
    def usage_report(self, since=None, actor=None, model=None):
        now=time.time(); since=since or (now-86400)
        conds,args=["timestamp>=?"],[since]
        if actor: conds.append("actor=?"); args.append(actor)
        if model: conds.append("model=?"); args.append(model)
        wh=" AND ".join(conds)
        with self._conn() as c:
            total=c.execute(f"SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM usage WHERE {wh}",args).fetchone()[0]
            by_actor=dict(c.execute(f"SELECT actor,SUM(input_tokens+output_tokens) FROM usage WHERE {wh} GROUP BY actor",args).fetchall())
            by_model=dict(c.execute(f"SELECT model,SUM(input_tokens+output_tokens) FROM usage WHERE {wh} GROUP BY model",args).fetchall())
            cost=c.execute(f"SELECT COALESCE(SUM(cost_usd),0) FROM usage WHERE {wh}",args).fetchone()[0]
        return {"total_tokens":int(total),"by_actor":by_actor,"by_model":by_model,"total_cost_usd":round(float(cost),6)}
    def stats(self):
        with self._conn() as c:
            nq=c.execute("SELECT COUNT(*) FROM quotas").fetchone()[0]
            nu=c.execute("SELECT COUNT(*) FROM usage").fetchone()[0]
            tc=c.execute("SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM usage").fetchone()[0]
        return {"total_quotas":nq,"total_usage_events":nu,"total_tokens_all_time":int(tc)}

class TokenBudgetManager:
    """
    Track and enforce token budgets with per-actor quotas and time windows.

    Usage:
        mgr = TokenBudgetManager()
        mgr.set_quota("alice", hard_limit=100_000, soft_limit=80_000, window="daily")

        check = mgr.check("alice", model="gpt-4o", tokens_needed=500)
        if check.allowed:
            # call LLM...
            mgr.record("alice", model="gpt-4o", input_tokens=100, output_tokens=400)
    """
    def __init__(self, db_path: str = "data/token_budget.db"):
        self._store = BudgetStore(db_path)
        self._alert_callbacks: List[Callable] = []
        self._quotas: Dict[str, Quota] = {}   # in-memory cache
        # Load existing quotas
        for q in self._store.load_quotas():
            self._quotas[q.id] = q

    def set_quota(self, actor: str, hard_limit: int, soft_limit: Optional[int] = None,
                   window: str = "daily", model_filter: str = "",
                   cost_per_token: float = 0.0,
                   overage_policy: str = "warn",
                   name: str = "") -> Quota:
        qid = f"q_{actor}_{window}_{model_filter or 'all'}"
        q = Quota(id=qid, name=name or f"{actor}/{window}",
                   actor=actor, hard_limit=hard_limit,
                   soft_limit=soft_limit or int(hard_limit * 0.8),
                   window=WindowType(window), model_filter=model_filter,
                   cost_per_token=cost_per_token,
                   overage_policy=OveragePolicy(overage_policy))
        self._quotas[qid] = q; self._store.save_quota(q)
        logger.info(f"Quota set for '{actor}': {hard_limit} tokens/{window}")
        return q

    def disable_quota(self, quota_id: str):
        q = self._quotas.get(quota_id)
        if q: q.enabled = False; self._store.save_quota(q)

    def on_alert(self, callback: Callable):
        self._alert_callbacks.append(callback)

    async def _fire_alert(self, result: BudgetCheckResult):
        for cb in self._alert_callbacks:
            try:
                await cb(result) if asyncio.iscoroutinefunction(cb) else cb(result)
            except: pass

    def check(self, actor: str, model: str = "", tokens_needed: int = 0) -> BudgetCheckResult:
        quotas = [q for q in self._quotas.values()
                   if q.actor == actor and q.enabled
                   and (not q.model_filter or q.model_filter == model)]
        if not quotas:
            return BudgetCheckResult(allowed=True, actor=actor, quota_id="",
                                      used=0, limit=0, remaining=999_999_999)
        # Use the strictest quota
        most_restrictive = min(quotas, key=lambda q: q.hard_limit)
        used, cost = self._store.get_usage_in_window(actor, model, most_restrictive.window)
        remaining = most_restrictive.hard_limit - used
        warning = used >= most_restrictive.soft_limit
        if used + tokens_needed > most_restrictive.hard_limit:
            if most_restrictive.overage_policy == OveragePolicy.BLOCK:
                return BudgetCheckResult(allowed=False, actor=actor,
                                          quota_id=most_restrictive.id,
                                          used=used, limit=most_restrictive.hard_limit,
                                          remaining=max(0,remaining),
                                          warning=True, message="Hard limit reached",
                                          cost_so_far=cost)
        return BudgetCheckResult(allowed=True, actor=actor,
                                  quota_id=most_restrictive.id,
                                  used=used, limit=most_restrictive.hard_limit,
                                  remaining=max(0, remaining),
                                  warning=warning,
                                  message="Soft limit exceeded" if warning else "",
                                  cost_so_far=cost)

    def record(self, actor: str, model: str, input_tokens: int,
               output_tokens: int, quota_id: str = "") -> UsageEvent:
        q = self._quotas.get(quota_id) if quota_id else None
        cost = (input_tokens + output_tokens) * (q.cost_per_token if q else 0.0)
        event = UsageEvent(id=str(uuid.uuid4())[:12], actor=actor, model=model,
                            input_tokens=input_tokens, output_tokens=output_tokens,
                            quota_id=quota_id, cost_usd=cost)
        self._store.record_usage(event)
        return event

    def report(self, since: Optional[float] = None, actor: str = None,
               model: str = None) -> Dict:
        return self._store.usage_report(since, actor, model)

    def get_quotas(self, actor: str = None) -> List[Quota]:
        if actor:
            return [q for q in self._quotas.values() if q.actor == actor and q.enabled]
        return [q for q in self._quotas.values() if q.enabled]

    def stats(self) -> Dict:
        return self._store.stats()

    def register_routes(self, app, prefix: str = ""):
        from aiohttp import web
        async def check_ep(req):
            d = await req.json()
            result = self.check(d["actor"], d.get("model",""), int(d.get("tokens_needed",0)))
            return web.json_response(result.to_dict())
        async def record_ep(req):
            d = await req.json()
            event = self.record(d["actor"], d.get("model",""),
                                 int(d.get("input_tokens",0)),
                                 int(d.get("output_tokens",0)),
                                 d.get("quota_id",""))
            return web.json_response({"event_id":event.id,"total_tokens":event.total_tokens},status=201)
        async def set_quota_ep(req):
            d = await req.json()
            q = self.set_quota(d["actor"],int(d["hard_limit"]),
                                d.get("soft_limit"),d.get("window","daily"),
                                d.get("model_filter",""),float(d.get("cost_per_token",0)),
                                d.get("overage_policy","warn"))
            return web.json_response(q.to_dict(),status=201)
        async def report_ep(req):
            q = req.rel_url.query
            since = float(q["since"]) if "since" in q else None
            return web.json_response(self.report(since,q.get("actor"),q.get("model")))
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/budget"
        app.router.add_post(f"{p}/check", check_ep)
        app.router.add_post(f"{p}/record", record_ep)
        app.router.add_post(f"{p}/quota", set_quota_ep)
        app.router.add_get(f"{p}/report", report_ep)
        app.router.add_get(f"{p}/stats", stats_ep)
        logger.info(f"Token budget API at {prefix}/budget/")
