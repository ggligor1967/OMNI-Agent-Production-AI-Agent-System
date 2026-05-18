"""
OMNI AGENT - Streaming & Server-Sent Events
Real-time token streaming for the REST API, live dashboard push,
and an in-process async event bus for agent observability.

Endpoints added to aiohttp:
  GET /stream/chat        SSE — stream tokens from a chat prompt
  GET /stream/events      SSE — live agent events (tool calls, model routing, errors)
  GET /stream/traces      SSE — live tracing spans

Internal:
  EventBus — async pub/sub within the process
  SSEResponse — aiohttp helper for SSE responses
"""
import json
import time
import asyncio
import logging
import uuid
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Set
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# SSE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def sse_format(data: Any, event: str = "message", id: str = None) -> str:
    """Format a payload as an SSE frame."""
    payload = json.dumps(data) if not isinstance(data, str) else data
    lines = []
    if id:
        lines.append(f"id: {id}")
    if event != "message":
        lines.append(f"event: {event}")
    lines.append(f"data: {payload}")
    lines.append("")          # blank line = end of SSE frame
    lines.append("")
    return "\n".join(lines)


def sse_heartbeat() -> str:
    """Periodic keepalive comment to prevent proxy timeouts."""
    return f": heartbeat {int(time.time())}\n\n"


# ══════════════════════════════════════════════════════════════════════════════
# EVENT BUS
# ══════════════════════════════════════════════════════════════════════════════

class EventBusEvent(str, Enum):
    TOKEN        = "token"          # streaming token from LLM
    TOOL_CALL    = "tool_call"      # tool invoked
    TOOL_RESULT  = "tool_result"    # tool returned
    ROUTE        = "route"          # routing decision
    TRACE_SPAN   = "trace_span"     # span completed
    SYSTEM       = "system"         # agent lifecycle
    ERROR        = "error"          # error occurred
    PIPELINE     = "pipeline_step"  # pipeline step update
    WORKFLOW     = "workflow_step"  # workflow step update


@dataclass
class BusMessage:
    event: EventBusEvent
    data: Any
    session_id: str = ""
    ts: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])

    def to_sse(self) -> str:
        return sse_format(
            {"data": self.data, "session_id": self.session_id, "ts": self.ts},
            event=self.event.value,
            id=self.id,
        )


