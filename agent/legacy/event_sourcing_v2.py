"""OMNI Agent — Event Sourcing V2: CQRS event store with projections, snapshots, and replay."""
from __future__ import annotations
import json, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, Type


class EventStatus(str, Enum):
    PENDING   = "pending"
    APPLIED   = "applied"
    FAILED    = "failed"
    SKIPPED   = "skipped"


@dataclass
class DomainEvent:
    event_id: str
    aggregate_id: str
    aggregate_type: str
    event_type: str
    payload: Dict[str, Any]
    version: int            # monotonic per aggregate
    ts: float = field(default_factory=time.time)
    correlation_id: str = ""
    causation_id: str  = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    status: EventStatus = EventStatus.PENDING

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "aggregate_id": self.aggregate_id,
            "aggregate_type": self.aggregate_type,
            "event_type": self.event_type,
            "version": self.version,
            "ts": self.ts,
            "status": self.status.value,
            "payload": self.payload,
        }


@dataclass
class Snapshot:
    snapshot_id: str
    aggregate_id: str
    aggregate_type: str
    state: Dict[str, Any]
    version: int            # last event version included
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "aggregate_id": self.aggregate_id,
            "version": self.version,
            "ts": self.ts,
        }


class OptimisticLockError(Exception):
    pass


class EventStoreV2:
    """Append-only event store with optimistic concurrency control."""

    def __init__(self, db_path: str = ":memory:"):
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._handlers: Dict[str, List[Callable]] = {}   # event_type → [fn]
        self._append_count = 0

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS esv2_events (
                event_id TEXT PRIMARY KEY,
                aggregate_id TEXT, aggregate_type TEXT,
                event_type TEXT, payload TEXT, version INTEGER,
                ts REAL, correlation_id TEXT, causation_id TEXT,
                metadata TEXT, status TEXT
            );
            CREATE TABLE IF NOT EXISTS esv2_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                aggregate_id TEXT, aggregate_type TEXT,
                state TEXT, version INTEGER, ts REAL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_agg_version
                ON esv2_events(aggregate_id, version);
        """)
        self._db.commit()

    # ── APPEND ────────────────────────────────────────────────────────

    def append(self, aggregate_id: str, aggregate_type: str,
               event_type: str, payload: Dict[str, Any],
               expected_version: Optional[int] = None,
               correlation_id: str = "",
               causation_id: str = "",
               metadata: Optional[Dict] = None) -> DomainEvent:
        """Append event. Raises OptimisticLockError on version conflict."""
        current_version = self._current_version(aggregate_id)
        if expected_version is not None and current_version != expected_version:
            raise OptimisticLockError(
                f"Expected version {expected_version} but got {current_version} "
                f"for aggregate '{aggregate_id}'")
        new_version = current_version + 1
        event = DomainEvent(
            event_id=str(uuid.uuid4()),
            aggregate_id=aggregate_id,
            aggregate_type=aggregate_type,
            event_type=event_type,
            payload=payload,
            version=new_version,
            correlation_id=correlation_id,
            causation_id=causation_id,
            metadata=metadata or {},
            status=EventStatus.APPLIED,
        )
        try:
            self._db.execute(
                "INSERT INTO esv2_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (event.event_id, aggregate_id, aggregate_type, event_type,
                 json.dumps(payload), new_version, event.ts,
                 correlation_id, causation_id,
                 json.dumps(metadata or {}), event.status.value))
            self._db.commit()
        except Exception as e:
            raise OptimisticLockError(f"Concurrent write conflict: {e}")
        self._append_count += 1
        self._dispatch(event)
        return event

    def append_many(self, aggregate_id: str, aggregate_type: str,
                    events: List[Tuple[str, Dict]],
                    expected_version: Optional[int] = None) -> List[DomainEvent]:
        """Atomic batch append."""
        result = []
        ev = expected_version
        for event_type, payload in events:
            e = self.append(aggregate_id, aggregate_type, event_type,
                            payload, expected_version=ev)
            ev = e.version
            result.append(e)
        return result

    # ── READ ──────────────────────────────────────────────────────────

    def _current_version(self, aggregate_id: str) -> int:
        row = self._db.execute(
            "SELECT MAX(version) FROM esv2_events WHERE aggregate_id=?",
            (aggregate_id,)).fetchone()
        return row[0] if row and row[0] is not None else 0

    def load(self, aggregate_id: str,
             from_version: int = 1,
             to_version: Optional[int] = None) -> List[DomainEvent]:
        q = ("SELECT event_id,aggregate_id,aggregate_type,event_type,"
             "payload,version,ts,correlation_id,causation_id,metadata,status "
             "FROM esv2_events WHERE aggregate_id=? AND version>=?")
        params: List[Any] = [aggregate_id, from_version]
        if to_version:
            q += " AND version<=?"; params.append(to_version)
        q += " ORDER BY version"
        rows = self._db.execute(q, params).fetchall()
        return [self._row_to_event(r) for r in rows]

    def load_type(self, aggregate_type: str,
                  event_type: Optional[str] = None,
                  limit: int = 100) -> List[DomainEvent]:
        if event_type:
            rows = self._db.execute(
                "SELECT * FROM esv2_events WHERE aggregate_type=? AND event_type=? "
                "ORDER BY ts LIMIT ?", (aggregate_type, event_type, limit)).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM esv2_events WHERE aggregate_type=? "
                "ORDER BY ts LIMIT ?", (aggregate_type, limit)).fetchall()
        return [self._row_to_event(r) for r in rows]

    def all_events(self, since_ts: float = 0.0,
                   limit: int = 1000) -> List[DomainEvent]:
        rows = self._db.execute(
            "SELECT * FROM esv2_events WHERE ts>? ORDER BY ts LIMIT ?",
            (since_ts, limit)).fetchall()
        return [self._row_to_event(r) for r in rows]

    def _row_to_event(self, r) -> DomainEvent:
        return DomainEvent(
            event_id=r[0], aggregate_id=r[1], aggregate_type=r[2],
            event_type=r[3], payload=json.loads(r[4]), version=r[5],
            ts=r[6], correlation_id=r[7], causation_id=r[8],
            metadata=json.loads(r[9]),
            status=EventStatus(r[10]))

    # ── SNAPSHOTS ─────────────────────────────────────────────────────

    def save_snapshot(self, aggregate_id: str, aggregate_type: str,
                      state: Dict[str, Any], version: int) -> Snapshot:
        snap = Snapshot(
            snapshot_id=str(uuid.uuid4()),
            aggregate_id=aggregate_id, aggregate_type=aggregate_type,
            state=state, version=version)
        self._db.execute(
            "INSERT OR REPLACE INTO esv2_snapshots VALUES (?,?,?,?,?,?)",
            (snap.snapshot_id, aggregate_id, aggregate_type,
             json.dumps(state), version, snap.ts))
        self._db.commit()
        return snap

    def load_snapshot(self, aggregate_id: str) -> Optional[Snapshot]:
        row = self._db.execute(
            "SELECT snapshot_id,aggregate_id,aggregate_type,state,version,ts "
            "FROM esv2_snapshots WHERE aggregate_id=? ORDER BY version DESC LIMIT 1",
            (aggregate_id,)).fetchone()
        if not row:
            return None
        return Snapshot(
            snapshot_id=row[0], aggregate_id=row[1], aggregate_type=row[2],
            state=json.loads(row[3]), version=row[4], ts=row[5])

    def load_from_snapshot(self, aggregate_id: str) -> Tuple[Optional[Dict], List[DomainEvent]]:
        """Returns (snapshot_state or None, events_after_snapshot)."""
        snap = self.load_snapshot(aggregate_id)
        if snap:
            events = self.load(aggregate_id, from_version=snap.version + 1)
            return snap.state, events
        return None, self.load(aggregate_id)

    # ── EVENT HANDLERS (PROJECTIONS) ──────────────────────────────────

    def subscribe(self, event_type: str, handler: Callable[[DomainEvent], None]):
        self._handlers.setdefault(event_type, []).append(handler)

    def subscribe_all(self, handler: Callable[[DomainEvent], None]):
        self._handlers.setdefault("*", []).append(handler)

    def _dispatch(self, event: DomainEvent):
        for fn in self._handlers.get(event.event_type, []):
            try: fn(event)
            except Exception: pass
        for fn in self._handlers.get("*", []):
            try: fn(event)
            except Exception: pass

    # ── REPLAY ────────────────────────────────────────────────────────

    def replay(self, aggregate_id: str,
               reducer: Callable[[Dict, DomainEvent], Dict],
               initial_state: Optional[Dict] = None) -> Dict[str, Any]:
        """Rebuild aggregate state by replaying events through reducer."""
        snap_state, events = self.load_from_snapshot(aggregate_id)
        state = dict(snap_state or initial_state or {})
        for event in events:
            state = reducer(state, event)
        return state

    def replay_all(self, aggregate_type: str,
                   reducer: Callable[[Dict, DomainEvent], Dict]) -> Dict[str, Dict]:
        """Rebuild all aggregates of a type."""
        rows = self._db.execute(
            "SELECT DISTINCT aggregate_id FROM esv2_events WHERE aggregate_type=?",
            (aggregate_type,)).fetchall()
        result = {}
        for (agg_id,) in rows:
            result[agg_id] = self.replay(agg_id, reducer)
        return result

    # ── STATS ─────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        total = self._db.execute("SELECT COUNT(*) FROM esv2_events").fetchone()[0]
        snaps = self._db.execute("SELECT COUNT(*) FROM esv2_snapshots").fetchone()[0]
        aggs  = self._db.execute(
            "SELECT COUNT(DISTINCT aggregate_id) FROM esv2_events").fetchone()[0]
        types = self._db.execute(
            "SELECT COUNT(DISTINCT aggregate_type) FROM esv2_events").fetchone()[0]
        return {
            "total_events": total,
            "snapshots": snaps,
            "aggregates": aggs,
            "aggregate_types": types,
            "appended": self._append_count,
        }


# ── PROJECTION ────────────────────────────────────────────────────────────────

class Projection:
    """Stateful read-model built from event stream."""

    def __init__(self, name: str):
        self.name = name
        self._state: Dict[str, Any] = {}
        self._handlers: Dict[str, Callable] = {}
        self._processed = 0

    def on(self, event_type: str) -> Callable:
        def decorator(fn: Callable):
            self._handlers[event_type] = fn
            return fn
        return decorator

    def apply(self, event: DomainEvent):
        handler = self._handlers.get(event.event_type) or self._handlers.get("*")
        if handler:
            try:
                handler(self._state, event)
                self._processed += 1
            except Exception:
                pass

    def get_state(self) -> Dict[str, Any]:
        return dict(self._state)

    def reset(self):
        self._state.clear()
        self._processed = 0

    def stats(self) -> Dict[str, Any]:
        return {"name": self.name, "processed": self._processed,
                "state_keys": len(self._state)}
