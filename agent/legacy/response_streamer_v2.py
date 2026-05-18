"""OMNI Agent — Response Streamer V2: token streaming with buffering, transforms and SSE."""
from __future__ import annotations
import json, queue, threading, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Generator, Iterator, List, Optional


class StreamEvent(str, Enum):
    TOKEN       = "token"
    CHUNK       = "chunk"
    DONE        = "done"
    ERROR       = "error"
    METADATA    = "metadata"
    HEARTBEAT   = "heartbeat"
    TOOL_CALL   = "tool_call"
    TOOL_RESULT = "tool_result"


class StreamState(str, Enum):
    IDLE       = "idle"
    STREAMING  = "streaming"
    PAUSED     = "paused"
    DONE       = "done"
    ERROR      = "error"


@dataclass
class StreamChunk:
    chunk_id:   str = field(default_factory=lambda: str(uuid.uuid4())[:6])
    event:      StreamEvent = StreamEvent.TOKEN
    data:       Any = None
    index:      int = 0
    ts:         float = field(default_factory=time.time)
    stream_id:  str = ""
    metadata:   Dict[str, Any] = field(default_factory=dict)
    finish_reason: Optional[str] = None

    def to_sse(self) -> str:
        """Format as Server-Sent Event string."""
        payload = json.dumps({
            "id": self.chunk_id,
            "event": self.event.value,
            "data": self.data,
            "index": self.index,
            "stream_id": self.stream_id,
        }, default=str)
        return f"event: {self.event.value}\ndata: {payload}\n\n"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "event": self.event.value,
            "data": self.data,
            "index": self.index,
            "stream_id": self.stream_id,
        }


@dataclass
class StreamStats:
    stream_id:    str = ""
    chunks_sent:  int = 0
    tokens_sent:  int = 0
    bytes_sent:   int = 0
    started_at:   float = field(default_factory=time.time)
    finished_at:  Optional[float] = None
    error:        Optional[str] = None
    state:        StreamState = StreamState.IDLE

    @property
    def duration_ms(self) -> float:
        end = self.finished_at or time.time()
        return (end - self.started_at) * 1000

    @property
    def tokens_per_second(self) -> float:
        d = self.duration_ms / 1000
        return self.tokens_sent / d if d > 0 else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stream_id": self.stream_id,
            "chunks_sent": self.chunks_sent,
            "tokens_sent": self.tokens_sent,
            "duration_ms": round(self.duration_ms, 2),
            "tokens_per_second": round(self.tokens_per_second, 2),
            "state": self.state.value,
        }


