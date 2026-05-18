"""
OMNI AGENT - Observability
Prometheus-compatible metrics collection, health checks, alerting rules,
and a /metrics endpoint for Grafana/Prometheus scraping.

Features:
- Counter, Gauge, Histogram, Summary metric types
- Label-dimension support (e.g. model="gpt4", status="success")
- Pre-wired agent metrics: requests, latency, tokens, errors, cache hits
- Health check registry: async checks with timeouts and status reporting
- Alert rules: threshold-based firing with cooldown and notification hooks
- /metrics → Prometheus text format (OpenMetrics-compatible)
- /health  → JSON health report
- /ready   → 200 OK / 503 based on critical checks
"""
import time
import math
import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict, deque

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# METRIC TYPES
# ══════════════════════════════════════════════════════════════════════════════

class MetricType(str, Enum):
    COUNTER   = "counter"
    GAUGE     = "gauge"
    HISTOGRAM = "histogram"
    SUMMARY   = "summary"


def _labels_key(labels: Dict[str, str]) -> str:
    return ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))


class Counter:
    """Monotonically increasing counter."""
    def __init__(self, name: str, help: str, labels: List[str] = None):
        self.name = name
        self.help = help
        self._label_names = labels or []
        self._values: Dict[str, float] = defaultdict(float)

    def inc(self, amount: float = 1.0, **labels):
        key = _labels_key(labels)
        self._values[key] += amount

    def get(self, **labels) -> float:
        return self._values[_labels_key(labels)]

    def samples(self) -> List[Tuple[Dict, float]]:
        out = []
        for key, val in self._values.items():
            lbl = {}
            if key:
                for pair in key.split(","):
                    k, v = pair.split("=", 1)
                    lbl[k] = v.strip('"')
            out.append((lbl, val))
        return out

    def to_prometheus(self) -> str:
        lines = [f"# HELP {self.name} {self.help}",
                 f"# TYPE {self.name} counter"]
        for lbl, val in self.samples():
            lset = "{" + ",".join(f'{k}="{v}"' for k,v in lbl.items()) + "}" if lbl else ""
            lines.append(f"{self.name}{lset} {val}")
        return "\n".join(lines)


class Gauge:
    """Arbitrary value that can go up or down."""
    def __init__(self, name: str, help: str, labels: List[str] = None):
        self.name = name
        self.help = help
        self._label_names = labels or []
        self._values: Dict[str, float] = defaultdict(float)

    def set(self, value: float, **labels):
        self._values[_labels_key(labels)] = value

    def inc(self, amount: float = 1.0, **labels):
        self._values[_labels_key(labels)] += amount

    def dec(self, amount: float = 1.0, **labels):
        self._values[_labels_key(labels)] -= amount

    def get(self, **labels) -> float:
        return self._values[_labels_key(labels)]

    def samples(self) -> List[Tuple[Dict, float]]:
        out = []
        for key, val in self._values.items():
            lbl = {}
            if key:
                for pair in key.split(","):
                    k, v = pair.split("=", 1)
                    lbl[k] = v.strip('"')
            out.append((lbl, val))
        return out

    def to_prometheus(self) -> str:
        lines = [f"# HELP {self.name} {self.help}",
                 f"# TYPE {self.name} gauge"]
        for lbl, val in self.samples():
            lset = "{" + ",".join(f'{k}="{v}"' for k,v in lbl.items()) + "}" if lbl else ""
            lines.append(f"{self.name}{lset} {val}")
        return "\n".join(lines)


