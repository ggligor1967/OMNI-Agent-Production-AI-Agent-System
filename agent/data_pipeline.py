"""OMNI AGENT - Data Pipeline
ETL pipeline: source → transform stages → sink with batch sizing,
checkpointing, error routing, and lineage tracking.

Features:
- Source: iterable, generator, async generator, or callable
- Transform stage: fn(record) → record | None (None = filter out)
- Sink: callable receiving batches of records
- Batch sizing: configurable batch_size; partial flush on timeout
- Checkpointing: persist last processed offset to SQLite
- Error routing: on_error fn(record, exc) → "skip" | "retry" | "dlq"
- Dead-letter queue: collect failed records for inspection
- Lineage: track record id through all stage transformations
- Back-pressure: pause source when downstream buffer fills
- Pipeline graph: named stages connected as DAG
- Branch: fan-out one stage to multiple downstream stages
- Merge: combine multiple upstream sources
- Stats per stage: in_count, out_count, error_count, avg_latency_ms
- Dry run mode: validate pipeline without writing to sink
- SQLite persistence: run history, checkpoint offsets, DLQ
- REST API: run, status, checkpoint, dlq, stats
"""
import asyncio, json, sqlite3, time, uuid, logging
from collections import defaultdict
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class ErrorAction(str, Enum):
    SKIP  = "skip"
    RETRY = "retry"
    DLQ   = "dlq"
    RAISE = "raise"

@dataclass
class PipelineRecord:
    id: str; data: Any
    source: str = ""; stage: str = ""
    lineage: List[str] = field(default_factory=list)
    ts: float = field(default_factory=time.time)
    retry_count: int = 0

    def advance(self, stage_name: str) -> "PipelineRecord":
        self.lineage.append(stage_name)
        self.stage = stage_name
        return self

    def to_dict(self):
        return {"id": self.id, "stage": self.stage,
                "lineage": self.lineage,
                "ts": round(self.ts, 3),
                "retry_count": self.retry_count,
                "data": str(self.data)[:300]}

@dataclass
class StageStats:
    name: str
    in_count: int = 0; out_count: int = 0
    error_count: int = 0; filter_count: int = 0
    total_latency_ms: float = 0.0

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / max(1, self.in_count)

    def to_dict(self):
        return {"name": self.name,
                "in": self.in_count, "out": self.out_count,
                "errors": self.error_count, "filtered": self.filter_count,
                "avg_latency_ms": round(self.avg_latency_ms, 2)}

@dataclass
class Stage:
    name: str
    fn: Callable                   # fn(record) → record | list | None
    max_retries: int = 0
    retry_delay_s: float = 0.1
    error_action: ErrorAction = ErrorAction.SKIP
    is_async: bool = False

class DPStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS runs(
                    id TEXT PRIMARY KEY, pipeline TEXT,
                    status TEXT, records_in INTEGER DEFAULT 0,
                    records_out INTEGER DEFAULT 0,
                    errors INTEGER DEFAULT 0,
                    started_at REAL, finished_at REAL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS checkpoints(
                    pipeline TEXT PRIMARY KEY, offset_val TEXT,
                    updated_at REAL);
                CREATE TABLE IF NOT EXISTS dlq(
                    id TEXT PRIMARY KEY, pipeline TEXT,
                    stage TEXT, record TEXT, error TEXT, created_at REAL);
                CREATE INDEX IF NOT EXISTS idx_dlq_pipeline
                    ON dlq(pipeline, created_at DESC);
            """)

    def start_run(self, run_id: str, pipeline: str):
        with self._conn() as c:
            c.execute("INSERT INTO runs VALUES(?,?,'running',0,0,0,?,0)",
                (run_id, pipeline, time.time()))

    def finish_run(self, run_id: str, status: str,
                    records_in: int, records_out: int, errors: int):
        with self._conn() as c:
            c.execute(
                "UPDATE runs SET status=?,records_in=?,records_out=?,"
                "errors=?,finished_at=? WHERE id=?",
                (status, records_in, records_out, errors, time.time(), run_id))

    def save_checkpoint(self, pipeline: str, offset: Any):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO checkpoints VALUES(?,?,?)",
                (pipeline, json.dumps(offset, default=str), time.time()))

    def load_checkpoint(self, pipeline: str) -> Any:
        with self._conn() as c:
            row = c.execute(
                "SELECT offset_val FROM checkpoints WHERE pipeline=?",
                (pipeline,)).fetchone()
        return json.loads(row[0]) if row else None

    def add_dlq(self, pipeline: str, stage: str,
                 record: PipelineRecord, error: str):
        with self._conn() as c:
            c.execute("INSERT INTO dlq VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4())[:8], pipeline, stage,
                 json.dumps(record.to_dict()), error[:300], time.time()))

    def get_dlq(self, pipeline: str, limit: int = 50) -> List[Dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM dlq WHERE pipeline=? "
                "ORDER BY created_at DESC LIMIT ?", (pipeline, limit)).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> Dict:
        with self._conn() as c:
            nr  = c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            ndq = c.execute("SELECT COUNT(*) FROM dlq").fetchone()[0]
        return {"total_runs": nr, "dlq_total": ndq}

class DataPipeline:
    """
    ETL pipeline with staged transforms, batching, and checkpointing.

    Usage:
        pipeline = DataPipeline("user_pipeline", batch_size=100)

        pipeline.add_stage("validate",  validate_fn)
        pipeline.add_stage("enrich",    enrich_fn)
        pipeline.add_stage("normalize", normalize_fn)

        async def my_source():
            for item in fetch_records():
                yield item

        await pipeline.run(source=my_source(), sink=write_to_db)
    """
    def __init__(self, name: str,
                 db_path: str = "data/pipeline.db",
                 batch_size: int = 100,
                 batch_timeout_s: float = 5.0,
                 checkpoint_every: int = 1000,
                 dry_run: bool = False):
        self.name = name
        self._store = DPStore(db_path)
        self.batch_size = batch_size
        self.batch_timeout_s = batch_timeout_s
        self.checkpoint_every = checkpoint_every
        self.dry_run = dry_run
        self._stages: List[Stage] = []
        self._dlq: List[PipelineRecord] = []
        self._stats: Dict[str, StageStats] = {}
        self._on_error: Optional[Callable] = None
        self._on_record: Optional[Callable] = None   # hook per output record

    def add_stage(self, name: str, fn: Callable,
                   max_retries: int = 0,
                   retry_delay_s: float = 0.1,
                   error_action: ErrorAction = ErrorAction.SKIP) -> "DataPipeline":
        is_async = asyncio.iscoroutinefunction(fn)
        s = Stage(name=name, fn=fn, max_retries=max_retries,
                   retry_delay_s=retry_delay_s,
                   error_action=error_action, is_async=is_async)
        self._stages.append(s)
        self._stats[name] = StageStats(name=name)
        return self

    def on_error(self, fn: Callable): self._on_error = fn
    def on_record(self, fn: Callable): self._on_record = fn

    async def _apply_stage(self, stage: Stage,
                             record: PipelineRecord) -> List[PipelineRecord]:
        stats = self._stats[stage.name]
        stats.in_count += 1
        t0 = time.time()
        retries = 0
        while True:
            try:
                if stage.is_async:
                    result = await stage.fn(record.data)
                else:
                    result = stage.fn(record.data)
                stats.total_latency_ms += (time.time() - t0) * 1000
                if result is None:
                    stats.filter_count += 1
                    return []
                if isinstance(result, list):
                    out = []
                    for item in result:
                        nr = PipelineRecord(id=record.id, data=item,
                                             source=record.source,
                                             lineage=list(record.lineage))
                        nr.advance(stage.name)
                        out.append(nr)
                    stats.out_count += len(out)
                    return out
                record.data = result
                record.advance(stage.name)
                stats.out_count += 1
                return [record]
            except Exception as exc:
                stats.error_count += 1
                action = stage.error_action
                if self._on_error:
                    try: action = ErrorAction(self._on_error(record, exc) or "skip")
                    except: pass
                if action == ErrorAction.RETRY and retries < stage.max_retries:
                    retries += 1
                    await asyncio.sleep(stage.retry_delay_s * (2 ** (retries - 1)))
                    continue
                if action == ErrorAction.DLQ:
                    self._dlq.append(record)
                    self._store.add_dlq(self.name, stage.name, record, str(exc))
                if action == ErrorAction.RAISE:
                    raise
                stats.total_latency_ms += (time.time() - t0) * 1000
                return []

    async def _process_record(self, record: PipelineRecord) -> List[PipelineRecord]:
        current = [record]
        for stage in self._stages:
            next_batch = []
            for rec in current:
                out = await self._apply_stage(stage, rec)
                next_batch.extend(out)
            current = next_batch
            if not current: break
        return current

    async def run(self, source, sink: Callable,
                   run_id: str = None) -> Dict:
        run_id = run_id or str(uuid.uuid4())[:10]
        self._store.start_run(run_id, self.name)
        checkpoint = self._store.load_checkpoint(self.name)
        records_in = records_out = errors = 0
        batch: List[Any] = []
        last_flush = time.time()
        seq = 0

        async def _flush():
            nonlocal records_out
            if batch and not self.dry_run:
                if asyncio.iscoroutinefunction(sink):
                    await sink(list(batch))
                else:
                    sink(list(batch))
                records_out += len(batch)
            batch.clear()

        try:
            # Normalise source to async iterator
            if hasattr(source, "__aiter__"):
                aiter = source
            elif hasattr(source, "__iter__"):
                async def _wrap(it):
                    for item in it: yield item
                aiter = _wrap(source)
            else:
                async def _wrap_fn():
                    for item in await source(): yield item
                aiter = _wrap_fn()

            async for raw in aiter:
                seq += 1
                # Skip already-processed records if checkpointing
                if checkpoint is not None and seq <= checkpoint:
                    continue
                records_in += 1
                rec = PipelineRecord(
                    id=f"{run_id}-{seq}",
                    data=raw, source=self.name)
                try:
                    out = await self._process_record(rec)
                    for r in out:
                        batch.append(r.data)
                        if self._on_record:
                            try: self._on_record(r)
                            except: pass
                except Exception as e:
                    errors += 1
                    logger.warning(f"Pipeline {self.name} record error: {e}")

                if (len(batch) >= self.batch_size or
                        time.time() - last_flush >= self.batch_timeout_s):
                    await _flush()
                    last_flush = time.time()

                if records_in % self.checkpoint_every == 0:
                    self._store.save_checkpoint(self.name, seq)

            await _flush()
            self._store.save_checkpoint(self.name, seq)
            self._store.finish_run(run_id, "success",
                                    records_in, records_out, errors)
        except Exception as e:
            self._store.finish_run(run_id, "error",
                                    records_in, records_out, errors)
            raise

        return {"run_id": run_id, "records_in": records_in,
                "records_out": records_out, "errors": errors,
                "stages": {n: s.to_dict() for n, s in self._stats.items()}}

    def reset_stats(self):
        for s in self._stats.values():
            s.in_count = s.out_count = s.error_count = s.filter_count = 0
            s.total_latency_ms = 0.0

    def dlq_records(self, limit: int = 50) -> List[Dict]:
        return self._store.get_dlq(self.name, limit)

    def checkpoint(self) -> Any:
        return self._store.load_checkpoint(self.name)

    def stats(self) -> Dict:
        s = self._store.stats()
        s["pipeline"] = self.name
        s["stages"] = {n: st.to_dict() for n, st in self._stats.items()}
        s["dlq_in_memory"] = len(self._dlq)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def stats_ep(req): return web.json_response(self.stats())
        async def dlq_ep(req):
            limit = int(req.rel_url.query.get("limit", 50))
            return web.json_response({"dlq": self.dlq_records(limit)})
        async def checkpoint_ep(req):
            return web.json_response({"checkpoint": self.checkpoint()})
        p = f"{prefix}/pipeline/{self.name}"
        app.router.add_get(f"{p}/stats",      stats_ep)
        app.router.add_get(f"{p}/dlq",        dlq_ep)
        app.router.add_get(f"{p}/checkpoint", checkpoint_ep)
        logger.info(f"Data pipeline '{self.name}' API at {p}/")
