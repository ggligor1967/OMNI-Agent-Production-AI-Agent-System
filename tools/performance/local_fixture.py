from __future__ import annotations

import gc
import os
import sys
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import AsyncIterator

from aiohttp import web

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.auth import AuthManager, Role, auth_context_from_request, effective_user_id, scoped_session_id
from agent.tracing import Tracer, TracingConfig


@dataclass
class LocalFixtureRuntime:
    base_url: str
    api_key: str
    runner: web.AppRunner
    tracer: Tracer
    tempdir: tempfile.TemporaryDirectory[str]


def _ensure_safe_env_defaults() -> None:
    os.environ.setdefault("SECRET_KEY", "test-secret-key-with-minimum-32-characters")
    os.environ.setdefault("AUTH_ENFORCE", "true")
    os.environ.setdefault("API_HOST", "127.0.0.1")


@asynccontextmanager
async def start_local_fixture() -> AsyncIterator[LocalFixtureRuntime]:
    _ensure_safe_env_defaults()

    from main import build_http_tracing_middleware

    tempdir = tempfile.TemporaryDirectory(prefix="omni-perf-fixture-")
    tracer = Tracer(settings=TracingConfig())
    auth = AuthManager(
        secret=os.environ["SECRET_KEY"],
        db_path=os.path.join(tempdir.name, "auth.db"),
        enforce_auth=True,
    )
    api_key, _ = auth.create_api_key(
        user_id="perf-user",
        role=Role.DEVELOPER,
        name="Phase 3.3 local performance harness",
        description="Loopback-only local performance fixture key",
    )

    app = web.Application(
        middlewares=[
            build_http_tracing_middleware(SimpleNamespace(tracer=tracer)),
            auth.middleware(public_paths=["/status"]),
        ]
    )

    async def status_endpoint(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "service": "omni-performance-fixture",
                "mode": "local-only",
            }
        )

    async def chat_endpoint(request: web.Request) -> web.Response:
        payload = await request.json()
        ctx = auth_context_from_request(request)
        user_id = effective_user_id(
            ctx,
            requested_user_id=payload.get("user_id", "perf-user"),
            default_user_id="perf-user",
        )
        try:
            session_id = scoped_session_id(
                ctx,
                requested_session_id=payload.get("session_id", "perf-session"),
                default_session_id="perf-session",
            )
        except PermissionError as exc:
            return web.json_response({"error": "forbidden", "detail": str(exc)}, status=403)

        text = str(payload.get("message", ""))
        deterministic_score = sum(ord(character) for character in text) % 97
        return web.json_response(
            {
                "response": f"perf-fixture:{len(text)}:{deterministic_score}",
                "session_id": session_id,
                "model": "perf-fixture-local",
                "user_id": user_id,
            }
        )

    app.router.add_get("/status", status_endpoint)
    app.router.add_post("/chat", chat_endpoint)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = getattr(site._server, "sockets", [])  # type: ignore[attr-defined]
    if not sockets:
        await runner.cleanup()
        tracer.shutdown()
        tempdir.cleanup()
        raise RuntimeError("Local performance fixture did not expose a listening socket")
    port = int(sockets[0].getsockname()[1])

    runtime = LocalFixtureRuntime(
        base_url=f"http://127.0.0.1:{port}",
        api_key=api_key,
        runner=runner,
        tracer=tracer,
        tempdir=tempdir,
    )
    try:
        yield runtime
    finally:
        await runner.cleanup()
        tracer.shutdown()
        gc.collect()
        try:
            tempdir.cleanup()
        except PermissionError:
            pass
