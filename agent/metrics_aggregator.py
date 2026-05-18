"""OMNI Agent — Metrics Aggregator: time-series metrics with windowed aggregation and alerts."""
from __future__ import annotations
import math, sqlite3, threading, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class MetricType(str, Enum):
    COUNTER   = "counter"     # monotonically increasing
    GAUGE     = "gauge"       # current value
    HISTOGRAM = "histogram"   # distribution
    TIMER     = "timer"       # duration measurements
    RATE      = "rate"        # events per second


class AggFunc(str, Enum):
    SUM   = "sum"
    AVG   = "avg"
    MIN   = "min"
    MAX   = "max"
    COUNT = "count"
    P50   = "p50"
    P95   = "p95"
    P99   = "p99"
    LAST  = "last"
    RATE  = "rate"


@dataclass
class MetricSample:
    metric_id: str
    name: str
    value: float
    labels: Dict[str, str] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {"metric_id": self.metric_id, "name": self.name,
                "value": self.value, "labels": self.labels, "ts": self.ts}


@dataclass
class AlertRule:
    rule_id: str
    metric_name: str
    condition: str      # gt|lt|gte|lte|eq
    threshold: float
    window_s: float = 60.0
    agg_fn: AggFunc = AggFunc.AVG
    enabled: bool = True
    cooldown_s: float = 60.0
    last_fired: float = 0.0


def _percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _apply_agg(values: List[float], fn: AggFunc,
               window_s: float = 1.0) -> float:
    if not values:
        return 0.0
    if fn == AggFunc.SUM:   return sum(values)
    if fn == AggFunc.AVG:   return sum(values) / len(values)
    if fn == AggFunc.MIN:   return min(values)
    if fn == AggFunc.MAX:   return max(values)
    if fn == AggFunc.COUNT: return float(len(values))
    if fn == AggFunc.P50:   return _percentile(values, 50)
    if fn == AggFunc.P95:   return _percentile(values, 95)
    if fn == AggFunc.P99:   return _percentile(values, 99)
    if fn == AggFunc.LAST:  return values[-1]
    if fn == AggFunc.RATE:  return len(values) / window_s if window_s > 0 else 0.0
    return 0.0


