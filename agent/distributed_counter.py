"""OMNI Agent — Distributed Counter: CRDT-inspired atomic counters with sharding and windows."""
from __future__ import annotations
import sqlite3, threading, time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class CounterType(str, Enum):
    MONOTONIC   = "monotonic"    # only increments
    BIDIRECTIONAL = "bidirectional"  # inc/dec (G-Counter + PN-Counter hybrid)
    RATE        = "rate"         # per-second rate tracker
    WINDOWED    = "windowed"     # rolling time-window counts
    GAUGE       = "gauge"        # absolute set value


@dataclass
class CounterShard:
    shard_id: str
    inc: float = 0.0    # total increments
    dec: float = 0.0    # total decrements
    ts: float = field(default_factory=time.time)

    @property
    def value(self) -> float:
        return self.inc - self.dec


@dataclass
class WindowedBucket:
    ts: float          # bucket start
    count: float = 0.0


class DistributedCounter:
    """
    Thread-safe counter with:
    - Multiple types (monotonic, bidirectional, rate, windowed, gauge)
    - Sharded internal representation (CRDT-inspired)
    - Rolling-window aggregation
    - Threshold callbacks
    - SQLite persistence and history
    """

    def __init__(self, name: str, counter_type: CounterType = CounterType.BIDIRECTIONAL,
                 n_shards: int = 4,
                 window_s: float = 60.0,
                 bucket_s: float = 1.0,
                 db_path: str = ":memory:"):
        self.name         = name
        self.counter_type = counter_type
        self.n_shards     = n_shards
        self.window_s     = window_s
        self.bucket_s     = bucket_s
        self._shards: List[CounterShard] = [
            CounterShard(shard_id=f"{name}:{i}") for i in range(n_shards)]
        self._gauge_val   = 0.0
        self._buckets: List[WindowedBucket] = []
        self._lock        = threading.Lock()
        self._thresholds: List[Tuple[float, Callable]] = []
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._ops = 0

    def _init_db(self):
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS dc_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT, op TEXT, delta REAL, value_after REAL, ts REAL
            )""")
        self._db.commit()

    # ── CORE OPS ──────────────────────────────────────────────────────

    def _shard(self) -> CounterShard:
        import random
        return self._shards[self._ops % self.n_shards]

    def increment(self, delta: float = 1.0) -> float:
        if self.counter_type == CounterType.GAUGE:
            raise TypeError("Use set() for GAUGE counters")
        with self._lock:
            s = self._shard()
            s.inc += delta
            s.ts   = time.time()
            self._ops += 1
            self._record_bucket(delta)
            val = self.value
        self._check_thresholds(val)
        self._log("inc", delta, val)
        return val

    def decrement(self, delta: float = 1.0) -> float:
        if self.counter_type == CounterType.MONOTONIC:
            raise TypeError("MONOTONIC counter cannot decrement")
        if self.counter_type == CounterType.GAUGE:
            raise TypeError("Use set() for GAUGE counters")
        with self._lock:
            s = self._shard()
            s.dec += delta
            s.ts   = time.time()
            self._ops += 1
            self._record_bucket(-delta)
            val = self.value
        self._check_thresholds(val)
        self._log("dec", delta, val)
        return val

    def set(self, value: float) -> float:
        if self.counter_type != CounterType.GAUGE:
            raise TypeError("set() only valid for GAUGE counters")
        with self._lock:
            self._gauge_val = value
            self._ops += 1
        self._log("set", value, value)
        return value

    def reset(self):
        with self._lock:
            for s in self._shards:
                s.inc = s.dec = 0.0
            self._gauge_val = 0.0
            self._buckets.clear()

    # ── VALUE ─────────────────────────────────────────────────────────

    @property
    def value(self) -> float:
        if self.counter_type == CounterType.GAUGE:
            return self._gauge_val
        return sum(s.value for s in self._shards)

    def window_value(self, window_s: Optional[float] = None) -> float:
        """Sum of increments within rolling window."""
        cutoff = time.time() - (window_s or self.window_s)
        with self._lock:
            return sum(b.count for b in self._buckets if b.ts >= cutoff)

    def rate(self, window_s: float = 1.0) -> float:
        """Events per second in the window."""
        total = self.window_value(window_s)
        return total / window_s if window_s > 0 else 0.0

    # ── WINDOWING ─────────────────────────────────────────────────────

    def _record_bucket(self, delta: float):
        """Add delta to current time bucket."""
        now = time.time()
        bk  = (now // self.bucket_s) * self.bucket_s
        # Prune old buckets
        cutoff = now - self.window_s
        self._buckets = [b for b in self._buckets if b.ts >= cutoff]
        # Find or create current bucket
        for bucket in self._buckets:
            if bucket.ts == bk:
                bucket.count += delta
                return
        self._buckets.append(WindowedBucket(ts=bk, count=delta))

    def snapshot_buckets(self) -> List[Dict[str, Any]]:
        now = time.time()
        cutoff = now - self.window_s
        with self._lock:
            return [{"ts": b.ts, "count": b.count}
                    for b in self._buckets if b.ts >= cutoff]

    # ── THRESHOLDS ────────────────────────────────────────────────────

    def on_threshold(self, threshold: float, fn: Callable[[float], None]):
        """Fire fn(current_value) when value crosses threshold."""
        self._thresholds.append((threshold, fn))

    def _check_thresholds(self, val: float):
        for threshold, fn in self._thresholds:
            if val >= threshold:
                try: fn(val)
                except Exception: pass

    # ── MERGE (CRDT) ──────────────────────────────────────────────────

    def merge(self, other: "DistributedCounter"):
        """Merge another counter's shards (CRDT merge = take max of each shard)."""
        with self._lock:
            for i, shard in enumerate(self._shards):
                if i < len(other._shards):
                    other_shard = other._shards[i]
                    shard.inc = max(shard.inc, other_shard.inc)
                    shard.dec = max(shard.dec, other_shard.dec)

    # ── LOGGING ───────────────────────────────────────────────────────

    def _log(self, op: str, delta: float, val: float):
        self._db.execute(
            "INSERT INTO dc_history (name,op,delta,value_after,ts) VALUES (?,?,?,?,?)",
            (self.name, op, delta, val, time.time()))
        self._db.commit()

    def history(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._db.execute(
            "SELECT op,delta,value_after,ts FROM dc_history "
            "WHERE name=? ORDER BY ts DESC LIMIT ?",
            (self.name, limit)).fetchall()
        return [{"op": r[0], "delta": r[1], "value_after": r[2], "ts": r[3]}
                for r in rows]

    def stats(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "type": self.counter_type.value,
            "value": self.value,
            "window_value": self.window_value(),
            "rate_per_s": self.rate(),
            "ops": self._ops,
            "shards": self.n_shards,
        }


# ── COUNTER REGISTRY ──────────────────────────────────────────────────────────

class CounterRegistry:
    """Manages multiple named distributed counters."""

    def __init__(self, db_path: str = ":memory:"):
        self._counters: Dict[str, DistributedCounter] = {}
        self._db_path  = db_path

    def get_or_create(self, name: str,
                      counter_type: CounterType = CounterType.BIDIRECTIONAL,
                      **kwargs) -> DistributedCounter:
        if name not in self._counters:
            self._counters[name] = DistributedCounter(
                name, counter_type, db_path=self._db_path, **kwargs)
        return self._counters[name]

    def get(self, name: str) -> Optional[DistributedCounter]:
        return self._counters.get(name)

    def increment(self, name: str, delta: float = 1.0) -> float:
        return self.get_or_create(name).increment(delta)

    def decrement(self, name: str, delta: float = 1.0) -> float:
        return self.get_or_create(name).decrement(delta)

    def value(self, name: str) -> Optional[float]:
        c = self._counters.get(name)
        return c.value if c else None

    def all_values(self) -> Dict[str, float]:
        return {name: c.value for name, c in self._counters.items()}

    def reset_all(self):
        for c in self._counters.values():
            c.reset()

    def stats_all(self) -> Dict[str, Any]:
        return {name: c.stats() for name, c in self._counters.items()}

    def list_counters(self) -> List[str]:
        return list(self._counters.keys())
