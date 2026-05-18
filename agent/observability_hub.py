"""OMNI Agent — Observability Hub: unified tracing, metrics, logs with correlation IDs."""
from __future__ import annotations
import json, sqlite3, threading, time, uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Generator, List, Optional


class LogLevel(str, Enum):
    DEBUG   = "debug"
    INFO    = "info"
    WARNING = "warning"
    ERROR   = "error"
    CRITICAL = "critical"


class SpanStatus(str, Enum):
    OK      = "ok"
    ERROR   = "error"
    TIMEOUT = "timeout"


LEVEL_RANK = {
    LogLevel.DEBUG: 0, LogLevel.INFO: 1, LogLevel.WARNING: 2,
    LogLevel.ERROR: 3, LogLevel.CRITICAL: 4,
}


@dataclass
class Span:
    span_id: str
    trace_id: str
    parent_id: Optional[str]
    name: str
    service: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    status: SpanStatus = SpanStatus.OK
    tags: Dict[str, Any] = field(default_factory=dict)
    logs: List[Dict] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def duration_ms(self) -> float:
        if self.finished_at:
            return (self.finished_at - self.started_at) * 1000
        return (time.time() - self.started_at) * 1000

    def finish(self, status: SpanStatus = SpanStatus.OK,
               error: Optional[str] = None):
        self.finished_at = time.time()
        self.status = status
        if error:
            self.error = error

    def set_tag(self, key: str, value: Any):
        self.tags[key] = value

    def log(self, message: str, level: str = "info", **kwargs):
        self.logs.append({"ts": time.time(), "level": level,
                          "msg": message, **kwargs})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "span_id": self.span_id,
            "trace_id": self.trace_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "service": self.service,
            "duration_ms": round(self.duration_ms, 2),
            "status": self.status.value,
            "tags": self.tags,
            "error": self.error,
        }


@dataclass
class LogEntry:
    entry_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    level: LogLevel = LogLevel.INFO
    message: str = ""
    service: str = ""
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    ts: float = field(default_factory=time.time)
    fields: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "level": self.level.value,
            "message": self.message,
            "service": self.service,
            "trace_id": self.trace_id,
            "ts": self.ts,
            **self.fields,
        }