class Histogram:
    """
    Bucketed latency/size histogram.
    Default buckets: .005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10 seconds.
    """
    DEFAULT_BUCKETS = (.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10)

    def __init__(self, name: str, help: str,
                 buckets: Tuple = None, labels: List[str] = None):
        self.name = name
        self.help = help
        self._label_names = labels or []
        self._buckets = sorted(buckets or self.DEFAULT_BUCKETS)
        # key → {bucket_le: count, _sum, _count}
        self._data: Dict[str, Dict] = defaultdict(
            lambda: {b: 0 for b in self._buckets} | {"_sum": 0.0, "_count": 0}
        )

    def observe(self, value: float, **labels):
        key = _labels_key(labels)
        d = self._data[key]
        d["_sum"] += value
        d["_count"] += 1
        for b in self._buckets:
            if value <= b:
                d[b] += 1

    def to_prometheus(self) -> str:
        lines = [f"# HELP {self.name} {self.help}",
                 f"# TYPE {self.name} histogram"]
        for key, d in self._data.items():
            lbl_str = ("{" + key + "," if key else "{")
            for b in self._buckets:
                le = "+Inf" if b == float("inf") else str(b)
                lines.append(f'{self.name}_bucket{{{key + "," if key else ""}le="{le}"}} {d[b]}')
            lines.append(f'{self.name}_bucket{{{key + "," if key else ""}le="+Inf"}} {d["_count"]}')
            lines.append(f'{self.name}_sum{{{key}}} {d["_sum"]}')
            lines.append(f'{self.name}_count{{{key}}} {d["_count"]}')
        return "\n".join(lines)

    def percentile(self, p: float, **labels) -> float:
        """Estimate percentile from bucket data (linear interpolation)."""
        key = _labels_key(labels)
        d = self._data.get(key)
        if not d or d["_count"] == 0:
            return 0.0
        target = p * d["_count"]
        prev_b, prev_c = 0.0, 0
        for b in self._buckets:
            c = d[b]
            if c >= target:
                if c == prev_c:
                    return b
                frac = (target - prev_c) / (c - prev_c)
                return prev_b + frac * (b - prev_b)
            prev_b, prev_c = b, c
        return self._buckets[-1]


class Summary:
    """Sliding-window quantile summary (last N observations)."""

    def __init__(self, name: str, help: str,
                 window: int = 1000, labels: List[str] = None):
        self.name = name
        self.help = help
        self._label_names = labels or []
        self._window = window
        self._data: Dict[str, deque] = defaultdict(lambda: deque(maxlen=window))

    def observe(self, value: float, **labels):
        self._data[_labels_key(labels)].append(value)

    def quantile(self, q: float, **labels) -> float:
        key = _labels_key(labels)
        vals = sorted(self._data.get(key, []))
        if not vals:
            return 0.0
        idx = int(q * len(vals))
        return vals[min(idx, len(vals) - 1)]

    def to_prometheus(self) -> str:
        lines = [f"# HELP {self.name} {self.help}",
                 f"# TYPE {self.name} summary"]
        for key, vals_deque in self._data.items():
            vals = sorted(vals_deque)
            total = len(vals)
            lp = ("{" + key + "," if key else "{")
            for q in (0.5, 0.9, 0.95, 0.99):
                idx = int(q * total)
                v = vals[min(idx, total - 1)] if vals else 0.0
                lines.append(f'{self.name}{{{key + "," if key else ""}quantile="{q}"}} {v}')
            lines.append(f'{self.name}_sum{{{key}}} {sum(vals)}')
            lines.append(f'{self.name}_count{{{key}}} {total}')
        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# HEALTH CHECKS
# ══════════════════════════════════════════════════════════════════════════════

class HealthStatus(str, Enum):
    HEALTHY   = "healthy"
    DEGRADED  = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass
class HealthResult:
    name: str
    status: HealthStatus
    message: str = ""
    latency_ms: float = 0.0
    critical: bool = False
    checked_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {
            "name": self.name, "status": self.status,
            "message": self.message,
            "latency_ms": round(self.latency_ms, 1),
            "critical": self.critical,
            "checked_at": self.checked_at,
        }


@dataclass
class CheckDef:
    name: str
    fn: Callable
    critical: bool = False
    timeout_s: float = 5.0
    interval_s: float = 30.0
    last_result: Optional[HealthResult] = None
    last_run: float = 0.0


