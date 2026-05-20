import json
import os
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


os.environ.setdefault("SECRET_KEY", "unit-test-secret-key-0123456789abcdef")
os.environ.setdefault("AUTH_ENFORCE", "true")
os.environ.setdefault("API_HOST", "127.0.0.1")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main
from agent.auth import AuthContext, Role
from agent.tracing import Tracer, TracingConfig


class _DummyMemory:
    def __init__(self) -> None:
        self.audit_events: list[dict[str, Any]] = []

    def get_state(self, key: str) -> str:
        return "running"

    def audit(self, action: str, actor: str = "system", details: dict[str, Any] | None = None) -> None:
        self.audit_events.append({
            "action": action,
            "actor": actor,
            "details": details or {},
        })

    def get_audit_log(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.audit_events:
            return [{"action": "audit", "limit": limit}]
        return self.audit_events[-limit:]


class _DummyLLM:
    def get_router_summary(self) -> dict[str, Any]:
        return {"router": "ok"}

    def get_stats(self) -> dict[str, Any]:
        return {"calls": 0}


class _DummyRegistrar:
    def __init__(self) -> None:
        self.prefix = None

    def register_routes(self, app, prefix: str = "") -> None:
        self.prefix = prefix


class _DummyAuth:
    def __init__(self) -> None:
        self.public_paths: list[str] = []
        self.audit_callback = None
        self.registered_prefix = None
        self.anonymous_hits: list[str] = []
        self.denied_hits: list[str] = []

    def middleware(self, public_paths: list[str] | None = None, audit_callback=None):
        self.public_paths = list(public_paths or [])
        self.audit_callback = audit_callback

        @web.middleware
        async def _middleware(request, handler):
            is_public = request.path in self.public_paths or any(
                request.path.startswith(path) for path in self.public_paths
            )
            if is_public:
                self.anonymous_hits.append(request.path)
                request["auth"] = AuthContext(
                    authenticated=False,
                    role=Role.READONLY,
                    auth_method="anonymous",
                )
                return await handler(request)

            self.denied_hits.append(request.path)
            return web.json_response({"error": "unauthorized"}, status=401)

        return _middleware

    def register_routes(self, app, prefix: str = "") -> None:
        self.registered_prefix = prefix


class _MiniAgent:
    def __init__(self) -> None:
        self.memory = _DummyMemory()
        self.heartbeat = SimpleNamespace(last_status={"ok": True})
        self.scheduler = SimpleNamespace(list_jobs=lambda: [{"name": "nightly"}])
        self.skills = SimpleNamespace(list_skills=lambda: [{"name": "search"}])
        self.llm = _DummyLLM()
        self.auth = _DummyAuth()
        self.config_mgr = _DummyRegistrar()
        self.exporter = _DummyRegistrar()
        self.tracer = Tracer(settings=TracingConfig())


async def _capture_api_app(monkeypatch: pytest.MonkeyPatch, agent: _MiniAgent):
    import aiohttp.web as aiohttp_web
    import agent.streaming as streaming_module

    captured: dict[str, Any] = {}

    class _FakeRunner:
        def __init__(self, app):
            self.app = app
            captured["app"] = app

        async def setup(self) -> None:
            captured["setup"] = True

        async def cleanup(self) -> None:
            captured["cleaned"] = True

    class _FakeSite:
        def __init__(self, runner, host: str, port: int):
            self.runner = runner
            self.host = host
            self.port = port
            captured["bind"] = (host, port)

        async def start(self) -> None:
            captured["started"] = True

    original_runner = aiohttp_web.AppRunner
    original_site = aiohttp_web.TCPSite

    monkeypatch.setattr(streaming_module, "register_streaming_routes", lambda app, current_agent: None)
    monkeypatch.setattr(main.CONFIG, "API_HOST", "127.0.0.1", raising=False)
    monkeypatch.setattr(main.CONFIG, "API_PORT", 8140, raising=False)
    monkeypatch.setattr(main.CONFIG, "API_FALLBACK_PORTS", [8141], raising=False)
    monkeypatch.setattr(main.CONFIG, "SEARXNG_URL", "http://127.0.0.1:18080", raising=False)

    aiohttp_web.AppRunner = _FakeRunner
    aiohttp_web.TCPSite = _FakeSite
    try:
        runner, _port = await main.run_api(agent)
    finally:
        aiohttp_web.AppRunner = original_runner
        aiohttp_web.TCPSite = original_site

    return captured["app"], runner


def _route_paths(app: web.Application) -> set[str]:
    paths: set[str] = set()
    for resource in app.router.resources():
        canonical = getattr(resource, "canonical", None)
        info = resource.get_info() if hasattr(resource, "get_info") else {}
        path = canonical or info.get("path") or info.get("formatter")
        if path:
            paths.add(path)
    return paths


@pytest.mark.asyncio
async def test_health_endpoint_is_registered_public_and_matches_status(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _MiniAgent()
    app, runner = await _capture_api_app(monkeypatch, agent)

    routes = _route_paths(app)
    assert "/health" in routes
    assert "/health" in agent.auth.public_paths

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        health_resp = await client.get("/health")
        health_body = await health_resp.json()

        status_resp = await client.get("/status")
        status_body = await status_resp.json()

        root_resp = await client.get("/")
    finally:
        await client.close()

    assert health_resp.status == 200
    assert status_resp.status == 200
    assert root_resp.status == 401

    assert health_body == status_body
    assert health_body["status"] == "running"
    assert health_body["health"] == {"ok": True}

    serialized = json.dumps(health_body)
    assert "unit-test-secret-key-0123456789abcdef" not in serialized
    assert "bootstrap-secret" not in serialized
    assert "omni_" not in serialized

    assert "/health" in agent.auth.anonymous_hits
    assert "/status" in agent.auth.anonymous_hits
    assert "/" in agent.auth.denied_hits

    await runner.cleanup()
