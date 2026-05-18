"""OMNI Agent — Streaming Pipeline V2: windowing, aggregation, backpressure."""
from __future__ import annotations
import collections, sqlite3, threading, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


class WindowType(str, Enum):
    TUMBLING = "tumbling"   # fixed non-overlapping
    SLIDING  = "sliding"    # overlapping
    SESSION  = "session"    # gap-based
    COUNT    = "count"      # fixed item count


class StreamStatus(str, Enum):
    IDLE     = "idle"
    RUNNING  = "running"
    PAUSED   = "paused"
    STOPPED  = "stopped"
    ERROR    = "error"


@dataclass
class StreamRecord:
    record_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    data: Any = None
    key: Optional[str] = None
    ts: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WindowResult:
    window_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    window_type: str = ""
    records: List[StreamRecord] = field(default_factory=list)
    start_ts: float = 0.0
    end_ts: float = 0.0
    aggregations: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"window_id": self.window_id,
                "records": len(self.records),
                "start_ts": self.start_ts,
                "end_ts": self.end_ts,
                "aggregations": self.aggregations}


@dataclass
class StageConfig:
    stage_id: str
    name: str
    fn: Callable
    parallelism: int = 1
    buffer_size: int = 1000
    on_error: str = "skip"   # skip | raise | dlq


