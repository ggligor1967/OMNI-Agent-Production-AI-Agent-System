"""OMNI Agent — Replay Manager V2: event replay, time travel, state reconstruction."""
from __future__ import annotations
import json, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple


class EventCategory(str, Enum):
    SYSTEM   = "system"
    USER     = "user"
    AGENT    = "agent"
    DATA     = "data"
    AUDIT    = "audit"
    CUSTOM   = "custom"


class ReplayStatus(str, Enum):
    PENDING  = "pending"
    RUNNING  = "running"
    DONE     = "done"
    FAILED   = "failed"
    PAUSED   = "paused"


@dataclass
class Event:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4())[:10])
    stream_id: str = ""
    category: EventCategory = EventCategory.CUSTOM
    event_type: str = ""
    payload: Any = None
    sequence: int = 0
    ts: float = field(default_factory=time.time)
    correlation_id: Optional[str] = None
    causation_id: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"event_id": self.event_id, "stream": self.stream_id,
                "type": self.event_type, "sequence": self.sequence,
                "ts": self.ts, "category": self.category.value}


@dataclass
class ReplaySession:
    session_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    stream_id: str = ""
    status: ReplayStatus = ReplayStatus.PENDING
    from_seq: int = 0
    to_seq: Optional[int] = None
    from_ts: Optional[float] = None
    to_ts:   Optional[float] = None
    filter_fn: Optional[Callable[[Event], bool]] = None
    events_replayed: int = 0
    events_total: int = 0
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    state: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at) * 1000
        return 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {"session_id": self.session_id, "stream": self.stream_id,
                "status": self.status.value,
                "events_replayed": self.events_replayed,
                "duration_ms": round(self.duration_ms, 2)}


