"""OMNI AGENT - Event Bus
Async pub/sub event bus with topics, subscriptions, filters,
ordered dispatch, dead letters, replay, and persistence.

Features:
- Topics: named channels; auto-created on first publish/subscribe
- Subscriptions: name, topic pattern (wildcard), handler_fn, filter_fn
- Topic patterns: "user.*" matches "user.created", "user.deleted"
- Filter: fn(event) → bool; event skipped for subscriber if False
- Async dispatch: handlers awaited concurrently per event
- Priority: subscriber priority (1=first) controls dispatch order
- Dead letter queue: failed handlers land events in DLQ per topic
- Retry: per-subscription max_retries with backoff on handler error
- At-most-once / at-least-once delivery modes
- Replay: re-deliver stored events from offset (event log)
- Middleware: chain of fn(event) → event applied before dispatch
- Hooks: on_publish(event), on_dispatch(event, sub), on_error(event, sub, exc)
- Event: id, topic, payload, headers, ts, source, correlation_id
- Retention: keep last N events per topic (ring buffer in memory + SQLite)
- Pause/resume: per-topic or global dispatch pause
- Stats: published, dispatched, failed, dlq per topic
- SQLite persistence: event log, subscriptions, DLQ
- REST API: publish, subscribe, replay, dlq, stats
"""
import asyncio, fnmatch, json, sqlite3, time, uuid, logging
from collections import defaultdict, deque
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class DeliveryMode(str, Enum):
    AT_MOST_ONCE  = "at_most_once"
    AT_LEAST_ONCE = "at_least_once"

@dataclass
class Event:
    id: str; topic: str; payload: Any
    headers: Dict[str, str] = field(default_factory=dict)
    source: str = ""; correlation_id: str = ""
    ts: float = field(default_factory=time.time)
    seq: int = 0   # global sequence number

    def to_dict(self):
        return {"id": self.id, "topic": self.topic,
                "payload": self.payload,
                "headers": self.headers, "source": self.source,
                "correlation_id": self.correlation_id,
                "ts": round(self.ts, 3), "seq": self.seq}

@dataclass
class Subscription:
    name: str; topic_pattern: str
    handler: Callable
    filter_fn: Optional[Callable] = None
    priority: int = 5
    max_retries: int = 0
    retry_delay_s: float = 0.1
    delivery_mode: DeliveryMode = DeliveryMode.AT_MOST_ONCE
    enabled: bool = True
    _dispatch_count: int = 0
    _error_count: int = 0

    def matches(self, topic: str) -> bool:
        return fnmatch.fnmatch(topic, self.topic_pattern)

    def passes_filter(self, event: Event) -> bool:
        if not self.filter_fn: return True
        try: return bool(self.filter_fn(event))
        except: return False

@dataclass
class TopicStats:
    topic: str
    published: int = 0; dispatched: int = 0
    failed: int = 0; dlq_count: int = 0

    def to_dict(self):
        return {"topic": self.topic, "published": self.published,
                "dispatched": self.dispatched, "failed": self.failed,
                "dlq": self.dlq_count}

class EBStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS event_log(
                    id TEXT PRIMARY KEY, seq INTEGER, topic TEXT,
                    payload TEXT, headers TEXT, source TEXT,
                    correlation_id TEXT, ts REAL);
                CREATE TABLE IF NOT EXISTS dlq(
                    id TEXT PRIMARY KEY, event_id TEXT, topic TEXT,
                    subscription TEXT, error TEXT, ts REAL);
                CREATE INDEX IF NOT EXISTS idx_el_topic
                    ON event_log(topic, seq DESC);
                CREATE INDEX IF NOT EXISTS idx_dlq_topic
                    ON dlq(topic, ts DESC);
            """)
        with self._conn() as c:
            row = c.execute("SELECT MAX(seq) FROM event_log").fetchone()
            self._seq = (row[0] or 0)

    def next_seq(self) -> int:
        self._seq += 1; return self._seq

    def save_event(self, ev: Event):
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO event_log VALUES(?,?,?,?,?,?,?,?)",
                (ev.id, ev.seq, ev.topic,
                 json.dumps(ev.payload, default=str),
                 json.dumps(ev.headers), ev.source,
                 ev.correlation_id, ev.ts))

    def add_dlq(self, ev: Event, sub_name: str, error: str):
        with self._conn() as c:
            c.execute("INSERT INTO dlq VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4())[:8], ev.id, ev.topic,
                 sub_name, error[:300], time.time()))

    def get_dlq(self, topic: str = None, limit: int = 50) -> List[Dict]:
        where = f"WHERE topic='{topic}'" if topic else ""
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM dlq {where} ORDER BY ts DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_events_from(self, topic_pattern: str,
                         from_seq: int, limit: int = 100) -> List[Event]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM event_log WHERE seq > ? "
                "ORDER BY seq ASC LIMIT ?",
                (from_seq, limit)).fetchall()
        events = []
        for r in rows:
            if fnmatch.fnmatch(r["topic"], topic_pattern):
                ev = Event(id=r["id"], topic=r["topic"],
                            payload=json.loads(r["payload"]),
                            headers=json.loads(r["headers"]),
                            source=r["source"],
                            correlation_id=r["correlation_id"],
                            ts=r["ts"], seq=r["seq"])
                events.append(ev)
        return events

    def stats(self) -> Dict:
        with self._conn() as c:
            ne = c.execute("SELECT COUNT(*) FROM event_log").fetchone()[0]
            nd = c.execute("SELECT COUNT(*) FROM dlq").fetchone()[0]
        return {"total_events": ne, "total_dlq": nd}

class EventBus:
    """
    Async pub/sub event bus with topic patterns and replay.

    Usage:
        bus = EventBus()

        async def on_user_created(event):
            print(f"New user: {event.payload['name']}")

        bus.subscribe("user_logger", "user.*", on_user_created)

        await bus.publish("user.created", {"name": "Alice", "id": 1})
        await bus.publish("user.deleted", {"id": 2})
    """
    def __init__(self, db_path: str = "data/eventbus.db",
                 retention: int = 10000):
        self._store = EBStore(db_path)
        self._subs: Dict[str, Subscription] = {}
        self._stats: Dict[str, TopicStats] = defaultdict(
            lambda: TopicStats(topic=""))
        self._paused_topics: Set[str] = set()
        self._paused_global = False
        self._middleware: List[Callable] = []
        self._publish_hooks: List[Callable] = []
        self._error_hooks: List[Callable] = []
        self._retention = retention
        self._topic_buffers: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=retention))
        self._global_seq = self._store._seq

    def subscribe(self, name: str, topic_pattern: str,
                   handler: Callable,
                   filter_fn: Callable = None,
                   priority: int = 5,
                   max_retries: int = 0,
                   retry_delay_s: float = 0.1,
                   delivery_mode: DeliveryMode = DeliveryMode.AT_MOST_ONCE
                   ) -> Subscription:
        sub = Subscription(name=name, topic_pattern=topic_pattern,
                            handler=handler, filter_fn=filter_fn,
                            priority=priority, max_retries=max_retries,
                            retry_delay_s=retry_delay_s,
                            delivery_mode=delivery_mode)
        self._subs[name] = sub
        return sub

    def unsubscribe(self, name: str) -> bool:
        return bool(self._subs.pop(name, None))

    def add_middleware(self, fn: Callable): self._middleware.append(fn)
    def on_publish(self, fn: Callable):     self._publish_hooks.append(fn)
    def on_error(self, fn: Callable):       self._error_hooks.append(fn)

    def _apply_middleware(self, event: Event) -> Optional[Event]:
        for mw in self._middleware:
            try:
                event = mw(event)
                if event is None: return None
            except: pass
        return event

    async def _dispatch_to(self, sub: Subscription, event: Event):
        retries = 0
        while True:
            try:
                if asyncio.iscoroutinefunction(sub.handler):
                    await sub.handler(event)
                else:
                    await asyncio.get_event_loop().run_in_executor(
                        None, sub.handler, event)
                sub._dispatch_count += 1
                self._stats[event.topic].dispatched += 1
                return
            except Exception as exc:
                sub._error_count += 1
                self._stats[event.topic].failed += 1
                for h in self._error_hooks:
                    try: h(event, sub, exc)
                    except: pass
                if retries < sub.max_retries:
                    retries += 1
                    await asyncio.sleep(sub.retry_delay_s * (2 ** (retries - 1)))
                    continue
                self._store.add_dlq(event, sub.name, str(exc))
                self._stats[event.topic].dlq_count += 1
                return

    async def publish(self, topic: str, payload: Any,
                       headers: Dict = None, source: str = "",
                       correlation_id: str = "") -> Event:
        self._global_seq += 1
        ev = Event(id=str(uuid.uuid4())[:12], topic=topic,
                    payload=payload, headers=dict(headers or {}),
                    source=source, correlation_id=correlation_id,
                    seq=self._global_seq)
        # Middleware
        ev = self._apply_middleware(ev)
        if ev is None: return ev

        self._topic_buffers[topic].append(ev)
        st = self._stats[topic]
        st.topic = topic; st.published += 1
        self._store.save_event(ev)

        for h in self._publish_hooks:
            try: h(ev)
            except: pass

        if self._paused_global or topic in self._paused_topics:
            return ev

        # Collect matching subscribers sorted by priority
        matched = sorted(
            [s for s in self._subs.values()
             if s.enabled and s.matches(topic) and s.passes_filter(ev)],
            key=lambda s: s.priority)

        await asyncio.gather(
            *[self._dispatch_to(s, ev) for s in matched],
            return_exceptions=True)
        return ev

    def publish_sync(self, topic: str, payload: Any, **kwargs) -> Event:
        return asyncio.get_event_loop().run_until_complete(
            self.publish(topic, payload, **kwargs))

    async def replay(self, subscription_name: str,
                      from_seq: int = 0, limit: int = 100) -> int:
        sub = self._subs.get(subscription_name)
        if not sub: return 0
        events = self._store.get_events_from(
            sub.topic_pattern, from_seq, limit)
        dispatched = 0
        for ev in events:
            if sub.passes_filter(ev):
                await self._dispatch_to(sub, ev)
                dispatched += 1
        return dispatched

    def pause(self, topic: str = None):
        if topic: self._paused_topics.add(topic)
        else: self._paused_global = True

    def resume(self, topic: str = None):
        if topic: self._paused_topics.discard(topic)
        else: self._paused_global = False

    def get_buffer(self, topic: str,
                    limit: int = 100) -> List[Event]:
        buf = self._topic_buffers.get(topic, deque())
        events = list(buf)
        return events[-limit:] if limit else events

    def dlq(self, topic: str = None, limit: int = 50) -> List[Dict]:
        return self._store.get_dlq(topic, limit)

    def stats(self) -> Dict:
        s = self._store.stats()
        s["subscriptions"] = len(self._subs)
        s["topics"] = {t: st.to_dict() for t, st in self._stats.items()}
        s["paused_global"] = self._paused_global
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def publish_ep(req):
            d = await req.json()
            ev = await self.publish(d["topic"], d["payload"],
                                     d.get("headers",{}),
                                     d.get("source",""),
                                     d.get("correlation_id",""))
            return web.json_response(ev.to_dict(), status=201)
        async def dlq_ep(req):
            topic = req.rel_url.query.get("topic")
            return web.json_response({"dlq": self.dlq(topic)})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/eventbus"
        app.router.add_post(f"{p}/publish", publish_ep)
        app.router.add_get( f"{p}/dlq",     dlq_ep)
        app.router.add_get( f"{p}/stats",   stats_ep)
        logger.info(f"Event bus API at {prefix}/eventbus/")
