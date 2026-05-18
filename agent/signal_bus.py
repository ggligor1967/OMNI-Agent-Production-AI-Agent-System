"""OMNI Agent — Signal Bus: typed signals with dispatch, filtering, priority."""
from __future__ import annotations
import inspect, sqlite3, threading, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


class SignalPriority(int, Enum):
    HIGHEST = 0
    HIGH    = 1
    NORMAL  = 2
    LOW     = 3
    LOWEST  = 4


class DispatchMode(str, Enum):
    SYNC    = "sync"
    ASYNC   = "async"     # fire in a daemon thread
    QUEUE   = "queue"     # enqueue for later processing


@dataclass
class Signal:
    signal_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = ""
    payload: Any = None
    source: str = ""
    correlation_id: Optional[str] = None
    ts: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"signal_id": self.signal_id, "name": self.name,
                "source": self.source, "ts": self.ts,
                "correlation_id": self.correlation_id}


@dataclass
class Subscription:
    sub_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    signal_name: str = ""       # exact or pattern ("*" wildcard)
    handler: Callable = field(default=lambda s: None)
    priority: SignalPriority = SignalPriority.NORMAL
    mode: DispatchMode = DispatchMode.SYNC
    filter_fn: Optional[Callable[[Signal], bool]] = None
    once: bool = False          # auto-unsubscribe after first dispatch
    enabled: bool = True
    dispatch_count: int = 0
    last_dispatch_at: Optional[float] = None
    tags: List[str] = field(default_factory=list)

    def matches(self, signal_name: str) -> bool:
        if self.signal_name == "*": return True
        if self.signal_name.endswith("*"):
            return signal_name.startswith(self.signal_name[:-1])
        return self.signal_name == signal_name

    def to_dict(self) -> Dict[str, Any]:
        return {"sub_id": self.sub_id, "signal": self.signal_name,
                "priority": self.priority.value, "mode": self.mode.value,
                "dispatch_count": self.dispatch_count, "once": self.once}


@dataclass
class DispatchRecord:
    record_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    signal_id: str = ""
    signal_name: str = ""
    subscribers_notified: int = 0
    errors: List[str] = field(default_factory=list)
    ts: float = field(default_factory=time.time)
    duration_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {"signal": self.signal_name,
                "notified": self.subscribers_notified,
                "errors": len(self.errors),
                "duration_ms": round(self.duration_ms, 2)}