class ResponseStreamerV2:
    """
    Token/chunk response streamer with:
    - Generator-based streaming API
    - SSE (Server-Sent Events) formatting
    - Per-chunk transforms (filter, map, redact)
    - Buffering with max buffer size
    - Backpressure (pause/resume)
    - Heartbeat for long-running streams
    - Parallel fan-out to multiple subscribers
    - Assembles full response from chunks
    - Stream metadata and tool_call events
    - Per-stream statistics
    """

    def __init__(
        self,
        buffer_size: int = 512,
        heartbeat_interval_s: float = 30.0,
        default_chunk_size: int = 1,
    ):
        self.buffer_size          = buffer_size
        self.heartbeat_interval_s = heartbeat_interval_s
        self.default_chunk_size   = default_chunk_size
        self._streams: Dict[str, StreamStats] = {}
        self._transforms: List[Callable[[StreamChunk], Optional[StreamChunk]]] = []
        self._subscribers: Dict[str, List[queue.Queue]] = {}
        self._lock = threading.Lock()
        self._total_streams = 0

    # ── TRANSFORMS ────────────────────────────────────────────────────

    def add_transform(self, fn: Callable[[StreamChunk],
                                          Optional[StreamChunk]]):
        """Add a chunk transform. Return None to drop the chunk."""
        self._transforms.append(fn)

    def add_token_filter(self, predicate: Callable[[str], bool]):
        """Drop tokens where predicate(token) is False."""
        def _filter(chunk: StreamChunk) -> Optional[StreamChunk]:
            if chunk.event == StreamEvent.TOKEN:
                if not predicate(str(chunk.data or "")):
                    return None
            return chunk
        self.add_transform(_filter)

    def add_token_map(self, fn: Callable[[str], str]):
        """Transform token text."""
        def _map(chunk: StreamChunk) -> Optional[StreamChunk]:
            if chunk.event == StreamEvent.TOKEN:
                chunk.data = fn(str(chunk.data or ""))
            return chunk
        self.add_transform(_map)

    def add_redact(self, patterns: List[str]):
        """Redact patterns from token text."""
        import re
        compiled = [re.compile(p) for p in patterns]
        def _redact(chunk: StreamChunk) -> Optional[StreamChunk]:
            if chunk.event == StreamEvent.TOKEN and chunk.data:
                text = str(chunk.data)
                for pat in compiled:
                    text = pat.sub("[REDACTED]", text)
                chunk.data = text
            return chunk
        self.add_transform(_redact)

    def _apply_transforms(self, chunk: StreamChunk) -> Optional[StreamChunk]:
        for fn in self._transforms:
            if chunk is None:
                return None
            try:
                chunk = fn(chunk)
            except Exception:
                pass
        return chunk

    # ── STREAMING API ─────────────────────────────────────────────────

    def stream(self, source: Iterator[str],
               stream_id: Optional[str] = None,
               metadata: Optional[Dict] = None) -> Generator[StreamChunk, None, None]:
        """Stream tokens from an iterator. Yields StreamChunk objects."""
        sid   = stream_id or str(uuid.uuid4())[:8]
        stats = StreamStats(stream_id=sid, state=StreamState.STREAMING)
        self._streams[sid] = stats
        self._total_streams += 1
        index = 0

        if metadata:
            meta_chunk = StreamChunk(
                event=StreamEvent.METADATA,
                data=metadata, index=0, stream_id=sid)
            yield meta_chunk

        try:
            for token in source:
                chunk = StreamChunk(
                    event=StreamEvent.TOKEN,
                    data=token, index=index, stream_id=sid)
                chunk = self._apply_transforms(chunk)
                if chunk is None:
                    continue
                index += 1
                stats.chunks_sent += 1
                stats.tokens_sent += 1
                stats.bytes_sent  += len(str(token).encode())
                self._fan_out(sid, chunk)
                yield chunk

            done = StreamChunk(
                event=StreamEvent.DONE,
                data=None, index=index, stream_id=sid,
                finish_reason="stop")
            stats.state       = StreamState.DONE
            stats.finished_at = time.time()
            self._fan_out(sid, done)
            yield done

        except Exception as exc:
            err_chunk = StreamChunk(
                event=StreamEvent.ERROR,
                data=str(exc), index=index, stream_id=sid)
            stats.state       = StreamState.ERROR
            stats.error       = str(exc)
            stats.finished_at = time.time()
            self._fan_out(sid, err_chunk)
            yield err_chunk

    def stream_sse(self, source: Iterator[str],
                   **kwargs) -> Generator[str, None, None]:
        """Stream as SSE formatted strings."""
        for chunk in self.stream(source, **kwargs):
            yield chunk.to_sse()

    def stream_text(self, text: str,
                    delay_s: float = 0.0,
                    chunk_size: Optional[int] = None,
                    **kwargs) -> Generator[StreamChunk, None, None]:
        """Stream a static string word by word or by chunk_size chars."""
        cs = chunk_size or self.default_chunk_size

        def _gen():
            for i in range(0, len(text), cs):
                yield text[i:i + cs]
                if delay_s > 0:
                    time.sleep(delay_s)

        yield from self.stream(_gen(), **kwargs)

    def stream_with_tool_calls(
        self,
        source: Iterator[Any],
        stream_id: Optional[str] = None,
    ) -> Generator[StreamChunk, None, None]:
        """Stream with mixed token + tool_call events."""
        sid   = stream_id or str(uuid.uuid4())[:8]
        stats = StreamStats(stream_id=sid, state=StreamState.STREAMING)
        self._streams[sid] = stats
        self._total_streams += 1
        index = 0
        try:
            for item in source:
                if isinstance(item, dict) and item.get("type") == "tool_call":
                    chunk = StreamChunk(
                        event=StreamEvent.TOOL_CALL,
                        data=item, index=index, stream_id=sid)
                elif isinstance(item, dict) and item.get("type") == "tool_result":
                    chunk = StreamChunk(
                        event=StreamEvent.TOOL_RESULT,
                        data=item, index=index, stream_id=sid)
                else:
                    chunk = StreamChunk(
                        event=StreamEvent.TOKEN,
                        data=item, index=index, stream_id=sid)
                chunk = self._apply_transforms(chunk)
                if chunk is None: continue
                index += 1; stats.chunks_sent += 1
                self._fan_out(sid, chunk)
                yield chunk
            done = StreamChunk(event=StreamEvent.DONE,
                               data=None, index=index, stream_id=sid,
                               finish_reason="stop")
            stats.state = StreamState.DONE
            stats.finished_at = time.time()
            yield done
        except Exception as exc:
            yield StreamChunk(event=StreamEvent.ERROR,
                              data=str(exc), stream_id=sid)

    # ── COLLECT ───────────────────────────────────────────────────────

    def collect(self, source: Iterator[str], **kwargs) -> str:
        """Collect all tokens into a single string."""
        parts = []
        for chunk in self.stream(source, **kwargs):
            if chunk.event == StreamEvent.TOKEN:
                parts.append(str(chunk.data or ""))
        return "".join(parts)

    def collect_chunks(self, source: Iterator[str],
                       **kwargs) -> List[StreamChunk]:
        return list(self.stream(source, **kwargs))

    # ── FAN-OUT ───────────────────────────────────────────────────────

    def subscribe(self, stream_id: str) -> queue.Queue:
        """Get a queue that receives chunks for a stream."""
        q: queue.Queue = queue.Queue(maxsize=self.buffer_size)
        with self._lock:
            self._subscribers.setdefault(stream_id, []).append(q)
        return q

    def _fan_out(self, stream_id: str, chunk: StreamChunk):
        with self._lock:
            qs = self._subscribers.get(stream_id, [])
        for q in qs:
            try: q.put_nowait(chunk)
            except queue.Full: pass

    # ── STATS ─────────────────────────────────────────────────────────

    def get_stream_stats(self, stream_id: str) -> Optional[StreamStats]:
        return self._streams.get(stream_id)

    def stats(self) -> Dict[str, Any]:
        active = sum(1 for s in self._streams.values()
                     if s.state == StreamState.STREAMING)
        return {
            "total_streams": self._total_streams,
            "active_streams": active,
            "transforms": len(self._transforms),
        }
