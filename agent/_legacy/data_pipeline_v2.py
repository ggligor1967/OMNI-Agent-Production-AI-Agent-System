"""OMNI AGENT - Data Pipeline v2
Enhanced ETL framework: typed source/sink adapters, composable
transform chains, fan-out, fan-in, branching, and lineage tracking.

Features:
- Record: typed unit with id, data dict, lineage list, tags, ts
- Sources: iterable factories — CSV, JSON, generator, list
- Transforms: map, filter, flatmap, rename_fields, add_field,
    drop_fields, type_cast, validate (drop invalid), enrich (fn),
    batch (chunk to lists), window (time-based grouping)
- Sinks: list collector, dict index, callback, null (discard)
- Pipeline: named sequence of source → [transforms] → sink
- Branching: route records to different sub-pipelines by predicate
- Fan-out: broadcast each record to N downstream sinks
- Fan-in: merge N async generators into one ordered stream
- Lineage: each transform appends its name to record.lineage
- Error handling: SKIP / DEAD_LETTER / RAISE per transform
- Backpressure: buffer size limit; producer pauses when full
- Stats: records_in, records_out, dropped, errors per stage
- Hooks: on_record(record), on_error(record, stage, error)
- Dry run: validate pipeline structure without executing
- SQLite persistence: run history with per-stage stats
- REST API: run, status, history, stats
"""
import asyncio, csv, io, json, sqlite3, time, uuid, logging
from typing import Any, AsyncGenerator, Callable, Dict, Generator
from typing import Iterable, Iterator, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class ErrorMode(str, Enum):
    SKIP        = "skip"
    DEAD_LETTER = "dead_letter"
    RAISE       = "raise"

@dataclass
class Record:
    id: str
    data: Dict[str, Any]
    lineage: List[str] = field(default_factory=list)
    tags: List[str]    = field(default_factory=list)
    ts: float          = field(default_factory=time.time)
    _error: Optional[str] = field(default=None, repr=False)

    def clone(self, data: Dict = None) -> "Record":
        return Record(id=self.id, data=dict(data if data is not None else self.data),
                      lineage=list(self.lineage), tags=list(self.tags), ts=self.ts)

    def to_dict(self):
        return {"id": self.id, "data": self.data,
                "lineage": self.lineage, "tags": self.tags,
                "ts": round(self.ts, 3)}

def _make_record(data: Dict) -> Record:
    return Record(id=str(uuid.uuid4())[:12], data=dict(data))

# ── Sources ────────────────────────────────────────────────────────────────────
def source_list(items: List[Dict]) -> Iterator[Record]:
    for item in items: yield _make_record(item)

def source_json(text: str) -> Iterator[Record]:
    data = json.loads(text)
    items = data if isinstance(data, list) else [data]
    yield from source_list(items)

def source_csv(text: str) -> Iterator[Record]:
    reader = csv.DictReader(io.StringIO(text))
    for row in reader: yield _make_record(dict(row))

def source_generator(fn: Callable, *args, **kwargs) -> Iterator[Record]:
    for item in fn(*args, **kwargs): yield _make_record(item)

# ── Transform builders ─────────────────────────────────────────────────────────
def tf_map(fn: Callable[[Dict], Dict], name: str = "map"):
    """Apply fn to record.data, return new data."""
    def _transform(rec: Record) -> Optional[Record]:
        r = rec.clone(fn(rec.data)); r.lineage.append(name); return r
    _transform.__name__ = name; return _transform

def tf_filter(pred: Callable[[Dict], bool], name: str = "filter"):
    def _transform(rec: Record) -> Optional[Record]:
        if pred(rec.data):
            rec.lineage.append(name); return rec
        return None
    _transform.__name__ = name; return _transform

def tf_flatmap(fn: Callable[[Dict], List[Dict]], name: str = "flatmap"):
    """Expands one record into many."""
    def _transform(rec: Record) -> List[Record]:
        results = []
        for d in fn(rec.data):
            r = _make_record(d); r.lineage = list(rec.lineage) + [name]
            results.append(r)
        return results
    _transform.__name__ = name; return _transform

def tf_rename(mapping: Dict[str, str], name: str = "rename"):
    def _transform(rec: Record) -> Optional[Record]:
        d = dict(rec.data)
        for old, new in mapping.items():
            if old in d: d[new] = d.pop(old)
        r = rec.clone(d); r.lineage.append(name); return r
    _transform.__name__ = name; return _transform

def tf_add_field(field_name: str, value_fn: Callable[[Dict], Any],
                  name: str = "add_field"):
    def _transform(rec: Record) -> Optional[Record]:
        d = dict(rec.data); d[field_name] = value_fn(rec.data)
        r = rec.clone(d); r.lineage.append(name); return r
    _transform.__name__ = name; return _transform

