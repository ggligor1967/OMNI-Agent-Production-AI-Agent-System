"""OMNI Agent — Stream Aggregator V2: real-time multi-source stream merging with windowing."""
from __future__ import annotations
import asyncio, collections, statistics, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


class WindowType(str, Enum):
    TUMBLING = "tumbling"   # fixed non-overlapping windows
    SLIDING  = "sliding"    # overlapping fixed-size windows
    SESSION  = "session"    # gap-based dynamic windows
    COUNT    = "count"      # trigger every N events


class AggFunc(str, Enum):
    SUM    = "sum"
    AVG    = "avg"
    MIN    = "min"
    MAX    = "max"
    COUNT  = "count"
    LAST   = "last"
    FIRST  = "first"
    MEDIAN = "median"
    STD    = "std"
    LIST   = "list"


@dataclass
class StreamEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    source: str = "default"
    value: Any = None
    timestamp: float = field(default_factory=time.time)
    tags: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "source": self.source,
            "value": self.value,
            "timestamp": self.timestamp,
            "tags": self.tags,
        }


@dataclass
class WindowResult:
    window_id: str
    source: str
    agg_func: AggFunc
    value: Any
    event_count: int
    window_start: float
    window_end: float
    closed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "window_id": self.window_id,
            "source": self.source,
            "agg_func": self.agg_func.value,
            "value": self.value,
            "event_count": self.event_count,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "closed": self.closed,
        }


def _apply_agg(values: List[Any], fn: AggFunc) -> Any:
    if not values:
        return None
    nums = []
    for v in values:
        try: nums.append(float(v))
        except (TypeError, ValueError): pass
    if fn == AggFunc.SUM:   return sum(nums) if nums else None
    if fn == AggFunc.AVG:   return statistics.mean(nums) if nums else None
    if fn == AggFunc.MIN:   return min(nums) if nums else None
    if fn == AggFunc.MAX:   return max(nums) if nums else None
    if fn == AggFunc.COUNT: return len(values)
    if fn == AggFunc.LAST:  return values[-1]
    if fn == AggFunc.FIRST: return values[0]
    if fn == AggFunc.MEDIAN: return statistics.median(nums) if nums else None
    if fn == AggFunc.STD:   return statistics.stdev(nums) if len(nums) > 1 else 0.0
    if fn == AggFunc.LIST:  return list(values)
    return None


class Stream:
    """Single named source stream with buffering and windowing."""

    def __init__(self, name: str, max_buffer: int = 10_000):
        self.name = name
        self.max_buffer = max_buffer
        self._buffer: collections.deque = collections.deque(maxlen=max_buffer)
        self._total_events = 0
        self._subscribers: List[Callable] = []
        self._filters: List[Callable] = []
        self._transforms: List[Callable] = []

    def emit(self, value: Any, tags: Optional[Dict[str, str]] = None) -> StreamEvent:
        event = StreamEvent(source=self.name, value=value, tags=tags or {})
        # Apply filters
        for f in self._filters:
            if not f(event):
                return event  # dropped
        # Apply transforms
        for t in self._transforms:
            event = t(event)
        self._buffer.append(event)
        self._total_events += 1
        for sub in self._subscribers:
            try: sub(event)
            except Exception: pass
        return event

    def subscribe(self, fn: Callable[[StreamEvent], None]):
        self._subscribers.append(fn)

    def filter(self, fn: Callable[[StreamEvent], bool]) -> "Stream":
        self._filters.append(fn)
        return self

    def transform(self, fn: Callable[[StreamEvent], StreamEvent]) -> "Stream":
        self._transforms.append(fn)
        return self

    def latest(self, n: int = 1) -> List[StreamEvent]:
        buf = list(self._buffer)
        return buf[-n:]

    def window(self, size_s: float, agg_fn: AggFunc = AggFunc.AVG,
               window_type: WindowType = WindowType.TUMBLING) -> List[WindowResult]:
        """Compute window aggregations over buffered events."""
        now = time.time()
        events = list(self._buffer)
        if not events:
            return []
        if window_type == WindowType.TUMBLING:
            return self._tumbling_windows(events, size_s, agg_fn, now)
        if window_type == WindowType.SLIDING:
            return self._sliding_windows(events, size_s, agg_fn, now)
        if window_type == WindowType.COUNT:
            return self._count_windows(events, int(size_s), agg_fn)
        return []

    def _tumbling_windows(self, events: List[StreamEvent],
                          size_s: float, fn: AggFunc, now: float) -> List[WindowResult]:
        if not events:
            return []
        start = events[0].timestamp
        results = []
        while start < now:
            end = start + size_s
            bucket = [e for e in events if start <= e.timestamp < end]
            if bucket:
                results.append(WindowResult(
                    window_id=str(uuid.uuid4())[:8],
                    source=self.name, agg_func=fn,
                    value=_apply_agg([e.value for e in bucket], fn),
                    event_count=len(bucket),
                    window_start=start, window_end=end,
                    closed=end <= now))
            start = end
        return results

    def _sliding_windows(self, events: List[StreamEvent],
                         size_s: float, fn: AggFunc, now: float) -> List[WindowResult]:
        results = []
        for event in events:
            w_start = event.timestamp
            w_end = w_start + size_s
            bucket = [e for e in events if w_start <= e.timestamp < w_end]
            results.append(WindowResult(
                window_id=str(uuid.uuid4())[:8],
                source=self.name, agg_func=fn,
                value=_apply_agg([e.value for e in bucket], fn),
                event_count=len(bucket),
                window_start=w_start, window_end=w_end,
                closed=w_end <= now))
        return results

    def _count_windows(self, events: List[StreamEvent],
                       n: int, fn: AggFunc) -> List[WindowResult]:
        results = []
        for i in range(0, len(events), n):
            chunk = events[i:i + n]
            results.append(WindowResult(
                window_id=str(uuid.uuid4())[:8],
                source=self.name, agg_func=fn,
                value=_apply_agg([e.value for e in chunk], fn),
                event_count=len(chunk),
                window_start=chunk[0].timestamp,
                window_end=chunk[-1].timestamp,
                closed=len(chunk) == n))
        return results

    def stats(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "buffered": len(self._buffer),
            "total_events": self._total_events,
            "subscribers": len(self._subscribers),
        }


