"""OMNI AGENT - Health Dashboard
System health monitor: register components, run periodic checks, aggregate
metrics, fire threshold alerts, and surface trend analysis.

Features:
- Component registry: name, check_fn, tags, criticality level
- Check types: ping (latency), boolean (up/down), metric (numeric value)
- Health states: HEALTHY, DEGRADED, UNHEALTHY, UNKNOWN
- Metric aggregation: p50/p95/p99 latency, moving averages, min/max
- Threshold alerts: per-component warn/critical thresholds
- Alert deduplication: suppress repeated alerts within cooldown window
- Trend analysis: linear regression on metric time-series for direction
- Dependency graph: mark components that depend on others
- Scheduled checks: async loop with configurable interval per component
- Overall system score: weighted average across all component health
- History: sliding window of last N check results per component
- SQLite persistence: check results and alert log
- REST API: status, check, history, alerts, score
"""
import asyncio, time, uuid, sqlite3, json, math, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class HealthState(str, Enum):
    HEALTHY   = "healthy"
    DEGRADED  = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN   = "unknown"

class Criticality(str, Enum):
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"

# ── Simple linear regression for trend ────────────────────────────────────────
def _trend_slope(values: List[float]) -> float:
    """Returns slope of least-squares fit. Positive = rising, negative = falling."""
    n = len(values)
    if n < 2: return 0.0
    xs = list(range(n)); xm = (n-1)/2.0; ym = sum(values)/n
    num = sum((xs[i]-xm)*(values[i]-ym) for i in range(n))
    den = sum((xs[i]-xm)**2 for i in range(n))
    return num / max(den, 1e-12)

def _percentile(values: List[float], p: float) -> float:
    if not values: return 0.0
    s = sorted(values)
    idx = (len(s)-1) * p / 100
    lo, hi = int(idx), min(int(idx)+1, len(s)-1)
    frac = idx - lo
    return s[lo] + (s[hi] - s[lo]) * frac

@dataclass
class CheckResult:
    component: str; state: HealthState
    value: float = 0.0          # latency ms or metric value
    message: str = ""
    timestamp: float = field(default_factory=time.time)
    check_ms: float = 0.0

    def to_dict(self):
        return {"component": self.component, "state": self.state,
                "value": round(self.value, 2), "message": self.message,
                "timestamp": round(self.timestamp, 1),
                "check_ms": round(self.check_ms, 1)}

@dataclass
class ComponentSpec:
    id: str; name: str
    check_fn: Callable           # async () → (state, value, message)
    criticality: Criticality = Criticality.MEDIUM
    check_interval_s: float = 30.0
    warn_threshold: float = 500.0    # ms or metric value
    critical_threshold: float = 2000.0
    tags: List[str] = field(default_factory=list)
    depends_on: List[str] = field(default_factory=list)
    history: List[CheckResult] = field(default_factory=list)
    history_size: int = 100
    last_state: HealthState = HealthState.UNKNOWN
    last_check: float = 0.0
    consecutive_failures: int = 0

    @property
    def due(self):
        return time.time() - self.last_check >= self.check_interval_s

    def add_result(self, r: CheckResult):
        self.history.append(r)
        if len(self.history) > self.history_size:
            self.history.pop(0)
        self.last_state = r.state
        self.last_check = r.timestamp
        if r.state == HealthState.HEALTHY:
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1

    def latency_stats(self) -> Dict:
        values = [r.value for r in self.history if r.value > 0]
        if not values: return {}
        return {"p50": round(_percentile(values, 50), 1),
                "p95": round(_percentile(values, 95), 1),
                "p99": round(_percentile(values, 99), 1),
                "min": round(min(values), 1),
                "max": round(max(values), 1),
                "mean": round(sum(values)/len(values), 1)}

    def trend(self) -> str:
        values = [r.value for r in self.history[-20:] if r.value > 0]
        slope = _trend_slope(values)
        if abs(slope) < 0.5: return "stable"
        return "rising" if slope > 0 else "falling"

    def to_dict(self):
        return {"id": self.id, "name": self.name,
                "state": self.last_state, "criticality": self.criticality,
                "consecutive_failures": self.consecutive_failures,
                "last_check": round(self.last_check, 1),
                "latency_stats": self.latency_stats(),
                "trend": self.trend(), "tags": self.tags}

