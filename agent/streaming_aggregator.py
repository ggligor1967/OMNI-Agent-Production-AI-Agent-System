"""OMNI AGENT - Streaming Aggregator
Merge and fan-out multiple async generators: interleave, concat, zip,
buffer, deduplicate, transform, and broadcast token streams.

Features:
- Merge: interleave N async generators with round-robin or priority
- Concat: sequential drain of generators one after another
- Zip: pair tokens from multiple streams
- Fan-out: broadcast one stream to multiple consumers
- Buffer: accumulate tokens with configurable chunk size or timeout
- Deduplicate: drop repeated consecutive tokens within a window
- Throttle: emit at max N tokens/second
- Transform: apply per-token or per-chunk transform functions
- Error isolation: one failing stream doesn't stop others
- Back-pressure: bounded queues apply back-pressure to producers
- Aggregation stats: tokens produced, dropped, latency per stream
- Async-safe: all operations use asyncio primitives
"""
import asyncio, time, logging
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
logger = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────
async def _drain(gen: AsyncGenerator, queue: asyncio.Queue, name: str, stats: Dict):
    """Drain a generator into a queue, putting a sentinel when done."""
    try:
        async for token in gen:
            await queue.put((name, token))
            stats[name]["produced"] = stats[name].get("produced", 0) + 1
    except Exception as e:
        logger.warning(f"Stream {name!r} error: {e}")
        stats[name]["error"] = str(e)
    finally:
        await queue.put((name, StopAsyncIteration))

# ── Core aggregation functions ────────────────────────────────────────────────

async def merge_streams(*generators: AsyncGenerator,
                         names: List[str] = None,
                         buffer_size: int = 100) -> AsyncGenerator:
    """
    Interleave multiple async generators, yielding tokens as they arrive.
    Any generator finishing does not stop others.
    """
    if not generators:
        return
    names = names or [f"stream_{i}" for i in range(len(generators))]
    stats = {n: {} for n in names}
    queue: asyncio.Queue = asyncio.Queue(maxsize=buffer_size)
    active = len(generators)

    tasks = [asyncio.create_task(_drain(gen, queue, name, stats))
             for gen, name in zip(generators, names)]

    while active > 0:
        name, token = await queue.get()
        if token is StopAsyncIteration:
            active -= 1
        else:
            yield token
    for t in tasks:
        t.cancel()

async def concat_streams(*generators: AsyncGenerator) -> AsyncGenerator:
    """Drain generators one after another (sequential)."""
    for gen in generators:
        async for token in gen:
            yield token

async def zip_streams(*generators: AsyncGenerator) -> AsyncGenerator:
    """Yield tuples, one token from each stream per round."""
    iters = [gen.__aiter__() for gen in generators]
    while True:
        results = []
        for it in iters:
            try:
                results.append(await it.__anext__())
            except StopAsyncIteration:
                return
        yield tuple(results)

async def buffer_stream(gen: AsyncGenerator,
                         chunk_size: int = 10,
                         timeout_s: float = 0.1) -> AsyncGenerator:
    """
    Accumulate tokens into chunks of up to chunk_size, or flush
    after timeout_s seconds of inactivity.
    """
    buf = []; last_flush = time.time()
    async for token in gen:
        buf.append(token)
        if len(buf) >= chunk_size or (time.time() - last_flush) >= timeout_s:
            yield buf; buf = []; last_flush = time.time()
    if buf:
        yield buf

async def deduplicate_stream(gen: AsyncGenerator,
                               window: int = 1) -> AsyncGenerator:
    """Drop repeated consecutive tokens within a sliding window."""
    recent = []
    async for token in gen:
        if token not in recent:
            yield token
        recent.append(token)
        if len(recent) > window:
            recent.pop(0)

async def throttle_stream(gen: AsyncGenerator,
                           rate: float = 10.0) -> AsyncGenerator:
    """Emit at most `rate` tokens per second."""
    interval = 1.0 / max(1e-9, rate)
    last = time.time()
    async for token in gen:
        now = time.time()
        elapsed = now - last
        if elapsed < interval:
            await asyncio.sleep(interval - elapsed)
        last = time.time()
        yield token

async def transform_stream(gen: AsyncGenerator,
                             fn: Callable) -> AsyncGenerator:
    """Apply a sync or async transform function to each token."""
    async for token in gen:
        if asyncio.iscoroutinefunction(fn):
            result = await fn(token)
        else:
            result = fn(token)
        if result is not None:
            yield result

async def filter_stream(gen: AsyncGenerator,
                         predicate: Callable) -> AsyncGenerator:
    """Yield only tokens for which predicate(token) is True."""
    async for token in gen:
        if asyncio.iscoroutinefunction(predicate):
            keep = await predicate(token)
        else:
            keep = predicate(token)
        if keep:
            yield token

