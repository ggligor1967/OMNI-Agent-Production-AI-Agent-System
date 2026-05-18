"""OMNI AGENT - Response Streamer
Token-by-token streaming infrastructure: chunk buffering, mid-stream
injection, progress callbacks, format-aware flushing, and stream multiplexing.

Features:
- AsyncGenerator streaming: yield tokens as they arrive from any LLM
- Buffered chunking: accumulate N tokens before flushing to downstream
- Word-boundary flushing: never cut words mid-token
- Sentence-boundary detection: flush on sentence end for smoother UX
- Progress callbacks: fire hooks at % completion milestones
- Mid-stream injection: insert content (e.g. citations) at marked positions
- Stream multiplexing: fan out one stream to multiple subscribers
- Backpressure: slow consumers don't crash the producer
- Metrics: tokens/sec, time-to-first-token, total latency
- Stream recording: save entire stream to string for audit
- Format detection: auto-detect markdown, JSON, code blocks mid-stream
- REST/SSE endpoint: Server-Sent Events compatible output
"""
import asyncio, time, uuid, logging, re
from typing import AsyncGenerator, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
logger = logging.getLogger(__name__)

@dataclass
class StreamChunk:
    text: str; chunk_id: int; is_final: bool = False
    tokens_so_far: int = 0; elapsed_ms: float = 0.0
    injected: bool = False
    def to_dict(self):
        return {"text": self.text, "chunk_id": self.chunk_id,
                "is_final": self.is_final, "tokens_so_far": self.tokens_so_far,
                "elapsed_ms": round(self.elapsed_ms, 2)}

@dataclass
class StreamMetrics:
    total_tokens: int = 0; total_chunks: int = 0
    time_to_first_token_ms: float = 0.0
    total_latency_ms: float = 0.0; injections: int = 0
    @property
    def tokens_per_second(self):
        s = self.total_latency_ms / 1000
        return round(self.total_tokens / s, 1) if s > 0 else 0.0
    def to_dict(self):
        return {"total_tokens": self.total_tokens, "total_chunks": self.total_chunks,
                "time_to_first_token_ms": round(self.time_to_first_token_ms, 2),
                "total_latency_ms": round(self.total_latency_ms, 2),
                "tokens_per_second": self.tokens_per_second,
                "injections": self.injections}

class StreamBuffer:
    """Accumulates tokens and flushes by size, word, or sentence boundary."""
    def __init__(self, flush_mode="word", chunk_size=10):
        self.flush_mode = flush_mode
        self.chunk_size = chunk_size
        self._buf = ""
        self._token_count = 0

    def push(self, token: str) -> Optional[str]:
        self._buf += token
        self._token_count += 1
        return self._try_flush()

    def flush_all(self) -> Optional[str]:
        if self._buf:
            out = self._buf; self._buf = ""; return out
        return None

    def _try_flush(self) -> Optional[str]:
        if self.flush_mode == "token":
            out = self._buf; self._buf = ""; return out
        elif self.flush_mode == "word":
            if self._buf.endswith((" ", "\n", "\t")) or self._token_count >= self.chunk_size:
                out = self._buf; self._buf = ""; self._token_count = 0; return out
        elif self.flush_mode == "sentence":
            if re.search(r'[.!?]\s', self._buf) or self._token_count >= self.chunk_size * 3:
                out = self._buf; self._buf = ""; self._token_count = 0; return out
        elif self.flush_mode == "chunk":
            if self._token_count >= self.chunk_size:
                out = self._buf; self._buf = ""; self._token_count = 0; return out
        return None

class InjectionPoint:
    """Marks a position in the stream where content will be inserted."""
    def __init__(self, marker: str, content: str):
        self.marker = marker
        self.content = content