class ObservabilityHub:
    """
    Unified observability hub combining:
    - Distributed tracing (spans + traces)
    - Structured logging with correlation IDs
    - In-process metrics (counters, gauges, histograms)
    - Context propagation via thread-local storage
    - Alert thresholds
    - SQLite persistence
    """

    def __init__(self, service: str = "omni",
                 min_log_level: LogLevel = LogLevel.DEBUG,
                 db_path: str = ":memory:"):
        self.service = service
        self.min_log_level = min_log_level
        self._spans: Dict[str, Span] = {}
        self._traces: Dict[str, List[str]] = {}     # trace_id → [span_ids]
        self._logs: List[LogEntry] = []
        self._metrics: Dict[str, List[float]] = {}  # name → values
        self._gauges: Dict[str, float] = {}
        self._counters: Dict[str, float] = {}
        self._alert_rules: List[Dict] = []
        self._alert_hooks: List[Callable] = []
        self._local = threading.local()
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._log_count = 0
        self._span_count = 0

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS ob_spans (
                span_id TEXT PRIMARY KEY, trace_id TEXT, parent_id TEXT,
                name TEXT, service TEXT, started_at REAL, finished_at REAL,
                status TEXT, error TEXT, tags TEXT
            );
            CREATE TABLE IF NOT EXISTS ob_logs (
                entry_id TEXT PRIMARY KEY, level TEXT, message TEXT,
                service TEXT, trace_id TEXT, ts REAL, fields TEXT
            );
            CREATE TABLE IF NOT EXISTS ob_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT, value REAL, ts REAL
            );
        """)
        self._db.commit()

    # ── TRACING ───────────────────────────────────────────────────────

    def start_span(self, name: str,
                   trace_id: Optional[str] = None,
                   parent_id: Optional[str] = None,
                   service: Optional[str] = None,
                   tags: Optional[Dict] = None) -> Span:
        # Inherit from thread-local context if available
        ctx_trace = getattr(self._local, "trace_id", None)
        ctx_span  = getattr(self._local, "span_id", None)
        tid = trace_id or ctx_trace or str(uuid.uuid4())
        pid = parent_id or ctx_span
        span = Span(
            span_id=str(uuid.uuid4())[:12],
            trace_id=tid, parent_id=pid,
            name=name, service=service or self.service,
            tags=dict(tags or {}))
        with self._lock:
            self._spans[span.span_id] = span
            self._traces.setdefault(tid, []).append(span.span_id)
            self._span_count += 1
        return span

    def finish_span(self, span: Span,
                    status: SpanStatus = SpanStatus.OK,
                    error: Optional[str] = None):
        span.finish(status, error)
        self._db.execute(
            "INSERT OR REPLACE INTO ob_spans VALUES (?,?,?,?,?,?,?,?,?,?)",
            (span.span_id, span.trace_id, span.parent_id, span.name,
             span.service, span.started_at, span.finished_at,
             span.status.value, span.error, json.dumps(span.tags)))
        self._db.commit()

    @contextmanager
    def span(self, name: str, **kwargs) -> Generator[Span, None, None]:
        """Context manager that auto-starts and finishes a span."""
        s = self.start_span(name, **kwargs)
        old_trace = getattr(self._local, "trace_id", None)
        old_span  = getattr(self._local, "span_id", None)
        self._local.trace_id = s.trace_id
        self._local.span_id  = s.span_id
        try:
            yield s
            self.finish_span(s, SpanStatus.OK)
        except Exception as exc:
            self.finish_span(s, SpanStatus.ERROR, str(exc))
            raise
        finally:
            self._local.trace_id = old_trace
            self._local.span_id  = old_span

    def get_trace(self, trace_id: str) -> List[Span]:
        with self._lock:
            sids = self._traces.get(trace_id, [])
            return [self._spans[sid] for sid in sids if sid in self._spans]

    def get_span(self, span_id: str) -> Optional[Span]:
        return self._spans.get(span_id)

    # ── LOGGING ───────────────────────────────────────────────────────

    def _log(self, level: LogLevel, message: str,
             service: Optional[str] = None, **fields):
        if LEVEL_RANK[level] < LEVEL_RANK[self.min_log_level]:
            return
        entry = LogEntry(
            level=level, message=message,
            service=service or self.service,
            trace_id=getattr(self._local, "trace_id", None),
            span_id=getattr(self._local, "span_id", None),
            fields=fields)
        with self._lock:
            self._logs.append(entry)
            self._log_count += 1
        self._db.execute(
            "INSERT INTO ob_logs VALUES (?,?,?,?,?,?,?)",
            (entry.entry_id, level.value, message,
             entry.service, entry.trace_id, entry.ts,
             json.dumps(fields)))
        self._db.commit()
        return entry

    def debug(self, msg: str, **kw):    return self._log(LogLevel.DEBUG, msg, **kw)
    def info(self, msg: str, **kw):     return self._log(LogLevel.INFO, msg, **kw)
    def warning(self, msg: str, **kw):  return self._log(LogLevel.WARNING, msg, **kw)
    def error(self, msg: str, **kw):    return self._log(LogLevel.ERROR, msg, **kw)
    def critical(self, msg: str, **kw): return self._log(LogLevel.CRITICAL, msg, **kw)

    def get_logs(self, level: Optional[LogLevel] = None,
                 service: Optional[str] = None,
                 trace_id: Optional[str] = None,
                 limit: int = 100) -> List[LogEntry]:
        logs = self._logs
        if level:
            logs = [l for l in logs if LEVEL_RANK[l.level] >= LEVEL_RANK[level]]
        if service:
            logs = [l for l in logs if l.service == service]
        if trace_id:
            logs = [l for l in logs if l.trace_id == trace_id]
        return logs[-limit:]

    # ── METRICS ───────────────────────────────────────────────────────

    def record_metric(self, name: str, value: float,
                       ts: Optional[float] = None):
        t = ts or time.time()
        with self._lock:
            self._metrics.setdefault(name, []).append(value)
        self._db.execute(
            "INSERT INTO ob_metrics (name,value,ts) VALUES (?,?,?)",
            (name, value, t))
        self._db.commit()
        self._check_metric_alerts(name, value)

    def gauge(self, name: str, value: float):
        self._gauges[name] = value
        self.record_metric(name, value)

    def increment(self, name: str, delta: float = 1.0) -> float:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0.0) + delta
        self.record_metric(name, self._counters[name])
        return self._counters[name]

    def metric_summary(self, name: str,
                        window_s: float = 3600) -> Dict[str, Any]:
        cutoff = time.time() - window_s
        rows = self._db.execute(
            "SELECT value FROM ob_metrics WHERE name=? AND ts>=?",
            (name, cutoff)).fetchall()
        vals = [r[0] for r in rows]
        if not vals:
            return {"name": name, "count": 0}
        return {
            "name": name,
            "count": len(vals),
            "avg": sum(vals) / len(vals),
            "min": min(vals),
            "max": max(vals),
            "last": vals[-1],
        }

    # ── ALERTS ────────────────────────────────────────────────────────

    def add_metric_alert(self, metric: str, condition: str,
                          threshold: float, cooldown_s: float = 60.0):
        self._alert_rules.append({
            "metric": metric, "condition": condition,
            "threshold": threshold, "cooldown_s": cooldown_s,
            "last_fired": 0.0,
        })

    def on_alert(self, fn: Callable):
        self._alert_hooks.append(fn)

    def _check_metric_alerts(self, name: str, value: float):
        now = time.time()
        for rule in self._alert_rules:
            if rule["metric"] != name:
                continue
            if now - rule["last_fired"] < rule["cooldown_s"]:
                continue
            c, t = rule["condition"], rule["threshold"]
            fired = (c == "gt" and value > t) or (c == "lt" and value < t) or \
                    (c == "gte" and value >= t) or (c == "lte" and value <= t) or \
                    (c == "eq" and value == t)
            if fired:
                rule["last_fired"] = now
                for fn in self._alert_hooks:
                    try: fn(name, value, rule)
                    except Exception: pass

    # ── CONTEXT PROPAGATION ───────────────────────────────────────────

    def set_context(self, trace_id: str, span_id: Optional[str] = None):
        self._local.trace_id = trace_id
        self._local.span_id  = span_id

    def clear_context(self):
        self._local.trace_id = None
        self._local.span_id  = None

    def current_trace_id(self) -> Optional[str]:
        return getattr(self._local, "trace_id", None)

    # ── STATS ─────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            error_spans = sum(1 for s in self._spans.values()
                              if s.status == SpanStatus.ERROR)
        return {
            "spans": self._span_count,
            "traces": len(self._traces),
            "logs": self._log_count,
            "error_spans": error_spans,
            "gauges": len(self._gauges),
            "counters": len(self._counters),
            "metric_series": len(self._metrics),
            "alert_rules": len(self._alert_rules),
        }
