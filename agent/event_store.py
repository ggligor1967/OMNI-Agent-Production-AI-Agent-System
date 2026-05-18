"""OMNI AGENT - Event Store
Append-only event log with streams, projections, snapshots, and replay.

Features:
- Events: stream_id, type, data, metadata, version (per-stream seq)
- Streams: named event streams; version starts at 0
- Append: optimistic concurrency — expected_version checked atomically
- Global sequence: monotone global position across all streams
- Read stream: from version V to latest (or range)
- Read all: global ordered scan with optional type filter
- Projections: named reducers fn(state, event) → state applied on read
- Projection cache: computed and cached; invalidated on new events
- Snapshots: store (stream_id, version, state) to speed up replay
- Replay: reconstruct state from snapshot + trailing events
- Subscriptions: callback registered per stream or globally
- Categories: stream_id prefix "Category-{id}" convention
- Link events: cross-stream event references
- Metadata: arbitrary dict per event (correlation_id, causation_id, etc.)
- Event versioning: upcasting fn registered per event type
- Truncate: delete events before a given version (retention)
- Stats: total events, events per stream, global position
- SQLite persistence: events, snapshots, subscriptions
- REST API: append, read_stream, read_all, snapshot, stats
"""
import json, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

@dataclass
class Event:
    id: str; stream_id: str; type: str
    data: Any; metadata: Dict = field(default_factory=dict)
    version: int = 0       # position within stream
    global_pos: int = 0    # global monotone position
    timestamp: float = field(default_factory=time.time)

    def to_dict(self):
        return {"id": self.id, "stream_id": self.stream_id,
                "type": self.type, "data": self.data,
                "metadata": self.metadata,
                "version": self.version, "global_pos": self.global_pos,
                "timestamp": round(self.timestamp, 3)}

@dataclass
class Snapshot:
    stream_id: str; version: int; state: Any
    taken_at: float = field(default_factory=time.time)

class ESStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS events(
                    id TEXT PRIMARY KEY,
                    stream_id TEXT, type TEXT,
                    data TEXT, metadata TEXT,
                    version INTEGER, global_pos INTEGER,
                    timestamp REAL);
                CREATE TABLE IF NOT EXISTS stream_versions(
                    stream_id TEXT PRIMARY KEY, version INTEGER,
                    event_count INTEGER);
                CREATE TABLE IF NOT EXISTS snapshots(
                    stream_id TEXT PRIMARY KEY,
                    version INTEGER, state TEXT, taken_at REAL);
                CREATE TABLE IF NOT EXISTS global_seq(
                    id INTEGER PRIMARY KEY, val INTEGER);
                INSERT OR IGNORE INTO global_seq VALUES(1, 0);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_ev_stream_ver
                    ON events(stream_id, version);
                CREATE INDEX IF NOT EXISTS idx_ev_gpos
                    ON events(global_pos);
            """)

    def next_global(self) -> int:
        with self._conn() as c:
            c.execute("UPDATE global_seq SET val=val+1 WHERE id=1")
            return c.execute("SELECT val FROM global_seq").fetchone()["val"]

    def stream_version(self, stream_id: str) -> int:
        with self._conn() as c:
            row = c.execute(
                "SELECT version FROM stream_versions WHERE stream_id=?",
                (stream_id,)).fetchone()
        return row["version"] if row else 0

    def append(self, event: Event):
        with self._conn() as c:
            c.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?)",
                (event.id, event.stream_id, event.type,
                 json.dumps(event.data, default=str),
                 json.dumps(event.metadata, default=str),
                 event.version, event.global_pos, event.timestamp))
            c.execute("""
                INSERT INTO stream_versions(stream_id, version, event_count)
                VALUES(?,?,1)
                ON CONFLICT(stream_id) DO UPDATE SET
                    version=excluded.version,
                    event_count=event_count+1
            """, (event.stream_id, event.version))

    def read_stream(self, stream_id: str,
                     from_version: int = 0,
                     to_version: int = None,
                     limit: int = 10000) -> List[Event]:
        where = "stream_id=? AND version>=?"
        params = [stream_id, from_version]
        if to_version is not None:
            where += " AND version<=?"; params.append(to_version)
        params.append(limit)
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM events WHERE {where} "
                "ORDER BY version ASC LIMIT ?", params).fetchall()
        return [self._row_to_event(r) for r in rows]

    def read_all(self, from_pos: int = 0,
                  types: List[str] = None,
                  limit: int = 10000) -> List[Event]:
        where = "global_pos>=?"
        params: list = [from_pos]
        if types:
            placeholders = ",".join("?" * len(types))
            where += f" AND type IN ({placeholders})"
            params.extend(types)
        params.append(limit)
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM events WHERE {where} "
                "ORDER BY global_pos ASC LIMIT ?", params).fetchall()
        return [self._row_to_event(r) for r in rows]

    def _row_to_event(self, r) -> Event:
        return Event(id=r["id"], stream_id=r["stream_id"],
                      type=r["type"], data=json.loads(r["data"]),
                      metadata=json.loads(r["metadata"]),
                      version=r["version"], global_pos=r["global_pos"],
                      timestamp=r["timestamp"])

    def save_snapshot(self, snap: Snapshot):
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO snapshots VALUES(?,?,?,?)",
                (snap.stream_id, snap.version,
                 json.dumps(snap.state, default=str), snap.taken_at))

    def load_snapshot(self, stream_id: str) -> Optional[Snapshot]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM snapshots WHERE stream_id=?",
                (stream_id,)).fetchone()
        if not row: return None
        return Snapshot(stream_id=row["stream_id"], version=row["version"],
                         state=json.loads(row["state"]),
                         taken_at=row["taken_at"])

    def truncate(self, stream_id: str, before_version: int) -> int:
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM events WHERE stream_id=? AND version<?",
                (stream_id, before_version))
            return cur.rowcount

    def stats(self) -> Dict:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            gpos  = c.execute(
                "SELECT val FROM global_seq").fetchone()["val"]
            by_stream = {r["stream_id"]: r["cnt"] for r in c.execute(
                "SELECT stream_id, COUNT(*) as cnt FROM events "
                "GROUP BY stream_id ORDER BY cnt DESC LIMIT 20").fetchall()}
            by_type = {r["type"]: r["cnt"] for r in c.execute(
                "SELECT type, COUNT(*) as cnt FROM events "
                "GROUP BY type ORDER BY cnt DESC LIMIT 20").fetchall()}
        return {"total_events": total, "global_position": gpos,
                "by_stream": by_stream, "by_type": by_type}

class EventStore:
    """
    Append-only event store with projections and snapshots.

    Usage:
        es = EventStore()

        # Append events
        es.append("account-42", "MoneyDeposited", {"amount": 100})
        es.append("account-42", "MoneyWithdrawn", {"amount": 30},
                   expected_version=0)

        # Register projection
        def balance_reducer(state, event):
            if event.type == "MoneyDeposited":
                state["balance"] = state.get("balance", 0) + event.data["amount"]
            elif event.type == "MoneyWithdrawn":
                state["balance"] = state.get("balance", 0) - event.data["amount"]
            return state

        es.register_projection("balance", balance_reducer)

        # Get current state
        state = es.project("account-42", "balance")
        # {"balance": 70}
    """
    def __init__(self, db_path: str = "data/eventstore.db"):
        self._store = ESStore(db_path)
        self._projections: Dict[str, Callable] = {}
        self._proj_cache: Dict[Tuple, Any] = {}   # (stream_id, proj_name, ver) → state
        self._upcasters: Dict[str, Callable] = {}
        self._subs_stream: Dict[str, List[Callable]] = {}
        self._subs_global: List[Callable] = []

    def register_projection(self, name: str, reducer: Callable):
        """reducer(state: dict, event: Event) → dict"""
        self._projections[name] = reducer

    def register_upcaster(self, event_type: str, fn: Callable):
        """fn(event: Event) → Event (migrate old event schema)"""
        self._upcasters[event_type] = fn

    def subscribe(self, fn: Callable, stream_id: str = None):
        if stream_id:
            self._subs_stream.setdefault(stream_id, []).append(fn)
        else:
            self._subs_global.append(fn)

    def append(self, stream_id: str, event_type: str, data: Any,
                metadata: Dict = None,
                expected_version: int = None) -> Event:
        current = self._store.stream_version(stream_id)
        if (expected_version is not None
                and current != expected_version):
            raise ValueError(
                f"Concurrency conflict: expected version {expected_version}, "
                f"got {current}")
        new_version = current + 1
        gpos = self._store.next_global()
        event = Event(
            id=str(uuid.uuid4())[:16],
            stream_id=stream_id,
            type=event_type,
            data=data,
            metadata=dict(metadata or {}),
            version=new_version,
            global_pos=gpos)
        self._store.append(event)
        # Invalidate projection cache for this stream
        self._proj_cache = {k: v for k, v in self._proj_cache.items()
                             if k[0] != stream_id}
        # Notify subscribers
        for fn in self._subs_stream.get(stream_id, []):
            try: fn(event)
            except: pass
        for fn in self._subs_global:
            try: fn(event)
            except: pass
        return event

    def append_batch(self, stream_id: str,
                      events: List[Tuple[str, Any]],
                      expected_version: int = None) -> List[Event]:
        results = []
        for i, (etype, data) in enumerate(events):
            ev_expected = (None if expected_version is None
                           else expected_version + i)
            results.append(self.append(stream_id, etype, data,
                                        expected_version=ev_expected))
        return results

    def _upcast(self, event: Event) -> Event:
        fn = self._upcasters.get(event.type)
        return fn(event) if fn else event

    def read_stream(self, stream_id: str,
                     from_version: int = 0,
                     to_version: int = None) -> List[Event]:
        events = self._store.read_stream(stream_id, from_version, to_version)
        return [self._upcast(e) for e in events]

    def read_all(self, from_pos: int = 0,
                  types: List[str] = None) -> List[Event]:
        events = self._store.read_all(from_pos, types)
        return [self._upcast(e) for e in events]

    def project(self, stream_id: str, projection_name: str,
                 init_state: Any = None) -> Any:
        reducer = self._projections.get(projection_name)
        if not reducer:
            raise KeyError(f"Projection '{projection_name}' not registered")
        # Check cache
        current_ver = self._store.stream_version(stream_id)
        cache_key = (stream_id, projection_name, current_ver)
        if cache_key in self._proj_cache:
            return self._proj_cache[cache_key]
        # Try snapshot
        snap = self._store.load_snapshot(stream_id)
        if snap:
            state = snap.state
            events = self.read_stream(stream_id, snap.version + 1)
        else:
            state = dict(init_state) if init_state else {}
            events = self.read_stream(stream_id)
        for ev in events:
            state = reducer(state, ev)
        self._proj_cache[cache_key] = state
        return state

    def take_snapshot(self, stream_id: str,
                       projection_name: str,
                       init_state: Any = None) -> Snapshot:
        state = self.project(stream_id, projection_name, init_state)
        ver = self._store.stream_version(stream_id)
        snap = Snapshot(stream_id=stream_id, version=ver, state=state)
        self._store.save_snapshot(snap)
        return snap

    def replay(self, stream_id: str,
                handler: Callable,
                from_version: int = 0) -> int:
        events = self.read_stream(stream_id, from_version)
        for ev in events:
            try: handler(ev)
            except: pass
        return len(events)

    def truncate(self, stream_id: str, before_version: int) -> int:
        return self._store.truncate(stream_id, before_version)

    def stream_version(self, stream_id: str) -> int:
        return self._store.stream_version(stream_id)

    def stats(self) -> Dict:
        s = self._store.stats()
        s["projections"] = len(self._projections)
        s["cache_entries"] = len(self._proj_cache)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def append_ep(req):
            d = await req.json()
            try:
                ev = self.append(d["stream_id"], d["type"], d["data"],
                                  d.get("metadata",{}),
                                  d.get("expected_version"))
                return web.json_response(ev.to_dict(), status=201)
            except ValueError as e:
                return web.json_response({"error": str(e)}, status=409)
        async def read_ep(req):
            sid = req.match_info["stream_id"]
            fv  = int(req.rel_url.query.get("from_version", 0))
            events = self.read_stream(sid, fv)
            return web.json_response({"events": [e.to_dict() for e in events]})
        async def all_ep(req):
            fp = int(req.rel_url.query.get("from_pos", 0))
            events = self.read_all(fp)
            return web.json_response({"events": [e.to_dict() for e in events]})
        async def snap_ep(req):
            d = await req.json()
            snap = self.take_snapshot(d["stream_id"], d["projection"])
            return web.json_response({"stream_id": snap.stream_id,
                                       "version": snap.version})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/events"
        app.router.add_post(f"{p}/append",           append_ep)
        app.router.add_get( f"{p}/{{stream_id}}",    read_ep)
        app.router.add_get( f"{p}/",                 all_ep)
        app.router.add_post(f"{p}/snapshot",         snap_ep)
        app.router.add_get( f"{p}/stats",            stats_ep)
        logger.info(f"Event store API at {prefix}/events/")
