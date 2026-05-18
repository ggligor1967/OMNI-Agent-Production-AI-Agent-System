"""OMNI Agent — Event Bus V2: pub/sub with wildcards, filters, priority, dead-letter."""
from __future__ import annotations
import json, queue, sqlite3, threading, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Pattern
import re as _re


class EventPriority(int, Enum):
    CRITICAL = 0
    HIGH     = 1
    NORMAL   = 2
    LOW      = 3


class SubscriptionMode(str, Enum):
    SYNC   = "sync"     # handler called inline
    ASYNC  = "async"    # handler called in background thread
    QUEUE  = "queue"    # events queued for batch processing


@dataclass
class Event:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4())[:10])
    topic: str = ""
    payload: Any = None
    priority: EventPriority = EventPriority.NORMAL
    source: str = ""
    correlation_id: str = ""
    ts: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)
    _retry_count: int = field(default=0, init=False, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "topic": self.topic,
            "priority": self.priority.value,
            "source": self.source,
            "ts": self.ts,
        }


@dataclass
class Subscription:
    sub_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    topic_pattern: str = ""        # supports * and ** wildcards
    handler: Callable = field(default=lambda e: None)
    mode: SubscriptionMode = SubscriptionMode.SYNC
    priority: EventPriority = EventPriority.NORMAL
    filter_fn: Optional[Callable[[Event], bool]] = None
    max_retries: int = 1
    active: bool = True
    received_count: int = 0
    error_count: int = 0
    created_at: float = field(default_factory=time.time)
    _compiled: Optional[Any] = field(default=None, init=False, repr=False)

    def matches(self, topic: str) -> bool:
        if not self._compiled:
            pat = self.topic_pattern
            pat = pat.replace("**", "__DSTAR__")
            pat = pat.replace("*",  "[^.]+")
            pat = pat.replace("__DSTAR__", ".+")
            self._compiled = _re.compile(f"^{pat}$")
        return bool(self._compiled.match(topic))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sub_id": self.sub_id,
            "pattern": self.topic_pattern,
            "mode": self.mode.value,
            "active": self.active,
            "received": self.received_count,
            "errors": self.error_count,
        }


@dataclass
class DeadLetterEntry:
    event: Event
    sub_id: str
    error: str
    attempts: int
    ts: float = field(default_factory=time.time)


