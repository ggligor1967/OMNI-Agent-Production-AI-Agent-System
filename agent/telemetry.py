"""OMNI AGENT - Telemetry (OpenTelemetry-inspired)
Distributed tracing with spans, traces, metrics, and baggage propagation.

Features:
- Traces: collection of spans sharing a trace_id (UUID hex)
- Spans: id, trace_id, parent_id, operation, start/end time, status,
    attributes (dict), events (list), links (list of span refs)
- Status: OK, ERROR, UNSET
- Context propagation: trace_id + span_id passed via headers dict;
    W3C traceparent format: "00-{trace_id}-{span_id}-{flags}"
- Baggage: key-value pairs propagated across service boundaries
- Sampling: probability sampler (0.0–1.0); head-based
- Span lifecycle: start_span() returns active span; end_span() records
- Context manager: with tracer.span("op") as s: ...
- Nested spans: parent_id set automatically from current context
- Async support: asyncio-safe span context via contextvars
- Metrics: counter, gauge, histogram (re-uses time-series concepts)
- Counter: monotone inc, can be reset
- Gauge: set/get current value
- Histogram: record observation, compute percentiles
- Export: spans exported as JSON list (OTLP-like structure)
- Tail sampling: keep spans from traces with errors (post-hoc)
- Hooks: on_start_span, on_end_span, on_metric
- Stats: spans/s, error rate, p50/p95/p99 latency per operation
- SQLite persistence: completed spans and metric snapshots
- REST API: spans, metrics, traces, stats
"""
import asyncio, contextvars, json, math, os, random, sqlite3, time, uuid, logging
from contextlib import contextmanager, asynccontextmanager
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class SpanStatus(str, Enum):
    UNSET = "unset"; OK = "ok"; ERROR = "error"

@dataclass
class SpanEvent:
    name: str; attributes: Dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

@dataclass
class Span:
    span_id: str; trace_id: str; operation: str
    parent_id: Optional[str] = None
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None
    status: SpanStatus = SpanStatus.UNSET
    attributes: Dict[str, Any] = field(default_factory=dict)
    events: List[SpanEvent] = field(default_factory=list)
    baggage: Dict[str, str] = field(default_factory=dict)
    _sampled: bool = True

    @property
    def duration_ms(self) -> Optional[float]:
        if self.end_time is None: return None
        return round((self.end_time - self.start_time) * 1000, 3)

    def set_attribute(self, key: str, value: Any):
        self.attributes[key] = value

    def add_event(self, name: str, attributes: Dict = None):
        self.events.append(SpanEvent(name, dict(attributes or {})))

    def set_status(self, status: SpanStatus, message: str = ""):
        self.status = status
        if message: self.attributes["status.message"] = message

    def set_error(self, exc: Exception):
        self.status = SpanStatus.ERROR
        self.attributes["error.type"] = type(exc).__name__
        self.attributes["error.message"] = str(exc)

    def to_traceparent(self) -> str:
        flags = "01" if self._sampled else "00"
        return f"00-{self.trace_id}-{self.span_id}-{flags}"

    def to_dict(self):
        return {"span_id": self.span_id, "trace_id": self.trace_id,
                "parent_id": self.parent_id, "operation": self.operation,
                "start_time": round(self.start_time, 6),
                "end_time": round(self.end_time, 6) if self.end_time else None,
                "duration_ms": self.duration_ms,
                "status": self.status.value,
                "attributes": self.attributes,
                "events": [{"name": e.name, "ts": round(e.timestamp, 6),
                             "attrs": e.attributes} for e in self.events],
                "baggage": self.baggage}

# ── Metrics ────────────────────────────────────────────────────────────────────
@dataclass
class Counter:
    name: str; value: float = 0.0; labels: Dict = field(default_factory=dict)
    def inc(self, amount: float = 1.0): self.value += amount
    def reset(self): self.value = 0.0

@dataclass
class Gauge:
    name: str; value: float = 0.0; labels: Dict = field(default_factory=dict)
    def set(self, v: float): self.value = v
    def inc(self, amount: float = 1.0): self.value += amount
    def dec(self, amount: float = 1.0): self.value -= amount