class HealthRegistry:
    """Register and run async health checks."""

    def __init__(self):
        self._checks: Dict[str, CheckDef] = {}

    def register(self, name: str, fn: Callable,
                 critical: bool = False,
                 timeout_s: float = 5.0,
                 interval_s: float = 30.0):
        """Register a health check function. fn() → (bool, str) or bool."""
        self._checks[name] = CheckDef(
            name=name, fn=fn, critical=critical,
            timeout_s=timeout_s, interval_s=interval_s
        )

    async def run_check(self, name: str) -> HealthResult:
        check = self._checks.get(name)
        if not check:
            return HealthResult(name, HealthStatus.UNHEALTHY,
                               "Check not registered", critical=False)
        start = time.time()
        try:
            if asyncio.iscoroutinefunction(check.fn):
                result = await asyncio.wait_for(check.fn(), timeout=check.timeout_s)
            else:
                result = check.fn()

            if isinstance(result, tuple):
                ok, msg = result[0], result[1] if len(result) > 1 else ""
            else:
                ok, msg = bool(result), ""

            status = HealthStatus.HEALTHY if ok else HealthStatus.DEGRADED
            latency = (time.time() - start) * 1000
            hr = HealthResult(name, status, msg, latency, check.critical)

        except asyncio.TimeoutError:
            latency = (time.time() - start) * 1000
            hr = HealthResult(name, HealthStatus.UNHEALTHY,
                             f"Timed out after {check.timeout_s}s",
                             latency, check.critical)
        except Exception as e:
            latency = (time.time() - start) * 1000
            hr = HealthResult(name, HealthStatus.UNHEALTHY,
                             str(e)[:200], latency, check.critical)

        check.last_result = hr
        check.last_run = time.time()
        return hr

    async def run_all(self) -> Dict[str, HealthResult]:
        tasks = {name: self.run_check(name) for name in self._checks}
        results = {}
        for name, coro in tasks.items():
            results[name] = await coro
        return results

    def overall_status(self, results: Dict[str, HealthResult]) -> HealthStatus:
        if any(r.status == HealthStatus.UNHEALTHY and r.critical
               for r in results.values()):
            return HealthStatus.UNHEALTHY
        if any(r.status != HealthStatus.HEALTHY for r in results.values()):
            return HealthStatus.DEGRADED
        return HealthStatus.HEALTHY

    def is_ready(self, results: Dict[str, HealthResult]) -> bool:
        """Ready = no critical checks are unhealthy."""
        return not any(
            r.critical and r.status == HealthStatus.UNHEALTHY
            for r in results.values()
        )


# ══════════════════════════════════════════════════════════════════════════════
# ALERT RULES
# ══════════════════════════════════════════════════════════════════════════════

class AlertSeverity(str, Enum):
    INFO     = "info"
    WARNING  = "warning"
    CRITICAL = "critical"


@dataclass
class AlertRule:
    name: str
    condition: Callable[["MetricsRegistry"], bool]
    message: str
    severity: AlertSeverity = AlertSeverity.WARNING
    cooldown_s: float = 300.0   # min seconds between firings
    last_fired: float = 0.0
    firing: bool = False


@dataclass
class Alert:
    rule_name: str
    message: str
    severity: AlertSeverity
    fired_at: float
    resolved_at: Optional[float] = None

    def to_dict(self) -> Dict:
        return {
            "rule": self.rule_name, "message": self.message,
            "severity": self.severity,
            "fired_at": self.fired_at,
            "resolved_at": self.resolved_at,
            "active": self.resolved_at is None,
        }


# ══════════════════════════════════════════════════════════════════════════════
# METRICS REGISTRY
# ══════════════════════════════════════════════════════════════════════════════

