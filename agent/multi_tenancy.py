"""OMNI Agent — Multi-Tenancy: tenant isolation, quotas, billing."""
from __future__ import annotations
import sqlite3, time, uuid, threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Plan(str, Enum):
    FREE       = "free"
    STARTER    = "starter"
    PRO        = "pro"
    ENTERPRISE = "enterprise"


PLAN_LIMITS: Dict[Plan, Dict[str, int]] = {
    Plan.FREE:       {"requests_per_day": 100,   "tokens_per_day": 50_000,  "concurrent": 2},
    Plan.STARTER:    {"requests_per_day": 1_000,  "tokens_per_day": 500_000, "concurrent": 5},
    Plan.PRO:        {"requests_per_day": 10_000, "tokens_per_day": 5_000_000, "concurrent": 20},
    Plan.ENTERPRISE: {"requests_per_day": -1,     "tokens_per_day": -1,      "concurrent": -1},
}


@dataclass
class Tenant:
    tenant_id: str
    name: str
    plan: Plan
    created_at: float = field(default_factory=time.time)
    active: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def limits(self) -> Dict[str, int]:
        return PLAN_LIMITS[self.plan]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "name": self.name,
            "plan": self.plan.value,
            "created_at": self.created_at,
            "active": self.active,
            "metadata": self.metadata,
            "limits": self.limits,
        }


@dataclass
class UsageRecord:
    tenant_id: str
    requests: int = 0
    tokens: int = 0
    errors: int = 0
    cost_usd: float = 0.0
    window_start: float = field(default_factory=time.time)

    def reset(self):
        self.requests = 0
        self.tokens = 0
        self.errors = 0
        self.cost_usd = 0.0
        self.window_start = time.time()


class QuotaExceeded(Exception):
    pass


class TenantNotFound(Exception):
    pass


