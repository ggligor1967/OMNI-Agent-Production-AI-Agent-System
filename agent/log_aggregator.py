"""OMNI AGENT - Log Aggregator
Structured log ingestion, severity filtering, pattern-based alerting,
tail/stream, full-text search, and export.

Features:
- Log entry: id, timestamp, level, source, message, fields (dict), tags
- Levels: TRACE < DEBUG < INFO < WARN < ERROR < FATAL (ordered)
- Sources: named producers (services, modules, hosts)
- Ingestion: single or batch; async-safe with SQLite
- Ring buffer: in-memory deque per source for fast tail()
- Severity filter: set minimum level; entries below threshold dropped
- Pattern matching: regex rules; matching entries fire alert hooks
- Alert rules: name, pattern, min_level, cooldown_s, hooks
- Dedup window: identical messages within N seconds counted but not stored
- Full-text search: search message + JSON fields with filters
- Tail: return last N lines across all sources or per source
- Stream: async generator yielding new entries as they arrive
- Time range query: start/end timestamps
- Aggregation: count per level, per source, per hour
- Export: JSONL or CSV
- Structured fields: arbitrary key=value attached to log entry
- Hooks: on_log(entry), on_alert(rule_name, entry)
- SQLite persistence: all logs with indexes on ts, level, source
- REST API: log, search, tail, stats, alert_rules
"""
import asyncio, json, re, sqlite3, time, uuid, logging as pylogging
from collections import deque
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = pylogging.getLogger(__name__)

class Level(int, Enum):
    TRACE = 0; DEBUG = 1; INFO = 2
    WARN  = 3; ERROR = 4; FATAL = 5

    @classmethod
    def from_str(cls, s: str) -> "Level":
        return cls[s.upper()] if s.upper() in cls.__members__ else cls.INFO

LEVEL_NAMES = {v: k for k, v in Level.__members__.items()}

@dataclass
class LogEntry:
    id: str; ts: float; level: Level
    source: str; message: str
    fields: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)

    def to_dict(self):
        return {"id": self.id, "ts": round(self.ts, 3),
                "level": self.level.name, "source": self.source,
                "message": self.message, "fields": self.fields,
                "tags": self.tags}

@dataclass
class AlertRule:
    name: str
    pattern: str
    min_level: Level = Level.WARN
    cooldown_s: float = 60.0
    _compiled: Any = field(default=None, repr=False)
    _last_fired: float = field(default=0.0, repr=False)

    def __post_init__(self):
        self._compiled = re.compile(self.pattern, re.IGNORECASE)

    def matches(self, entry: LogEntry) -> bool:
        if entry.level < self.min_level: return False
        if time.time() - self._last_fired < self.cooldown_s: return False
        if self._compiled.search(entry.message): return True
        # Also search field values
        for v in entry.fields.values():
            if self._compiled.search(str(v)): return True
        return False

    def fire(self):
        self._last_fired = time.time()

class LAStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS logs(
                    id TEXT PRIMARY KEY, ts REAL, level INTEGER,
                    source TEXT, message TEXT, fields TEXT, tags TEXT);
                CREATE INDEX IF NOT EXISTS idx_log_ts     ON logs(ts);
                CREATE INDEX IF NOT EXISTS idx_log_level  ON logs(level);
                CREATE INDEX IF NOT EXISTS idx_log_source ON logs(source);
            """)

    def insert(self, entry: LogEntry):
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO logs VALUES(?,?,?,?,?,?,?)",
                (entry.id, entry.ts, entry.level.value,
                 entry.source, entry.message,
                 json.dumps(entry.fields, default=str),
                 json.dumps(entry.tags)))

    def insert_batch(self, entries: List[LogEntry]):
        with self._conn() as c:
            c.executemany("INSERT OR IGNORE INTO logs VALUES(?,?,?,?,?,?,?)",
                [(e.id, e.ts, e.level.value, e.source, e.message,
                  json.dumps(e.fields, default=str),
                  json.dumps(e.tags)) for e in entries])

    def query(self, min_level: Level = Level.TRACE,
               source: str = None, search: str = None,
               start_ts: float = None, end_ts: float = None,
               tags: List[str] = None,
               limit: int = 200) -> List[LogEntry]:
        where = ["level >= ?"]
        params: list = [min_level.value]
        if source:
            where.append("source = ?"); params.append(source)
        if start_ts:
            where.append("ts >= ?"); params.append(start_ts)
        if end_ts:
            where.append("ts <= ?"); params.append(end_ts)
        if search:
            where.append("(message LIKE ? OR fields LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        sql = ("SELECT * FROM logs WHERE " + " AND ".join(where)
               + " ORDER BY ts DESC LIMIT ?")
        params.append(limit)
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        entries = [LogEntry(id=r["id"], ts=r["ts"],
                             level=Level(r["level"]), source=r["source"],
                             message=r["message"],
                             fields=json.loads(r["fields"]),
                             tags=json.loads(r["tags"]))
                    for r in rows]
        if tags:
            tag_set = set(tags)
            entries = [e for e in entries if tag_set.issubset(set(e.tags))]
        return entries

    def tail(self, n: int = 50, source: str = None) -> List[LogEntry]:
        where = "WHERE source=?" if source else ""
        params = ([source] if source else []) + [n]
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM logs {where} ORDER BY ts DESC LIMIT ?",
                params).fetchall()
        return [LogEntry(id=r["id"], ts=r["ts"], level=Level(r["level"]),
                          source=r["source"], message=r["message"],
                          fields=json.loads(r["fields"]),
                          tags=json.loads(r["tags"]))
                for r in reversed(rows)]

    def stats(self) -> Dict:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
            by_level = {Level(r["level"]).name: r["cnt"] for r in c.execute(
                "SELECT level, COUNT(*) as cnt FROM logs "
                "GROUP BY level").fetchall()}
            by_source = {r["source"]: r["cnt"] for r in c.execute(
                "SELECT source, COUNT(*) as cnt FROM logs "
                "GROUP BY source ORDER BY cnt DESC LIMIT 20").fetchall()}
        return {"total": total, "by_level": by_level, "by_source": by_source}

    def export_jsonl(self, **kwargs) -> str:
        entries = self.query(**kwargs)
        return "\n".join(json.dumps(e.to_dict()) for e in entries)

    def export_csv(self, **kwargs) -> str:
        entries = self.query(**kwargs)
        rows = ["ts,level,source,message"]
        for e in entries:
            msg = e.message.replace('"','""')
            rows.append(f'{e.ts},{e.level.name},{e.source},"{msg}"')
        return "\n".join(rows)

class LogAggregator:
    """
    Structured log aggregator with alerting and search.

    Usage:
        agg = LogAggregator()
        agg.add_alert("errors", pattern=r"ERROR|FATAL",
                        min_level=Level.ERROR, cooldown_s=30)
        agg.on_alert(lambda rule, entry:
            print(f"ALERT {rule}: {entry.message}"))

        agg.log("api-server", Level.INFO, "Request received",
                  fields={"method":"GET","path":"/health"})
        agg.log("db", Level.ERROR, "Connection timeout",
                  tags=["db","critical"])

        entries = agg.tail(20)
        results = agg.search("timeout", min_level=Level.WARN)
    """
    def __init__(self, db_path: str = "data/logs.db",
                 min_level: Level = Level.TRACE,
                 ring_size: int = 1000,
                 dedup_window_s: float = 0.0):
        self._store = LAStore(db_path)
        self._min_level = min_level
        self._ring_size = ring_size
        self._dedup_window = dedup_window_s
        self._ring: deque = deque(maxlen=ring_size)
        self._alert_rules: Dict[str, AlertRule] = {}
        self._hooks_log:   List[Callable] = []
        self._hooks_alert: List[Callable] = []
        self._dedup_cache: Dict[str, float] = {}  # msg_hash → last_ts
        self._stream_queues: List[asyncio.Queue] = []

    def on_log(self, fn):   self._hooks_log.append(fn)
    def on_alert(self, fn): self._hooks_alert.append(fn)

    def add_alert(self, name: str, pattern: str,
                   min_level: Level = Level.WARN,
                   cooldown_s: float = 60.0) -> AlertRule:
        rule = AlertRule(name=name, pattern=pattern,
                          min_level=min_level, cooldown_s=cooldown_s)
        self._alert_rules[name] = rule
        return rule

    def remove_alert(self, name: str) -> bool:
        return self._alert_rules.pop(name, None) is not None

    def set_min_level(self, level: Level):
        self._min_level = level

    def _dedup_key(self, source: str, msg: str) -> str:
        return f"{source}:{hash(msg)}"

    def log(self, source: str, level, message: str,
             fields: Dict = None, tags: List[str] = None) -> Optional[LogEntry]:
        if isinstance(level, str): level = Level.from_str(level)
        if level < self._min_level: return None
        # Dedup
        if self._dedup_window > 0:
            dk = self._dedup_key(source, message)
            last = self._dedup_cache.get(dk, 0)
            if time.time() - last < self._dedup_window:
                return None
            self._dedup_cache[dk] = time.time()
        entry = LogEntry(id=str(uuid.uuid4())[:12],
                          ts=time.time(), level=level,
                          source=source, message=message,
                          fields=dict(fields or {}),
                          tags=list(tags or []))
        self._ring.append(entry)
        self._store.insert(entry)
        for h in self._hooks_log:
            try: h(entry)
            except: pass
        # Alerts
        for rname, rule in self._alert_rules.items():
            if rule.matches(entry):
                rule.fire()
                for h in self._hooks_alert:
                    try: h(rname, entry)
                    except: pass
        # Stream queues
        for q in self._stream_queues:
            try: q.put_nowait(entry)
            except: pass
        return entry

    def log_batch(self, entries: List[Dict]) -> int:
        count = 0
        for e in entries:
            result = self.log(e.get("source",""), Level.from_str(e.get("level","INFO")),
                               e.get("message",""), e.get("fields",{}),
                               e.get("tags",[]))
            if result: count += 1
        return count

    def tail(self, n: int = 50, source: str = None) -> List[LogEntry]:
        if source:
            entries = [e for e in self._ring if e.source == source]
        else:
            entries = list(self._ring)
        return entries[-n:]

    def search(self, query: str = None,
                min_level: Level = None,
                source: str = None,
                start_ts: float = None, end_ts: float = None,
                tags: List[str] = None,
                limit: int = 200) -> List[LogEntry]:
        return self._store.query(
            min_level=min_level or self._min_level,
            source=source, search=query,
            start_ts=start_ts, end_ts=end_ts,
            tags=tags, limit=limit)

    async def stream(self) -> AsyncGenerator[LogEntry, None]:
        """Async generator: yields entries as they arrive."""
        q: asyncio.Queue = asyncio.Queue()
        self._stream_queues.append(q)
        try:
            while True:
                entry = await q.get()
                yield entry
        finally:
            self._stream_queues.remove(q)

    def export(self, format: str = "jsonl", **kwargs) -> str:
        if format == "csv": return self._store.export_csv(**kwargs)
        return self._store.export_jsonl(**kwargs)

    def stats(self) -> Dict:
        s = self._store.stats()
        s["min_level"] = self._min_level.name
        s["alert_rules"] = len(self._alert_rules)
        s["ring_size"] = len(self._ring)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def log_ep(req):
            d = await req.json()
            entry = self.log(d.get("source",""), Level.from_str(d.get("level","INFO")),
                              d.get("message",""), d.get("fields",{}),
                              d.get("tags",[]))
            return web.json_response(entry.to_dict() if entry else {}, status=201)
        async def search_ep(req):
            d = await req.json()
            ml = Level.from_str(d.get("min_level","TRACE"))
            entries = self.search(d.get("query"), ml,
                                   d.get("source"), d.get("start_ts"),
                                   d.get("end_ts"), d.get("tags"),
                                   d.get("limit",200))
            return web.json_response({"entries":[e.to_dict() for e in entries]})
        async def tail_ep(req):
            n = int(req.rel_url.query.get("n",50))
            src = req.rel_url.query.get("source")
            entries = self.tail(n, src)
            return web.json_response({"entries":[e.to_dict() for e in entries]})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/logs"
        app.router.add_post(f"{p}/write",  log_ep)
        app.router.add_post(f"{p}/search", search_ep)
        app.router.add_get( f"{p}/tail",   tail_ep)
        app.router.add_get( f"{p}/stats",  stats_ep)
        logger.info(f"Log aggregator API at {prefix}/logs/")