# ── Fan-out ────────────────────────────────────────────────────────────────────
@dataclass
class FanOut:
    """
    Broadcast a single async generator to multiple consumers.
    Each consumer gets its own queue and can read independently.

    Usage:
        fo = FanOut(source_gen, consumers=3, buffer_size=50)
        await fo.start()
        async for token in fo.consumer(0): print("A:", token)
    """
    source: AsyncGenerator
    consumers: int = 2
    buffer_size: int = 100
    _queues: List[asyncio.Queue] = field(default_factory=list)
    _started: bool = False
    _task: Optional[asyncio.Task] = None
    _stats: Dict = field(default_factory=dict)

    def __post_init__(self):
        self._queues = [asyncio.Queue(maxsize=self.buffer_size)
                         for _ in range(self.consumers)]
        self._stats = {"produced": 0, "errors": 0}

    async def _broadcast(self):
        try:
            async for token in self.source:
                self._stats["produced"] += 1
                for q in self._queues:
                    await q.put(token)
        except Exception as e:
            self._stats["errors"] += 1
            logger.warning(f"FanOut source error: {e}")
        finally:
            for q in self._queues:
                await q.put(StopAsyncIteration)

    async def start(self):
        if not self._started:
            self._task = asyncio.create_task(self._broadcast())
            self._started = True

    async def consumer(self, index: int) -> AsyncGenerator:
        if index >= len(self._queues):
            raise IndexError(f"Consumer index {index} out of range")
        q = self._queues[index]
        while True:
            token = await q.get()
            if token is StopAsyncIteration:
                break
            yield token

    async def stop(self):
        if self._task:
            self._task.cancel()

# ── StreamAggregator class ────────────────────────────────────────────────────
class StreamingAggregator:
    """
    High-level streaming aggregator: merge, concat, fan-out, and transform.

    Usage:
        agg = StreamingAggregator()

        # Merge two LLM streams
        merged = agg.merge(stream_a, stream_b, names=["gpt","claude"])
        async for token in merged:
            print(token, end="", flush=True)

        # Fan-out one stream to logging + UI
        fo = await agg.fan_out(source_stream, consumers=2)
        async for t in fo.consumer(0): log(t)
        async for t in fo.consumer(1): update_ui(t)
    """
    def __init__(self):
        self._stats: Dict[str, int] = {"merged": 0, "concat": 0,
                                        "fan_out": 0, "transforms": 0}

    def merge(self, *generators: AsyncGenerator,
               names: List[str] = None,
               buffer_size: int = 100) -> AsyncGenerator:
        self._stats["merged"] += 1
        return merge_streams(*generators, names=names, buffer_size=buffer_size)

    def concat(self, *generators: AsyncGenerator) -> AsyncGenerator:
        self._stats["concat"] += 1
        return concat_streams(*generators)

    def zip(self, *generators: AsyncGenerator) -> AsyncGenerator:
        return zip_streams(*generators)

    def buffer(self, gen: AsyncGenerator,
                chunk_size: int = 10,
                timeout_s: float = 0.1) -> AsyncGenerator:
        return buffer_stream(gen, chunk_size, timeout_s)

    def deduplicate(self, gen: AsyncGenerator, window: int = 1) -> AsyncGenerator:
        return deduplicate_stream(gen, window)

    def throttle(self, gen: AsyncGenerator, rate: float = 10.0) -> AsyncGenerator:
        return throttle_stream(gen, rate)

    def transform(self, gen: AsyncGenerator, fn: Callable) -> AsyncGenerator:
        self._stats["transforms"] += 1
        return transform_stream(gen, fn)

    def filter(self, gen: AsyncGenerator, predicate: Callable) -> AsyncGenerator:
        return filter_stream(gen, predicate)

    async def fan_out(self, source: AsyncGenerator,
                       consumers: int = 2,
                       buffer_size: int = 100) -> FanOut:
        self._stats["fan_out"] += 1
        fo = FanOut(source=source, consumers=consumers, buffer_size=buffer_size)
        await fo.start()
        return fo

    async def collect(self, gen: AsyncGenerator, max_tokens: int = 10000) -> List:
        """Collect all tokens from a generator into a list."""
        tokens = []
        async for token in gen:
            tokens.append(token)
            if len(tokens) >= max_tokens:
                break
        return tokens

    async def join(self, gen: AsyncGenerator, sep: str = "") -> str:
        """Collect and join string tokens."""
        parts = []
        async for token in gen:
            parts.append(str(token))
        return sep.join(parts)

    def stats(self) -> Dict:
        return dict(self._stats)

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/stream"
        app.router.add_get(f"{p}/stats", stats_ep)
        logger.info(f"Streaming aggregator stats at {prefix}/stream/stats")

# ── Utility async generators for testing ─────────────────────────────────────
async def tokens_from_list(items: List, delay: float = 0.0) -> AsyncGenerator:
    """Create an async generator from a list (useful for tests)."""
    for item in items:
        if delay > 0:
            await asyncio.sleep(delay)
        yield item