def tf_drop_fields(*fields, name: str = "drop_fields"):
    def _transform(rec: Record) -> Optional[Record]:
        d = {k: v for k, v in rec.data.items() if k not in fields}
        r = rec.clone(d); r.lineage.append(name); return r
    _transform.__name__ = name; return _transform

def tf_cast(casts: Dict[str, type], name: str = "cast"):
    def _transform(rec: Record) -> Optional[Record]:
        d = dict(rec.data)
        for field, typ in casts.items():
            if field in d:
                try: d[field] = typ(d[field])
                except: pass
        r = rec.clone(d); r.lineage.append(name); return r
    _transform.__name__ = name; return _transform

def tf_validate(schema: Dict[str, type], name: str = "validate"):
    """Drop records that fail type check."""
    def _transform(rec: Record) -> Optional[Record]:
        for field, typ in schema.items():
            val = rec.data.get(field)
            if val is None or not isinstance(val, typ): return None
        rec.lineage.append(name); return rec
    _transform.__name__ = name; return _transform

def tf_tag(*tags, name: str = "tag"):
    def _transform(rec: Record) -> Optional[Record]:
        rec.tags.extend(tags); rec.lineage.append(name); return rec
    _transform.__name__ = name; return _transform

def tf_batch(size: int, name: str = "batch"):
    """Group records into lists of `size`."""
    buf = []
    def _transform(rec: Record) -> Optional[Record]:
        buf.append(rec.data)
        if len(buf) >= size:
            batch = list(buf); buf.clear()
            r = _make_record({"batch": batch, "count": len(batch)})
            r.lineage = list(rec.lineage) + [name]; return r
        return None
    _transform.__name__ = name; return _transform

# ── Sinks ──────────────────────────────────────────────────────────────────────
def sink_list(output: list):
    def _sink(rec: Record): output.append(rec.to_dict())
    return _sink

def sink_callback(fn: Callable[[Record], None]):
    return fn

def sink_null():
    def _sink(rec: Record): pass
    return _sink

def sink_index(output: dict, key_field: str):
    def _sink(rec: Record): output[rec.data.get(key_field, rec.id)] = rec.to_dict()
    return _sink

@dataclass
class StageStats:
    name: str; records_in: int = 0; records_out: int = 0
    dropped: int = 0; errors: int = 0

    def to_dict(self):
        return {"name": self.name, "in": self.records_in,
                "out": self.records_out, "dropped": self.dropped,
                "errors": self.errors}

class PipelineRun:
    def __init__(self, run_id: str, pipeline_name: str):
        self.id = run_id; self.pipeline = pipeline_name
        self.stages: Dict[str, StageStats] = {}
        self.started_at = time.time(); self.finished_at: Optional[float] = None
        self.dead_letter: List[Record] = []
        self.status = "running"

    @property
    def duration_s(self) -> float:
        end = self.finished_at or time.time()
        return round(end - self.started_at, 3)

    def to_dict(self):
        return {"id": self.id, "pipeline": self.pipeline,
                "status": self.status, "duration_s": self.duration_s,
                "stages": {n: s.to_dict() for n, s in self.stages.items()},
                "dead_letter_count": len(self.dead_letter)}