class StreamAggregatorV2:
    """
    Merges multiple named streams, applies cross-stream joins,
    and provides real-time aggregation with async support.
    """

    def __init__(self):
        self._streams: Dict[str, Stream] = {}
        self._merged_buffer: collections.deque = collections.deque(maxlen=100_000)
        self._global_subscribers: List[Callable] = []
        self._total_events = 0

    # ── STREAMS ───────────────────────────────────────────────────────

    def add_stream(self, name: str, max_buffer: int = 10_000) -> Stream:
        stream = Stream(name, max_buffer)
        stream.subscribe(self._on_event)
        self._streams[name] = stream
        return stream

    def get_stream(self, name: str) -> Optional[Stream]:
        return self._streams.get(name)

    def remove_stream(self, name: str):
        self._streams.pop(name, None)

    def _on_event(self, event: StreamEvent):
        self._merged_buffer.append(event)
        self._total_events += 1
        for sub in self._global_subscribers:
            try: sub(event)
            except Exception: pass

    # ── EMIT ──────────────────────────────────────────────────────────

    def emit(self, stream_name: str, value: Any,
             tags: Optional[Dict[str, str]] = None) -> Optional[StreamEvent]:
        stream = self._streams.get(stream_name)
        if stream is None:
            return None
        return stream.emit(value, tags)

    async def emit_async(self, stream_name: str, value: Any,
                          tags: Optional[Dict[str, str]] = None) -> Optional[StreamEvent]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.emit, stream_name, value, tags)

    # ── QUERY ─────────────────────────────────────────────────────────

    def latest(self, stream_name: str, n: int = 1) -> List[StreamEvent]:
        stream = self._streams.get(stream_name)
        return stream.latest(n) if stream else []

    def merged_latest(self, n: int = 10) -> List[StreamEvent]:
        buf = list(self._merged_buffer)
        return buf[-n:]

    def aggregate(self, stream_name: str, fn: AggFunc,
                  window_s: Optional[float] = None) -> Any:
        """Aggregate all buffered events in a stream, optionally within window_s seconds."""
        stream = self._streams.get(stream_name)
        if not stream:
            return None
        now = time.time()
        events = list(stream._buffer)
        if window_s:
            cutoff = now - window_s
            events = [e for e in events if e.timestamp >= cutoff]
        return _apply_agg([e.value for e in events], fn)

    def cross_aggregate(self, stream_names: List[str],
                        fn: AggFunc, window_s: Optional[float] = None) -> Any:
        """Aggregate values from multiple streams together."""
        all_values = []
        now = time.time()
        for name in stream_names:
            stream = self._streams.get(name)
            if not stream:
                continue
            events = list(stream._buffer)
            if window_s:
                cutoff = now - window_s
                events = [e for e in events if e.timestamp >= cutoff]
            all_values.extend(e.value for e in events)
        return _apply_agg(all_values, fn)

    def window(self, stream_name: str, size_s: float,
               agg_fn: AggFunc = AggFunc.AVG,
               window_type: WindowType = WindowType.TUMBLING) -> List[WindowResult]:
        stream = self._streams.get(stream_name)
        if not stream:
            return []
        return stream.window(size_s, agg_fn, window_type)

    def join(self, stream_a: str, stream_b: str,
             window_s: float) -> List[Tuple[StreamEvent, StreamEvent]]:
        """Time-based inner join: pair events from two streams within window_s of each other."""
        sa = self._streams.get(stream_a)
        sb = self._streams.get(stream_b)
        if not sa or not sb:
            return []
        events_a = list(sa._buffer)
        events_b = list(sb._buffer)
        pairs = []
        for ea in events_a:
            for eb in events_b:
                if abs(ea.timestamp - eb.timestamp) <= window_s:
                    pairs.append((ea, eb))
        return pairs

    # ── SUBSCRIBE ─────────────────────────────────────────────────────

    def subscribe_all(self, fn: Callable[[StreamEvent], None]):
        """Subscribe to all events from all streams."""
        self._global_subscribers.append(fn)

    # ── STATS ─────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        return {
            "streams": len(self._streams),
            "total_events": self._total_events,
            "merged_buffer": len(self._merged_buffer),
            "by_stream": {name: s.stats() for name, s in self._streams.items()},
        }