class MetricsRegistry:
    """
    Central registry for all metrics, health checks, and alert rules.

    Usage:
        metrics = MetricsRegistry()

        # Pre-wired agent metrics available immediately:
        metrics.requests_total.inc(model="gpt4", status="success")
        metrics.latency_seconds.observe(0.42, model="gpt4")
        metrics.tokens_total.inc(312, model="gpt4", direction="out")
        metrics.active_sessions.inc()
        metrics.cache_hits.inc(backend="redis")
        metrics.errors_total.inc(type="timeout", model="gpt4")

        # Custom metrics
        my_counter = metrics.counter("my_counter", "My counter", labels=["env"])
        my_counter.inc(env="prod")

        # Health checks
        metrics.health.register("database", lambda: db.ping(), critical=True)
        metrics.health.register("redis", check_redis, critical=False)

        # Prometheus endpoint
        text = metrics.to_prometheus()

        # Alert rules
        metrics.add_alert(AlertRule(
            name="high_error_rate",
            condition=lambda m: m.errors_total.get(type="all") > 100,
            message="Error rate too high",
            severity=AlertSeverity.CRITICAL,
        ))
    """

    def __init__(self):
        self._metrics: Dict[str, Any] = {}
        self.health = HealthRegistry()
        self._alert_rules: Dict[str, AlertRule] = {}
        self._alert_history: List[Alert] = []
        self._alert_callbacks: List[Callable] = []
        self._start_time = time.time()
        self._eval_task: Optional[asyncio.Task] = None

        # ── Pre-wired agent metrics ───────────────────────────────────────────
        self.requests_total = self.counter(
            "agent_requests_total",
            "Total requests handled",
            labels=["model", "status"])

        self.latency_seconds = self.histogram(
            "agent_request_latency_seconds",
            "Request latency in seconds",
            buckets=(.05, .1, .25, .5, 1, 2, 5, 10, 30),
            labels=["model"])

        self.tokens_total = self.counter(
            "agent_tokens_total",
            "Total tokens processed",
            labels=["model", "direction"])  # direction: in/out

        self.active_sessions = self.gauge(
            "agent_active_sessions",
            "Currently active sessions")

        self.cache_hits = self.counter(
            "agent_cache_hits_total",
            "Cache hit count",
            labels=["backend"])

        self.cache_misses = self.counter(
            "agent_cache_misses_total",
            "Cache miss count",
            labels=["backend"])

        self.errors_total = self.counter(
            "agent_errors_total",
            "Total errors",
            labels=["type", "model"])

        self.rag_queries = self.counter(
            "agent_rag_queries_total",
            "RAG retrieval queries")

        self.tool_calls = self.counter(
            "agent_tool_calls_total",
            "Tool invocation count",
            labels=["tool", "status"])

        self.webhook_deliveries = self.counter(
            "agent_webhook_deliveries_total",
            "Webhook delivery attempts",
            labels=["status"])

        self.job_executions = self.counter(
            "agent_job_executions_total",
            "Background job executions",
            labels=["type", "status"])

        self.model_cost_usd = self.counter(
            "agent_model_cost_usd_total",
            "Estimated model cost in USD",
            labels=["model"])

        self.uptime_seconds = self.gauge(
            "agent_uptime_seconds",
            "Agent uptime in seconds")

        # Register built-in health checks
        self.health.register(
            "metrics_registry",
            lambda: (True, f"{len(self._metrics)} metrics registered"),
            critical=False,
        )

    # ── Metric factories ──────────────────────────────────────────────────────

    def counter(self, name: str, help: str, labels: List[str] = None) -> Counter:
        c = Counter(name, help, labels)
        self._metrics[name] = c
        return c

    def gauge(self, name: str, help: str, labels: List[str] = None) -> Gauge:
        g = Gauge(name, help, labels)
        self._metrics[name] = g
        return g

    def histogram(self, name: str, help: str,
                  buckets: Tuple = None, labels: List[str] = None) -> Histogram:
        h = Histogram(name, help, buckets, labels)
        self._metrics[name] = h
        return h

    def summary(self, name: str, help: str,
                window: int = 1000, labels: List[str] = None) -> Summary:
        s = Summary(name, help, window, labels)
        self._metrics[name] = s
        return s

    def get(self, name: str):
        return self._metrics.get(name)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def record_request(self, model: str, status: str,
                       latency_s: float, tokens_in: int = 0,
                       tokens_out: int = 0, cost_usd: float = 0.0):
        """Convenience: record a complete LLM request."""
        self.requests_total.inc(model=model, status=status)
        self.latency_seconds.observe(latency_s, model=model)
        if tokens_in:
            self.tokens_total.inc(tokens_in, model=model, direction="in")
        if tokens_out:
            self.tokens_total.inc(tokens_out, model=model, direction="out")
        if cost_usd:
            self.model_cost_usd.inc(cost_usd, model=model)
        self.uptime_seconds.set(time.time() - self._start_time)

    def record_error(self, error_type: str, model: str = "unknown"):
        self.errors_total.inc(type=error_type, model=model)

    def record_cache(self, hit: bool, backend: str = "memory"):
        if hit:
            self.cache_hits.inc(backend=backend)
        else:
            self.cache_misses.inc(backend=backend)

    # ── Prometheus output ─────────────────────────────────────────────────────

    def to_prometheus(self) -> str:
        """Render all metrics as Prometheus text format."""
        self.uptime_seconds.set(time.time() - self._start_time)
        parts = []
        for name, metric in self._metrics.items():
            text = metric.to_prometheus()
            if text.strip():
                parts.append(text)
        return "\n\n".join(parts) + "\n"

    def snapshot(self) -> Dict:
        """Return a JSON-serializable snapshot of all metrics."""
        self.uptime_seconds.set(time.time() - self._start_time)
        snap = {}
        for name, metric in self._metrics.items():
            if isinstance(metric, (Counter, Gauge)):
                snap[name] = {str(k): v for k, v in
                              [(str(lbl), val) for lbl, val in metric.samples()]}
        return snap

    # ── Alert rules ───────────────────────────────────────────────────────────

    def add_alert(self, rule: AlertRule):
        self._alert_rules[rule.name] = rule

    def on_alert(self, callback: Callable):
        """Register a callback(alert: Alert) fired when an alert fires."""
        self._alert_callbacks.append(callback)

    async def evaluate_alerts(self):
        """Evaluate all alert rules. Called periodically."""
        now = time.time()
        for rule in self._alert_rules.values():
            try:
                firing = rule.condition(self)
            except Exception as e:
                logger.warning(f"Alert rule '{rule.name}' evaluation error: {e}")
                continue

            if firing and not rule.firing:
                # New alert
                rule.firing = True
                if now - rule.last_fired >= rule.cooldown_s:
                    rule.last_fired = now
                    alert = Alert(
                        rule_name=rule.name, message=rule.message,
                        severity=rule.severity, fired_at=now
                    )
                    self._alert_history.append(alert)
                    logger.warning(f"ALERT [{rule.severity}] {rule.name}: {rule.message}")
                    for cb in self._alert_callbacks:
                        try:
                            if asyncio.iscoroutinefunction(cb):
                                await cb(alert)
                            else:
                                cb(alert)
                        except Exception as e:
                            logger.error(f"Alert callback error: {e}")

            elif not firing and rule.firing:
                # Alert resolved
                rule.firing = False
                for alert in reversed(self._alert_history):
                    if alert.rule_name == rule.name and alert.resolved_at is None:
                        alert.resolved_at = now
                        logger.info(f"Alert resolved: {rule.name}")
                        break

    def active_alerts(self) -> List[Alert]:
        return [a for a in self._alert_history if a.resolved_at is None]

    def alert_history(self, limit: int = 50) -> List[Dict]:
        return [a.to_dict() for a in reversed(self._alert_history[-limit:])]

    # ── Background evaluator ──────────────────────────────────────────────────

    async def start(self, alert_interval_s: float = 60.0):
        """Start periodic alert evaluation."""
        async def _loop():
            while True:
                await asyncio.sleep(alert_interval_s)
                await self.evaluate_alerts()

        self._eval_task = asyncio.create_task(_loop())
        logger.info(f"Metrics observability started (alert_interval={alert_interval_s}s)")

    async def stop(self):
        if self._eval_task:
            self._eval_task.cancel()
            try:
                await self._eval_task
            except asyncio.CancelledError:
                pass

    # ── aiohttp routes ────────────────────────────────────────────────────────

    def register_routes(self, app, prefix: str = ""):
        from aiohttp import web

        async def metrics_endpoint(request):
            text = self.to_prometheus()
            return web.Response(text=text,
                               content_type="text/plain; version=0.0.4")

        async def health_endpoint(request):
            results = await self.health.run_all()
            overall = self.health.overall_status(results)
            status_code = 200 if overall != HealthStatus.UNHEALTHY else 503
            return web.json_response({
                "status": overall,
                "checks": {k: v.to_dict() for k, v in results.items()},
                "uptime_s": round(time.time() - self._start_time, 1),
            }, status=status_code)

        async def ready_endpoint(request):
            results = await self.health.run_all()
            ready = self.health.is_ready(results)
            return web.Response(
                text="ready" if ready else "not ready",
                status=200 if ready else 503
            )

        async def alerts_endpoint(request):
            return web.json_response({
                "active": [a.to_dict() for a in self.active_alerts()],
                "history": self.alert_history(limit=20),
                "rules": list(self._alert_rules.keys()),
            })

        async def snapshot_endpoint(request):
            return web.json_response(self.snapshot())

        app.router.add_get(f"{prefix}/metrics",         metrics_endpoint)
        app.router.add_get(f"{prefix}/health",          health_endpoint)
        app.router.add_get(f"{prefix}/ready",           ready_endpoint)
        app.router.add_get(f"{prefix}/alerts",          alerts_endpoint)
        app.router.add_get(f"{prefix}/metrics/snapshot", snapshot_endpoint)
        logger.info(f"Observability routes registered at {prefix}/metrics, /health, /ready")


# Singleton for import convenience
metrics = MetricsRegistry()
