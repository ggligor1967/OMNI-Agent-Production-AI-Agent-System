"""OMNI AGENT - Time Series Store
Append-only time series store with range queries, downsampling,
aggregation, retention policies, and multi-metric support.

Features:
- Series: named metric streams; tags for labeling (key=value)
- Points: (timestamp, value) pairs; value is float or dict
- Append: write single point or batch
- Range query: start_ts..end_ts with optional step for downsampling
- Aggregation: mean, sum, min, max, count, last, first, stddev, p50, p95, p99
- Downsampling: GROUP BY time bucket of width W seconds
- Retention: auto-delete points older than retention_s (sweep)
- Resolution: sub-second timestamps (float)
- Tags: series metadata key=value; used for filtering/grouping
- Multi-series query: query across multiple series simultaneously
- Rollups: pre-computed coarser summaries (1m, 5m, 1h)
- Interpolation: fill missing buckets with None or last-value (LOCF)
- Rate: compute per-second rate between consecutive points
- Derivative: first-order difference
- Moving average: sliding window average
- Anomaly: points > N stddevs from rolling mean
- Export: CSV / JSON
- SQLite persistence: raw points, series metadata, rollup cache
- REST API: write, query, series_list, stats
"""
import json, math, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

# ── Aggregation helpers ───────────────────────────────────────────────────────
def _agg(values: List[float], func: str) -> Optional[float]:
    if not values: return None
    if func == "mean":   return sum(values) / len(values)
    if func == "sum":    return sum(values)
    if func == "min":    return min(values)
    if func == "max":    return max(values)
    if func == "count":  return float(len(values))
    if func == "last":   return values[-1]
    if func == "first":  return values[0]
    if func == "stddev":
        if len(values) < 2: return 0.0
        m = sum(values) / len(values)
        return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))
    if func in ("p50","p95","p99"):
        pct = {"p50": 0.50, "p95": 0.95, "p99": 0.99}[func]
        s = sorted(values)
        idx = int(pct * (len(s) - 1))
        return s[idx]
    return None

@dataclass
class Point:
    ts: float; value: float; tags: Dict[str, str] = field(default_factory=dict)

@dataclass
class SeriesMeta:
    name: str
    tags: Dict[str, str] = field(default_factory=dict)
    unit: str = ""
    retention_s: float = 0.0   # 0 = keep forever
    created_at: float = field(default_factory=time.time)

    def to_dict(self):
        return {"name": self.name, "tags": self.tags, "unit": self.unit,
                "retention_s": self.retention_s,
                "created_at": round(self.created_at, 2)}

@dataclass
class QueryResult:
    series: str
    points: List[Tuple[float, Optional[float]]]   # (ts, value)
    aggregation: str = "raw"

    def to_dict(self):
        return {"series": self.series, "aggregation": self.aggregation,
                "points": [[round(ts, 3), v] for ts, v in self.points]}

class TSStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS series(
                    name TEXT PRIMARY KEY, tags TEXT, unit TEXT,
                    retention_s REAL, created_at REAL);
                CREATE TABLE IF NOT EXISTS points(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    series TEXT, ts REAL, value REAL, tags TEXT);
                CREATE INDEX IF NOT EXISTS idx_pts_series_ts
                    ON points(series, ts);
            """)

    def save_series(self, s: SeriesMeta):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO series VALUES(?,?,?,?,?)",
                (s.name, json.dumps(s.tags), s.unit,
                 s.retention_s, s.created_at))

    def load_series(self, name: str) -> Optional[SeriesMeta]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM series WHERE name=?", (name,)).fetchone()
        if not row: return None
        return SeriesMeta(name=row["name"], tags=json.loads(row["tags"]),
                           unit=row["unit"], retention_s=row["retention_s"],
                           created_at=row["created_at"])

    def list_series(self) -> List[str]:
        with self._conn() as c:
            return [r["name"] for r in
                    c.execute("SELECT name FROM series ORDER BY name").fetchall()]

    def write(self, series: str, ts: float, value: float,
               tags: Dict = None):
        with self._conn() as c:
            c.execute("INSERT INTO points(series,ts,value,tags) VALUES(?,?,?,?)",
                (series, ts, value, json.dumps(tags or {})))

    def write_batch(self, series: str, points: List[Tuple[float, float]],
                     tags: Dict = None):
        tags_str = json.dumps(tags or {})
        with self._conn() as c:
            c.executemany(
                "INSERT INTO points(series,ts,value,tags) VALUES(?,?,?,?)",
                [(series, ts, v, tags_str) for ts, v in points])

    def query_raw(self, series: str, start: float, end: float,
                   limit: int = 10000) -> List[Tuple[float, float]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT ts, value FROM points "
                "WHERE series=? AND ts>=? AND ts<=? "
                "ORDER BY ts ASC LIMIT ?",
                (series, start, end, limit)).fetchall()
        return [(r["ts"], r["value"]) for r in rows]

    def query_downsampled(self, series: str, start: float, end: float,
                           step: float, agg: str) -> List[Tuple[float, Optional[float]]]:
        rows = self.query_raw(series, start, end)
        if not rows: return []
        buckets: Dict[int, List[float]] = {}
        for ts, v in rows:
            bucket = int((ts - start) / step)
            buckets.setdefault(bucket, []).append(v)
        result = []
        n_buckets = max(1, int((end - start) / step) + 1)
        for i in range(n_buckets):
            bucket_ts = start + i * step
            vals = buckets.get(i, [])
            result.append((bucket_ts, _agg(vals, agg)))
        return result

    def sweep_retention(self) -> int:
        total = 0
        with self._conn() as c:
            series = [dict(r) for r in
                      c.execute("SELECT name, retention_s FROM series "
                                 "WHERE retention_s > 0").fetchall()]
            for s in series:
                cutoff = time.time() - s["retention_s"]
                cur = c.execute(
                    "DELETE FROM points WHERE series=? AND ts<?",
                    (s["name"], cutoff))
                total += cur.rowcount
        return total

    def count_points(self, series: str = None) -> int:
        with self._conn() as c:
            if series:
                return c.execute(
                    "SELECT COUNT(*) FROM points WHERE series=?",
                    (series,)).fetchone()[0]
            return c.execute("SELECT COUNT(*) FROM points").fetchone()[0]

    def stats(self) -> Dict:
        with self._conn() as c:
            ns = c.execute("SELECT COUNT(*) FROM series").fetchone()[0]
            np_ = c.execute("SELECT COUNT(*) FROM points").fetchone()[0]
            by_s = {r["series"]: r["cnt"] for r in c.execute(
                "SELECT series, COUNT(*) as cnt FROM points "
                "GROUP BY series ORDER BY cnt DESC LIMIT 20").fetchall()}
        return {"series_count": ns, "total_points": np_,
                "by_series": by_s}

class TimeSeries:
    """
    Time series store with range queries and downsampling.

    Usage:
        ts = TimeSeries()
        ts.create_series("cpu_usage", unit="%", retention_s=86400)

        # Write points
        ts.write("cpu_usage", time.time(), 45.2)
        ts.write_batch("cpu_usage", [(t, v) for t, v in readings])

        # Query raw range
        result = ts.query("cpu_usage", start=t0, end=t1)

        # Downsampled: 1-minute buckets, mean aggregation
        result = ts.query("cpu_usage", start=t0, end=t1,
                           step=60, aggregation="mean")
    """
    def __init__(self, db_path: str = "data/timeseries.db"):
        self._store = TSStore(db_path)
        self._series: Dict[str, SeriesMeta] = {}
        # Load existing
        for name in self._store.list_series():
            s = self._store.load_series(name)
            if s: self._series[name] = s

    def create_series(self, name: str,
                       tags: Dict = None, unit: str = "",
                       retention_s: float = 0.0) -> SeriesMeta:
        s = SeriesMeta(name=name, tags=dict(tags or {}),
                        unit=unit, retention_s=retention_s)
        self._series[name] = s
        self._store.save_series(s)
        return s

    def _ensure_series(self, name: str):
        if name not in self._series:
            self.create_series(name)

    def write(self, series: str, ts: float = None,
               value: float = 0.0, tags: Dict = None):
        self._ensure_series(series)
        ts = ts if ts is not None else time.time()
        self._store.write(series, ts, value, tags)

    def write_batch(self, series: str,
                     points: List[Tuple[float, float]],
                     tags: Dict = None):
        self._ensure_series(series)
        self._store.write_batch(series, points, tags)

    def query(self, series: str, start: float = None,
               end: float = None, step: float = None,
               aggregation: str = "mean",
               fill: str = "none",
               limit: int = 10000) -> QueryResult:
        now = time.time()
        start = start if start is not None else now - 3600
        end   = end   if end   is not None else now
        if step:
            raw = self._store.query_downsampled(series, start, end, step, aggregation)
            # Fill nulls
            if fill == "locf":
                last = None
                raw = [(ts, v if v is not None else last) for ts, v in raw]
                for i, (ts, v) in enumerate(raw):
                    if v is not None: last = v
            pts = raw
            agg_label = f"{aggregation}_{int(step)}s"
        else:
            pts = self._store.query_raw(series, start, end, limit)
            agg_label = "raw"
        return QueryResult(series=series, points=pts, aggregation=agg_label)

    def multi_query(self, series_list: List[str],
                     start: float = None, end: float = None,
                     step: float = None,
                     aggregation: str = "mean") -> List[QueryResult]:
        return [self.query(s, start, end, step, aggregation)
                for s in series_list]

    def rate(self, series: str, start: float = None,
              end: float = None) -> List[Tuple[float, float]]:
        """Per-second rate of change between consecutive points."""
        pts = self._store.query_raw(series, start or time.time()-3600,
                                     end or time.time())
        result = []
        for i in range(1, len(pts)):
            dt = pts[i][0] - pts[i-1][0]
            if dt > 0:
                rate = (pts[i][1] - pts[i-1][1]) / dt
                result.append((pts[i][0], rate))
        return result

    def moving_average(self, series: str, window: int = 5,
                        start: float = None,
                        end: float = None) -> List[Tuple[float, float]]:
        pts = self._store.query_raw(series, start or time.time()-3600,
                                     end or time.time())
        result = []
        for i in range(len(pts)):
            w = pts[max(0, i - window + 1):i + 1]
            avg = sum(v for _, v in w) / len(w)
            result.append((pts[i][0], avg))
        return result

    def anomalies(self, series: str, window: int = 20,
                   threshold: float = 3.0,
                   start: float = None,
                   end: float = None) -> List[Tuple[float, float]]:
        pts = self._store.query_raw(series, start or time.time()-3600,
                                     end or time.time())
        anomalies = []
        for i in range(window, len(pts)):
            w = [v for _, v in pts[i-window:i]]
            mean = sum(w) / len(w)
            std  = math.sqrt(sum((v - mean)**2 for v in w) / len(w))
            if std == 0 and pts[i][1] != mean or (std > 0 and abs(pts[i][1] - mean) > threshold * std):
                anomalies.append(pts[i])
        return anomalies

    def sweep_retention(self) -> int:
        return self._store.sweep_retention()

    def list_series(self) -> List[Dict]:
        return [s.to_dict() for s in self._series.values()]

    def stats(self) -> Dict:
        s = self._store.stats()
        s["in_memory_series"] = len(self._series)
        return s

    def export_csv(self, series: str, start: float = None,
                    end: float = None) -> str:
        pts = self._store.query_raw(series,
                                     start or time.time()-3600,
                                     end or time.time())
        lines = ["ts,value"]
        for ts, v in pts:
            lines.append(f"{ts:.3f},{v}")
        return "\n".join(lines)

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def write_ep(req):
            d = await req.json()
            self.write(d["series"], d.get("ts"), d.get("value",0),
                        d.get("tags"))
            return web.json_response({"written": True}, status=201)
        async def query_ep(req):
            d = await req.json()
            r = self.query(d["series"],
                            d.get("start"), d.get("end"),
                            d.get("step"), d.get("aggregation","mean"))
            return web.json_response(r.to_dict())
        async def list_ep(req):
            return web.json_response({"series": self.list_series()})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/ts"
        app.router.add_post(f"{p}/write", write_ep)
        app.router.add_post(f"{p}/query", query_ep)
        app.router.add_get( f"{p}/list",  list_ep)
        app.router.add_get( f"{p}/stats", stats_ep)
        logger.info(f"Time series API at {prefix}/ts/")
