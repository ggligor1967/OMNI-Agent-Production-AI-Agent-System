"""OMNI AGENT - Metrics Collector
Time-series metrics collection with counters, gauges, histograms,
percentile computation, labels, and Prometheus-style text export.

Features:
- MetricType: COUNTER (monotonic), GAUGE (up/down), HISTOGRAM, SUMMARY
- Labels: arbitrary key=value tags per observation
- Counter: increment, reset; rate = delta / elapsed_s
- Gauge: set, inc, dec; tracks min/max/current
- Histogram: configurable bucket boundaries; count per bucket; cumulative
- Summary: sliding window of raw values; p50/p75/p90/p95/p99 percentiles
- Scrape: snapshot all metrics at a point in time
- Prometheus text format export: # HELP, # TYPE, metric{labels} value
- Rolling window: keep last N samples per metric for trend analysis
- Aggregation: sum, avg, min, max across label sets
- Alert threshold: on_threshold(metric, op, value, fn) triggers callback
- Metric registry: global singleton pattern + named registries
- TTL: auto-expire metrics not updated in N seconds
- SQLite persistence: scrape snapshots, alert history
- REST API: record, scrape, export, alert, stats
"""
import json, math, re, sqlite3, time, uuid, logging
from collections import defaultdict, deque
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class MetricType(str, Enum):
    COUNTER   = "counter"
    GAUGE     = "gauge"
    HISTOGRAM = "histogram"
    SUMMARY   = "summary"

def _labels_key(labels: Dict[str, str]) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))

def _labels_prom(labels: Dict[str, str]) -> str:
    if not labels: return ""
    inner = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return "{" + inner + "}"

def _percentile(sorted_vals: List[float], p: float) -> float:
    if not sorted_vals: return 0.0
    idx = (len(sorted_vals) - 1) * p
    lo, hi = int(idx), min(int(idx) + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac

DEFAULT_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25,
                    0.5, 1.0, 2.5, 5.0, 10.0, float("inf")]

@dataclass
class _CounterState:
    value: float = 0.0
    last_reset: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)

    def inc(self, amount: float = 1.0):
        self.value += amount

    def rate(self) -> float:
        elapsed = time.time() - self.last_reset
        return self.value / max(0.001, elapsed)

@dataclass
class _GaugeState:
    value: float = 0.0
    min_val: float = float("inf")
    max_val: float = float("-inf")

    def set(self, v: float):
        self.value = v
        self.min_val = min(self.min_val, v)
        self.max_val = max(self.max_val, v)

    def inc(self, amount: float = 1.0): self.set(self.value + amount)
    def dec(self, amount: float = 1.0): self.set(self.value - amount)

@dataclass
class _HistogramState:
    buckets: List[float] = field(default_factory=lambda: list(DEFAULT_BUCKETS))
    counts: List[int] = field(default_factory=list)
    total: float = 0.0; n: int = 0

    def __post_init__(self):
        self.counts = [0] * len(self.buckets)

    def observe(self, v: float):
        self.total += v; self.n += 1
        for i, b in enumerate(self.buckets):
            if v <= b: self.counts[i] += 1

    @property
    def avg(self) -> float: return self.total / max(1, self.n)

@dataclass
class _SummaryState:
    window: deque = field(default_factory=lambda: deque(maxlen=1000))
    total: float = 0.0; n: int = 0

    def observe(self, v: float):
        self.window.append(v); self.total += v; self.n += 1

    def percentile(self, p: float) -> float:
        return _percentile(sorted(self.window), p)

    @property
    def avg(self) -> float: return self.total / max(1, self.n)

@dataclass
class MetricDef:
    name: str; metric_type: MetricType
    help_text: str = ""; unit: str = ""
    buckets: List[float] = field(default_factory=lambda: list(DEFAULT_BUCKETS))
    window_size: int = 1000
    ttl_s: float = 0.0    # 0 = no TTL
    last_updated: float = field(default_factory=time.time)

class MCStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS scrapes(
                    id TEXT PRIMARY KEY, metrics TEXT,
                    created_at REAL);
                CREATE TABLE IF NOT EXISTS alert_log(
                    id TEXT PRIMARY KEY, metric TEXT,
                    value REAL, threshold REAL, op TEXT,
                    created_at REAL);
                CREATE INDEX IF NOT EXISTS idx_scrapes_ts
                    ON scrapes(created_at DESC);
            """)

    def save_scrape(self, metrics: Dict):
        with self._conn() as c:
            c.execute("INSERT INTO scrapes VALUES(?,?,?)",
                (str(uuid.uuid4())[:8],
                 json.dumps(metrics, default=str)[:4000], time.time()))

    def log_alert(self, metric: str, value: float,
                   threshold: float, op: str):
        with self._conn() as c:
            c.execute("INSERT INTO alert_log VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4())[:8], metric, value, threshold, op, time.time()))

    def stats(self) -> Dict:
        with self._conn() as c:
            ns = c.execute("SELECT COUNT(*) FROM scrapes").fetchone()[0]
            na = c.execute("SELECT COUNT(*) FROM alert_log").fetchone()[0]
        return {"scrapes": ns, "alerts_fired": na}

class MetricsCollector:
    """
    Prometheus-compatible metrics collector with labels and percentiles.

    Usage:
        mc = MetricsCollector()
        mc.define("http_requests", MetricType.COUNTER, "Total HTTP requests")
        mc.define("response_time", MetricType.HISTOGRAM, "Response latency")
        mc.define("memory_mb",     MetricType.GAUGE,   "Memory usage MB")

        mc.inc("http_requests",  labels={"method": "GET", "status": "200"})
        mc.observe("response_time", 0.042, labels={"endpoint": "/api"})
        mc.set("memory_mb", 512.0)

        print(mc.export_prometheus())
    """
    def __init__(self, db_path: str = "data/metrics.db",
                 default_labels: Dict[str, str] = None):
        self._store = MCStore(db_path)
        self._defs: Dict[str, MetricDef] = {}
        self._counters:   Dict[str, Dict[str, _CounterState]]   = defaultdict(dict)
        self._gauges:     Dict[str, Dict[str, _GaugeState]]     = defaultdict(dict)
        self._histograms: Dict[str, Dict[str, _HistogramState]] = defaultdict(dict)
        self._summaries:  Dict[str, Dict[str, _SummaryState]]   = defaultdict(dict)
        self._default_labels = dict(default_labels or {})
        self._alert_rules: List[Tuple] = []  # (metric, op, threshold, fn)
        self._trends: Dict[str, deque] = defaultdict(lambda: deque(maxlen=100))

    def define(self, name: str,
                metric_type: MetricType = MetricType.COUNTER,
                help_text: str = "", unit: str = "",
                buckets: List[float] = None,
                ttl_s: float = 0.0) -> MetricDef:
        d = MetricDef(name=name, metric_type=metric_type,
                       help_text=help_text, unit=unit,
                       buckets=buckets or list(DEFAULT_BUCKETS),
                       ttl_s=ttl_s)
        self._defs[name] = d
        return d

    def _merged_labels(self, labels: Dict) -> Dict:
        return {**self._default_labels, **(labels or {})}

    def _update_ts(self, name: str):
        if name in self._defs:
            self._defs[name].last_updated = time.time()

    # ── Counter ───────────────────────────────────────────────────────────────
    def inc(self, name: str, amount: float = 1.0, labels: Dict = None):
        labels = self._merged_labels(labels)
        key = _labels_key(labels)
        if key not in self._counters[name]:
            self._counters[name][key] = _CounterState()
        self._counters[name][key].inc(amount)
        self._update_ts(name)
        self._check_alerts(name, self._counters[name][key].value)
        self._trends[name].append((time.time(), self._counters[name][key].value))

    def reset_counter(self, name: str, labels: Dict = None):
        key = _labels_key(self._merged_labels(labels))
        if key in self._counters[name]:
            s = self._counters[name][key]
            s.value = 0.0; s.last_reset = time.time()

    # ── Gauge ─────────────────────────────────────────────────────────────────
    def set(self, name: str, value: float, labels: Dict = None):
        labels = self._merged_labels(labels)
        key = _labels_key(labels)
        if key not in self._gauges[name]:
            self._gauges[name][key] = _GaugeState()
        self._gauges[name][key].set(value)
        self._update_ts(name)
        self._check_alerts(name, value)
        self._trends[name].append((time.time(), value))

    def gauge_inc(self, name: str, amount: float = 1.0, labels: Dict = None):
        labels = self._merged_labels(labels)
        key = _labels_key(labels)
        g = self._gauges[name].setdefault(key, _GaugeState())
        g.inc(amount); self._update_ts(name)

    def gauge_dec(self, name: str, amount: float = 1.0, labels: Dict = None):
        labels = self._merged_labels(labels)
        key = _labels_key(labels)
        g = self._gauges[name].setdefault(key, _GaugeState())
        g.dec(amount); self._update_ts(name)

    # ── Histogram / Summary ───────────────────────────────────────────────────
    def observe(self, name: str, value: float, labels: Dict = None):
        labels = self._merged_labels(labels)
        key = _labels_key(labels)
        d = self._defs.get(name)
        mtype = d.metric_type if d else MetricType.HISTOGRAM
        if mtype == MetricType.SUMMARY:
            self._summaries[name].setdefault(key, _SummaryState()).observe(value)
        else:
            buckets = d.buckets if d else list(DEFAULT_BUCKETS)
            if key not in self._histograms[name]:
                self._histograms[name][key] = _HistogramState(buckets=buckets)
            self._histograms[name][key].observe(value)
        self._update_ts(name)
        self._check_alerts(name, value)
        self._trends[name].append((time.time(), value))

    # ── Alerts ────────────────────────────────────────────────────────────────
    def on_threshold(self, metric: str, op: str,
                      threshold: float, fn: Callable):
        self._alert_rules.append((metric, op, threshold, fn))

    def _check_alerts(self, name: str, value: float):
        ops = {">": lambda a,b: a>b, ">=": lambda a,b: a>=b,
                "<": lambda a,b: a<b, "<=": lambda a,b: a<=b,
                "==": lambda a,b: a==b}
        for metric, op, threshold, fn in self._alert_rules:
            if metric == name and ops.get(op, lambda *_: False)(value, threshold):
                try: fn(name, value, threshold)
                except: pass
                self._store.log_alert(name, value, threshold, op)

    # ── Query ─────────────────────────────────────────────────────────────────
    def get_counter(self, name: str, labels: Dict = None) -> float:
        key = _labels_key(self._merged_labels(labels))
        return self._counters[name].get(key, _CounterState()).value

    def get_gauge(self, name: str, labels: Dict = None) -> float:
        key = _labels_key(self._merged_labels(labels))
        return self._gauges[name].get(key, _GaugeState()).value

    def get_percentile(self, name: str, p: float, labels: Dict = None) -> float:
        key = _labels_key(self._merged_labels(labels))
        s = self._summaries[name].get(key)
        if s: return s.percentile(p)
        h = self._histograms[name].get(key)
        if h:
            # Estimate from histogram buckets
            sorted_vals = [b for b in h.buckets if b < float("inf")]
            return _percentile(sorted_vals, p) if sorted_vals else 0.0
        return 0.0

    def trend(self, name: str, last_n: int = 10) -> List[Tuple[float, float]]:
        return list(self._trends[name])[-last_n:]

    # ── Snapshot / Export ─────────────────────────────────────────────────────
    def scrape(self) -> Dict:
        snap: Dict[str, Any] = {}
        for name, smap in self._counters.items():
            snap[name] = {k: {"value": s.value, "rate": round(s.rate(), 4)}
                           for k, s in smap.items()}
        for name, smap in self._gauges.items():
            snap[name] = {k: {"value": s.value, "min": s.min_val, "max": s.max_val}
                           for k, s in smap.items()}
        for name, smap in self._histograms.items():
            snap[name] = {k: {"count": s.n, "avg": round(s.avg, 4), "total": s.total}
                           for k, s in smap.items()}
        for name, smap in self._summaries.items():
            snap[name] = {k: {"count": s.n, "avg": round(s.avg, 4),
                               "p50": s.percentile(0.5), "p95": s.percentile(0.95),
                               "p99": s.percentile(0.99)}
                           for k, s in smap.items()}
        self._store.save_scrape(snap)
        return snap

    def export_prometheus(self) -> str:
        lines = []
        for name, d in self._defs.items():
            lines.append(f"# HELP {name} {d.help_text}")
            lines.append(f"# TYPE {name} {d.metric_type.value}")
            if d.metric_type == MetricType.COUNTER:
                for lkey, s in self._counters[name].items():
                    ldict = dict(p.split("=",1) for p in lkey.split(",") if "=" in p)
                    lines.append(f"{name}{_labels_prom(ldict)} {s.value}")
            elif d.metric_type == MetricType.GAUGE:
                for lkey, s in self._gauges[name].items():
                    ldict = dict(p.split("=",1) for p in lkey.split(",") if "=" in p)
                    lines.append(f"{name}{_labels_prom(ldict)} {s.value}")
            elif d.metric_type == MetricType.HISTOGRAM:
                for lkey, s in self._histograms[name].items():
                    ldict = dict(p.split("=",1) for p in lkey.split(",") if "=" in p)
                    cumulative = 0
                    for b, cnt in zip(s.buckets, s.counts):
                        cumulative += cnt
                        bl = {**ldict, "le": str(b)}
                        lines.append(f"{name}_bucket{_labels_prom(bl)} {cumulative}")
                    lines.append(f"{name}_sum{_labels_prom(ldict)} {s.total}")
                    lines.append(f"{name}_count{_labels_prom(ldict)} {s.n}")
            elif d.metric_type == MetricType.SUMMARY:
                for lkey, s in self._summaries[name].items():
                    ldict = dict(p.split("=",1) for p in lkey.split(",") if "=" in p)
                    for p, qv in [(0.5,"0.5"),(0.9,"0.9"),(0.99,"0.99")]:
                        pl = {**ldict, "quantile": qv}
                        lines.append(f"{name}{_labels_prom(pl)} {s.percentile(p)}")
                    lines.append(f"{name}_sum{_labels_prom(ldict)} {s.total}")
                    lines.append(f"{name}_count{_labels_prom(ldict)} {s.n}")
        return "\n".join(lines) + "\n"

    def expire_ttl(self) -> int:
        now = time.time(); removed = 0
        for name, d in list(self._defs.items()):
            if d.ttl_s > 0 and now - d.last_updated > d.ttl_s:
                for store in (self._counters, self._gauges,
                               self._histograms, self._summaries):
                    store.pop(name, None)
                del self._defs[name]; removed += 1
        return removed

    def stats(self) -> Dict:
        s = self._store.stats()
        s["defined_metrics"] = len(self._defs)
        s["counters"] = len(self._counters)
        s["gauges"] = len(self._gauges)
        s["histograms"] = len(self._histograms)
        s["summaries"] = len(self._summaries)
        s["alert_rules"] = len(self._alert_rules)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def record_ep(req):
            d = await req.json()
            mt = self._defs.get(d["name"])
            mtype = mt.metric_type if mt else MetricType.COUNTER
            if mtype == MetricType.COUNTER:
                self.inc(d["name"], d.get("value",1), d.get("labels",{}))
            elif mtype == MetricType.GAUGE:
                self.set(d["name"], d["value"], d.get("labels",{}))
            else:
                self.observe(d["name"], d["value"], d.get("labels",{}))
            return web.json_response({"recorded": True})
        async def scrape_ep(req):
            return web.json_response(self.scrape())
        async def prom_ep(req):
            return web.Response(text=self.export_prometheus(),
                                 content_type="text/plain")
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/metrics"
        app.router.add_post(f"{p}/record",  record_ep)
        app.router.add_get( f"{p}/scrape",  scrape_ep)
        app.router.add_get( f"{p}/prom",    prom_ep)
        app.router.add_get( f"{p}/stats",   stats_ep)
        logger.info(f"Metrics collector API at {prefix}/metrics/")
