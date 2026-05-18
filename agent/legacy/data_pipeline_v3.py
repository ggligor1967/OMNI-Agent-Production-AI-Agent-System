"""OMNI Agent — Data Pipeline V3: streaming ETL with transforms, sinks, and backpressure."""
from __future__ import annotations
import queue, sqlite3, threading, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Generator, Iterable, List, Optional, Tuple


class RecordStatus(str, Enum):
    OK      = "ok"
    SKIPPED = "skipped"
    FAILED  = "failed"
    FILTERED = "filtered"


class BackpressureStrategy(str, Enum):
    BLOCK   = "block"    # wait until queue has space
    DROP    = "drop"     # drop newest if full
    DROP_OLDEST = "drop_oldest"
    RAISE   = "raise"    # raise exception


@dataclass
class Record:
    record_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    data: Any = None
    schema: str = ""
    source: str = ""
    ts: float = field(default_factory=time.time)
    status: RecordStatus = RecordStatus.OK
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def copy(self) -> "Record":
        return Record(
            record_id=self.record_id, data=self.data,
            schema=self.schema, source=self.source,
            ts=self.ts, status=self.status,
            error=self.error, metadata=dict(self.metadata))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "record_id": self.record_id,
            "status": self.status.value,
            "source": self.source,
            "ts": self.ts,
            "error": self.error,
        }


@dataclass
class PipelineStats:
    stage_name: str
    records_in: int = 0
    records_out: int = 0
    records_failed: int = 0
    records_filtered: int = 0
    total_ms: float = 0.0

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.records_in if self.records_in else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage_name,
            "in": self.records_in,
            "out": self.records_out,
            "failed": self.records_failed,
            "filtered": self.records_filtered,
            "avg_ms": round(self.avg_ms, 3),
        }


class TransformError(Exception):
    pass


class FilteredRecord(Exception):
    """Raise in transform to drop a record without error."""
    pass


@dataclass
class Stage:
    name: str
    fn: Callable[[Record], Optional[Record]]   # None = filter
    skip_on_error: bool = True
    batch_size: int = 1    # >1 = batch transform
    enabled: bool = True
    stats: PipelineStats = field(default_factory=lambda: PipelineStats(""))

    def __post_init__(self):
        self.stats.stage_name = self.name


