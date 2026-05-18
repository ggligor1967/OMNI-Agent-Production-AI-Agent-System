"""OMNI AGENT - Feature Flags
Feature flag system with percentage rollout, user targeting,
A/B experiments, gradual rollout, and override management.

Features:
- Flag types: BOOLEAN (on/off), PERCENTAGE (0-100% rollout),
    VARIANT (A/B/multivariate), SCHEDULE (time-based on/off)
- Targeting: user-level overrides, user attribute rules (segment)
- Segment rules: field op value (eq, neq, in, not_in, gt, lt, contains)
- Percentage rollout: deterministic hash(flag_name + user_id) % 100
- Variant assignment: weighted bucket allocation from hash
- Gradual rollout: increment percentage over time
- Kill switch: instant off for all users
- Dependency: flag enabled only if another flag is enabled
- Experiment: track variant exposure for analytics
- Override: per-user or per-session explicit flag value
- Default value: fallback when flag is disabled or not found
- Environment: per-env flag definitions (dev/staging/prod)
- Change log: record every flag modification with author
- SQLite persistence: flags, overrides, exposures, audit log
- REST API: evaluate, set, override, rollout, stats
"""
import hashlib, json, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, List, Optional, Set, Union
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class FlagType(str, Enum):
    BOOLEAN    = "boolean"
    PERCENTAGE = "percentage"
    VARIANT    = "variant"
    SCHEDULE   = "schedule"

class RuleOp(str, Enum):
    EQ       = "eq";       NEQ      = "neq"
    IN       = "in";       NOT_IN   = "not_in"
    GT       = "gt";       LT       = "lt"
    GTE      = "gte";      LTE      = "lte"
    CONTAINS = "contains"

def _hash_bucket(flag: str, user_id: str) -> int:
    """Deterministic 0-99 bucket for a user+flag combination."""
    h = hashlib.md5(f"{flag}:{user_id}".encode()).hexdigest()
    return int(h[:8], 16) % 100

def _eval_rule(op: RuleOp, field_val: Any, target: Any) -> bool:
    if op == RuleOp.EQ:       return field_val == target
    if op == RuleOp.NEQ:      return field_val != target
    if op == RuleOp.IN:       return field_val in (target if isinstance(target, list) else [target])
    if op == RuleOp.NOT_IN:   return field_val not in (target if isinstance(target, list) else [target])
    if op == RuleOp.GT:       return float(field_val or 0) > float(target)
    if op == RuleOp.LT:       return float(field_val or 0) < float(target)
    if op == RuleOp.GTE:      return float(field_val or 0) >= float(target)
    if op == RuleOp.LTE:      return float(field_val or 0) <= float(target)
    if op == RuleOp.CONTAINS: return str(target) in str(field_val or "")
    return False

@dataclass
class SegmentRule:
    field: str; op: RuleOp; value: Any

    def matches(self, attrs: Dict) -> bool:
        return _eval_rule(self.op, attrs.get(self.field), self.value)

@dataclass
class Variant:
    name: str; weight: int = 50; payload: Any = None

@dataclass
class Flag:
    name: str; flag_type: FlagType = FlagType.BOOLEAN
    enabled: bool = True; kill_switch: bool = False
    default_value: Any = False
    percentage: float = 100.0      # for PERCENTAGE flags
    variants: List[Variant] = field(default_factory=list)
    rules: List[SegmentRule] = field(default_factory=list)
    depends_on: Optional[str] = None
    schedule_on: float = 0.0       # unix ts when flag turns on
    schedule_off: float = 0.0      # unix ts when flag turns off
    environment: str = "default"
    description: str = ""
    tags: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def is_scheduled_active(self) -> bool:
        now = time.time()
        if self.schedule_on > 0 and now < self.schedule_on: return False
        if self.schedule_off > 0 and now > self.schedule_off: return False
        return True

    def to_dict(self):
        return {"name": self.name, "type": self.flag_type.value,
                "enabled": self.enabled, "kill_switch": self.kill_switch,
                "percentage": self.percentage,
                "variants": [{"name": v.name, "weight": v.weight} for v in self.variants],
                "depends_on": self.depends_on,
                "environment": self.environment,
                "description": self.description}

