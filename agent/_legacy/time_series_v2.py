"""OMNI Agent — Time Series V2: storage, aggregation, anomaly detection, forecasting."""
from __future__ import annotations
import json, math, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class AggFunc(str, Enum):
    SUM    = "sum"
    AVG    = "avg"
    MIN    = "min"
    MAX    = "max"
    COUNT  = "count"
    LAST   = "last"
    FIRST  = "first"
    STDDEV = "stddev"
    P50    = "p50"
    P95    = "p95"
    P99    = "p99"


class AnomalyMethod(str, Enum):
    ZSCORE  = "zscore"
    IQR     = "iqr"
    MAD     = "mad"        # Median Absolute Deviation
    EWMA    = "ewma"       # Exponential Weighted Moving Average


@dataclass
class DataPoint:
    ts: float
    value: float
    tags: Dict[str, str] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"ts": self.ts, "value": self.value, "tags": self.tags}


@dataclass
class TimeSeries:
    series_id: str
    name: str
    unit: str = ""
    description: str = ""
    tags: Dict[str, str] = field(default_factory=dict)
    retention_s: Optional[float] = None   # auto-evict old points
    created_at: float = field(default_factory=time.time)
    _points: List[DataPoint] = field(default_factory=list, repr=False)

    def append(self, value: float,
               ts: Optional[float] = None,
               tags: Optional[Dict] = None) -> DataPoint:
        p = DataPoint(ts=time.time() if ts is None else ts,
                      value=value, tags=dict(tags or {}))
        self._points.append(p)
        self._points.sort(key=lambda x: x.ts)
        return p

    def values(self) -> List[float]:
        return [p.value for p in self._points]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "series_id": self.series_id,
            "name": self.name,
            "unit": self.unit,
            "points": len(self._points),
            "retention_s": self.retention_s,
        }


@dataclass
class AggWindow:
    start_ts: float
    end_ts: float
    value: float
    count: int
    func: str

    def to_dict(self) -> Dict[str, Any]:
        return {"start": self.start_ts, "end": self.end_ts,
                "value": round(self.value, 6), "count": self.count}


@dataclass
class Anomaly:
    ts: float
    value: float
    score: float
    method: AnomalyMethod
    series_id: str
    threshold: float = 3.0
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts, "value": self.value,
            "score": round(self.score, 4),
            "method": self.method.value,
            "series_id": self.series_id,
        }


