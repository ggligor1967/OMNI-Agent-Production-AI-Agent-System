import os
import sys
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class _MemoryStub:
    def __init__(self, history=None):
        self.history = list(history or [])
        self.added = []

    def get_history(self, session_id, limit=20):
        return self.history[-limit:]

    def add_message(self, session_id, role, content):
        self.added.append({"session_id": session_id, "role": role, "content": content})


class _LLMStub:
    def __init__(self, *, tokens=None, error=None):
        self.tokens = list(tokens or [])
        self.error = error
        self.calls = []

    async def chat_stream(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        for token in self.tokens:
            yield token


class _ScriptedBus:
    def __init__(self, *, listen_messages=None, history=None):
        self.listen_messages = list(listen_messages or [])
        self._history = list(history or [])
        self.subscriptions = []
        self.unsubscribed = []

    def subscribe(self, events=None, sub_id=None):
        sid = sub_id or f"sub-{len(self.subscriptions) + 1}"
        self.subscriptions.append({"sid": sid, "events": events})
        return sid

    def unsubscribe(self, sub_id):
        self.unsubscribed.append(sub_id)

    async def publish(self, msg):
        self._history.append(msg)

    async def listen(self, sub_id, timeout=60.0):
        for msg in self.listen_messages:
            yield msg

    def recent(self, event=None, limit=50):
        messages = self._history
        if event is not None:
            messages = [msg for msg in messages if msg.event == event]
        return messages[-limit:]

    @property
    def subscriber_count(self):
        return len(self.subscriptions) - len(self.unsubscribed)


@dataclass
class _FakeSpan:
    payload: dict
    ended_at: float | None = 1.0

    def to_dict(self):
        return dict(self.payload)


class _FakeTracer:
    def __init__(self, spans):
        self._spans = spans

    def get_spans(self, last_n=20):
        return self._spans[-last_n:]


@pytest.mark.asyncio
async def test_stream_chat_tokens_replays_history_publishes_tokens_and_persists_response(monkeypatch):
    import agent.streaming as streaming_module
    from agent.streaming import EventBus, EventBusEvent, stream_chat_tokens

    fresh_bus = EventBus(max_queue=32, max_history=100)
    monkeypatch.setattr(streaming_module, "bus", fresh_bus)

    memory = _MemoryStub(
        history=[
            {"role": "system", "content": "skip system"},
            {"role": "user", "content": "old user"},
            {"role": "assistant", "content": "old assistant"},
            {"role": "tool", "content": "skip tool"},
        ]
    )
    llm = _LLMStub(tokens=["Hel", "lo"])
    agent = SimpleNamespace(memory=memory, llm=llm)

    frames = [
        frame
        async for frame in stream_chat_tokens(agent, "new prompt", "sess-1", model="phi4")
    ]

    assert any("event: token" in frame and '"Hel"' in frame for frame in frames)
    assert any("event: token" in frame and '"lo"' in frame for frame in frames)
    assert any("event: done" in frame and '"Hello"' in frame for frame in frames)

    assert memory.added == [
        {"session_id": "sess-1", "role": "user", "content": "new prompt"},
        {"session_id": "sess-1", "role": "assistant", "content": "Hello"},
    ]

    llm_call = llm.calls[0]
    assert llm_call["model"] == "phi4"
    assert llm_call["session_id"] == "sess-1"
    assert llm_call["messages"] == [
        {"role": "user", "content": "old user"},
        {"role": "assistant", "content": "old assistant"},
        {"role": "user", "content": "new prompt"},
    ]

    recent = fresh_bus.recent(limit=10)
    assert recent[0].event == EventBusEvent.SYSTEM
    assert any(msg.event == EventBusEvent.TOKEN and msg.data == "Hel" for msg in recent)
    assert recent[-1].event == EventBusEvent.SYSTEM
    assert recent[-1].data["event"] == "stream_done"


@pytest.mark.asyncio
async def test_stream_chat_tokens_yields_error_frame_when_llm_fails(monkeypatch):
    import agent.streaming as streaming_module
    from agent.streaming import EventBus, stream_chat_tokens

    monkeypatch.setattr(streaming_module, "bus", EventBus(max_queue=8, max_history=16))

    memory = _MemoryStub(history=[])
    llm = _LLMStub(error=RuntimeError("stream blew up"))
    agent = SimpleNamespace(memory=memory, llm=llm)

    frames = [frame async for frame in stream_chat_tokens(agent, "new prompt", "sess-2")]

    assert len(frames) == 1
    assert "event: error" in frames[0]
    assert "stream blew up" in frames[0]
    assert memory.added == [{"session_id": "sess-2", "role": "user", "content": "new prompt"}]


@pytest.mark.asyncio
async def test_register_streaming_routes_chat_route_streams_and_validates_prompt(monkeypatch):
    import agent.streaming as streaming_module
    from agent.streaming import register_streaming_routes, sse_format

    async def fake_stream_chat_tokens(agent, prompt, session_id, model):
        assert prompt == "hello"
        assert session_id == "chat-1"
        assert model == "phi4"
        yield sse_format({"token": "Hi"}, event="token")
        yield sse_format({"content": "Hi", "done": True}, event="done")

    monkeypatch.setattr(streaming_module, "stream_chat_tokens", fake_stream_chat_tokens)

    app = web.Application()
    register_streaming_routes(app, agent=SimpleNamespace())

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        ok_resp = await client.get("/stream/chat?prompt=hello&session_id=chat-1&model=phi4")
        ok_body = await ok_resp.text()

        bad_resp = await client.get("/stream/chat")
        bad_body = await bad_resp.text()
    finally:
        await client.close()

    assert ok_resp.status == 200
    assert ok_resp.headers["Content-Type"] == "text/event-stream"
    assert "event: token" in ok_body
    assert "event: done" in ok_body

    assert bad_resp.status == 400
    assert bad_body == "prompt required"


@pytest.mark.asyncio
async def test_register_streaming_routes_chat_rejects_forbidden_session(monkeypatch):
    import agent.auth as auth_module
    from agent.auth import AuthContext, Role
    from agent.streaming import register_streaming_routes

    monkeypatch.setattr(
        auth_module,
        "auth_context_from_request",
        lambda request: AuthContext(authenticated=True, user_id="alice", role=Role.USER, auth_method="jwt"),
    )

    def reject_session(*args, **kwargs):
        raise PermissionError("foreign session")

    monkeypatch.setattr(auth_module, "scoped_session_id", reject_session)

    app = web.Application()
    register_streaming_routes(app, agent=SimpleNamespace())

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        resp = await client.get("/stream/chat?prompt=hello&session_id=user:bob:chat")
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 403
    assert body == {"error": "forbidden", "detail": "foreign session"}


@pytest.mark.asyncio
async def test_register_streaming_routes_events_route_filters_by_session_and_stats(monkeypatch):
    import agent.streaming as streaming_module
    from agent.streaming import BusMessage, EventBusEvent, register_streaming_routes

    scripted_bus = _ScriptedBus(
        listen_messages=[
            BusMessage(EventBusEvent.TOKEN, "visible", session_id="sess-1"),
            BusMessage(EventBusEvent.TOKEN, "hidden", session_id="sess-2"),
        ],
        history=[BusMessage(EventBusEvent.ROUTE, {"model": "phi4"}, session_id="sess-1")],
    )
    monkeypatch.setattr(streaming_module, "bus", scripted_bus)

    app = web.Application()
    register_streaming_routes(app, agent=SimpleNamespace())

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        events_resp = await client.get("/stream/events?session_id=sess-1&events=token&timeout=0.01")
        events_body = await events_resp.text()

        stats_resp = await client.get("/stream/stats")
        stats_body = await stats_resp.json()
    finally:
        await client.close()

    assert events_resp.status == 200
    assert "visible" in events_body
    assert "hidden" not in events_body
    assert scripted_bus.subscriptions[0]["events"] == [EventBusEvent.TOKEN]
    assert scripted_bus.unsubscribed == [scripted_bus.subscriptions[0]["sid"]]

    assert stats_resp.status == 200
    assert stats_body["subscribers"] == 0
    assert stats_body["history_size"] == 1
    assert stats_body["recent_events"] == [{"event": "route", "ts": scripted_bus._history[0].ts}]


@pytest.mark.asyncio
async def test_register_streaming_routes_events_route_uses_authenticated_owner_prefix(monkeypatch):
    import agent.auth as auth_module
    import agent.streaming as streaming_module
    from agent.auth import AuthContext, Role
    from agent.streaming import BusMessage, EventBusEvent, register_streaming_routes

    monkeypatch.setattr(
        auth_module,
        "auth_context_from_request",
        lambda request: AuthContext(authenticated=True, user_id="alice", role=Role.USER, auth_method="jwt"),
    )

    scripted_bus = _ScriptedBus(
        listen_messages=[
            BusMessage(EventBusEvent.TOKEN, "alice-token", session_id="user:alice:chat"),
            BusMessage(EventBusEvent.TOKEN, "bob-token", session_id="user:bob:chat"),
            BusMessage(EventBusEvent.TOKEN, "anonymous-token", session_id=""),
        ]
    )
    monkeypatch.setattr(streaming_module, "bus", scripted_bus)

    app = web.Application()
    register_streaming_routes(app, agent=SimpleNamespace())

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        resp = await client.get("/stream/events?events=token&timeout=0.01")
        body = await resp.text()
    finally:
        await client.close()

    assert resp.status == 200
    assert "alice-token" in body
    assert "bob-token" not in body
    assert "anonymous-token" not in body


@pytest.mark.asyncio
async def test_register_streaming_routes_traces_route_replays_recent_and_live_spans(monkeypatch):
    import agent.streaming as streaming_module
    import agent.tracing as tracing_module
    from agent.streaming import BusMessage, EventBusEvent, register_streaming_routes

    scripted_bus = _ScriptedBus(
        listen_messages=[
            BusMessage(EventBusEvent.SYSTEM, "__heartbeat__"),
            BusMessage(EventBusEvent.TRACE_SPAN, {"span": "live"}),
        ]
    )
    monkeypatch.setattr(streaming_module, "bus", scripted_bus)
    monkeypatch.setattr(
        tracing_module,
        "tracer",
        _FakeTracer([
            _FakeSpan({"span": "recent-ended"}, ended_at=1.0),
            _FakeSpan({"span": "ignore-open"}, ended_at=None),
        ]),
    )

    app = web.Application()
    register_streaming_routes(app, agent=SimpleNamespace())

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        resp = await client.get("/stream/traces?timeout=0.01")
        body = await resp.text()
    finally:
        await client.close()

    assert resp.status == 200
    assert '"span": "recent-ended"' in body
    assert ': heartbeat' in body
    assert '"span": "live"' in body