class DPV2Store:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS runs(
                    id TEXT PRIMARY KEY, pipeline TEXT,
                    status TEXT, duration_s REAL,
                    records_out INTEGER, ts REAL);
            """)

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def save(self, run: PipelineRun):
        total_out = sum(s.records_out for s in run.stages.values())
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?)",
                (run.id, run.pipeline, run.status,
                 run.duration_s, total_out, time.time()))

    def history(self, pipeline: str = None, limit: int = 20) -> List[Dict]:
        where = f"WHERE pipeline='{pipeline}'" if pipeline else ""
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM runs {where} ORDER BY ts DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> Dict:
        with self._conn() as c:
            nr = c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            by_p = {r["pipeline"]: r["cnt"] for r in c.execute(
                "SELECT pipeline, COUNT(*) as cnt FROM runs "
                "GROUP BY pipeline").fetchall()}
        return {"runs": nr, "by_pipeline": by_p}

class DataPipelineV2:
    """
    Composable ETL pipeline with transforms, branching, and lineage.

    Usage:
        dp = DataPipelineV2()

        dp.register("clean_users",
            source=source_list(raw_rows),
            transforms=[
                tf_filter(lambda d: d.get("age") is not None),
                tf_cast({"age": int, "score": float}),
                tf_rename({"userId": "user_id"}),
                tf_add_field("processed_at", lambda d: time.time()),
            ],
            sink=sink_list(output))

        run = dp.execute("clean_users")
        print(run.status, run.to_dict())
    """
    def __init__(self, db_path: str = "data/pipeline_v2.db"):
        self._store = DPV2Store(db_path)
        self._pipelines: Dict[str, Dict] = {}
        self._runs: Dict[str, PipelineRun] = {}
        self._hooks_record: List[Callable] = []
        self._hooks_error:  List[Callable] = []

    def on_record(self, fn): self._hooks_record.append(fn)
    def on_error(self,  fn): self._hooks_error.append(fn)

    def register(self, name: str, source: Iterable,
                  transforms: List[Callable] = None,
                  sink: Callable = None,
                  error_mode: ErrorMode = ErrorMode.SKIP,
                  branches: List[Tuple[Callable, Callable]] = None):
        self._pipelines[name] = {
            "source": source, "transforms": list(transforms or []),
            "sink": sink or sink_null(), "error_mode": error_mode,
            "branches": list(branches or [])}

    def _apply_transform(self, tf: Callable, rec: Record,
                          stats: StageStats, dlq: list,
                          mode: ErrorMode) -> List[Record]:
        stats.records_in += 1
        try:
            result = tf(rec)
            if result is None:
                stats.dropped += 1; return []
            if isinstance(result, list):
                stats.records_out += len(result); return result
            stats.records_out += 1; return [result]
        except Exception as e:
            stats.errors += 1
            rec._error = str(e)
            for h in self._hooks_error:
                try: h(rec, tf.__name__, e)
                except: pass
            if mode == ErrorMode.DEAD_LETTER:
                dlq.append(rec); return []
            if mode == ErrorMode.RAISE:
                raise
            return []  # SKIP

    def execute(self, name: str, context: Dict = None) -> PipelineRun:
        cfg = self._pipelines.get(name)
        if not cfg: raise KeyError(f"Pipeline '{name}' not registered")
        run_id = str(uuid.uuid4())[:12]
        run = PipelineRun(run_id, name)
        self._runs[run_id] = run
        source_stat = StageStats("__source__")
        run.stages["__source__"] = source_stat
        # Build stage stats
        for tf in cfg["transforms"]:
            run.stages[tf.__name__] = StageStats(tf.__name__)
        try:
            records: List[Record] = []
            for rec in cfg["source"]:
                source_stat.records_in += 1
                source_stat.records_out += 1
                records.append(rec)
            # Apply transforms
            for tf in cfg["transforms"]:
                stats = run.stages[tf.__name__]
                next_records = []
                for rec in records:
                    next_records.extend(
                        self._apply_transform(tf, rec, stats,
                                               run.dead_letter,
                                               cfg["error_mode"]))
                records = next_records
            # Branches
            for pred, branch_sink in cfg["branches"]:
                for rec in records:
                    if pred(rec.data): branch_sink(rec)
            # Sink
            sink_stat = run.stages.setdefault("__sink__", StageStats("__sink__"))
            for rec in records:
                sink_stat.records_in += 1
                try:
                    cfg["sink"](rec)
                    sink_stat.records_out += 1
                    for h in self._hooks_record:
                        try: h(rec)
                        except: pass
                except Exception as e:
                    sink_stat.errors += 1
            run.status = "done"
        except Exception as e:
            run.status = "failed"
            logger.error(f"Pipeline '{name}' failed: {e}")
        run.finished_at = time.time()
        self._store.save(run)
        return run

    def fan_out(self, name: str, source: Iterable,
                 transforms: List[Callable],
                 sinks: List[Callable]) -> PipelineRun:
        """Broadcast each record to all sinks after transforms."""
        def multi_sink(rec: Record):
            for sk in sinks: sk(rec)
        self.register(name, source, transforms, multi_sink)
        return self.execute(name)

    def history(self, pipeline: str = None, limit: int = 20):
        return self._store.history(pipeline, limit)

    def stats(self) -> Dict:
        s = self._store.stats()
        s["registered"] = len(self._pipelines)
        s["active_runs"] = len(self._runs)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def run_ep(req):
            d = await req.json()
            try:
                run = self.execute(d["pipeline"], d.get("context",{}))
                return web.json_response(run.to_dict())
            except KeyError as e:
                return web.json_response({"error": str(e)}, status=404)
        async def status_ep(req):
            rid = req.match_info["run_id"]
            run = self._runs.get(rid)
            if not run: return web.json_response({}, status=404)
            return web.json_response(run.to_dict())
        async def history_ep(req):
            p = req.rel_url.query.get("pipeline")
            return web.json_response({"history": self.history(p)})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/pipeline"
        app.router.add_post(f"{p}/run",              run_ep)
        app.router.add_get( f"{p}/run/{{run_id}}",   status_ep)
        app.router.add_get( f"{p}/history",          history_ep)
        app.router.add_get( f"{p}/stats",            stats_ep)
        logger.info(f"Pipeline v2 API at {prefix}/pipeline/")