class SignalBus:
    """
    Typed signal bus:
    - Named signals with arbitrary payloads
    - Subscribe/unsubscribe with handler + priority
    - Wildcard subscriptions ("auth.*", "*")
    - Per-subscription filter predicates
    - Dispatch modes: SYNC / ASYNC (thread) / QUEUE (deferred)
    - Priority-ordered dispatch (HIGHEST first)
    - Once-subscriptions (auto-remove after first fire)
    - Enable/disable subscriptions
    - Correlation IDs for signal chains
    - Signal history ring buffer
    - Queued signal draining
    - Error isolation (one handler failure doesn't stop others)
    - SQLite persistence for dispatch records
    """

    def __init__(self, db_path: str = ":memory:",
                 history_size: int = 500):
        self._subs:    Dict[str, Subscription] = {}
        self._queue:   List[Signal] = []
        self._history: List[Signal] = []
        self._records: List[DispatchRecord] = []
        self._history_size = history_size
        self._lock   = threading.Lock()
        self._db     = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS sb_records (
                record_id TEXT PRIMARY KEY, signal_name TEXT,
                notified INTEGER, errors INTEGER, ts REAL, duration_ms REAL
            );
        """)
        self._db.commit()

    # ── SUBSCRIBE ─────────────────────────────────────────────────────

    def subscribe(self, signal_name: str,
                   handler: Callable,
                   priority: SignalPriority = SignalPriority.NORMAL,
                   mode: DispatchMode = DispatchMode.SYNC,
                   filter_fn: Optional[Callable[[Signal], bool]] = None,
                   once: bool = False,
                   tags: Optional[List[str]] = None,
                   sub_id: Optional[str] = None) -> Subscription:
        sid  = sub_id or str(uuid.uuid4())[:8]
        sub  = Subscription(
            sub_id=sid, signal_name=signal_name,
            handler=handler, priority=priority,
            mode=mode, filter_fn=filter_fn,
            once=once, tags=list(tags or []))
        with self._lock:
            self._subs[sid] = sub
        return sub

    def unsubscribe(self, sub_id: str) -> bool:
        with self._lock:
            return self._subs.pop(sub_id, None) is not None

    def enable(self, sub_id: str):
        s = self._subs.get(sub_id)
        if s: s.enabled = True

    def disable(self, sub_id: str):
        s = self._subs.get(sub_id)
        if s: s.enabled = False

    # ── EMIT ──────────────────────────────────────────────────────────

    def emit(self, name: str,
              payload: Any = None,
              source: str = "",
              correlation_id: Optional[str] = None,
              metadata: Optional[Dict] = None) -> DispatchRecord:
        sig = Signal(name=name, payload=payload, source=source,
                      correlation_id=correlation_id,
                      metadata=metadata or {})
        self._add_history(sig)
        return self._dispatch(sig)

    def emit_signal(self, signal: Signal) -> DispatchRecord:
        self._add_history(signal)
        return self._dispatch(signal)

    def queue(self, name: str, payload: Any = None,
               source: str = "", **kwargs) -> Signal:
        """Enqueue signal for later drain()."""
        sig = Signal(name=name, payload=payload, source=source)
        with self._lock:
            self._queue.append(sig)
        return sig

    def drain(self) -> List[DispatchRecord]:
        """Process all queued signals synchronously."""
        with self._lock:
            queued = list(self._queue)
            self._queue.clear()
        return [self._dispatch(s) for s in queued]

    # ── DISPATCH ──────────────────────────────────────────────────────

    def _dispatch(self, signal: Signal) -> DispatchRecord:
        t0  = time.time()
        rec = DispatchRecord(signal_id=signal.signal_id,
                              signal_name=signal.name)
        with self._lock:
            subs = [s for s in self._subs.values()
                    if s.enabled and s.matches(signal.name)]
        subs.sort(key=lambda s: s.priority.value)

        to_remove: List[str] = []
        for sub in subs:
            if sub.filter_fn:
                try:
                    if not sub.filter_fn(signal):
                        continue
                except Exception:
                    continue
            try:
                if sub.mode == DispatchMode.ASYNC:
                    t = threading.Thread(
                        target=sub.handler, args=(signal,), daemon=True)
                    t.start()
                else:
                    sub.handler(signal)
                sub.dispatch_count   += 1
                sub.last_dispatch_at  = time.time()
                rec.subscribers_notified += 1
            except Exception as exc:
                rec.errors.append(f"{sub.sub_id}: {exc}")
            if sub.once:
                to_remove.append(sub.sub_id)

        with self._lock:
            for sid in to_remove:
                self._subs.pop(sid, None)

        rec.duration_ms = (time.time() - t0) * 1000
        self._records.append(rec)
        self._db.execute(
            "INSERT INTO sb_records VALUES (?,?,?,?,?,?)",
            (rec.record_id, signal.name, rec.subscribers_notified,
             len(rec.errors), rec.ts, rec.duration_ms))
        self._db.commit()
        return rec

    def _add_history(self, sig: Signal):
        self._history.append(sig)
        if len(self._history) > self._history_size:
            self._history = self._history[-self._history_size:]

    # ── QUERY ─────────────────────────────────────────────────────────

    def history(self, name: Optional[str] = None,
                 limit: int = 50) -> List[Dict]:
        h = self._history
        if name: h = [s for s in h if s.name == name]
        return [s.to_dict() for s in h[-limit:]]

    def list_subscriptions(self, signal_name: Optional[str] = None,
                             tag: Optional[str] = None) -> List[Dict]:
        subs = list(self._subs.values())
        if signal_name:
            subs = [s for s in subs if s.signal_name == signal_name]
        if tag:
            subs = [s for s in subs if tag in s.tags]
        return [s.to_dict() for s in subs]

    def dispatch_records(self, limit: int = 50) -> List[Dict]:
        return [r.to_dict() for r in self._records[-limit:]]

    def stats(self) -> Dict[str, Any]:
        return {
            "subscriptions": len(self._subs),
            "signals_emitted": len(self._history),
            "queued": len(self._queue),
            "dispatch_records": len(self._records),
            "total_dispatched": sum(r.subscribers_notified
                                     for r in self._records),
        }