class MetricsAggregator:
    """
    Time-series metrics aggregation system:
    - Record gauges, counters, histograms, timers
    - Windowed aggregation (sum/avg/min/max/p50/p95/p99/rate)
    - Label-based filtering
    - Alert rules with cooldown
    - Rollup: downsample to minute/hour buckets
    - SQLite persistence
    """

    def __init__(self, retention_s: float = 3600.0,
                 db_path: str = ":memory:"):
        self.retention_s = retention_s
        self._samples: List[MetricSample] = []
        self._gauges: Dict[str, float] = {}
        self._counters: Dict[str, float] = {}
        self._alerts: Dict[str, AlertRule] = {}
        self._alert_hooks: List[Callable] = []
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._record_count = 0

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS ma_samples (
                metric_id TEXT PRIMARY KEY, name TEXT, value REAL,
                labels TEXT, ts REAL
            );
            CREATE TABLE IF NOT EXISTS ma_rollups (
                name TEXT, bucket REAL, agg TEXT, value REAL,
                PRIMARY KEY (name, bucket, agg)
            );
            CREATE TABLE IF NOT EXISTS ma_alert_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id TEXT, metric_name TEXT, value REAL, ts REAL
            );
        """)
        self._db.commit()

    # ── RECORD ────────────────────────────────────────────────────────

    def record(self, name: str, value: float,
               labels: Optional[Dict[str, str]] = None,
               ts: Optional[float] = None) -> MetricSample:
        with self._lock:
            sample = MetricSample(
                metric_id=str(uuid.uuid4()),
                name=name, value=value,
                labels=labels or {},
                ts=ts or time.time())
            self._samples.append(sample)
            self._record_count += 1
            self._prune()
        self._check_alerts(name)
        self._db.execute(
            "INSERT OR REPLACE INTO ma_samples VALUES (?,?,?,?,?)",
            (sample.metric_id, name, value,
             str(labels or {}), sample.ts))
        self._db.commit()
        return sample

    def gauge(self, name: str, value: float,
              labels: Optional[Dict[str, str]] = None) -> MetricSample:
        with self._lock:
            self._gauges[name] = value
        return self.record(name, value, labels)

    def increment(self, name: str, delta: float = 1.0,
                  labels: Optional[Dict[str, str]] = None) -> float:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0.0) + delta
            val = self._counters[name]
        self.record(name, val, labels)
        return val

    def timer(self, name: str) -> "_TimerContext":
        return _TimerContext(self, name)

    def _prune(self):
        cutoff = time.time() - self.retention_s
        self._samples = [s for s in self._samples if s.ts >= cutoff]

    # ── QUERY ─────────────────────────────────────────────────────────

    def query(self, name: str,
              agg_fn: AggFunc = AggFunc.AVG,
              window_s: float = 60.0,
              labels: Optional[Dict[str, str]] = None,
              since_ts: Optional[float] = None) -> float:
        cutoff = since_ts or (time.time() - window_s)
        with self._lock:
            samples = [s for s in self._samples
                       if s.name == name and s.ts >= cutoff]
        if labels:
            samples = [s for s in samples
                       if all(s.labels.get(k) == v for k, v in labels.items())]
        values = [s.value for s in samples]
        return _apply_agg(values, agg_fn, window_s)

    def query_range(self, name: str,
                    start_ts: float, end_ts: float,
                    bucket_s: float = 60.0,
                    agg_fn: AggFunc = AggFunc.AVG) -> List[Dict[str, Any]]:
        """Return time-bucketed aggregations."""
        with self._lock:
            samples = [s for s in self._samples
                       if s.name == name and start_ts <= s.ts <= end_ts]
        buckets: Dict[float, List[float]] = {}
        for s in samples:
            bk = math.floor(s.ts / bucket_s) * bucket_s
            buckets.setdefault(bk, []).append(s.value)
        return [{"ts": bk, "value": _apply_agg(vs, agg_fn, bucket_s),
                 "count": len(vs)}
                for bk, vs in sorted(buckets.items())]

    def current_gauge(self, name: str) -> Optional[float]:
        return self._gauges.get(name)

    def current_counter(self, name: str) -> float:
        return self._counters.get(name, 0.0)

    def metric_names(self) -> List[str]:
        with self._lock:
            return list({s.name for s in self._samples})

    def latest(self, name: str, n: int = 10) -> List[MetricSample]:
        with self._lock:
            relevant = [s for s in self._samples if s.name == name]
        return relevant[-n:]

    # ── ROLLUPS ───────────────────────────────────────────────────────

    def rollup(self, name: str, bucket_s: float = 60.0,
               agg_fn: AggFunc = AggFunc.AVG) -> List[Dict[str, Any]]:
        now = time.time()
        result = self.query_range(name, now - self.retention_s, now,
                                  bucket_s, agg_fn)
        for row in result:
            self._db.execute(
                "INSERT OR REPLACE INTO ma_rollups VALUES (?,?,?,?)",
                (name, row["ts"], agg_fn.value, row["value"]))
        self._db.commit()
        return result

    # ── ALERTS ────────────────────────────────────────────────────────

    def add_alert(self, metric_name: str, condition: str,
                  threshold: float, window_s: float = 60.0,
                  agg_fn: AggFunc = AggFunc.AVG,
                  cooldown_s: float = 60.0,
                  rule_id: Optional[str] = None) -> AlertRule:
        rid  = rule_id or str(uuid.uuid4())[:8]
        rule = AlertRule(rule_id=rid, metric_name=metric_name,
                         condition=condition, threshold=threshold,
                         window_s=window_s, agg_fn=agg_fn,
                         cooldown_s=cooldown_s)
        self._alerts[rid] = rule
        return rule

    def remove_alert(self, rule_id: str):
        self._alerts.pop(rule_id, None)

    def on_alert(self, fn: Callable[[AlertRule, float], None]):
        self._alert_hooks.append(fn)

    def _check_alerts(self, metric_name: str):
        for rule in self._alerts.values():
            if not rule.enabled or rule.metric_name != metric_name:
                continue
            now = time.time()
            if now - rule.last_fired < rule.cooldown_s:
                continue
            val = self.query(metric_name, rule.agg_fn, rule.window_s)
            fired = False
            if rule.condition == "gt"  and val >  rule.threshold: fired = True
            if rule.condition == "lt"  and val <  rule.threshold: fired = True
            if rule.condition == "gte" and val >= rule.threshold: fired = True
            if rule.condition == "lte" and val <= rule.threshold: fired = True
            if rule.condition == "eq"  and val == rule.threshold: fired = True
            if fired:
                rule.last_fired = now
                self._db.execute(
                    "INSERT INTO ma_alert_events (rule_id,metric_name,value,ts) "
                    "VALUES (?,?,?,?)",
                    (rule.rule_id, metric_name, val, now))
                self._db.commit()
                for fn in self._alert_hooks:
                    try: fn(rule, val)
                    except Exception: pass

    def alert_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._db.execute(
            "SELECT rule_id,metric_name,value,ts FROM ma_alert_events "
            "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [{"rule_id": r[0], "metric": r[1],
                 "value": r[2], "ts": r[3]} for r in rows]

    # ── STATS ─────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            n = len(self._samples)
        return {
            "samples_in_window": n,
            "total_recorded": self._record_count,
            "gauges": len(self._gauges),
            "counters": len(self._counters),
            "alert_rules": len(self._alerts),
            "retention_s": self.retention_s,
        }


class _TimerContext:
    def __init__(self, agg: "MetricsAggregator", name: str):
        self._agg = agg; self._name = name; self._start = 0.0
    def __enter__(self):
        self._start = time.time(); return self
    def __exit__(self, *_):
        ms = (time.time() - self._start) * 1000
        self._agg.record(self._name, ms)
    def elapsed_ms(self) -> float:
        return (time.time() - self._start) * 1000
