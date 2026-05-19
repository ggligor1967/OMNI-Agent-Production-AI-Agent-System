import os
from types import SimpleNamespace

import pytest
from aiohttp import web

os.environ.setdefault("SECRET_KEY", "unit-test-secret-key-0123456789abcdef")
os.environ.setdefault("AUTH_ENFORCE", "true")
os.environ.setdefault("API_HOST", "127.0.0.1")

from agent.auth import AuthContext, Role
from agent.tracing import SpanKind, Tracer, TracingConfig

from main import build_http_tracing_middleware


class _FakeResource:
    def __init__(self, canonical: str | None):
        self.canonical = canonical

    def get_info(self) -> dict[str, str]:
        if not self.canonical:
            return {}
        return {"formatter": self.canonical}


class _FakeRequest(dict):
    def __init__(
        self,
        *,
        method: str = "GET",
        path: str = "/status",
        canonical: str | None = "/status",
        query: dict[str, str] | None = None,
        auth: AuthContext | None = None,
        content_length: int = 0,
    ) -> None:
        super().__init__()
        self.method = method
        self.path = path
        self.rel_url = SimpleNamespace(path=path, query=query or {})
        self.match_info = SimpleNamespace(
            route=SimpleNamespace(resource=_FakeResource(canonical))
        )
        self.content_length = content_length
        self.remote = "127.0.0.1"
        if auth is not None:
            self["auth"] = auth


@pytest.mark.asyncio
async def test_http_tracing_middleware_records_safe_authenticated_request_span() -> None:
    local_tracer = Tracer(settings=TracingConfig())
    agent = SimpleNamespace(tracer=local_tracer)
    middleware = build_http_tracing_middleware(agent)
    request = _FakeRequest(
        method="POST",
        path="/personas/session/user:alice:chat-session",
        canonical="/personas/session/{session_id}",
        query={"verbose": "1"},
        auth=AuthContext(
            authenticated=True,
            user_id="alice",
            role=Role.USER,
            auth_method="jwt",
        ),
        content_length=128,
    )

    async def _created_handler(_: _FakeRequest) -> web.Response:
        return web.Response(status=201)

    response = await middleware(request, _created_handler)

    assert response.status == 201
    spans = local_tracer.get_spans()
    request_span = next(span for span in spans if span.name == "http.request")
    attrs = request_span.attributes
    attr_values = {str(value) for value in attrs.values()}

    assert request_span.kind == SpanKind.HTTP
    assert attrs["http_method"] == "POST"
    assert attrs["http_route"] == "/personas/session/{session_id}"
    assert attrs["query_count"] == 1
    assert attrs["status_code"] == 201
    assert attrs["authenticated"] is True
    assert attrs["role"] == "user"
    assert attrs["user_id_hash"].startswith("sha256:")
    assert "alice" not in attr_values
    assert "/personas/session/user:alice:chat-session" not in attr_values


@pytest.mark.asyncio
async def test_http_tracing_middleware_tracks_anonymous_rejections() -> None:
    local_tracer = Tracer(settings=TracingConfig())
    agent = SimpleNamespace(tracer=local_tracer)
    middleware = build_http_tracing_middleware(agent)
    request = _FakeRequest(
        method="POST",
        path="/chat",
        canonical="/chat",
        query={"model": "auto"},
        content_length=64,
    )

    async def _rejected_handler(_: _FakeRequest) -> web.Response:
        return web.json_response({"error": "unauthorized"}, status=401)

    response = await middleware(request, _rejected_handler)

    assert response.status == 401
    request_span = next(span for span in local_tracer.get_spans() if span.name == "http.request")
    attrs = request_span.attributes

    assert attrs["http_route"] == "/chat"
    assert attrs["status_code"] == 401
    assert attrs["authenticated"] is False
    assert attrs["role"] == "readonly"
    assert attrs["query_count"] == 1
    assert "user_id_hash" not in attrs


@pytest.mark.asyncio
async def test_http_tracing_middleware_masks_dynamic_paths_without_route_template() -> None:
    local_tracer = Tracer(settings=TracingConfig())
    agent = SimpleNamespace(tracer=local_tracer)
    middleware = build_http_tracing_middleware(agent)
    request = _FakeRequest(
        method="GET",
        path="/exports/user/alice/history",
        canonical=None,
    )

    async def _no_content_handler(_: _FakeRequest) -> web.Response:
        return web.Response(status=204)

    response = await middleware(request, _no_content_handler)

    assert response.status == 204
    request_span = next(span for span in local_tracer.get_spans() if span.name == "http.request")
    assert request_span.attributes["http_route"] == "/unknown"
