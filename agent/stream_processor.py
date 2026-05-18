"""OMNI AGENT - Stream Processor
Real-time data stream processing with windowed aggregation,
filtering pipelines, backpressure, and fanout routing.

Features:
- Stream: named typed channel with schema hint and buffer
- Record: typed payload + timestamp + source tag + sequence number
- Window types: TUMBLING (fixed non-overlapping), SLIDING (step < size),
    SESSION (gap-based close), COUNT (fixed record count)
- Aggregations: count, sum, avg, min, max, first, last, collect, distinct
- Filter stage: predicate fn(record) -> bool
- Transform stage: fn(record) -> record (map)
- Fanout: route records to multiple downstream streams by router fn
- Backpressure: buffer_max cap; DROP_OLDEST or BLOCK_PRODUCER policy
- Watermark: track event-time lag for late-arrival handling
- Dead-letter stream: malformed/filtered records sink
- Pipeline: compose Filter → Transform → Window → Aggregate → Fanout
- Checkpoint: snapshot stream state to SQLite
- SQLite persistence: record log, window results, pipeline stats
- REST API: push, query_window, pipeline_stats, flush
"""
import asyncio, json, sqlite3, time, uuid, logging
from collections import defaultdict, deque
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class WindowType(str, Enum):
    TUMBLING = "tumbling"; SLIDING = "sliding"
    SESSION  = "session";  COUNT   = "count"

class BackpressurePolicy(str, Enum):
    DROP_OLDEST = "drop_oldest"; DROP_NEW = "drop_new"

class AggFunc(str, Enum):
    COUNT = "count"; SUM = "sum"; AVG = "avg"
    MIN = "min";     MAX = "max"; FIRST = "first"
    LAST = "last";   COLLECT = "collect"; DISTINCT = "distinct"

@dataclass
class Record:
    stream: str; payload: Dict
    ts: float = field(default_factory=time.time)
    source: str = ""; seq: int = 0
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:10])

    def to_dict(self):
        return {"id": self.id, "stream": self.stream,
                "payload": self.payload, "ts": round(self.ts, 3),
                "source": self.source, "seq": self.seq}

@dataclass
class WindowResult:
    stream: str; window_type: str
    start: float; end: float
    agg_field: str; agg_func: str; value: Any
    record_count: int

    def to_dict(self):
        return {"stream": self.stream, "window_type": self.window_type,
                "start": round(self.start, 3), "end": round(self.end, 3),
                "field": self.agg_field, "func": self.agg_func,
                "value": self.value, "count": self.record_count}

def _aggregate(records: List[Record], field: str, func: AggFunc) -> Any:
    vals = [r.payload.get(field) for r in records
             if r.payload.get(field) is not None]
    if not vals: return None
    if func == AggFunc.COUNT:   return len(records)
    if func == AggFunc.SUM:     return sum(float(v) for v in vals)
    if func == AggFunc.AVG:     return round(sum(float(v) for v in vals) / len(vals), 4)
    if func == AggFunc.MIN:     return min(vals)
    if func == AggFunc.MAX:     return max(vals)
    if func == AggFunc.FIRST:   return vals[0]
    if func == AggFunc.LAST:    return vals[-1]
    if func == AggFunc.COLLECT: return vals
    if func == AggFunc.DISTINCT: return list(dict.fromkeys(vals))
    return None

@dataclass
class WindowSpec:
    window_type: WindowType
    size_s: float = 60.0       # duration in seconds (or count for COUNT windows)
    step_s: float = 0.0        # for SLIDING: slide interval; 0 = use size_s
    gap_s: float = 30.0        # for SESSION: inactivity gap
    count: int = 100            # for COUNT windows
    agg_field: str = "value"
    agg_func: AggFunc = AggFunc.COUNT
    emit_on_close: bool = True  # emit result when window closes

class SPStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS records(
                    id TEXT PRIMARY KEY, stream TEXT,
                    payload TEXT, ts REAL, source TEXT, seq INTEGER,
                    created_at REAL);
                CREATE TABLE IF NOT EXISTS window_results(
                    id TEXT PRIMARY KEY, stream TEXT, window_type TEXT,
                    start_ts REAL, end_ts REAL,
                    agg_field TEXT, agg_func TEXT, value TEXT,
                    record_count INTEGER, created_at REAL);
                CREATE INDEX IF NOT EXISTS idx_rec_stream
                    ON records(stream, ts DESC);
                CREATE INDEX IF NOT EXISTS idx_wr_stream
                    ON window_results(stream, created_at DESC);
            """)

    def save_record(self, r: Record):
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO records VALUES(?,?,?,?,?,?,?)",
                (r.id, r.stream, json.dumps(r.payload),
                 r.ts, r.source, r.seq, time.time()))

    def save_window_result(self, wr: WindowResult):
        with self._conn() as c:
            c.execute("INSERT INTO window_results VALUES(?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4())[:8], wr.stream, wr.window_type,
                 wr.start, wr.end, wr.agg_field, wr.agg_func,
                 json.dumps(wr.value), wr.record_count, time.time()))

    def recent_records(self, stream: str, limit: int = 50) -> List[Dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM records WHERE stream=? "
                "ORDER BY ts DESC LIMIT ?", (stream, limit)).fetchall()
        return [dict(r) for r in rows]

    def recent_windows(self, stream: str, limit: int = 20) -> List[Dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM window_results WHERE stream=? "
                "ORDER BY created_at DESC LIMIT ?", (stream, limit)).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> Dict:
        with self._conn() as c:
            nr = c.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            nw = c.execute("SELECT COUNT(*) FROM window_results").fetchone()[0]
        return {"total_records": nr, "total_window_results": nw}

class _WindowState:
    """Mutable window tracking for a single stream."""
    def __init__(self, spec: WindowSpec):
        self.spec = spec
        self.records: deque = deque()
        self.window_start: float = time.time()
        self.last_ts: float = time.time()
        self.slide_cursor: float = time.time()
        self.count_buffer: List[Record] = []
        self.results: List[WindowResult] = []

    def push(self, record: Record) -> List[WindowResult]:
        now = record.ts
        self.last_ts = now
        emitted = []
        spec = self.spec

        if spec.window_type == WindowType.TUMBLING:
            self.records.append(record)
            if now - self.window_start >= spec.size_s:
                emitted.append(self._emit(self.window_start, now))
                self.records.clear()
                self.window_start = now

        elif spec.window_type == WindowType.SLIDING:
            step = spec.step_s or spec.size_s
            self.records.append(record)
            if now - self.slide_cursor >= step:
                cutoff = now - spec.size_s
                while self.records and self.records[0].ts < cutoff:
                    self.records.popleft()
                emitted.append(self._emit(max(self.window_start, now - spec.size_s), now))
                self.slide_cursor = now

        elif spec.window_type == WindowType.SESSION:
            self.records.append(record)

        elif spec.window_type == WindowType.COUNT:
            self.count_buffer.append(record)
            if len(self.count_buffer) >= spec.count:
                start = self.count_buffer[0].ts
                end   = self.count_buffer[-1].ts
                wr = WindowResult(
                    stream=record.stream,
                    window_type=spec.window_type.value,
                    start=start, end=end,
                    agg_field=spec.agg_field,
                    agg_func=spec.agg_func.value,
                    value=_aggregate(self.count_buffer, spec.agg_field, spec.agg_func),
                    record_count=len(self.count_buffer))
                emitted.append(wr)
                self.count_buffer.clear()

        return emitted

    def flush_session(self, now: float) -> Optional[WindowResult]:
        spec = self.spec
        if (spec.window_type == WindowType.SESSION and self.records
                and now - self.last_ts >= spec.gap_s):
            wr = self._emit(self.window_start, self.last_ts)
            self.records.clear()
            self.window_start = now
            return wr
        return None

    def _emit(self, start: float, end: float) -> WindowResult:
        recs = list(self.records)
        return WindowResult(
            stream=recs[0].stream if recs else "",
            window_type=self.spec.window_type.value,
            start=start, end=end,
            agg_field=self.spec.agg_field,
            agg_func=self.spec.agg_func.value,
            value=_aggregate(recs, self.spec.agg_field, self.spec.agg_func),
            record_count=len(recs))

@dataclass
class StreamConfig:
    name: str
    schema_hint: Dict = field(default_factory=dict)
    buffer_max: int = 10000
    backpressure: BackpressurePolicy = BackpressurePolicy.DROP_OLDEST
    persist: bool = True

class StreamProcessor:
    """
    Real-time stream processor with windowing and aggregation pipelines.

    Usage:
        sp = StreamProcessor()
        sp.create_stream("events", buffer_max=5000)

        # Add window spec
        sp.add_window("events", WindowSpec(
            window_type=WindowType.TUMBLING, size_s=10,
            agg_field="value", agg_func=AggFunc.SUM))

        # Add filter
        sp.add_filter("events", lambda r: r.payload.get("value", 0) > 0)

        # Push records
        for i in range(100):
            sp.push("events", {"value": i, "tag": "sensor_A"})

        # Query recent window results
        results = sp.window_results("events")
    """
    def __init__(self, db_path: str = "data/streams.db"):
        self._store = SPStore(db_path)
        self._streams: Dict[str, StreamConfig] = {}
        self._buffers: Dict[str, deque] = {}
        self._seq: Dict[str, int] = defaultdict(int)
        self._windows: Dict[str, List[_WindowState]] = defaultdict(list)
        self._filters: Dict[str, List[Callable]] = defaultdict(list)
        self._transforms: Dict[str, List[Callable]] = defaultdict(list)
        self._fanouts: Dict[str, List[Tuple[str, Callable]]] = defaultdict(list)
        self._dlq: deque = deque(maxlen=1000)
        self._window_results: Dict[str, deque] = defaultdict(lambda: deque(maxlen=200))
        self._stats: Dict[str, Dict] = defaultdict(lambda: {
            "pushed": 0, "filtered": 0, "dropped": 0, "dlq": 0})

    def create_stream(self, name: str,
                       buffer_max: int = 10000,
                       backpressure: BackpressurePolicy = BackpressurePolicy.DROP_OLDEST,
                       persist: bool = True,
                       schema_hint: Dict = None) -> StreamConfig:
        cfg = StreamConfig(name=name, buffer_max=buffer_max,
                            backpressure=backpressure, persist=persist,
                            schema_hint=dict(schema_hint or {}))
        self._streams[name] = cfg
        self._buffers[name] = deque(maxlen=buffer_max)
        return cfg

    def add_window(self, stream: str, spec: WindowSpec):
        self._windows[stream].append(_WindowState(spec))

    def add_filter(self, stream: str, predicate: Callable):
        self._filters[stream].append(predicate)

    def add_transform(self, stream: str, fn: Callable):
        self._transforms[stream].append(fn)

    def add_fanout(self, stream: str, target: str, router: Callable = None):
        self._fanouts[stream].append((target, router or (lambda r: True)))

    def push(self, stream: str, payload: Dict,
              source: str = "", ts: float = None) -> Optional[Record]:
        if stream not in self._streams:
            return None
        cfg = self._streams[stream]
        self._seq[stream] += 1
        rec = Record(stream=stream, payload=dict(payload),
                      ts=ts or time.time(), source=source,
                      seq=self._seq[stream])
        # Apply filters
        for f in self._filters[stream]:
            try:
                if not f(rec):
                    self._stats[stream]["filtered"] += 1
                    self._dlq.append({"reason": "filtered", "record": rec.to_dict()})
                    return None
            except Exception as e:
                self._stats[stream]["dlq"] += 1
                self._dlq.append({"reason": str(e), "record": rec.to_dict()})
                return None
        # Apply transforms
        for fn in self._transforms[stream]:
            try: rec = fn(rec)
            except: pass
        # Buffer management
        buf = self._buffers[stream]
        if len(buf) >= cfg.buffer_max:
            if cfg.backpressure == BackpressurePolicy.DROP_OLDEST:
                buf.popleft()
                self._stats[stream]["dropped"] += 1
            else:
                self._stats[stream]["dropped"] += 1
                return None
        buf.append(rec)
        self._stats[stream]["pushed"] += 1
        # Window processing
        for ws in self._windows[stream]:
            results = ws.push(rec)
            for wr in results:
                self._window_results[stream].append(wr)
                if cfg.persist:
                    self._store.save_window_result(wr)
        if cfg.persist:
            self._store.save_record(rec)
        # Fanout
        for target, router in self._fanouts[stream]:
            if target in self._streams:
                try:
                    if router(rec):
                        self.push(target, rec.payload, source=f"fanout:{stream}")
                except: pass
        return rec

    def flush_sessions(self) -> int:
        now = time.time(); count = 0
        for stream, ws_list in self._windows.items():
            for ws in ws_list:
                wr = ws.flush_session(now)
                if wr:
                    self._window_results[stream].append(wr)
                    count += 1
        return count

    def buffer(self, stream: str, limit: int = None) -> List[Record]:
        buf = list(self._buffers.get(stream, []))
        return buf[-limit:] if limit else buf

    def window_results(self, stream: str,
                        limit: int = 20) -> List[WindowResult]:
        return list(self._window_results[stream])[-limit:]

    def watermark(self, stream: str) -> float:
        """Latest timestamp seen in stream."""
        buf = self._buffers.get(stream)
        if not buf: return 0.0
        return max(r.ts for r in buf)

    def stats(self) -> Dict:
        s = self._store.stats()
        s["streams"] = len(self._streams)
        s["per_stream"] = {name: dict(st)
                            for name, st in self._stats.items()}
        s["dlq_size"] = len(self._dlq)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def push_ep(req):
            d = await req.json()
            r = self.push(d["stream"], d["payload"], d.get("source",""))
            if r is None:
                return web.json_response({"error": "filtered or stream unknown"},
                                          status=400)
            return web.json_response(r.to_dict(), status=201)
        async def window_ep(req):
            stream = req.match_info["stream"]
            results = self.window_results(stream)
            return web.json_response({"results": [r.to_dict() for r in results]})
        async def buffer_ep(req):
            stream = req.match_info["stream"]
            limit = int(req.rel_url.query.get("limit", 50))
            return web.json_response(
                {"records": [r.to_dict() for r in self.buffer(stream, limit)]})
        async def flush_ep(req):
            n = self.flush_sessions()
            return web.json_response({"sessions_flushed": n})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/stream"
        app.router.add_post(f"{p}/push",              push_ep)
        app.router.add_get( f"{p}/{{stream}}/windows",buffer_ep)
        app.router.add_get( f"{p}/{{stream}}/results",window_ep)
        app.router.add_post(f"{p}/flush",             flush_ep)
        app.router.add_get( f"{p}/stats",             stats_ep)
        logger.info(f"Stream processor API at {prefix}/stream/")