class TenantManager:
    """Manages tenants, quotas, isolation, and billing."""

    def __init__(self, db_path: str = ":memory:", window_seconds: int = 86400):
        self._lock = threading.Lock()
        self._tenants: Dict[str, Tenant] = {}
        self._usage: Dict[str, UsageRecord] = {}
        self._concurrent: Dict[str, int] = {}
        self._window_s = window_seconds
        self._db_path = db_path
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS tenants (
                tenant_id TEXT PRIMARY KEY,
                name TEXT, plan TEXT, created_at REAL, active INTEGER, metadata TEXT
            )""")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS billing_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT, ts REAL, requests INTEGER,
                tokens INTEGER, cost_usd REAL, period TEXT
            )""")
        self._db.commit()

    # ── CRUD ──────────────────────────────────────────────────────────

    def create_tenant(self, name: str, plan: Plan = Plan.FREE,
                      metadata: Optional[Dict] = None) -> Tenant:
        t = Tenant(
            tenant_id=str(uuid.uuid4()),
            name=name,
            plan=plan,
            metadata=metadata or {},
        )
        with self._lock:
            self._tenants[t.tenant_id] = t
            self._usage[t.tenant_id] = UsageRecord(t.tenant_id)
            self._concurrent[t.tenant_id] = 0
            import json
            self._db.execute(
                "INSERT INTO tenants VALUES (?,?,?,?,?,?)",
                (t.tenant_id, t.name, t.plan.value,
                 t.created_at, 1, json.dumps(t.metadata)))
            self._db.commit()
        return t

    def get_tenant(self, tenant_id: str) -> Tenant:
        t = self._tenants.get(tenant_id)
        if t is None:
            raise TenantNotFound(tenant_id)
        return t

    def list_tenants(self, active_only: bool = False) -> List[Tenant]:
        ts = list(self._tenants.values())
        return [t for t in ts if t.active] if active_only else ts

    def update_plan(self, tenant_id: str, plan: Plan) -> Tenant:
        t = self.get_tenant(tenant_id)
        t.plan = plan
        self._db.execute("UPDATE tenants SET plan=? WHERE tenant_id=?",
                         (plan.value, tenant_id))
        self._db.commit()
        return t

    def deactivate_tenant(self, tenant_id: str):
        t = self.get_tenant(tenant_id)
        t.active = False
        self._db.execute("UPDATE tenants SET active=0 WHERE tenant_id=?", (tenant_id,))
        self._db.commit()

    def delete_tenant(self, tenant_id: str):
        self.get_tenant(tenant_id)
        with self._lock:
            del self._tenants[tenant_id]
            del self._usage[tenant_id]
            del self._concurrent[tenant_id]
        self._db.execute("DELETE FROM tenants WHERE tenant_id=?", (tenant_id,))
        self._db.commit()

    # ── QUOTA ─────────────────────────────────────────────────────────

    def _reset_if_expired(self, usage: UsageRecord):
        if time.time() - usage.window_start >= self._window_s:
            usage.reset()

    def check_quota(self, tenant_id: str, tokens: int = 0) -> bool:
        t = self.get_tenant(tenant_id)
        if not t.active:
            raise QuotaExceeded(f"Tenant {tenant_id} is inactive")
        usage = self._usage[tenant_id]
        self._reset_if_expired(usage)
        limits = t.limits
        if limits["requests_per_day"] != -1 and usage.requests >= limits["requests_per_day"]:
            raise QuotaExceeded(f"requests_per_day limit reached for {tenant_id}")
        if limits["tokens_per_day"] != -1 and usage.tokens + tokens > limits["tokens_per_day"]:
            raise QuotaExceeded(f"tokens_per_day limit reached for {tenant_id}")
        if limits["concurrent"] != -1 and self._concurrent.get(tenant_id, 0) >= limits["concurrent"]:
            raise QuotaExceeded(f"concurrent limit reached for {tenant_id}")
        return True

    def record_usage(self, tenant_id: str, tokens: int = 0,
                     cost_usd: float = 0.0, error: bool = False):
        usage = self._usage[tenant_id]
        self._reset_if_expired(usage)
        usage.requests += 1
        usage.tokens += tokens
        usage.cost_usd += cost_usd
        if error:
            usage.errors += 1

    def enter_request(self, tenant_id: str):
        self._concurrent[tenant_id] = self._concurrent.get(tenant_id, 0) + 1

    def exit_request(self, tenant_id: str):
        self._concurrent[tenant_id] = max(0, self._concurrent.get(tenant_id, 0) - 1)

    def get_usage(self, tenant_id: str) -> Dict[str, Any]:
        self.get_tenant(tenant_id)
        usage = self._usage[tenant_id]
        self._reset_if_expired(usage)
        return {
            "tenant_id": tenant_id,
            "requests": usage.requests,
            "tokens": usage.tokens,
            "errors": usage.errors,
            "cost_usd": usage.cost_usd,
            "concurrent": self._concurrent.get(tenant_id, 0),
            "window_start": usage.window_start,
        }

    # ── NAMESPACE ─────────────────────────────────────────────────────

    def namespace(self, tenant_id: str, key: str) -> str:
        """Prefix a key with tenant namespace."""
        self.get_tenant(tenant_id)
        return f"tenant:{tenant_id}:{key}"

    # ── BILLING ───────────────────────────────────────────────────────

    def flush_billing(self, tenant_id: str, period: str = "daily") -> Dict[str, Any]:
        usage = self._usage[tenant_id]
        record = {
            "tenant_id": tenant_id,
            "ts": time.time(),
            "requests": usage.requests,
            "tokens": usage.tokens,
            "cost_usd": usage.cost_usd,
            "period": period,
        }
        self._db.execute(
            "INSERT INTO billing_events (tenant_id,ts,requests,tokens,cost_usd,period) VALUES (?,?,?,?,?,?)",
            (record["tenant_id"], record["ts"], record["requests"],
             record["tokens"], record["cost_usd"], record["period"]))
        self._db.commit()
        return record

    def billing_history(self, tenant_id: str) -> List[Dict[str, Any]]:
        rows = self._db.execute(
            "SELECT ts,requests,tokens,cost_usd,period FROM billing_events WHERE tenant_id=? ORDER BY ts DESC",
            (tenant_id,)).fetchall()
        return [{"ts": r[0], "requests": r[1], "tokens": r[2],
                 "cost_usd": r[3], "period": r[4]} for r in rows]

    # ── STATS ─────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        return {
            "total_tenants": len(self._tenants),
            "active_tenants": sum(1 for t in self._tenants.values() if t.active),
            "plans": {p.value: sum(1 for t in self._tenants.values() if t.plan == p)
                      for p in Plan},
        }