class TimeSeriesV2:
    """
    Time series store with:
    - Named series with retention policies
    - Append single or batch points
    - Range queries (from_ts, to_ts, tag filter)
    - Aggregation windows: sum/avg/min/max/count/last/stddev/pXX
    - Downsampling (reduce to N buckets)
    - Rolling statistics (moving avg, moving stddev)
    - Anomaly detection: Z-score, IQR, MAD, EWMA
    - Simple forecasting (linear extrapolation, EWM)
    - Rate-of-change computation
    - SQLite persistence
    """

    def __init__(self, db_path: str = ":memory:"):
        self._series: Dict[str, TimeSeries] = {}
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS ts_series (
                series_id TEXT PRIMARY KEY, name TEXT, unit TEXT,
                description TEXT, tags TEXT, retention_s REAL, created_at REAL
            );
            CREATE TABLE IF NOT EXISTS ts_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id TEXT, ts REAL, value REAL, tags TEXT
            );
            CREATE INDEX IF NOT EXISTS ts_pts_idx ON ts_points(series_id, ts);
        """)
        self._db.commit()

    # ── SERIES MANAGEMENT ────────────────────────────────────────────

    def create_series(self, name: str,
                       unit: str = "",
                       description: str = "",
                       tags: Optional[Dict[str, str]] = None,
                       retention_s: Optional[float] = None,
                       series_id: Optional[str] = None) -> TimeSeries:
        sid = series_id or str(uuid.uuid4())[:8]
        s   = TimeSeries(series_id=sid, name=name, unit=unit,
                         description=description,
                         tags=dict(tags or {}),
                         retention_s=retention_s)
        self._series[sid] = s
        self._db.execute(
            "INSERT OR REPLACE INTO ts_series VALUES (?,?,?,?,?,?,?)",
            (sid, name, unit, description,
             json.dumps(tags or {}), retention_s, s.created_at))
        self._db.commit()
        return s

    def get_series(self, series_id: str) -> Optional[TimeSeries]:
        return self._series.get(series_id)

    def list_series(self) -> List[Dict]:
        return [s.to_dict() for s in self._series.values()]

    def delete_series(self, series_id: str):
        self._series.pop(series_id, None)
        self._db.execute("DELETE FROM ts_series WHERE series_id=?", (series_id,))
        self._db.execute("DELETE FROM ts_points WHERE series_id=?", (series_id,))
        self._db.commit()

    # ── WRITE ────────────────────────────────────────────────────────

    def append(self, series_id: str, value: float,
               ts: Optional[float] = None,
               tags: Optional[Dict[str, str]] = None) -> Optional[DataPoint]:
        s = self._series.get(series_id)
        if not s: return None
        p = s.append(value, ts, tags)
        self._db.execute(
            "INSERT INTO ts_points (series_id,ts,value,tags) VALUES (?,?,?,?)",
            (series_id, p.ts, p.value, json.dumps(p.tags)))
        self._db.commit()
        self._apply_retention(series_id)
        return p

    def append_batch(self, series_id: str,
                     points: List[Tuple[float, float]]) -> int:
        """Batch append: [(ts, value), ...]"""
        s = self._series.get(series_id)
        if not s: return 0
        rows = []
        for ts, val in points:
            p = s.append(val, ts)
            rows.append((series_id, p.ts, p.value, "{}"))
        self._db.executemany(
            "INSERT INTO ts_points (series_id,ts,value,tags) VALUES (?,?,?,?)",
            rows)
        self._db.commit()
        self._apply_retention(series_id)
        return len(rows)

    def _apply_retention(self, series_id: str):
        s = self._series.get(series_id)
        if not s or not s.retention_s: return
        cutoff = time.time() - s.retention_s
        s._points = [p for p in s._points if p.ts >= cutoff]
        self._db.execute(
            "DELETE FROM ts_points WHERE series_id=? AND ts<?",
            (series_id, cutoff))
        self._db.commit()

    # ── QUERY ────────────────────────────────────────────────────────

    def query(self, series_id: str,
              from_ts: Optional[float] = None,
              to_ts: Optional[float] = None,
              tag_filter: Optional[Dict[str, str]] = None,
              limit: int = 10000) -> List[DataPoint]:
        s = self._series.get(series_id)
        if not s: return []
        pts = s._points
        if from_ts: pts = [p for p in pts if p.ts >= from_ts]
        if to_ts:   pts = [p for p in pts if p.ts <= to_ts]
        if tag_filter:
            pts = [p for p in pts
                   if all(p.tags.get(k) == v for k, v in tag_filter.items())]
        return pts[:limit]

    def latest(self, series_id: str, n: int = 1) -> List[DataPoint]:
        s = self._series.get(series_id)
        if not s: return []
        return s._points[-n:]

    # ── AGGREGATION ──────────────────────────────────────────────────

    def _percentile(self, vals: List[float], p: float) -> float:
        if not vals: return 0.0
        sv = sorted(vals)
        idx = (len(sv) - 1) * p / 100
        lo  = int(idx); hi = lo + 1
        if hi >= len(sv): return sv[lo]
        return sv[lo] + (sv[hi] - sv[lo]) * (idx - lo)

    def _apply_agg(self, vals: List[float], func: AggFunc) -> float:
        if not vals: return 0.0
        if func == AggFunc.SUM:    return sum(vals)
        if func == AggFunc.AVG:    return sum(vals) / len(vals)
        if func == AggFunc.MIN:    return min(vals)
        if func == AggFunc.MAX:    return max(vals)
        if func == AggFunc.COUNT:  return float(len(vals))
        if func == AggFunc.LAST:   return vals[-1]
        if func == AggFunc.FIRST:  return vals[0]
        if func == AggFunc.STDDEV:
            m = sum(vals) / len(vals)
            return math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))
        if func == AggFunc.P50:    return self._percentile(vals, 50)
        if func == AggFunc.P95:    return self._percentile(vals, 95)
        if func == AggFunc.P99:    return self._percentile(vals, 99)
        return 0.0

    def aggregate(self, series_id: str,
                  window_s: float,
                  func: AggFunc = AggFunc.AVG,
                  from_ts: Optional[float] = None,
                  to_ts: Optional[float] = None) -> List[AggWindow]:
        pts = self.query(series_id, from_ts=from_ts, to_ts=to_ts)
        if not pts: return []
        start = pts[0].ts
        end   = pts[-1].ts
        results: List[AggWindow] = []
        t = start
        while t <= end + window_s:
            bucket = [p.value for p in pts if t <= p.ts < t + window_s]
            if bucket:
                results.append(AggWindow(
                    start_ts=t, end_ts=t + window_s,
                    value=self._apply_agg(bucket, func),
                    count=len(bucket), func=func.value))
            t += window_s
        return results

    def downsample(self, series_id: str,
                   n_buckets: int,
                   func: AggFunc = AggFunc.AVG) -> List[AggWindow]:
        pts = self.query(series_id)
        if not pts or n_buckets <= 0: return []
        total_span = pts[-1].ts - pts[0].ts
        if total_span <= 0: return []
        window_s = total_span / n_buckets
        return self.aggregate(series_id, window_s, func)

    # ── ROLLING STATS ────────────────────────────────────────────────

    def rolling(self, series_id: str,
                window: int,
                func: AggFunc = AggFunc.AVG) -> List[Tuple[float, float]]:
        pts = self.query(series_id)
        if not pts: return []
        result = []
        for i in range(window - 1, len(pts)):
            bucket = [pts[j].value for j in range(i - window + 1, i + 1)]
            result.append((pts[i].ts, self._apply_agg(bucket, func)))
        return result

    def rate_of_change(self, series_id: str) -> List[Tuple[float, float]]:
        pts = self.query(series_id)
        if len(pts) < 2: return []
        roc = []
        for i in range(1, len(pts)):
            dt = pts[i].ts - pts[i - 1].ts
            dv = pts[i].value - pts[i - 1].value
            roc.append((pts[i].ts, dv / dt if dt > 0 else 0.0))
        return roc

    # ── ANOMALY DETECTION ────────────────────────────────────────────

    def detect_anomalies(self, series_id: str,
                          method: AnomalyMethod = AnomalyMethod.ZSCORE,
                          threshold: float = 3.0,
                          window: Optional[int] = None) -> List[Anomaly]:
        pts = self.query(series_id)
        if len(pts) < 3: return []
        vals = [p.value for p in pts]
        anomalies: List[Anomaly] = []

        if method == AnomalyMethod.ZSCORE:
            mean = sum(vals) / len(vals)
            std  = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))
            if std == 0: return []
            for p in pts:
                score = abs((p.value - mean) / std)
                if score > threshold:
                    anomalies.append(Anomaly(ts=p.ts, value=p.value,
                                             score=score, method=method,
                                             series_id=series_id,
                                             threshold=threshold))

        elif method == AnomalyMethod.IQR:
            sv  = sorted(vals)
            q1  = self._percentile(sv, 25)
            q3  = self._percentile(sv, 75)
            iqr = q3 - q1
            lo  = q1 - threshold * iqr
            hi  = q3 + threshold * iqr
            for p in pts:
                if p.value < lo or p.value > hi:
                    score = max(abs(p.value - lo), abs(p.value - hi)) / (iqr or 1)
                    anomalies.append(Anomaly(ts=p.ts, value=p.value,
                                             score=score, method=method,
                                             series_id=series_id))

        elif method == AnomalyMethod.MAD:
            median = self._percentile(sorted(vals), 50)
            mad = self._percentile(sorted(abs(v - median) for v in vals), 50)
            if mad == 0: return []
            for p in pts:
                score = 0.6745 * abs(p.value - median) / mad
                if score > threshold:
                    anomalies.append(Anomaly(ts=p.ts, value=p.value,
                                             score=score, method=method,
                                             series_id=series_id))

        elif method == AnomalyMethod.EWMA:
            alpha = 0.3
            ewma  = vals[0]
            ewmv  = 0.0
            for p, v in zip(pts, vals):
                ewmv  = (1 - alpha) * (ewmv + alpha * (v - ewma) ** 2)
                ewma  = alpha * v + (1 - alpha) * ewma
                std   = math.sqrt(ewmv) if ewmv > 0 else 0.0
                if std > 0:
                    score = abs(v - ewma) / std
                    if score > threshold:
                        anomalies.append(Anomaly(ts=p.ts, value=p.value,
                                                  score=score, method=method,
                                                  series_id=series_id))

        return anomalies

    # ── FORECAST ─────────────────────────────────────────────────────

    def forecast_linear(self, series_id: str,
                         steps: int,
                         step_s: float = 60.0) -> List[Tuple[float, float]]:
        pts = self.query(series_id)
        if len(pts) < 2: return []
        n   = len(pts)
        xs  = [p.ts for p in pts]
        ys  = [p.value for p in pts]
        x_mean = sum(xs) / n
        y_mean = sum(ys) / n
        num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
        den = sum((x - x_mean) ** 2 for x in xs)
        slope = num / den if den else 0.0
        intercept = y_mean - slope * x_mean
        last_ts = xs[-1]
        return [(last_ts + (i + 1) * step_s,
                 slope * (last_ts + (i + 1) * step_s) + intercept)
                for i in range(steps)]

    def forecast_ewm(self, series_id: str,
                      steps: int, alpha: float = 0.3) -> List[Tuple[float, float]]:
        pts = self.query(series_id)
        if not pts: return []
        ewma = pts[0].value
        for p in pts:
            ewma = alpha * p.value + (1 - alpha) * ewma
        last_ts = pts[-1].ts
        step_s  = ((pts[-1].ts - pts[0].ts) / len(pts)) if len(pts) > 1 else 60.0
        return [(last_ts + (i + 1) * step_s, ewma) for i in range(steps)]

    def stats(self) -> Dict[str, Any]:
        total_pts = sum(len(s._points) for s in self._series.values())
        return {
            "series": len(self._series),
            "total_points": total_pts,
        }