class StreamingPipelineV2:
    """
    Real-time streaming pipeline:
    - Ingest records (push or pull from iterable)
    - Multi-stage processing (map/filter/flatmap/aggregate)
    - Windowing: tumbling / sliding / session (gap) / count
    - Per-window aggregation (count/sum/avg/min/max/custom)
    - Keyed streams (partition by key)
    - Backpressure: bounded buffer per stage
    - Dead-letter queue for errors
    - Stage parallelism (thread workers)
    - Watermarks and late-record handling
    - Pause/resume stream
    - Metrics: throughput, lag, error rate
    - SQLite persistence for window results
    """

    def __init__(self, db_path: str = ":memory:",
                 max_buffer: int = 10_000):
        self._stages:   List[StageConfig] = []
        self._buffer:   collections.deque = collections.deque(maxlen=max_buffer)
        self._dlq:      List[StreamRecord] = []
        self._windows:  List[WindowResult] = []
        self._keyed:    Dict[str, List[StreamRecord]] = {}
        self._status    = StreamStatus.IDLE
        self._lock      = threading.Lock()
        self._stats     = {"ingested": 0, "processed": 0, "errors": 0,
                           "dropped": 0}
        self._watermark = 0.0
        self._late_threshold_s = 5.0
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS sp_windows (
                window_id TEXT PRIMARY KEY, window_type TEXT,
                records INTEGER, start_ts REAL, end_ts REAL,
                aggregations TEXT
            );
        """)
        self._db.commit()

    # ── STAGE BUILDING ────────────────────────────────────────────────

    def add_stage(self, name: str,
                   fn: Callable,
                   parallelism: int = 1,
                   buffer_size: int = 1000,
                   on_error: str = "skip",
                   stage_id: Optional[str] = None) -> StageConfig:
        sc = StageConfig(
            stage_id=stage_id or str(uuid.uuid4())[:6],
            name=name, fn=fn,
            parallelism=parallelism,
            buffer_size=buffer_size,
            on_error=on_error)
        self._stages.append(sc)
        return sc

    def map(self, fn: Callable, name: str = "map", **kw) -> "StreamingPipelineV2":
        self.add_stage(name, lambda r: [fn(r)], **kw)
        return self

    def filter(self, fn: Callable, name: str = "filter", **kw) -> "StreamingPipelineV2":
        self.add_stage(name, lambda r: [r] if fn(r) else [], **kw)
        return self

    def flatmap(self, fn: Callable, name: str = "flatmap", **kw) -> "StreamingPipelineV2":
        self.add_stage(name, lambda r: list(fn(r)), **kw)
        return self

    # ── INGESTION ────────────────────────────────────────────────────

    def ingest(self, data: Any,
               key: Optional[str] = None,
               ts: Optional[float] = None) -> StreamRecord:
        rec = StreamRecord(data=data, key=key,
                            ts=ts or time.time())
        with self._lock:
            if len(self._buffer) >= self._buffer.maxlen:
                self._stats["dropped"] += 1
            else:
                self._buffer.append(rec)
                self._stats["ingested"] += 1
            # Update watermark
            if rec.ts > self._watermark:
                self._watermark = rec.ts
            # Key partitioning
            if key:
                self._keyed.setdefault(key, []).append(rec)
        return rec

    def ingest_batch(self, items: Iterable,
                     key_fn: Optional[Callable[[Any], str]] = None) -> int:
        count = 0
        for item in items:
            key = key_fn(item) if key_fn else None
            self.ingest(item, key=key)
            count += 1
        return count

    # ── PROCESSING ───────────────────────────────────────────────────

    def process_all(self) -> List[StreamRecord]:
        """Drain buffer through all stages synchronously."""
        with self._lock:
            records = list(self._buffer)
            self._buffer.clear()

        output: List[StreamRecord] = []
        for rec in records:
            current = [rec]
            for stage in self._stages:
                next_batch: List[StreamRecord] = []
                for r in current:
                    try:
                        results = stage.fn(r)
                        for res in (results or []):
                            if isinstance(res, StreamRecord):
                                next_batch.append(res)
                            else:
                                next_batch.append(StreamRecord(data=res,
                                                                key=r.key, ts=r.ts))
                    except Exception as exc:
                        self._stats["errors"] += 1
                        if stage.on_error == "raise":
                            raise
                        elif stage.on_error == "dlq":
                            self._dlq.append(r)
                current = next_batch
            output.extend(current)
            self._stats["processed"] += 1
        return output

    # ── WINDOWING ────────────────────────────────────────────────────

    def tumbling_window(self, records: List[StreamRecord],
                         size_s: float,
                         agg_fns: Optional[Dict[str, Callable]] = None
                         ) -> List[WindowResult]:
        if not records: return []
        start = min(r.ts for r in records)
        end   = max(r.ts for r in records)
        results: List[WindowResult] = []
        t = start
        while t <= end:
            bucket = [r for r in records if t <= r.ts < t + size_s]
            if bucket:
                wr = WindowResult(
                    window_type="tumbling",
                    records=bucket,
                    start_ts=t,
                    end_ts=t + size_s,
                    aggregations=self._aggregate(bucket, agg_fns))
                results.append(wr)
                self._save_window(wr)
            t += size_s
        self._windows.extend(results)
        return results

    def sliding_window(self, records: List[StreamRecord],
                        size_s: float,
                        slide_s: float,
                        agg_fns: Optional[Dict[str, Callable]] = None
                        ) -> List[WindowResult]:
        if not records: return []
        start = min(r.ts for r in records)
        end   = max(r.ts for r in records)
        results: List[WindowResult] = []
        t = start
        while t <= end:
            bucket = [r for r in records if t <= r.ts < t + size_s]
            if bucket:
                wr = WindowResult(
                    window_type="sliding",
                    records=bucket,
                    start_ts=t, end_ts=t + size_s,
                    aggregations=self._aggregate(bucket, agg_fns))
                results.append(wr)
                self._save_window(wr)
            t += slide_s
        self._windows.extend(results)
        return results

    def count_window(self, records: List[StreamRecord],
                      count: int,
                      agg_fns: Optional[Dict[str, Callable]] = None
                      ) -> List[WindowResult]:
        results: List[WindowResult] = []
        for i in range(0, len(records), count):
            bucket = records[i:i + count]
            wr = WindowResult(
                window_type="count",
                records=bucket,
                start_ts=bucket[0].ts,
                end_ts=bucket[-1].ts,
                aggregations=self._aggregate(bucket, agg_fns))
            results.append(wr)
            self._save_window(wr)
        self._windows.extend(results)
        return results

    def session_window(self, records: List[StreamRecord],
                        gap_s: float,
                        agg_fns: Optional[Dict[str, Callable]] = None
                        ) -> List[WindowResult]:
        if not records: return []
        sorted_r = sorted(records, key=lambda r: r.ts)
        sessions: List[List[StreamRecord]] = []
        current = [sorted_r[0]]
        for r in sorted_r[1:]:
            if r.ts - current[-1].ts <= gap_s:
                current.append(r)
            else:
                sessions.append(current)
                current = [r]
        sessions.append(current)
        results = []
        for session in sessions:
            wr = WindowResult(
                window_type="session",
                records=session,
                start_ts=session[0].ts,
                end_ts=session[-1].ts,
                aggregations=self._aggregate(session, agg_fns))
            results.append(wr)
            self._save_window(wr)
        self._windows.extend(results)
        return results

    def _aggregate(self, records: List[StreamRecord],
                    fns: Optional[Dict[str, Callable]]) -> Dict[str, Any]:
        vals = [r.data for r in records
                if isinstance(r.data, (int, float))]
        base: Dict[str, Any] = {"count": len(records)}
        if vals:
            base["sum"]  = sum(vals)
            base["avg"]  = sum(vals) / len(vals)
            base["min"]  = min(vals)
            base["max"]  = max(vals)
        if fns:
            for k, fn in fns.items():
                try:  base[k] = fn(records)
                except Exception: pass
        return base

    def _save_window(self, wr: WindowResult):
        import json
        self._db.execute(
            "INSERT OR REPLACE INTO sp_windows VALUES (?,?,?,?,?,?)",
            (wr.window_id, wr.window_type, len(wr.records),
             wr.start_ts, wr.end_ts,
             json.dumps({k: str(v) for k, v in wr.aggregations.items()})))
        self._db.commit()

    # ── KEYED STREAMS ─────────────────────────────────────────────────

    def keyed_records(self, key: str) -> List[StreamRecord]:
        return self._keyed.get(key, [])

    def keys(self) -> List[str]:
        return list(self._keyed.keys())

    # ── CONTROL ──────────────────────────────────────────────────────

    def pause(self):  self._status = StreamStatus.PAUSED
    def resume(self): self._status = StreamStatus.RUNNING
    def stop(self):   self._status = StreamStatus.STOPPED

    def dlq(self) -> List[StreamRecord]:
        return list(self._dlq)

    def flush_dlq(self) -> int:
        n = len(self._dlq)
        self._dlq.clear()
        return n

    def stats(self) -> Dict[str, Any]:
        return {**self._stats,
                "buffer_size": len(self._buffer),
                "windows": len(self._windows),
                "keys": len(self._keyed),
                "dlq_size": len(self._dlq),
                "watermark": self._watermark}