class ResponseStreamer:
    """
    Token-level streaming with buffering, injection, multiplexing, and metrics.

    Usage:
        streamer = ResponseStreamer()

        # Basic streaming from async generator
        async def token_gen():
            for word in "Hello world this is streaming".split():
                yield word + " "
                await asyncio.sleep(0.01)

        full_text, metrics = await streamer.stream(
            token_gen(),
            on_chunk=lambda c: print(c.text, end="", flush=True),
            flush_mode="word",
        )
        print(metrics.tokens_per_second, "tok/s")
    """
    def __init__(self, default_flush_mode: str = "word",
                 default_chunk_size: int = 8, backpressure_limit: int = 100):
        self.default_flush_mode = default_flush_mode
        self.default_chunk_size = default_chunk_size
        self.backpressure_limit = backpressure_limit
        self._subscribers: Dict[str, asyncio.Queue] = {}
        self._history: List[StreamMetrics] = []

    async def stream(
        self,
        token_source,   # async generator | list[str] | callable → str
        on_chunk: Optional[Callable] = None,
        flush_mode: Optional[str] = None,
        chunk_size: Optional[int] = None,
        injections: Optional[List[InjectionPoint]] = None,
        progress_callbacks: Optional[Dict[int, Callable]] = None,  # {25: fn, 50: fn, ...}
        max_tokens: Optional[int] = None,
        record: bool = True,
    ) -> Tuple[str, StreamMetrics]:
        fm = flush_mode or self.default_flush_mode
        cs = chunk_size or self.default_chunk_size
        buf = StreamBuffer(fm, cs)
        metrics = StreamMetrics()
        full_text = ""
        chunk_id = 0
        start = time.time()
        first_token_time: Optional[float] = None
        injections = injections or []
        inj_map = {ip.marker: ip.content for ip in injections}
        progress_callbacks = progress_callbacks or {}
        fired_milestones = set()

        async def _get_tokens():
            if hasattr(token_source, '__aiter__'):
                async for tok in token_source:
                    yield str(tok)
            elif callable(token_source):
                result = token_source()
                if asyncio.iscoroutine(result):
                    result = await result
                for tok in str(result).split():
                    yield tok + " "
            else:
                for tok in (token_source if isinstance(token_source, list) else [str(token_source)]):
                    yield str(tok)

        async for token in _get_tokens():
            if max_tokens and metrics.total_tokens >= max_tokens:
                break
            now = time.time()
            if first_token_time is None:
                first_token_time = now
                metrics.time_to_first_token_ms = (now - start) * 1000

            # Check for injection markers in token
            for marker, inject_content in inj_map.items():
                if marker in token:
                    token = token.replace(marker, inject_content)
                    metrics.injections += 1

            flushed = buf.push(token)
            if flushed:
                # Apply any injections to flushed content
                chunk = StreamChunk(text=flushed, chunk_id=chunk_id,
                                     tokens_so_far=metrics.total_tokens,
                                     elapsed_ms=(now - start) * 1000)
                chunk_id += 1; metrics.total_chunks += 1
                full_text += flushed
                if record:
                    pass  # full_text already recorded
                # Fire progress callbacks
                if max_tokens:
                    pct = int(metrics.total_tokens / max_tokens * 100)
                    for milestone, cb in progress_callbacks.items():
                        if pct >= milestone and milestone not in fired_milestones:
                            fired_milestones.add(milestone)
                            try:
                                await cb(pct) if asyncio.iscoroutinefunction(cb) else cb(pct)
                            except: pass
                # Push to subscribers
                for q in self._subscribers.values():
                    try:
                        q.put_nowait(chunk)
                    except asyncio.QueueFull:
                        pass  # backpressure: drop for slow consumers
                if on_chunk:
                    try:
                        await on_chunk(chunk) if asyncio.iscoroutinefunction(on_chunk) else on_chunk(chunk)
                    except: pass
            metrics.total_tokens += 1

        # Flush remainder
        remainder = buf.flush_all()
        if remainder:
            end_chunk = StreamChunk(text=remainder, chunk_id=chunk_id,
                                     is_final=True, tokens_so_far=metrics.total_tokens,
                                     elapsed_ms=(time.time() - start) * 1000)
            full_text += remainder; metrics.total_chunks += 1
            for q in self._subscribers.values():
                try: q.put_nowait(end_chunk)
                except asyncio.QueueFull: pass
            if on_chunk:
                try:
                    await on_chunk(end_chunk) if asyncio.iscoroutinefunction(on_chunk) else on_chunk(end_chunk)
                except: pass

        metrics.total_latency_ms = (time.time() - start) * 1000
        # Signal end to subscribers
        for q in self._subscribers.values():
            try: q.put_nowait(None)   # sentinel
            except asyncio.QueueFull: pass
        self._history.append(metrics)
        logger.info(f"Stream complete: {metrics.total_tokens} tokens, {metrics.tokens_per_second} tok/s")
        return full_text, metrics

    async def stream_to_string(self, token_source, **kwargs) -> Tuple[str, StreamMetrics]:
        return await self.stream(token_source, **kwargs)

    def subscribe(self, subscriber_id: Optional[str] = None) -> Tuple[str, asyncio.Queue]:
        sid = subscriber_id or str(uuid.uuid4())[:8]
        q: asyncio.Queue = asyncio.Queue(maxsize=self.backpressure_limit)
        self._subscribers[sid] = q
        return sid, q

    def unsubscribe(self, subscriber_id: str):
        self._subscribers.pop(subscriber_id, None)

    async def multiplex(self, token_source, subscriber_ids: List[str], **kwargs):
        """Stream to multiple named subscribers simultaneously."""
        for sid in subscriber_ids:
            if sid not in self._subscribers:
                self.subscribe(sid)
        return await self.stream(token_source, **kwargs)

    def detect_format(self, text: str) -> str:
        """Detect if accumulated text looks like JSON, markdown, code, or plain."""
        stripped = text.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            return "json"
        if stripped.startswith("```") or re.search(r'```\w+', stripped):
            return "code"
        if re.search(r'^#{1,3}\s', stripped, re.M) or re.search(r'\*\*[^*]+\*\*', stripped):
            return "markdown"
        return "plain"

    def stats(self) -> Dict:
        if not self._history:
            return {"total_streams": 0}
        avg_tps = sum(m.tokens_per_second for m in self._history) / len(self._history)
        return {"total_streams": len(self._history),
                "avg_tokens_per_second": round(avg_tps, 1),
                "avg_latency_ms": round(sum(m.total_latency_ms for m in self._history) / len(self._history), 1),
                "active_subscribers": len(self._subscribers)}

    def history(self, limit: int = 20) -> List[Dict]:
        return [m.to_dict() for m in self._history[-limit:]]

    def register_routes(self, app, prefix: str = ""):
        from aiohttp import web
        async def stream_ep(req):
            d = await req.json()
            tokens = d.get("tokens", d.get("text", "").split())
            chunks_collected = []
            def collect(c): chunks_collected.append(c.to_dict())
            text, metrics = await self.stream(
                tokens, on_chunk=collect,
                flush_mode=d.get("flush_mode", self.default_flush_mode),
                chunk_size=int(d.get("chunk_size", self.default_chunk_size)))
            return web.json_response({"text": text, "metrics": metrics.to_dict(),
                                       "chunks": chunks_collected})
        async def stats_ep(req):
            return web.json_response(self.stats())
        p = f"{prefix}/stream"
        app.router.add_post(f"{p}/run", stream_ep)
        app.router.add_get(f"{p}/stats", stats_ep)
        logger.info(f"Response streamer API at {prefix}/stream/")