class ReplayManagerV2:
    """
    Event replay engine:
    - Append events to named streams with monotonic sequence numbers
    - Full event store with category, type, payload, ts, correlation/causation IDs
    - Time-travel replay: replay from/to sequence or timestamp
    - Filter replay: by event type, category, tag, custom predicate
    - State reconstruction: fold events with reducer function
    - Snapshot state at sequence N (checkpointing)
    - Replay sessions with progress tracking
    - Catch-up replay: events since last snapshot
    - Stream slicing and merging
    - Event projection (map events → view)
    - SQLite persistence
    """

    def __init__(self, db_path: str = ":memory:"):
        self._streams:   Dict[str, List[Event]] = {}
        self._seq:       Dict[str, int] = {}
        self._snapshots: Dict[str, List[Tuple[int, Dict]]] = {}  # stream → [(seq, state)]
        self._sessions:  List[ReplaySession] = []
        self._handlers:  Dict[str, Callable[[Event], None]] = {}
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS rm_events (
                event_id TEXT PRIMARY KEY, stream_id TEXT,
                category TEXT, event_type TEXT,
                payload TEXT, sequence INTEGER, ts REAL,
                correlation_id TEXT, causation_id TEXT
            );
            CREATE TABLE IF NOT EXISTS rm_snapshots (
                snap_id TEXT PRIMARY KEY, stream_id TEXT,
                sequence INTEGER, state TEXT, ts REAL
            );
        """)
        self._db.commit()

    # ── APPEND ────────────────────────────────────────────────────────

    def append(self, stream_id: str,
               event_type: str,
               payload: Any = None,
               category: EventCategory = EventCategory.CUSTOM,
               correlation_id: Optional[str] = None,
               causation_id: Optional[str] = None,
               tags: Optional[List[str]] = None,
               metadata: Optional[Dict] = None) -> Event:
        self._streams.setdefault(stream_id, [])
        seq  = self._seq.get(stream_id, 0) + 1
        self._seq[stream_id] = seq
        e = Event(stream_id=stream_id, category=category,
                   event_type=event_type, payload=payload,
                   sequence=seq, correlation_id=correlation_id,
                   causation_id=causation_id,
                   tags=list(tags or []), metadata=metadata or {})
        self._streams[stream_id].append(e)
        self._db.execute(
            "INSERT INTO rm_events VALUES (?,?,?,?,?,?,?,?,?)",
            (e.event_id, stream_id, category.value,
             event_type, json.dumps(payload, default=str),
             seq, e.ts, correlation_id, causation_id))
        self._db.commit()
        return e

    def append_batch(self, stream_id: str,
                      events: List[Dict]) -> List[Event]:
        return [self.append(stream_id, **ev) for ev in events]

    # ── QUERY ─────────────────────────────────────────────────────────

    def get_events(self, stream_id: str,
                    from_seq: int = 0,
                    to_seq: Optional[int] = None,
                    from_ts: Optional[float] = None,
                    to_ts: Optional[float] = None,
                    event_type: Optional[str] = None,
                    category: Optional[EventCategory] = None,
                    tag: Optional[str] = None,
                    filter_fn: Optional[Callable[[Event], bool]] = None
                    ) -> List[Event]:
        events = self._streams.get(stream_id, [])
        if from_seq:
            events = [e for e in events if e.sequence >= from_seq]
        if to_seq is not None:
            events = [e for e in events if e.sequence <= to_seq]
        if from_ts is not None:
            events = [e for e in events if e.ts >= from_ts]
        if to_ts is not None:
            events = [e for e in events if e.ts <= to_ts]
        if event_type:
            events = [e for e in events if e.event_type == event_type]
        if category:
            events = [e for e in events if e.category == category]
        if tag:
            events = [e for e in events if tag in e.tags]
        if filter_fn:
            events = [e for e in events if filter_fn(e)]
        return events

    def latest(self, stream_id: str, n: int = 10) -> List[Event]:
        return self._streams.get(stream_id, [])[-n:]

    def stream_ids(self) -> List[str]:
        return list(self._streams.keys())

    def event_count(self, stream_id: str) -> int:
        return len(self._streams.get(stream_id, []))

    # ── REPLAY ────────────────────────────────────────────────────────

    def replay(self, stream_id: str,
               handler: Callable[[Event], None],
               from_seq: int = 0,
               to_seq: Optional[int] = None,
               from_ts: Optional[float] = None,
               to_ts: Optional[float] = None,
               filter_fn: Optional[Callable[[Event], bool]] = None,
               speed: float = 0.0) -> ReplaySession:
        """Replay events calling handler for each. speed=0 → instant."""
        events = self.get_events(stream_id, from_seq, to_seq,
                                  from_ts, to_ts, filter_fn=filter_fn)
        sess = ReplaySession(
            stream_id=stream_id, from_seq=from_seq,
            to_seq=to_seq, from_ts=from_ts, to_ts=to_ts,
            filter_fn=filter_fn,
            events_total=len(events),
            status=ReplayStatus.RUNNING,
            started_at=time.time())

        prev_ts: Optional[float] = None
        for e in events:
            if speed > 0 and prev_ts is not None:
                delay = (e.ts - prev_ts) / speed
                if 0 < delay < 5:
                    time.sleep(delay)
            try:
                handler(e)
                sess.events_replayed += 1
            except Exception:
                sess.status = ReplayStatus.FAILED
                break
            prev_ts = e.ts

        if sess.status != ReplayStatus.FAILED:
            sess.status = ReplayStatus.DONE
        sess.finished_at = time.time()
        self._sessions.append(sess)
        return sess

    def replay_iter(self, stream_id: str,
                     **kwargs) -> Iterator[Event]:
        """Generator for manual iteration."""
        events = self.get_events(stream_id, **{
            k: v for k, v in kwargs.items()
            if k in ("from_seq", "to_seq", "from_ts", "to_ts",
                     "event_type", "category", "tag", "filter_fn")})
        yield from events

    # ── STATE RECONSTRUCTION ──────────────────────────────────────────

    def fold(self, stream_id: str,
              reducer: Callable[[Dict, Event], Dict],
              initial_state: Optional[Dict] = None,
              from_seq: int = 0,
              to_seq: Optional[int] = None) -> Dict:
        state = dict(initial_state or {})
        events = self.get_events(stream_id, from_seq, to_seq)
        for e in events:
            state = reducer(state, e)
        return state

    def snapshot_state(self, stream_id: str,
                        state: Dict,
                        sequence: Optional[int] = None) -> str:
        seq  = sequence or self._seq.get(stream_id, 0)
        snaps = self._snapshots.setdefault(stream_id, [])
        snaps.append((seq, dict(state)))
        snap_id = str(uuid.uuid4())[:8]
        self._db.execute(
            "INSERT INTO rm_snapshots VALUES (?,?,?,?,?)",
            (snap_id, stream_id, seq,
             json.dumps(state, default=str), time.time()))
        self._db.commit()
        return snap_id

    def latest_snapshot(self, stream_id: str) -> Optional[Tuple[int, Dict]]:
        snaps = self._snapshots.get(stream_id)
        return snaps[-1] if snaps else None

    def catchup(self, stream_id: str,
                 reducer: Callable[[Dict, Event], Dict]) -> Dict:
        """Reconstruct state from latest snapshot + subsequent events."""
        snap = self.latest_snapshot(stream_id)
        if snap:
            seq, state = snap
            return self.fold(stream_id, reducer, state, from_seq=seq + 1)
        return self.fold(stream_id, reducer)

    # ── PROJECTION ────────────────────────────────────────────────────

    def project(self, stream_id: str,
                 fn: Callable[[Event], Any],
                 filter_fn: Optional[Callable[[Event], bool]] = None
                 ) -> List[Any]:
        events = self.get_events(stream_id, filter_fn=filter_fn)
        return [fn(e) for e in events]

    def merge_streams(self, stream_ids: List[str],
                       new_stream_id: str) -> int:
        """Merge multiple streams into one, sorted by ts."""
        all_events: List[Event] = []
        for sid in stream_ids:
            all_events.extend(self._streams.get(sid, []))
        all_events.sort(key=lambda e: e.ts)
        count = 0
        for e in all_events:
            self.append(new_stream_id, e.event_type, e.payload,
                         e.category, e.correlation_id, e.causation_id,
                         e.tags, e.metadata)
            count += 1
        return count

    def stats(self) -> Dict[str, Any]:
        return {
            "streams": len(self._streams),
            "total_events": sum(len(v) for v in self._streams.values()),
            "replay_sessions": len(self._sessions),
            "snapshots": sum(len(v) for v in self._snapshots.values()),
        }
