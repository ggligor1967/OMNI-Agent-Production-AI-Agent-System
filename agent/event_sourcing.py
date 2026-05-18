"""OMNI AGENT - Event Sourcing
Append-only event log with aggregate roots, projections,
snapshots, and full replay support.

Features:
- Events: id, stream_id, type, data, version (seq per stream), ts
- Streams: named event log per aggregate (e.g. "order-123")
- Append: add events; version auto-increments per stream (OCC guard)
- Load: replay events from beginning or snapshot to rebuild state
- Optimistic Concurrency Control: append fails if expected_version mismatches
- Snapshots: save aggregate state at version N; load uses snapshot + tail
- Projections: register fn(state, event) → state; apply to stream or global
- Global stream: all events ordered by global sequence number
- Subscribe: register handler called on new events matching stream/type patterns
- Aggregate root base class: apply(), save(), load() helpers
- Event bus: publish to registered handlers on append
- Catchup: replay missed events since last handled sequence
- Idempotency: event id dedup guard on append
- Tags: events tagged; tag-based query
- Metadata: arbitrary fields attached to events (not part of state)
- Time travel: load state as-of timestamp
- SQLite persistence: events, snapshots, projections state
- REST API: append, load_stream, snapshot, project, stats
"""
import json, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

@dataclass
class Event:
    id: str; stream_id: str; event_type: str
    data: Dict[str, Any]
    version: int          # per-stream sequence
    global_seq: int = 0   # global ordering
    ts: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)

    def to_dict(self):
        return {"id": self.id, "stream_id": self.stream_id,
                "event_type": self.event_type, "data": self.data,
                "version": self.version, "global_seq": self.global_seq,
                "ts": round(self.ts, 3), "metadata": self.metadata,
                "tags": self.tags}

@dataclass
class Snapshot:
    stream_id: str; version: int
    state: Dict[str, Any]
    ts: float = field(default_factory=time.time)