class EventBus:
    """
    In-process async publish/subscribe event bus.
    Subscribers receive messages via asyncio.Queue.

    Usage:
        bus = EventBus()
        sub_id = bus.subscribe()

        await bus.publish(BusMessage(EventBusEvent.TOKEN, "hello"))

        async for msg in bus.listen(sub_id, timeout=30):
            print(msg.event, msg.data)
    """

    def __init__(self, max_queue: int = 256, max_history: int = 1000):
        self._subscribers: Dict[str, asyncio.Queue] = {}
        self._history: List[BusMessage] = []
        self._max_history = max_history
        self._max_queue = max_queue
        self._filters: Dict[str, Set[EventBusEvent]] = {}

    def subscribe(self, events: List[EventBusEvent] = None,
                  sub_id: str = None) -> str:
        """Register a new subscriber. Returns subscriber ID."""
        sid = sub_id or str(uuid.uuid4())[:8]
        self._subscribers[sid] = asyncio.Queue(maxsize=self._max_queue)
        if events:
            self._filters[sid] = set(events)
        return sid

    def unsubscribe(self, sub_id: str):
        self._subscribers.pop(sub_id, None)
        self._filters.pop(sub_id, None)

    async def publish(self, msg: BusMessage):
        """Broadcast a message to all subscribers (non-blocking)."""
        self._history.append(msg)
        if len(self._history) > self._max_history:
            self._history = self._history[-(self._max_history // 2):]

        for sid, queue in list(self._subscribers.items()):
            allowed = self._filters.get(sid)
            if allowed and msg.event not in allowed:
                continue
            try:
                queue.put_nowait(msg)
            except asyncio.QueueFull:
                # Slow subscriber — drop oldest message
                try:
                    queue.get_nowait()
                    queue.put_nowait(msg)
                except Exception:
                    pass

    async def listen(self, sub_id: str,
                     timeout: float = 60.0) -> AsyncIterator[BusMessage]:
        """Async generator yielding messages for a subscriber until timeout."""
        queue = self._subscribers.get(sub_id)
        if not queue:
            return

        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = deadline - time.time()
            try:
                msg = await asyncio.wait_for(
                    queue.get(), timeout=min(remaining, 5.0)
                )
                yield msg
            except asyncio.TimeoutError:
                yield BusMessage(EventBusEvent.SYSTEM, "__heartbeat__")

    def recent(self, event: EventBusEvent = None,
               limit: int = 50) -> List[BusMessage]:
        msgs = self._history
        if event:
            msgs = [m for m in msgs if m.event == event]
        return msgs[-limit:]

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


# Global bus instance
bus = EventBus()


# ══════════════════════════════════════════════════════════════════════════════
# STREAMING CHAT
# ══════════════════════════════════════════════════════════════════════════════

async def stream_chat_tokens(agent, prompt: str, session_id: str,
                              model: str = None) -> AsyncIterator[str]:
    """
    Stream tokens from the LLM as SSE events.
    Yields raw SSE-formatted strings.
    """
    await bus.publish(BusMessage(
        EventBusEvent.SYSTEM,
        {"event": "stream_start", "session_id": session_id},
        session_id=session_id,
    ))

    messages = []
    history = agent.memory.get_history(session_id, limit=20)
    for m in history:
        if m["role"] in ("user", "assistant"):
            messages.append({"role": m["role"], "content": m["content"]})

    messages.append({"role": "user", "content": prompt})
    agent.memory.add_message(session_id, "user", prompt)

    full_response = []
    token_count = 0

    try:
        async for token in agent.llm.chat_stream(
            messages=messages,
            model=model,
            session_id=session_id,
        ):
            full_response.append(token)
            token_count += 1

            await bus.publish(BusMessage(
                EventBusEvent.TOKEN, token, session_id=session_id
            ))

            yield sse_format(
                {"token": token, "index": token_count},
                event="token",
            )

        assembled = "".join(full_response)
        agent.memory.add_message(session_id, "assistant", assembled)

        yield sse_format(
            {"content": assembled, "tokens": token_count, "done": True},
            event="done",
        )

        await bus.publish(BusMessage(
            EventBusEvent.SYSTEM,
            {"event": "stream_done", "tokens": token_count},
            session_id=session_id,
        ))

    except Exception as e:
        logger.error(f"Streaming error: {e}")
        yield sse_format({"error": str(e)}, event="error")


# ══════════════════════════════════════════════════════════════════════════════
# AIOHTTP SSE ENDPOINT BUILDERS
# ══════════════════════════════════════════════════════════════════════════════

def register_streaming_routes(app, agent):
    """Register all SSE streaming routes on an aiohttp Application."""
    from aiohttp import web
    from agent.auth import (
        auth_context_from_request,
        owned_session_prefix,
        scoped_session_id,
    )

    def _forbidden(detail: str) -> web.Response:
        return web.json_response({"error": "forbidden", "detail": detail}, status=403)

    async def stream_chat(request):
        """
        GET /stream/chat?prompt=...&session_id=...&model=...
        Streams tokens as SSE.
        """
        prompt = request.rel_url.query.get("prompt", "")
        ctx = auth_context_from_request(request)
        try:
            session_id = scoped_session_id(
                ctx,
                requested_session_id=request.rel_url.query.get("session_id", ""),
                default_session_id=f"stream:{int(time.time())}",
            )
        except PermissionError as exc:
            return _forbidden(str(exc))
        model = request.rel_url.query.get("model") or None

        if not prompt:
            return web.Response(status=400, text="prompt required")

        response = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        })
        await response.prepare(request)

        try:
            async for chunk in stream_chat_tokens(agent, prompt, session_id, model):
                await response.write(chunk.encode())
                await response.drain()
        except Exception as e:
            logger.error(f"/stream/chat error: {e}")

        await response.write_eof()
        return response

    async def stream_events(request):
        """
        GET /stream/events?session_id=...&events=token,tool_call,...
        Live SSE event stream from the internal event bus.
        """
        ctx = auth_context_from_request(request)
        requested_session_id = request.rel_url.query.get("session_id", "")
        try:
            session_id = scoped_session_id(
                ctx,
                requested_session_id=requested_session_id,
                default_session_id="events",
            ) if requested_session_id else ""
        except PermissionError as exc:
            return _forbidden(str(exc))
        event_filter_str = request.rel_url.query.get("events", "")
        timeout = float(request.rel_url.query.get("timeout", "120"))
        session_prefix = owned_session_prefix(ctx.user_id) if ctx.authenticated and not session_id else ""

        event_filter = None
        if event_filter_str:
            try:
                event_filter = [EventBusEvent(e.strip())
                               for e in event_filter_str.split(",")]
            except ValueError:
                pass

        response = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        })
        await response.prepare(request)

        sub_id = bus.subscribe(events=event_filter)
        try:
            heartbeat_at = time.time()
            async for msg in bus.listen(sub_id, timeout=timeout):
                if msg.data == "__heartbeat__":
                    if time.time() - heartbeat_at > 15:
                        await response.write(sse_heartbeat().encode())
                        heartbeat_at = time.time()
                    continue

                # Filter by session if specified
                if session_id and msg.session_id and msg.session_id != session_id:
                    continue
                if session_prefix and (not msg.session_id or not msg.session_id.startswith(session_prefix)):
                    continue

                await response.write(msg.to_sse().encode())
                await response.drain()
        except Exception as e:
            logger.error(f"/stream/events error: {e}")
        finally:
            bus.unsubscribe(sub_id)

        await response.write_eof()
        return response

    async def stream_traces(request):
        """
        GET /stream/traces
        Stream completed tracing spans as SSE.
        """
        from agent.tracing import tracer, SpanStatus
        timeout = float(request.rel_url.query.get("timeout", "60"))

        response = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        })
        await response.prepare(request)

        # Send existing recent spans
        recent = tracer.get_spans(last_n=20)
        for span in recent:
            if span.ended_at:
                frame = sse_format(span.to_dict(), event="span")
                await response.write(frame.encode())

        # Live spans via bus
        sub_id = bus.subscribe(events=[EventBusEvent.TRACE_SPAN])
        try:
            async for msg in bus.listen(sub_id, timeout=timeout):
                if msg.data == "__heartbeat__":
                    await response.write(sse_heartbeat().encode())
                    continue
                frame = sse_format(msg.data, event="span")
                await response.write(frame.encode())
                await response.drain()
        finally:
            bus.unsubscribe(sub_id)

        await response.write_eof()
        return response

    async def bus_stats(request):
        """GET /stream/stats — event bus stats"""
        return web.json_response({
            "subscribers": bus.subscriber_count,
            "history_size": len(bus._history),
            "recent_events": [
                {"event": m.event.value, "ts": m.ts}
                for m in bus.recent(limit=10)
            ],
        })

    app.router.add_get("/stream/chat",   stream_chat)
    app.router.add_get("/stream/events", stream_events)
    app.router.add_get("/stream/traces", stream_traces)
    app.router.add_get("/stream/stats",  bus_stats)

    logger.info("Streaming SSE routes registered: /stream/chat, /stream/events, "
               "/stream/traces, /stream/stats")