@dataclass
class Evaluation:
    flag: str; value: Any
    reason: str; user_id: str = ""
    variant: Optional[str] = None
    ts: float = field(default_factory=time.time)

    def to_dict(self):
        return {"flag": self.flag, "value": self.value,
                "reason": self.reason, "user_id": self.user_id,
                "variant": self.variant}

class FFStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS flags(
                    name TEXT PRIMARY KEY, data TEXT, updated_at REAL);
                CREATE TABLE IF NOT EXISTS overrides(
                    id TEXT PRIMARY KEY, flag TEXT,
                    user_id TEXT, value TEXT, expires_at REAL);
                CREATE TABLE IF NOT EXISTS exposures(
                    id TEXT PRIMARY KEY, flag TEXT,
                    user_id TEXT, variant TEXT,
                    value TEXT, reason TEXT, ts REAL);
                CREATE TABLE IF NOT EXISTS audit(
                    id TEXT PRIMARY KEY, flag TEXT,
                    action TEXT, author TEXT, detail TEXT, ts REAL);
                CREATE INDEX IF NOT EXISTS idx_exp_flag
                    ON exposures(flag, ts DESC);
                CREATE INDEX IF NOT EXISTS idx_ov_flag
                    ON overrides(flag, user_id);
            """)

    def save_flag(self, flag: Flag):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO flags VALUES(?,?,?)",
                (flag.name, json.dumps(flag.to_dict()), flag.updated_at))

    def save_override(self, flag: str, user_id: str,
                       value: Any, ttl_s: float = 0):
        expires = time.time() + ttl_s if ttl_s > 0 else 0
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO overrides VALUES(?,?,?,?,?)",
                (f"{flag}:{user_id}", flag, user_id,
                 json.dumps(value), expires))

    def get_override(self, flag: str, user_id: str) -> Optional[Any]:
        with self._conn() as c:
            row = c.execute(
                "SELECT value, expires_at FROM overrides "
                "WHERE flag=? AND user_id=?", (flag, user_id)).fetchone()
        if not row: return None
        if row["expires_at"] > 0 and time.time() > row["expires_at"]:
            return None
        return json.loads(row["value"])

    def log_exposure(self, ev: Evaluation):
        with self._conn() as c:
            c.execute("INSERT INTO exposures VALUES(?,?,?,?,?,?,?)",
                (str(uuid.uuid4())[:8], ev.flag, ev.user_id,
                 ev.variant or "", json.dumps(ev.value),
                 ev.reason, ev.ts))

    def audit(self, flag: str, action: str, author: str, detail: str = ""):
        with self._conn() as c:
            c.execute("INSERT INTO audit VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4())[:8], flag, action, author, detail, time.time()))

    def exposure_stats(self, flag: str) -> Dict:
        with self._conn() as c:
            total = c.execute(
                "SELECT COUNT(*) FROM exposures WHERE flag=?", (flag,)).fetchone()[0]
            by_variant = {r["variant"]: r["cnt"] for r in c.execute(
                "SELECT variant, COUNT(*) as cnt FROM exposures "
                "WHERE flag=? GROUP BY variant", (flag,)).fetchall()}
        return {"total": total, "by_variant": by_variant}

    def stats(self) -> Dict:
        with self._conn() as c:
            nf = c.execute("SELECT COUNT(*) FROM flags").fetchone()[0]
            ne = c.execute("SELECT COUNT(*) FROM exposures").fetchone()[0]
            no = c.execute("SELECT COUNT(*) FROM overrides").fetchone()[0]
        return {"flags": nf, "exposures": ne, "overrides": no}

class FeatureFlags:
    """
    Feature flag system with percentage rollout and A/B experiments.

    Usage:
        ff = FeatureFlags()
        ff.define("new_ui", FlagType.PERCENTAGE, percentage=20.0,
                   description="New UI for 20% of users")
        ff.define("checkout_v2", FlagType.VARIANT,
                   variants=[Variant("control",50), Variant("treatment",50)])

        # Evaluate for a user
        enabled = ff.is_enabled("new_ui", user_id="user_123")
        variant = ff.get_variant("checkout_v2", user_id="user_123")
    """
    def __init__(self, db_path: str = "data/flags.db",
                 environment: str = "default",
                 track_exposures: bool = True):
        self._store = FFStore(db_path)
        self._flags: Dict[str, Flag] = {}
        self._environment = environment
        self._track = track_exposures
        self._on_change: List[Callable] = []

    def define(self, name: str,
                flag_type: FlagType = FlagType.BOOLEAN,
                enabled: bool = True,
                default_value: Any = False,
                percentage: float = 100.0,
                variants: List[Variant] = None,
                rules: List[SegmentRule] = None,
                depends_on: str = None,
                description: str = "",
                tags: List[str] = None,
                schedule_on: float = 0.0,
                schedule_off: float = 0.0,
                author: str = "system") -> Flag:
        flag = Flag(name=name, flag_type=flag_type, enabled=enabled,
                     default_value=default_value, percentage=percentage,
                     variants=list(variants or []),
                     rules=list(rules or []),
                     depends_on=depends_on, description=description,
                     tags=list(tags or []),
                     schedule_on=schedule_on, schedule_off=schedule_off,
                     environment=self._environment)
        self._flags[name] = flag
        self._store.save_flag(flag)
        self._store.audit(name, "define", author)
        return flag

    def on_change(self, fn: Callable): self._on_change.append(fn)

    def _fire_change(self, flag: Flag):
        for h in self._on_change:
            try: h(flag)
            except: pass

    def _check_deps(self, flag: Flag, user_id: str, attrs: Dict) -> bool:
        if flag.depends_on:
            dep = self._flags.get(flag.depends_on)
            if not dep or not self._is_active(dep, user_id, attrs): return False
        return True

    def _matches_rules(self, flag: Flag, attrs: Dict) -> bool:
        if not flag.rules: return True
        return all(r.matches(attrs) for r in flag.rules)

    def _is_active(self, flag: Flag, user_id: str, attrs: Dict) -> bool:
        if flag.kill_switch: return False
        if not flag.enabled:  return False
        if not flag.is_scheduled_active(): return False
        if not self._check_deps(flag, user_id, attrs): return False
        if not self._matches_rules(flag, attrs): return False
        return True

    def evaluate(self, name: str, user_id: str = "",
                  attrs: Dict = None) -> Evaluation:
        attrs = attrs or {}
        flag = self._flags.get(name)
        if not flag:
            return Evaluation(name, False, "flag_not_found", user_id)

        # Check user override
        override = self._store.get_override(name, user_id)
        if override is not None:
            ev = Evaluation(name, override, "override", user_id)
            if self._track: self._store.log_exposure(ev)
            return ev

        if not self._is_active(flag, user_id, attrs):
            ev = Evaluation(name, flag.default_value, "disabled", user_id)
            return ev

        if flag.flag_type == FlagType.BOOLEAN:
            ev = Evaluation(name, True, "enabled", user_id)

        elif flag.flag_type == FlagType.PERCENTAGE:
            bucket = _hash_bucket(name, user_id) if user_id else 50
            value = bucket < flag.percentage
            reason = "in_rollout" if value else "out_rollout"
            ev = Evaluation(name, value, reason, user_id)

        elif flag.flag_type == FlagType.VARIANT:
            if not flag.variants:
                ev = Evaluation(name, flag.default_value, "no_variants", user_id)
            else:
                bucket = _hash_bucket(name, user_id) if user_id else 0
                total_weight = sum(v.weight for v in flag.variants)
                cumulative = 0; chosen = flag.variants[0]
                for v in flag.variants:
                    cumulative += (v.weight / max(1, total_weight)) * 100
                    if bucket < cumulative: chosen = v; break
                ev = Evaluation(name, chosen.payload if chosen.payload is not None
                                 else chosen.name, "variant", user_id,
                                 variant=chosen.name)

        elif flag.flag_type == FlagType.SCHEDULE:
            value = flag.is_scheduled_active()
            ev = Evaluation(name, value,
                             "schedule_on" if value else "schedule_off", user_id)
        else:
            ev = Evaluation(name, flag.default_value, "unknown_type", user_id)

        if self._track: self._store.log_exposure(ev)
        return ev

    def is_enabled(self, name: str, user_id: str = "",
                    attrs: Dict = None) -> bool:
        return bool(self.evaluate(name, user_id, attrs).value)

    def get_variant(self, name: str, user_id: str = "",
                     attrs: Dict = None) -> Optional[str]:
        ev = self.evaluate(name, user_id, attrs)
        return ev.variant

    def get_value(self, name: str, user_id: str = "",
                   default: Any = None, attrs: Dict = None) -> Any:
        ev = self.evaluate(name, user_id, attrs)
        return ev.value if ev.reason != "flag_not_found" else default

    def set_override(self, flag: str, user_id: str,
                      value: Any, ttl_s: float = 0,
                      author: str = "system"):
        self._store.save_override(flag, user_id, value, ttl_s)
        self._store.audit(flag, "override", author,
                           f"user={user_id} value={value}")

    def kill(self, name: str, author: str = "system"):
        flag = self._flags.get(name)
        if flag:
            flag.kill_switch = True; flag.updated_at = time.time()
            self._store.save_flag(flag)
            self._store.audit(name, "kill_switch", author)
            self._fire_change(flag)

    def revive(self, name: str, author: str = "system"):
        flag = self._flags.get(name)
        if flag:
            flag.kill_switch = False; flag.updated_at = time.time()
            self._store.save_flag(flag)
            self._store.audit(name, "revive", author)
            self._fire_change(flag)

    def set_percentage(self, name: str, pct: float, author: str = "system"):
        flag = self._flags.get(name)
        if flag:
            flag.percentage = max(0.0, min(100.0, pct))
            flag.updated_at = time.time()
            self._store.save_flag(flag)
            self._store.audit(name, "set_percentage", author, f"pct={pct}")
            self._fire_change(flag)

    def evaluate_all(self, user_id: str = "",
                      attrs: Dict = None) -> Dict[str, Any]:
        return {name: self.evaluate(name, user_id, attrs).value
                for name in self._flags}

    def exposure_stats(self, flag: str) -> Dict:
        return self._store.exposure_stats(flag)

    def stats(self) -> Dict:
        s = self._store.stats()
        s["in_memory"] = len(self._flags)
        s["by_type"] = {}
        for f in self._flags.values():
            t = f.flag_type.value
            s["by_type"][t] = s["by_type"].get(t, 0) + 1
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def eval_ep(req):
            d = await req.json()
            ev = self.evaluate(d["flag"], d.get("user_id",""),
                                d.get("attrs",{}))
            return web.json_response(ev.to_dict())
        async def override_ep(req):
            d = await req.json()
            self.set_override(d["flag"], d["user_id"], d["value"],
                               d.get("ttl_s",0))
            return web.json_response({"ok": True})
        async def list_ep(req):
            return web.json_response(
                {"flags": [f.to_dict() for f in self._flags.values()]})
        async def kill_ep(req):
            d = await req.json(); self.kill(d["flag"])
            return web.json_response({"killed": True})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/flags"
        app.router.add_post(f"{p}/evaluate", eval_ep)
        app.router.add_post(f"{p}/override", override_ep)
        app.router.add_get( f"{p}/list",     list_ep)
        app.router.add_post(f"{p}/kill",     kill_ep)
        app.router.add_get( f"{p}/stats",    stats_ep)
        logger.info(f"Feature flags API at {prefix}/flags/")