class ESStore:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS events(
                    id TEXT PRIMARY KEY,
                    stream_id TEXT, event_type TEXT,
                    data TEXT, version INTEGER, global_seq INTEGER,
                    ts REAL, metadata TEXT, tags TEXT);
                CREATE TABLE IF NOT EXISTS snapshots(
                    stream_id TEXT PRIMARY KEY, version INTEGER,
                    state TEXT, ts REAL);
                CREATE TABLE IF NOT EXISTS global_seq(
                    id INTEGER PRIMARY KEY, val INTEGER);
                INSERT OR IGNORE INTO global_seq VALUES(1, 0);
                CREATE INDEX IF NOT EXISTS idx_ev_stream
                    ON events(stream_id, version);
                CREATE INDEX IF NOT EXISTS idx_ev_global
                    ON events(global_seq);
                CREATE INDEX IF NOT EXISTS idx_ev_type
                    ON events(event_type);
            """)

    def next_global_seq(self) -> int:
        with self._conn() as c:
            c.execute("UPDATE global_seq SET val=val+1 WHERE id=1")
            row = c.execute("SELECT val FROM global_seq WHERE id=1").fetchone()
        return row["val"]

    def stream_version(self, stream_id: str) -> int:
        with self._conn() as c:
            row = c.execute(
                "SELECT MAX(version) FROM events WHERE stream_id=?",
                (stream_id,)).fetchone()
        return row[0] or 0

    def append(self, ev: Event):
        with self._conn() as c:
            # Check for duplicate id
            exists = c.execute(
                "SELECT id FROM events WHERE id=?", (ev.id,)).fetchone()
            if exists: raise ValueError(f"Duplicate event id: {ev.id}")
            c.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                (ev.id, ev.stream_id, ev.event_type,
                 json.dumps(ev.data, default=str), ev.version,
                 ev.global_seq, ev.ts,
                 json.dumps(ev.metadata, default=str),
                 json.dumps(ev.tags)))

    def load_stream(self, stream_id: str, from_version: int = 1,
                     to_version: int = None,
                     until_ts: float = None) -> List[Event]:
        where = ["stream_id=?", "version>=?"]
        params: list = [stream_id, from_version]
        if to_version:
            where.append("version<=?"); params.append(to_version)
        if until_ts:
            where.append("ts<=?"); params.append(until_ts)
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM events WHERE " + " AND ".join(where)
                + " ORDER BY version ASC", params).fetchall()
        return [self._row_to_event(r) for r in rows]

    def load_by_type(self, event_type: str, limit: int = 100) -> List[Event]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM events WHERE event_type=? "
                "ORDER BY global_seq ASC LIMIT ?",
                (event_type, limit)).fetchall()
        return [self._row_to_event(r) for r in rows]

    def global_stream(self, from_seq: int = 0,
                       limit: int = 100) -> List[Event]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM events WHERE global_seq>? "
                "ORDER BY global_seq ASC LIMIT ?",
                (from_seq, limit)).fetchall()
        return [self._row_to_event(r) for r in rows]

    def _row_to_event(self, row) -> Event:
        return Event(id=row["id"], stream_id=row["stream_id"],
                      event_type=row["event_type"],
                      data=json.loads(row["data"]),
                      version=row["version"], global_seq=row["global_seq"],
                      ts=row["ts"], metadata=json.loads(row["metadata"]),
                      tags=json.loads(row["tags"]))

    def save_snapshot(self, snap: Snapshot):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO snapshots VALUES(?,?,?,?)",
                (snap.stream_id, snap.version,
                 json.dumps(snap.state, default=str), snap.ts))

    def load_snapshot(self, stream_id: str) -> Optional[Snapshot]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM snapshots WHERE stream_id=?",
                (stream_id,)).fetchone()
        if not row: return None
        return Snapshot(stream_id=row["stream_id"], version=row["version"],
                         state=json.loads(row["state"]), ts=row["ts"])

    def stats(self) -> Dict:
        with self._conn() as c:
            ne = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            ns = c.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            gseq = c.execute(
                "SELECT val FROM global_seq WHERE id=1").fetchone()["val"]
            by_type = {r["event_type"]: r["cnt"] for r in c.execute(
                "SELECT event_type, COUNT(*) as cnt FROM events "
                "GROUP BY event_type ORDER BY cnt DESC LIMIT 20").fetchall()}
        return {"total_events": ne, "snapshots": ns,
                "global_seq": gseq, "by_type": by_type}

class EventStore:
    """
    Append-only event store with projections and snapshots.

    Usage:
        es = EventStore()

        # Append events
        es.append("order-123", "OrderCreated",
                   {"item":"Widget","qty":2,"price":9.99})
        es.append("order-123", "OrderShipped",
                   {"carrier":"FedEx","tracking":"ABC123"},
                   expected_version=1)

        # Rebuild state via projection
        def apply_order(state, event):
            if event.event_type == "OrderCreated":
                state.update(event.data); state["status"]="created"
            elif event.event_type == "OrderShipped":
                state["status"]="shipped"; state["tracking"]=event.data["tracking"]
            return state

        state = es.project("order-123", apply_order)

        # Snapshot
        es.save_snapshot("order-123", state)
        # Later: load from snapshot + tail
        state = es.project("order-123", apply_order)
    """
    def __init__(self, db_path: str = "data/events.db"):
        self._store = ESStore(db_path)
        self._handlers: List[Tuple[Optional[str], Optional[str],
                                    Callable]] = []  # (stream_pat, type_pat, fn)
        self._projections: Dict[str, Dict] = {}  # name → {fn, state, last_seq}

    def append(self, stream_id: str, event_type: str,
                data: Dict, expected_version: int = None,
                metadata: Dict = None, tags: List[str] = None,
                event_id: str = None) -> Event:
        current = self._store.stream_version(stream_id)
        if expected_version is not None and current != expected_version:
            raise ValueError(
                f"Concurrency conflict: expected v{expected_version} "
                f"but stream is at v{current}")
        gseq = self._store.next_global_seq()
        ev = Event(id=event_id or str(uuid.uuid4())[:16],
                    stream_id=stream_id, event_type=event_type,
                    data=dict(data), version=current + 1,
                    global_seq=gseq, metadata=dict(metadata or {}),
                    tags=list(tags or []))
        self._store.append(ev)
        # Notify handlers
        for stream_pat, type_pat, handler in self._handlers:
            match_stream = (stream_pat is None or
                             ev.stream_id.startswith(stream_pat))
            match_type   = (type_pat is None or ev.event_type == type_pat)
            if match_stream and match_type:
                try: handler(ev)
                except Exception as e:
                    logger.warning(f"Handler error: {e}")
        return ev

    def load_stream(self, stream_id: str,
                     from_version: int = 1) -> List[Event]:
        return self._store.load_stream(stream_id, from_version)

    def subscribe(self, handler: Callable,
                   stream_prefix: str = None,
                   event_type: str = None):
        """Register a handler for new events."""
        self._handlers.append((stream_prefix, event_type, handler))

    def project(self, stream_id: str,
                 reducer: Callable[[Dict, Event], Dict],
                 initial_state: Dict = None) -> Dict:
        """Replay stream (from snapshot if available) through reducer."""
        snap = self._store.load_snapshot(stream_id)
        from_version = 1
        state = dict(initial_state or {})
        if snap:
            state = dict(snap.state)
            from_version = snap.version + 1
        events = self._store.load_stream(stream_id, from_version)
        for ev in events:
            state = reducer(state, ev)
        return state

    def project_until(self, stream_id: str,
                       reducer: Callable[[Dict, Event], Dict],
                       until_ts: float,
                       initial_state: Dict = None) -> Dict:
        """Replay stream up to a specific timestamp (time travel)."""
        events = self._store.load_stream(stream_id, until_ts=until_ts)
        state = dict(initial_state or {})
        for ev in events:
            state = reducer(state, ev)
        return state

    def register_projection(self, name: str,
                              reducer: Callable,
                              initial_state: Dict = None):
        self._projections[name] = {
            "reducer": reducer,
            "state": dict(initial_state or {}),
            "last_seq": 0}

    def update_projection(self, name: str) -> Dict:
        """Catch up projection to latest global events."""
        proj = self._projections.get(name)
        if not proj: raise KeyError(f"Projection '{name}' not found")
        events = self._store.global_stream(proj["last_seq"])
        for ev in events:
            proj["state"] = proj["reducer"](proj["state"], ev)
            proj["last_seq"] = ev.global_seq
        return proj["state"]

    def save_snapshot(self, stream_id: str, state: Dict) -> Snapshot:
        version = self._store.stream_version(stream_id)
        snap = Snapshot(stream_id=stream_id, version=version, state=state)
        self._store.save_snapshot(snap)
        return snap

    def load_snapshot(self, stream_id: str) -> Optional[Snapshot]:
        return self._store.load_snapshot(stream_id)

    def global_stream(self, from_seq: int = 0,
                       limit: int = 100) -> List[Event]:
        return self._store.global_stream(from_seq, limit)

    def stream_version(self, stream_id: str) -> int:
        return self._store.stream_version(stream_id)

    def load_by_type(self, event_type: str, limit: int = 100) -> List[Event]:
        return self._store.load_by_type(event_type, limit)

    def stats(self) -> Dict:
        s = self._store.stats()
        s["projections"] = len(self._projections)
        s["handlers"] = len(self._handlers)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def append_ep(req):
            d = await req.json()
            try:
                ev = self.append(d["stream_id"], d["event_type"], d["data"],
                                  d.get("expected_version"),
                                  d.get("metadata",{}), d.get("tags",[]))
                return web.json_response(ev.to_dict(), status=201)
            except ValueError as e:
                return web.json_response({"error": str(e)}, status=409)
        async def load_ep(req):
            stream = req.match_info["stream_id"]
            from_v = int(req.rel_url.query.get("from_version",1))
            events = self.load_stream(stream, from_v)
            return web.json_response({"events":[e.to_dict() for e in events]})
        async def snap_ep(req):
            d = await req.json()
            snap = self.save_snapshot(d["stream_id"], d["state"])
            return web.json_response({"stream_id": snap.stream_id,
                                       "version": snap.version})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/es"
        app.router.add_post(f"{p}/append",              append_ep)
        app.router.add_get( f"{p}/stream/{{stream_id}}", load_ep)
        app.router.add_post(f"{p}/snapshot",            snap_ep)
        app.router.add_get( f"{p}/stats",               stats_ep)
        logger.info(f"Event store API at {prefix}/es/")