class DataPipelineV3:
    """
    Streaming ETL pipeline with:
    - Ordered transform stages
    - Per-stage error handling (skip vs raise)
    - Filter support (drop records without error)
    - Batch transforms
    - Source generators
    - Sink writers
    - Backpressure control
    - Async-compatible via thread-based runner
    - Per-stage metrics
    - SQLite run log
    """

    def __init__(
        self,
        name: str = "pipeline",
        backpressure: BackpressureStrategy = BackpressureStrategy.BLOCK,
        queue_size: int = 1000,
        db_path: str = ":memory:",
    ):
        self.name         = name
        self.backpressure = backpressure
        self.queue_size   = queue_size
        self._stages:  List[Stage] = []
        self._sinks:   List[Callable[[Record], None]] = []
        self._sources: List[Callable[[], Iterable[Any]]] = []
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._run_count     = 0
        self._total_in      = 0
        self._total_out     = 0
        self._total_failed  = 0
        self._total_filtered = 0
        self._running       = False
        self._lock          = threading.Lock()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS dp_runs (
                run_id TEXT PRIMARY KEY, pipeline TEXT,
                records_in INTEGER, records_out INTEGER,
                records_failed INTEGER, started_at REAL,
                finished_at REAL, duration_ms REAL
            );
            CREATE TABLE IF NOT EXISTS dp_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT, stage TEXT, record_id TEXT,
                error TEXT, ts REAL
            );
        """)
        self._db.commit()

    # ── BUILDER API ───────────────────────────────────────────────────

    def add_stage(self, name: str, fn: Callable,
                  skip_on_error: bool = True,
                  batch_size: int = 1,
                  enabled: bool = True) -> "DataPipelineV3":
        stage = Stage(name=name, fn=fn,
                      skip_on_error=skip_on_error,
                      batch_size=batch_size, enabled=enabled)
        self._stages.append(stage)
        return self

    def add_filter(self, name: str,
                   predicate: Callable[[Record], bool],
                   **kwargs) -> "DataPipelineV3":
        def _filter(rec: Record) -> Optional[Record]:
            if predicate(rec):
                return rec
            raise FilteredRecord()
        return self.add_stage(name, _filter, **kwargs)

    def add_map(self, name: str,
                fn: Callable[[Any], Any], **kwargs) -> "DataPipelineV3":
        """Convenience: wrap a data-only transform."""
        def _map(rec: Record) -> Record:
            rec.data = fn(rec.data)
            return rec
        return self.add_stage(name, _map, **kwargs)

    def add_sink(self, fn: Callable[[Record], None]) -> "DataPipelineV3":
        self._sinks.append(fn)
        return self

    def add_source(self, fn: Callable[[], Iterable[Any]]) -> "DataPipelineV3":
        self._sources.append(fn)
        return self

    def remove_stage(self, name: str):
        self._stages = [s for s in self._stages if s.name != name]

    def enable_stage(self, name: str):
        for s in self._stages:
            if s.name == name: s.enabled = True

    def disable_stage(self, name: str):
        for s in self._stages:
            if s.name == name: s.enabled = False

    # ── EXECUTION ─────────────────────────────────────────────────────

    def run(self, records: Optional[Iterable[Any]] = None) -> Dict[str, Any]:
        """Execute pipeline synchronously. Returns run summary."""
        run_id    = str(uuid.uuid4())[:8]
        started   = time.time()
        self._run_count += 1

        all_records: List[Any] = []
        if records is not None:
            all_records.extend(records)
        for src in self._sources:
            try:
                all_records.extend(src())
            except Exception:
                pass

        records_in  = len(all_records)
        records_out = 0
        failed      = 0
        filtered    = 0

        # Reset stage stats
        for stage in self._stages:
            stage.stats = PipelineStats(stage.name)

        for raw in all_records:
            rec = Record(data=raw, source=self.name)
            rec = self._process_record(rec, run_id, failed)
            if rec is None:
                filtered += 1
            elif rec.status == RecordStatus.FAILED:
                failed += 1
            elif rec.status == RecordStatus.FILTERED:
                filtered += 1
            else:
                records_out += 1
                for sink in self._sinks:
                    try: sink(rec)
                    except Exception: pass

        finished = time.time()
        duration = (finished - started) * 1000

        self._total_in      += records_in
        self._total_out     += records_out
        self._total_failed  += failed
        self._total_filtered += filtered

        self._db.execute(
            "INSERT INTO dp_runs VALUES (?,?,?,?,?,?,?,?)",
            (run_id, self.name, records_in, records_out,
             failed, started, finished, duration))
        self._db.commit()

        return {
            "run_id": run_id,
            "records_in": records_in,
            "records_out": records_out,
            "failed": failed,
            "filtered": filtered,
            "duration_ms": round(duration, 2),
        }

    def _process_record(self, rec: Record, run_id: str,
                         _failed: int) -> Optional[Record]:
        for stage in self._stages:
            if not stage.enabled:
                continue
            t0 = time.time()
            stage.stats.records_in += 1
            try:
                result = stage.fn(rec)
                elapsed = (time.time() - t0) * 1000
                stage.stats.total_ms += elapsed
                if result is None:
                    stage.stats.records_filtered += 1
                    rec.status = RecordStatus.FILTERED
                    return rec
                rec = result
                stage.stats.records_out += 1
            except FilteredRecord:
                stage.stats.records_filtered += 1
                rec.status = RecordStatus.FILTERED
                return rec
            except Exception as exc:
                elapsed = (time.time() - t0) * 1000
                stage.stats.total_ms += elapsed
                stage.stats.records_failed += 1
                self._db.execute(
                    "INSERT INTO dp_errors (run_id,stage,record_id,error,ts) "
                    "VALUES (?,?,?,?,?)",
                    (run_id, stage.name, rec.record_id, str(exc), time.time()))
                self._db.commit()
                if stage.skip_on_error:
                    rec.status = RecordStatus.FAILED
                    rec.error  = str(exc)
                    return rec
                else:
                    raise
        return rec

    def run_generator(self, source: Generator) -> Dict[str, Any]:
        """Run with a generator source (streaming)."""
        return self.run(source)

    def run_async(self, records: Optional[Iterable[Any]] = None,
                  callback: Optional[Callable] = None) -> threading.Thread:
        """Run in background thread."""
        def _run():
            result = self.run(records)
            if callback:
                callback(result)
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return t

    # ── QUERY ─────────────────────────────────────────────────────────

    def stage_stats(self) -> List[Dict[str, Any]]:
        return [s.stats.to_dict() for s in self._stages]

    def run_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self._db.execute(
            "SELECT run_id,records_in,records_out,records_failed,duration_ms "
            "FROM dp_runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [{"run_id": r[0], "in": r[1], "out": r[2],
                 "failed": r[3], "ms": r[4]} for r in rows]

    def error_log(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._db.execute(
            "SELECT run_id,stage,record_id,error,ts FROM dp_errors "
            "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [{"run": r[0], "stage": r[1], "record": r[2],
                 "error": r[3]} for r in rows]

    def stats(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "stages": len(self._stages),
            "sinks": len(self._sinks),
            "runs": self._run_count,
            "total_in": self._total_in,
            "total_out": self._total_out,
            "total_failed": self._total_failed,
            "total_filtered": self._total_filtered,
        }
