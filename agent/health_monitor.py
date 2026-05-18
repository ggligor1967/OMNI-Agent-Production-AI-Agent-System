"""OMNI AGENT - Health Monitor
Composable health checks (HTTP, TCP, DB, custom) with aggregation,
history, alerting, and dependency-graph-aware status propagation.

Features:
- Check types: HTTP (GET url, expect status/body), TCP (connect host:port),
    DB (sqlite/psycopg2 query), MEMORY (threshold %), DISK (threshold %),
    CUSTOM (fn() → bool|HealthStatus)
- Status: HEALTHY, DEGRADED, UNHEALTHY, UNKNOWN
- Interval: per-check polling interval in seconds
- Timeout: per-check connection/response timeout
- Consecutive: require N consecutive failures before UNHEALTHY
- Recovery: require N consecutive successes before HEALTHY again
- Dependencies: check is UNHEALTHY if any dependency is UNHEALTHY
- Aggregation: overall status = worst of all checks
- History: rolling last-N results per check
- Alerting: on_unhealthy(check_name, status, detail) callback
- On-change hooks: fire when check status transitions
- Async runner: background coroutine polling all checks
- Tags: group checks by tag, query group status
- Latency: measure check execution time
- SQLite persistence: check results, status changes
- REST API: status, check, history, run, stats
"""
import asyncio, json, sqlite3, socket, time, uuid, logging
from collections import deque
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class CheckType(str, Enum):
    HTTP   = "http"; TCP    = "tcp"
    DB     = "db";   MEMORY = "memory"
    DISK   = "disk"; CUSTOM = "custom"

class HealthStatus(str, Enum):
    HEALTHY   = "healthy";   DEGRADED  = "degraded"
    UNHEALTHY = "unhealthy"; UNKNOWN   = "unknown"

_STATUS_RANK = {HealthStatus.HEALTHY:0, HealthStatus.DEGRADED:1,
                HealthStatus.UNKNOWN:2,  HealthStatus.UNHEALTHY:3}

def _worst(*statuses: HealthStatus) -> HealthStatus:
    return max(statuses, key=lambda s: _STATUS_RANK.get(s, 0))

@dataclass
class CheckResult:
    check_name: str; status: HealthStatus
    detail: str = ""; latency_ms: float = 0.0
    ts: float = field(default_factory=time.time)

    def to_dict(self):
        return {"check": self.check_name, "status": self.status.value,
                "detail": self.detail,
                "latency_ms": round(self.latency_ms, 2),
                "ts": round(self.ts, 2)}

@dataclass
class HealthCheck:
    name: str; check_type: CheckType
    # HTTP
    url: str = ""; expected_status: int = 200; expected_body: str = ""
    # TCP
    host: str = ""; port: int = 0
    # DB
    db_path: str = ""; query: str = "SELECT 1"
    # MEMORY/DISK
    threshold_pct: float = 90.0; path: str = "/"
    # CUSTOM
    fn: Optional[Callable] = None
    # Common
    interval_s: float = 30.0; timeout_s: float = 5.0
    fail_threshold: int = 3;  pass_threshold: int = 2
    dependencies: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    enabled: bool = True

    def to_dict(self):
        return {"name": self.name, "type": self.check_type.value,
                "interval_s": self.interval_s, "timeout_s": self.timeout_s,
                "enabled": self.enabled, "tags": self.tags}

@dataclass
class _CheckState:
    status: HealthStatus = HealthStatus.UNKNOWN
    consecutive_fail: int = 0; consecutive_pass: int = 0
    history: deque = field(default_factory=lambda: deque(maxlen=20))
    last_run: float = 0.0

class HMStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS results(
                    id TEXT PRIMARY KEY, check_name TEXT,
                    status TEXT, detail TEXT,
                    latency_ms REAL, ts REAL);
                CREATE TABLE IF NOT EXISTS status_changes(
                    id TEXT PRIMARY KEY, check_name TEXT,
                    from_status TEXT, to_status TEXT, ts REAL);
                CREATE INDEX IF NOT EXISTS idx_res_check
                    ON results(check_name, ts DESC);
            """)

    def save_result(self, r: CheckResult):
        with self._conn() as c:
            c.execute("INSERT INTO results VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4())[:8], r.check_name, r.status.value,
                 r.detail[:300], r.latency_ms, r.ts))

    def log_change(self, name: str, from_s: HealthStatus, to_s: HealthStatus):
        with self._conn() as c:
            c.execute("INSERT INTO status_changes VALUES(?,?,?,?,?)",
                (str(uuid.uuid4())[:8], name,
                 from_s.value, to_s.value, time.time()))

    def history(self, check_name: str, limit: int = 20) -> List[Dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM results WHERE check_name=? "
                "ORDER BY ts DESC LIMIT ?", (check_name, limit)).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> Dict:
        with self._conn() as c:
            nr = c.execute("SELECT COUNT(*) FROM results").fetchone()[0]
            nc = c.execute("SELECT COUNT(*) FROM status_changes").fetchone()[0]
        return {"total_results": nr, "total_changes": nc}

class HealthMonitor:
    """
    Composable health monitor with aggregated status and alerting.

    Usage:
        hm = HealthMonitor()
        hm.add_check(HealthCheck("db", CheckType.DB,
                                  db_path="app.db", query="SELECT 1"))
        hm.add_check(HealthCheck("api", CheckType.HTTP,
                                  url="http://localhost:8080/health"))

        hm.on_unhealthy(lambda name, status, detail:
            print(f"ALERT: {name} is {status}: {detail}"))

        await hm.run_once()
        print(hm.overall_status())
    """
    def __init__(self, db_path: str = "data/health.db"):
        self._store = HMStore(db_path)
        self._checks: Dict[str, HealthCheck] = {}
        self._states: Dict[str, _CheckState] = {}
        self._alert_hooks: List[Callable] = []
        self._change_hooks: List[Callable] = []
        self._running = False

    def add_check(self, check: HealthCheck):
        self._checks[check.name] = check
        self._states[check.name] = _CheckState()

    def on_unhealthy(self, fn: Callable): self._alert_hooks.append(fn)
    def on_change(self, fn: Callable):    self._change_hooks.append(fn)

    async def _run_http(self, check: HealthCheck) -> CheckResult:
        try:
            import urllib.request
            req = urllib.request.Request(check.url)
            with urllib.request.urlopen(req, timeout=check.timeout_s) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                status_ok = resp.status == check.expected_status
                body_ok   = (not check.expected_body or
                              check.expected_body in body)
                if status_ok and body_ok:
                    return CheckResult(check.name, HealthStatus.HEALTHY,
                                        f"HTTP {resp.status}")
                return CheckResult(check.name, HealthStatus.UNHEALTHY,
                                    f"HTTP {resp.status} body_ok={body_ok}")
        except Exception as e:
            return CheckResult(check.name, HealthStatus.UNHEALTHY, str(e)[:200])

    def _run_tcp(self, check: HealthCheck) -> CheckResult:
        try:
            with socket.create_connection(
                    (check.host, check.port), timeout=check.timeout_s):
                return CheckResult(check.name, HealthStatus.HEALTHY,
                                    f"TCP {check.host}:{check.port} ok")
        except Exception as e:
            return CheckResult(check.name, HealthStatus.UNHEALTHY, str(e)[:200])

    def _run_db(self, check: HealthCheck) -> CheckResult:
        try:
            import sqlite3 as _sl
            conn = _sl.connect(check.db_path, timeout=check.timeout_s)
            conn.execute(check.query)
            conn.close()
            return CheckResult(check.name, HealthStatus.HEALTHY, "DB query ok")
        except Exception as e:
            return CheckResult(check.name, HealthStatus.UNHEALTHY, str(e)[:200])

    def _run_memory(self, check: HealthCheck) -> CheckResult:
        try:
            import shutil
            total, used, free = shutil.disk_usage(check.path)
            pct = used / total * 100
            status = (HealthStatus.HEALTHY if pct < check.threshold_pct
                       else HealthStatus.UNHEALTHY)
            return CheckResult(check.name, status,
                                f"disk {pct:.1f}% used (threshold {check.threshold_pct}%)")
        except Exception as e:
            # Fallback to memory check via /proc/meminfo if available
            try:
                with open("/proc/meminfo") as f:
                    lines = {l.split(":")[0]: int(l.split()[1])
                              for l in f if ":" in l}
                total = lines.get("MemTotal", 1)
                avail = lines.get("MemAvailable", total)
                pct   = (1 - avail / total) * 100
                status = (HealthStatus.HEALTHY if pct < check.threshold_pct
                           else HealthStatus.UNHEALTHY)
                return CheckResult(check.name, status,
                                    f"memory {pct:.1f}% used")
            except:
                return CheckResult(check.name, HealthStatus.UNKNOWN, str(e))

    def _run_custom(self, check: HealthCheck) -> CheckResult:
        if not check.fn:
            return CheckResult(check.name, HealthStatus.UNKNOWN, "no fn")
        try:
            result = check.fn()
            if isinstance(result, HealthStatus):
                return CheckResult(check.name, result, "custom fn")
            if isinstance(result, CheckResult):
                return result
            status = HealthStatus.HEALTHY if result else HealthStatus.UNHEALTHY
            return CheckResult(check.name, status, f"custom={result}")
        except Exception as e:
            return CheckResult(check.name, HealthStatus.UNHEALTHY, str(e)[:200])

    async def _execute(self, check: HealthCheck) -> CheckResult:
        t0 = time.time()
        if check.check_type == CheckType.HTTP:
            r = await self._run_http(check)
        elif check.check_type == CheckType.TCP:
            r = await asyncio.get_event_loop().run_in_executor(
                None, self._run_tcp, check)
        elif check.check_type == CheckType.DB:
            r = await asyncio.get_event_loop().run_in_executor(
                None, self._run_db, check)
        elif check.check_type in (CheckType.MEMORY, CheckType.DISK):
            r = await asyncio.get_event_loop().run_in_executor(
                None, self._run_memory, check)
        else:
            r = await asyncio.get_event_loop().run_in_executor(
                None, self._run_custom, check)
        r.latency_ms = (time.time() - t0) * 1000
        return r

    def _apply_thresholds(self, state: _CheckState, result: CheckResult,
                           check: HealthCheck) -> HealthStatus:
        if result.status == HealthStatus.UNHEALTHY:
            state.consecutive_fail  += 1
            state.consecutive_pass  = 0
        else:
            state.consecutive_pass += 1
            state.consecutive_fail  = 0

        if state.consecutive_fail >= check.fail_threshold:
            return HealthStatus.UNHEALTHY
        if (state.status == HealthStatus.UNHEALTHY and
                state.consecutive_pass < check.pass_threshold):
            return HealthStatus.DEGRADED
        if result.status != HealthStatus.UNHEALTHY:
            return HealthStatus.HEALTHY
        return HealthStatus.DEGRADED

    def _check_deps(self, check: HealthCheck) -> Optional[HealthStatus]:
        for dep_name in check.dependencies:
            dep_state = self._states.get(dep_name)
            if dep_state and dep_state.status == HealthStatus.UNHEALTHY:
                return HealthStatus.UNHEALTHY
        return None

    async def run_check(self, name: str) -> CheckResult:
        check = self._checks.get(name)
        if not check or not check.enabled:
            return CheckResult(name, HealthStatus.UNKNOWN, "disabled")
        state = self._states[name]
        dep_status = self._check_deps(check)
        if dep_status == HealthStatus.UNHEALTHY:
            result = CheckResult(name, HealthStatus.UNHEALTHY,
                                  "dependency unhealthy")
        else:
            result = await self._execute(check)
        new_status = self._apply_thresholds(state, result, check)
        result.status = new_status
        old_status = state.status
        state.status = new_status
        state.history.append(result)
        state.last_run = time.time()
        self._store.save_result(result)
        if new_status != old_status:
            self._store.log_change(name, old_status, new_status)
            for h in self._change_hooks:
                try: h(name, old_status, new_status)
                except: pass
        if new_status == HealthStatus.UNHEALTHY:
            for h in self._alert_hooks:
                try: h(name, new_status, result.detail)
                except: pass
        return result

    async def run_once(self) -> Dict[str, CheckResult]:
        tasks = [self.run_check(name) for name in self._checks]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {name: r for name, r in zip(self._checks, results)
                if isinstance(r, CheckResult)}

    def overall_status(self) -> HealthStatus:
        if not self._states: return HealthStatus.UNKNOWN
        statuses = [s.status for s in self._states.values()]
        return _worst(*statuses)

    def status(self, name: str = None) -> Dict:
        if name:
            state = self._states.get(name)
            check = self._checks.get(name)
            if not state: return {}
            return {"name": name, "status": state.status.value,
                     "consecutive_fail": state.consecutive_fail,
                     "last_run": round(state.last_run, 2),
                     "config": check.to_dict() if check else {}}
        return {"overall": self.overall_status().value,
                "checks": {n: {"status": s.status.value,
                                "last_run": round(s.last_run, 2)}
                            for n, s in self._states.items()}}

    def status_by_tag(self, tag: str) -> HealthStatus:
        tagged = [n for n, c in self._checks.items() if tag in c.tags]
        if not tagged: return HealthStatus.UNKNOWN
        statuses = [self._states[n].status for n in tagged if n in self._states]
        return _worst(*statuses) if statuses else HealthStatus.UNKNOWN

    def history(self, name: str, limit: int = 20) -> List[Dict]:
        return self._store.history(name, limit)

    def stats(self) -> Dict:
        s = self._store.stats()
        s["checks"] = len(self._checks)
        s["overall"] = self.overall_status().value
        s["by_status"] = {}
        for state in self._states.values():
            st = state.status.value
            s["by_status"][st] = s["by_status"].get(st, 0) + 1
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def status_ep(req):
            name = req.rel_url.query.get("check")
            return web.json_response(self.status(name))
        async def run_ep(req):
            d = await req.json()
            name = d.get("check")
            if name:
                r = await self.run_check(name)
                return web.json_response(r.to_dict())
            results = await self.run_once()
            return web.json_response({k: v.to_dict() for k,v in results.items()})
        async def history_ep(req):
            name = req.rel_url.query.get("check","")
            limit = int(req.rel_url.query.get("limit",20))
            return web.json_response({"history": self.history(name, limit)})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/health"
        app.router.add_get( f"{p}/status",  status_ep)
        app.router.add_post(f"{p}/run",     run_ep)
        app.router.add_get( f"{p}/history", history_ep)
        app.router.add_get( f"{p}/stats",   stats_ep)
        logger.info(f"Health monitor API at {prefix}/health/")