@dataclass
class Alert:
    id: str; component: str; state: HealthState
    message: str; criticality: Criticality
    created_at: float = field(default_factory=time.time)
    resolved_at: float = 0.0; resolved: bool = False

    def to_dict(self):
        return {"id": self.id, "component": self.component, "state": self.state,
                "message": self.message, "criticality": self.criticality,
                "created_at": round(self.created_at, 1),
                "resolved": self.resolved}

class HDStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS check_results(
                    id TEXT PRIMARY KEY, component TEXT, state TEXT,
                    value REAL DEFAULT 0, message TEXT DEFAULT '',
                    timestamp REAL, check_ms REAL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS alerts(
                    id TEXT PRIMARY KEY, component TEXT, state TEXT,
                    message TEXT DEFAULT '', criticality TEXT,
                    created_at REAL, resolved_at REAL DEFAULT 0,
                    resolved INTEGER DEFAULT 0);
                CREATE INDEX IF NOT EXISTS idx_cr_comp ON check_results(component, timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_al_comp ON alerts(component, resolved);
            """)

    def log_result(self, r: CheckResult):
        with self._conn() as c:
            c.execute("INSERT INTO check_results VALUES(?,?,?,?,?,?,?)",
                (str(uuid.uuid4())[:8], r.component, r.state,
                 r.value, r.message, r.timestamp, r.check_ms))

    def log_alert(self, a: Alert):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO alerts VALUES(?,?,?,?,?,?,?,?)",
                (a.id, a.component, a.state, a.message, a.criticality,
                 a.created_at, a.resolved_at, int(a.resolved)))

    def recent_results(self, component: str = None, limit: int = 50) -> List[Dict]:
        with self._conn() as c:
            if component:
                rows = c.execute(
                    "SELECT * FROM check_results WHERE component=? "
                    "ORDER BY timestamp DESC LIMIT ?", (component, limit)).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM check_results ORDER BY timestamp DESC LIMIT ?",
                    (limit,)).fetchall()
        return [dict(r) for r in rows]

    def open_alerts(self) -> List[Dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM alerts WHERE resolved=0 ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> Dict:
        with self._conn() as c:
            nr = c.execute("SELECT COUNT(*) FROM check_results").fetchone()[0]
            na = c.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
            no = c.execute("SELECT COUNT(*) FROM alerts WHERE resolved=0").fetchone()[0]
        return {"total_checks": nr, "total_alerts": na, "open_alerts": no}

class HealthDashboard:
    """
    System health monitor with component checks, alerts, and trend analysis.

    Usage:
        dashboard = HealthDashboard()

        async def check_db():
            start = time.time()
            # ... run DB ping ...
            ms = (time.time()-start)*1000
            return ("healthy" if ms < 100 else "degraded", ms, "")

        dashboard.register("postgres", check_db,
                            criticality="high", warn_threshold=100,
                            critical_threshold=500, check_interval_s=30)

        await dashboard.start()
        status = dashboard.get_status()
        print(status["overall_score"])
    """
    def __init__(self, db_path: str = "data/health.db",
                 alert_cooldown_s: float = 300.0, **_kwargs):
        self._store = HDStore(db_path)
        self._components: Dict[str, ComponentSpec] = {}
        self._alerts: Dict[str, Alert] = {}
        self._alert_last: Dict[str, float] = {}   # component → last alert ts
        self._cooldown = alert_cooldown_s
        self._loop_task: Optional[asyncio.Task] = None
        self._running = False
        self._alert_hooks: List[Callable] = []

    def register(self, name: str, check_fn: Callable,
                  criticality: str = "medium",
                  check_interval_s: float = 30.0,
                  warn_threshold: float = 500.0,
                  critical_threshold: float = 2000.0,
                  tags: List[str] = None,
                  depends_on: List[str] = None) -> ComponentSpec:
        spec = ComponentSpec(
            id=str(uuid.uuid4())[:8], name=name, check_fn=check_fn,
            criticality=Criticality(criticality),
            check_interval_s=check_interval_s,
            warn_threshold=warn_threshold, critical_threshold=critical_threshold,
            tags=tags or [], depends_on=depends_on or [])
        self._components[name] = spec
        logger.info(f"Component registered: {name!r}")
        return spec

    def add_alert_hook(self, fn: Callable): self._alert_hooks.append(fn)

    async def check_component(self, name: str) -> CheckResult:
        spec = self._components.get(name)
        if not spec:
            return CheckResult(component=name, state=HealthState.UNKNOWN,
                                message="Not registered")
        start = time.time()
        try:
            fn = spec.check_fn
            if asyncio.iscoroutinefunction(fn):
                raw = await asyncio.wait_for(fn(), timeout=spec.critical_threshold/1000+5)
            else:
                raw = fn()
            check_ms = (time.time()-start)*1000

            # Normalise return: (state, value, message) or just state-string
            if isinstance(raw, tuple) and len(raw) >= 2:
                state_raw, value = raw[0], float(raw[1])
                message = str(raw[2]) if len(raw) > 2 else ""
            elif isinstance(raw, (int, float)):
                value = float(raw); message = ""
                if value >= spec.critical_threshold: state_raw = "unhealthy"
                elif value >= spec.warn_threshold:   state_raw = "degraded"
                else:                                state_raw = "healthy"
            else:
                value = check_ms; message = str(raw)
                state_raw = "healthy"

            # Map thresholds
            if isinstance(state_raw, str) and state_raw in [s.value for s in HealthState]:
                state = HealthState(state_raw)
            else:
                if value >= spec.critical_threshold: state = HealthState.UNHEALTHY
                elif value >= spec.warn_threshold:   state = HealthState.DEGRADED
                else:                                state = HealthState.HEALTHY

        except asyncio.TimeoutError:
            state = HealthState.UNHEALTHY; value = spec.critical_threshold
            message = "Timeout"; check_ms = (time.time()-start)*1000
        except Exception as e:
            state = HealthState.UNHEALTHY; value = 0.0
            message = str(e); check_ms = (time.time()-start)*1000

        result = CheckResult(component=name, state=state,
                              value=value, message=message, check_ms=check_ms)
        spec.add_result(result)
        self._store.log_result(result)
        self._maybe_alert(spec, result)
        return result

    def _maybe_alert(self, spec: ComponentSpec, result: CheckResult):
        if result.state == HealthState.HEALTHY: return
        last = self._alert_last.get(spec.name, 0)
        if time.time() - last < self._cooldown: return
        alert = Alert(id=str(uuid.uuid4())[:8], component=spec.name,
                       state=result.state, message=result.message,
                       criticality=spec.criticality)
        self._alerts[alert.id] = alert
        self._alert_last[spec.name] = time.time()
        self._store.log_alert(alert)
        for hook in self._alert_hooks:
            try: hook(alert)
            except: pass
        logger.warning(f"ALERT [{spec.criticality}] {spec.name}: {result.state}")

    def resolve_alert(self, alert_id: str):
        alert = self._alerts.get(alert_id)
        if alert:
            alert.resolved = True; alert.resolved_at = time.time()
            self._store.log_alert(alert)


    # ── v12 compat methods ───────────────────────────────────────────────────
    def register_ok(self, name: str, message: str = ""):
        """v12: register a component that always returns healthy."""
        self.register(name, lambda: ("healthy", 0.0, message))

    def register_lambda(self, name: str, fn):
        """v12: register with a simplified (status, message) lambda."""
        def wrapped():
            r = fn()
            if isinstance(r, tuple):
                st = r[0]; msg = r[1] if len(r)>1 else ""
                val = 0.0
            else:
                st = r; msg = ""; val = 0.0
            # Normalise HealthStatus/HealthState values
            st_val = st.value if hasattr(st, 'value') else str(st)
            return (st_val, val, msg)
        self.register(name, wrapped)

    async def check_all(self):
        tasks = {name: asyncio.create_task(self.check_component(name))
                 for name in self._components}
        results = {}
        for name, task in tasks.items():
            results[name] = await task
        # Build combined result: dict-accessible (v26) + .checks (v12)
        class _CheckAllResult(dict):
            pass
        out = _CheckAllResult(results)
        snap = _HealthSnapshot(self._components, len(self._store.open_alerts()))
        snap.overall_score = self.overall_score()
        snap.results = results
        out.checks = snap.checks   # v12 compat
        out.overall_score = snap.overall_score
        return out

    def overall_score(self) -> float:
        """Weighted health score 0-1 based on criticality."""
        weights = {Criticality.LOW: 1, Criticality.MEDIUM: 2,
                   Criticality.HIGH: 4, Criticality.CRITICAL: 8}
        state_scores = {HealthState.HEALTHY: 1.0, HealthState.DEGRADED: 0.5,
                        HealthState.UNHEALTHY: 0.0, HealthState.UNKNOWN: 0.5}
        total_w = 0; weighted_score = 0.0
        for spec in self._components.values():
            w = weights.get(spec.criticality, 2)
            s = state_scores.get(spec.last_state, 0.5)
            weighted_score += s * w; total_w += w
        return round(weighted_score / max(1, total_w), 4)

    def get_status(self, as_snapshot: bool = False) -> "Dict":
        snap = _HealthSnapshot(self._components, len(self._store.open_alerts()))
        snap.overall_score = self.overall_score()
        if as_snapshot: return snap
        return {"overall_score": snap.overall_score,
                "components": {n: s.to_dict() for n, s in self._components.items()},
                "open_alerts": snap.open_alerts, "checks": snap.checks}

    async def _monitor_loop(self):
        while self._running:
            due = [n for n, s in self._components.items() if s.due]
            for name in due:
                await self.check_component(name)
            await asyncio.sleep(1.0)

    async def start(self):
        self._running = True
        self._loop_task = asyncio.create_task(self._monitor_loop())

    async def stop(self):
        self._running = False
        if self._loop_task: self._loop_task.cancel()

    def stats(self) -> Dict:
        s = self._store.stats()
        s["registered_components"] = len(self._components)
        s["registered_checks"] = len(self._components)
        s["overall_score"] = self.overall_score()
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def status_ep(req):
            return web.json_response(self.get_status())
        async def check_ep(req):
            d = await req.json()
            result = await self.check_component(d["component"])
            return web.json_response(result.to_dict())
        async def check_all_ep(req):
            results = await self.check_all()
            return web.json_response({n: r.to_dict() for n, r in results.items()})
        async def alerts_ep(req):
            return web.json_response({"alerts": self._store.open_alerts()})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/health"
        app.router.add_get( f"{p}/status",    status_ep)
        app.router.add_post(f"{p}/check",     check_ep)
        app.router.add_get( f"{p}/check-all", check_all_ep)
        app.router.add_get( f"{p}/alerts",    alerts_ep)
        app.router.add_get( f"{p}/stats",     stats_ep)
        logger.info(f"Health dashboard API at {prefix}/health/")

# ── Backward-compatibility shims (v12 API) ───────────────────────────────────
HealthStatus = HealthState   # alias for old name


# v12 compat: old status names
HealthState.OK      = HealthState.HEALTHY
HealthState.ERROR   = HealthState.UNHEALTHY
HealthState.WARNING = HealthState.DEGRADED

def _aggregate_status(states) -> HealthState:
    """Return worst HealthState; empty list → UNKNOWN (v12 compat)."""
    if not states: return HealthState.UNKNOWN
    order = [HealthState.UNHEALTHY, HealthState.DEGRADED,
             HealthState.UNKNOWN, HealthState.HEALTHY]
    for s in order:
        if s in states: return s
    return HealthState.HEALTHY

# v12 compat extra aliases
HealthState.DOWN    = HealthState.UNHEALTHY
HealthState.UP      = HealthState.HEALTHY
HealthState.WARN    = HealthState.DEGRADED

class _HealthSnapshot:
    """v12 compat: get_status() result with .checks iterable."""
    def __init__(self, components_dict, alerts):
        self.overall_score = 0.0
        self.open_alerts   = alerts
        self.checks = [type("C", (), {"name": n, "state": s.last_state,
                                       "latency_stats": s.latency_stats(),
                                       "trend": s.trend(),
                                       "consecutive_failures": s.consecutive_failures})()
                       for n, s in components_dict.items()]
    def __getitem__(self, k): return self.__dict__[k]
    def get(self, k, d=None): return self.__dict__.get(k, d)