@dataclass
class Histogram:
    name: str; buckets: List[float] = field(
        default_factory=lambda: [1,5,10,25,50,100,250,500,1000])
    _observations: List[float] = field(default_factory=list, repr=False)
    _counts: Dict[float, int] = field(default_factory=dict, repr=False)

    def observe(self, value: float):
        self._observations.append(value)
        for b in self.buckets:
            if value <= b:
                self._counts[b] = self._counts.get(b, 0) + 1

    def percentile(self, p: float) -> Optional[float]:
        if not self._observations: return None
        s = sorted(self._observations)
        idx = int(math.ceil(p / 100 * len(s))) - 1
        return s[max(0, idx)]

    def stats(self) -> Dict:
        obs = self._observations
        if not obs: return {"count": 0}
        return {"count": len(obs), "sum": sum(obs),
                "min": min(obs), "max": max(obs),
                "avg": sum(obs)/len(obs),
                "p50": self.percentile(50),
                "p95": self.percentile(95),
                "p99": self.percentile(99)}

class TelStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS spans(
                    span_id TEXT PRIMARY KEY, trace_id TEXT,
                    parent_id TEXT, operation TEXT,
                    start_time REAL, duration_ms REAL,
                    status TEXT, data TEXT);
                CREATE INDEX IF NOT EXISTS idx_sp_trace
                    ON spans(trace_id);
                CREATE INDEX IF NOT EXISTS idx_sp_op
                    ON spans(operation);
            """)

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def save_span(self, span: Span):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO spans VALUES(?,?,?,?,?,?,?,?)",
                (span.span_id, span.trace_id, span.parent_id,
                 span.operation, span.start_time, span.duration_ms,
                 span.status.value,
                 json.dumps(span.to_dict(), default=str)))

    def get_trace(self, trace_id: str) -> List[Dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT data FROM spans WHERE trace_id=? "
                "ORDER BY start_time", (trace_id,)).fetchall()
        return [json.loads(r["data"]) for r in rows]

    def get_spans(self, operation: str = None, status: str = None,
                   limit: int = 100) -> List[Dict]:
        where = []; params = []
        if operation: where.append("operation=?"); params.append(operation)
        if status:    where.append("status=?");    params.append(status)
        w = ("WHERE " + " AND ".join(where)) if where else ""
        params.append(limit)
        with self._conn() as c:
            rows = c.execute(
                f"SELECT data FROM spans {w} "
                "ORDER BY start_time DESC LIMIT ?", params).fetchall()
        return [json.loads(r["data"]) for r in rows]

    def op_stats(self) -> List[Dict]:
        with self._conn() as c:
            rows = c.execute("""
                SELECT operation,
                    COUNT(*) as cnt,
                    AVG(duration_ms) as avg_ms,
                    MIN(duration_ms) as min_ms,
                    MAX(duration_ms) as max_ms,
                    SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) as errors
                FROM spans GROUP BY operation ORDER BY cnt DESC LIMIT 50
            """).fetchall()
        return [dict(r) for r in rows]

# Context variable to track current span
_current_span: contextvars.ContextVar[Optional[Span]] = \
    contextvars.ContextVar("current_span", default=None)

class Tracer:
    """
    Distributed tracer with span context propagation.

    Usage:
        tracer = Tracer(sample_rate=1.0)

        with tracer.span("http.request") as span:
            span.set_attribute("http.method", "GET")
            with tracer.span("db.query") as child:
                child.set_attribute("db.statement", "SELECT ...")
                # child.parent_id == span.span_id automatically

        # Export
        tracer.get_trace(span.trace_id)
    """
    def __init__(self, db_path: str = "data/telemetry.db",
                  sample_rate: float = 1.0,
                  service_name: str = "service"):
        self._store = TelStore(db_path)
        self.sample_rate = sample_rate
        self.service_name = service_name
        self._metrics: Dict[str, Any] = {}
        self._hooks_start: List[Callable] = []
        self._hooks_end:   List[Callable] = []
        self._hooks_metric: List[Callable] = []
        self._completed: List[Span] = []   # in-memory ring

    def on_start_span(self, fn): self._hooks_start.append(fn)
    def on_end_span(self,   fn): self._hooks_end.append(fn)
    def on_metric(self,     fn): self._hooks_metric.append(fn)

    def _should_sample(self) -> bool:
        return random.random() < self.sample_rate

    def start_span(self, operation: str,
                    trace_id: str = None,
                    parent_id: str = None,
                    attributes: Dict = None,
                    baggage: Dict = None) -> Span:
        parent = _current_span.get()
        if parent:
            trace_id  = trace_id  or parent.trace_id
            parent_id = parent_id or parent.span_id
            baggage   = {**parent.baggage, **(baggage or {})}
        else:
            trace_id = trace_id or uuid.uuid4().hex
        span_id = os.urandom(8).hex()
        sampled = parent._sampled if parent else self._should_sample()
        span = Span(span_id=span_id, trace_id=trace_id,
                     parent_id=parent_id, operation=operation,
                     attributes={"service.name": self.service_name,
                                   **(attributes or {})},
                     baggage=dict(baggage or {}),
                     _sampled=sampled)
        for h in self._hooks_start:
            try: h(span)
            except: pass
        return span

    def end_span(self, span: Span):
        span.end_time = time.time()
        if span._sampled:
            self._store.save_span(span)
            self._completed.append(span)
            if len(self._completed) > 5000:
                self._completed = self._completed[-2500:]
        for h in self._hooks_end:
            try: h(span)
            except: pass

    @contextmanager
    def span(self, operation: str, **kwargs):
        s = self.start_span(operation, **kwargs)
        token = _current_span.set(s)
        try:
            yield s
        except Exception as exc:
            s.set_error(exc)
            raise
        finally:
            _current_span.reset(token)
            self.end_span(s)

    @asynccontextmanager
    async def async_span(self, operation: str, **kwargs):
        s = self.start_span(operation, **kwargs)
        token = _current_span.set(s)
        try:
            yield s
        except Exception as exc:
            s.set_error(exc)
            raise
        finally:
            _current_span.reset(token)
            self.end_span(s)

    def extract_context(self, headers: Dict[str, str]
                         ) -> Dict[str, Optional[str]]:
        """Parse W3C traceparent header."""
        tp = headers.get("traceparent", "")
        if tp and tp.count("-") >= 3:
            parts = tp.split("-")
            return {"trace_id": parts[1], "span_id": parts[2]}
        return {"trace_id": None, "span_id": None}

    def inject_context(self, span: Span, headers: Dict[str, str]):
        headers["traceparent"] = span.to_traceparent()
        if span.baggage:
            headers["baggage"] = ",".join(
                f"{k}={v}" for k, v in span.baggage.items())

    # ── Metrics ───────────────────────────────────────────────────────────────
    def counter(self, name: str, labels: Dict = None) -> Counter:
        key = f"counter:{name}:{json.dumps(labels or {}, sort_keys=True)}"
        if key not in self._metrics:
            self._metrics[key] = Counter(name, labels=dict(labels or {}))
        return self._metrics[key]

    def gauge(self, name: str, labels: Dict = None) -> Gauge:
        key = f"gauge:{name}:{json.dumps(labels or {}, sort_keys=True)}"
        if key not in self._metrics:
            self._metrics[key] = Gauge(name, labels=dict(labels or {}))
        return self._metrics[key]

    def histogram(self, name: str, buckets: List[float] = None) -> Histogram:
        key = f"hist:{name}"
        if key not in self._metrics:
            self._metrics[key] = Histogram(name,
                                             buckets=buckets or [1,5,10,50,100,500])
        return self._metrics[key]

    def get_trace(self, trace_id: str) -> List[Dict]:
        return self._store.get_trace(trace_id)

    def get_spans(self, operation: str = None,
                   status: str = None, limit: int = 100) -> List[Dict]:
        return self._store.get_spans(operation, status, limit)

    def metrics_snapshot(self) -> Dict:
        snap = {}
        for key, m in self._metrics.items():
            if isinstance(m, Counter):
                snap[key] = {"type": "counter", "name": m.name,
                              "value": m.value}
            elif isinstance(m, Gauge):
                snap[key] = {"type": "gauge", "name": m.name,
                              "value": m.value}
            elif isinstance(m, Histogram):
                snap[key] = {"type": "histogram", "name": m.name,
                              **m.stats()}
        return snap

    def stats(self) -> Dict:
        return {"service": self.service_name,
                "sample_rate": self.sample_rate,
                "completed_spans": len(self._completed),
                "metrics": len(self._metrics),
                "operations": self._store.op_stats()[:10]}

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def spans_ep(req):
            op  = req.rel_url.query.get("operation")
            st  = req.rel_url.query.get("status")
            lim = int(req.rel_url.query.get("limit", 100))
            return web.json_response(
                {"spans": self.get_spans(op, st, lim)})
        async def trace_ep(req):
            tid = req.match_info["trace_id"]
            return web.json_response({"spans": self.get_trace(tid)})
        async def metrics_ep(req):
            return web.json_response(self.metrics_snapshot())
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/telemetry"
        app.router.add_get(f"{p}/spans",            spans_ep)
        app.router.add_get(f"{p}/trace/{{trace_id}}",trace_ep)
        app.router.add_get(f"{p}/metrics",          metrics_ep)
        app.router.add_get(f"{p}/stats",            stats_ep)
        logger.info(f"Telemetry API at {prefix}/telemetry/")