class EventBusV2:
    """
    In-process publish/subscribe event bus:
    - Wildcard topic matching (* = single segment, ** = multi-segment)
    - Sync, async (threaded), and queued delivery modes
    - Per-subscription filter predicates
    - Priority ordering for delivery
    - Max retries per subscription with dead-letter queue
    - Middleware chain (pre-publish transforms)
    - Per-topic event history (configurable size)
    - Pause/resume subscriptions
    - Correlated event chains (correlation_id)
    - Event replay from history
    - SQLite persistence of event log
    """

    def __init__(self, db_path: str = ":memory:",
                 history_size: int = 1000):
        self._subs:        Dict[str, Subscription] = {}
        self._history:     List[Event] = []
        self._dead_letter: List[DeadLetterEntry] = []
        self._middleware:  List[Callable[[Event], Optional[Event]]] = []
        self._paused_topics: set = set()
        self._history_size = history_size
        self._lock  = threading.Lock()
        self._db    = sqlite3.connect(db_path, check_same_thread=False)
        self._published = 0
        self._delivered = 0
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS eb_events (
                event_id TEXT PRIMARY KEY, topic TEXT,
                source TEXT, priority INTEGER, ts REAL
            );
        """)
        self._db.commit()

    # ── SUBSCRIPTIONS ────────────────────────────────────────────────

    def subscribe(self, topic_pattern: str,
                   handler: Callable[[Event], Any],
                   mode: SubscriptionMode = SubscriptionMode.SYNC,
                   priority: EventPriority = EventPriority.NORMAL,
                   filter_fn: Optional[Callable[[Event], bool]] = None,
                   max_retries: int = 1,
                   sub_id: Optional[str] = None) -> Subscription:
        sid  = sub_id or str(uuid.uuid4())[:8]
        sub  = Subscription(
            sub_id=sid, topic_pattern=topic_pattern,
            handler=handler, mode=mode, priority=priority,
            filter_fn=filter_fn, max_retries=max_retries)
        self._subs[sid] = sub
        return sub

    def unsubscribe(self, sub_id: str) -> bool:
        return self._subs.pop(sub_id, None) is not None

    def pause_subscription(self, sub_id: str):
        sub = self._subs.get(sub_id)
        if sub: sub.active = False

    def resume_subscription(self, sub_id: str):
        sub = self._subs.get(sub_id)
        if sub: sub.active = True

    def pause_topic(self, topic: str):
        self._paused_topics.add(topic)

    def resume_topic(self, topic: str):
        self._paused_topics.discard(topic)

    # ── MIDDLEWARE ───────────────────────────────────────────────────

    def use(self, fn: Callable[[Event], Optional[Event]]):
        """Add middleware. Return None from fn to drop event."""
        self._middleware.append(fn)

    # ── PUBLISH ──────────────────────────────────────────────────────

    def publish(self, topic: str,
                 payload: Any = None,
                 priority: EventPriority = EventPriority.NORMAL,
                 source: str = "",
                 correlation_id: str = "",
                 event_id: Optional[str] = None,
                 metadata: Optional[Dict] = None) -> Optional[Event]:

        if topic in self._paused_topics:
            return None

        ev = Event(
            event_id=event_id or str(uuid.uuid4())[:10],
            topic=topic, payload=payload, priority=priority,
            source=source, correlation_id=correlation_id,
            metadata=metadata or {})

        # Run middleware
        for mw in self._middleware:
            try:
                ev = mw(ev)
                if ev is None:
                    return None
            except Exception:
                pass

        self._published += 1
        self._record(ev)

        # Find matching, active subscribers sorted by priority
        matching = sorted(
            [s for s in self._subs.values()
             if s.active and s.matches(topic)
             and (not s.filter_fn or s.filter_fn(ev))],
            key=lambda s: s.priority.value)

        for sub in matching:
            if sub.mode == SubscriptionMode.ASYNC:
                t = threading.Thread(
                    target=self._deliver, args=(sub, ev), daemon=True)
                t.start()
            else:
                self._deliver(sub, ev)

        return ev

    def publish_batch(self, events: List[Dict]) -> List[Optional[Event]]:
        return [self.publish(**e) for e in events]

    def _deliver(self, sub: Subscription, ev: Event):
        for attempt in range(sub.max_retries + 1):
            try:
                sub.handler(ev)
                sub.received_count += 1
                self._delivered += 1
                return
            except Exception as exc:
                if attempt >= sub.max_retries:
                    sub.error_count += 1
                    self._dead_letter.append(
                        DeadLetterEntry(event=ev, sub_id=sub.sub_id,
                                         error=str(exc), attempts=attempt + 1))

    def _record(self, ev: Event):
        with self._lock:
            self._history.append(ev)
            if len(self._history) > self._history_size:
                self._history = self._history[-self._history_size:]
        self._db.execute(
            "INSERT OR IGNORE INTO eb_events VALUES (?,?,?,?,?)",
            (ev.event_id, ev.topic, ev.source,
             ev.priority.value, ev.ts))
        self._db.commit()

    # ── REPLAY ───────────────────────────────────────────────────────

    def replay(self, topic_pattern: Optional[str] = None,
               from_ts: Optional[float] = None,
               sub_id: Optional[str] = None) -> int:
        """Re-deliver historical events to a subscription or inline."""
        sub = self._subs.get(sub_id) if sub_id else None
        count = 0
        for ev in self._history:
            if topic_pattern:
                tmp = Subscription(topic_pattern=topic_pattern,
                                   handler=lambda e: None)
                if not tmp.matches(ev.topic):
                    continue
            if from_ts and ev.ts < from_ts:
                continue
            if sub:
                self._deliver(sub, ev)
            count += 1
        return count

    # ── QUERY ────────────────────────────────────────────────────────

    def history(self, topic: Optional[str] = None,
                limit: int = 50) -> List[Dict]:
        events = self._history
        if topic:
            events = [e for e in events if e.topic == topic]
        return [e.to_dict() for e in events[-limit:]]

    def dead_letter(self) -> List[Dict]:
        return [{"event": dl.event.to_dict(),
                 "sub_id": dl.sub_id,
                 "error": dl.error,
                 "attempts": dl.attempts}
                for dl in self._dead_letter]

    def list_subscriptions(self) -> List[Dict]:
        return [s.to_dict() for s in self._subs.values()]

    def get_subscription(self, sub_id: str) -> Optional[Subscription]:
        return self._subs.get(sub_id)

    def correlated(self, correlation_id: str) -> List[Event]:
        return [e for e in self._history
                if e.correlation_id == correlation_id]

    def stats(self) -> Dict[str, Any]:
        return {
            "subscriptions": len(self._subs),
            "published": self._published,
            "delivered": self._delivered,
            "history": len(self._history),
            "dead_letter": len(self._dead_letter),
        }
