import os
import sys
import types
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


class _DictRecord:
    def __init__(self, **data: Any) -> None:
        self._data = data

    def to_dict(self) -> dict[str, Any]:
        return dict(self._data)


class _DummyRouter:
    def __init__(self) -> None:
        self._session_models: dict[str, str] = {}

    def set_session_model(self, session_id: str, model_id: str) -> None:
        self._session_models[session_id] = model_id

    def clear_session_model(self, session_id: str) -> None:
        self._session_models.pop(session_id, None)

    def get_session_model(self, session_id: str) -> str | None:
        return self._session_models.get(session_id)

    def route(self, text: str, session_id: str = "", has_image: bool = False) -> Any:
        return SimpleNamespace(
            model_id="qwen3-coder-next:cloud",
            model_spec=SimpleNamespace(display_name="Qwen 3 Coder Next"),
            task_type=SimpleNamespace(value="vision" if has_image else "code"),
            confidence=0.92,
            reason=f"routed:{session_id}:{text[:8]}",
            fallback_chain=["phi4-reasoning-plus:cloud"],
        )


class _DummyLLM:
    def __init__(self) -> None:
        self.router = _DummyRouter()

    def get_router_summary(self) -> dict[str, Any]:
        return {"router": "ok"}

    def get_stats(self) -> dict[str, Any]:
        return {"calls": 0}

    async def _list_ollama_models(self, cache_seconds: int = 0) -> set[str]:
        return {"qwen3-coder-next:cloud", "extra-local-model"}

    async def chat_parallel(self, messages: list[dict[str, str]], model_ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
        return {
            model_id: {"content": f"reply from {model_id}", "eval_count": 7}
            for model_id in (model_ids or [])
        }


class _DummyMemory:
    def get_state(self, key: str) -> str:
        return "running"

    def search_memories(self, query: str) -> list[dict[str, str]]:
        return [{"query": query}]

    def get_memories_by_category(self, category: str) -> list[dict[str, str]]:
        return [{"category": category}]

    def get_audit_log(self, limit: int = 50) -> list[dict[str, Any]]:
        return [{"action": "audit", "limit": limit}]


class _DummyAuth:
    def __init__(self) -> None:
        self.public_paths: list[str] = []
        self.audit_callback = None
        self.registered_prefix = None

    def middleware(self, public_paths: list[str] | None = None, audit_callback=None):
        self.public_paths = list(public_paths or [])
        self.audit_callback = audit_callback

        @web.middleware
        async def _middleware(request, handler):
            is_public = request.path in self.public_paths or any(
                request.path.startswith(path) for path in self.public_paths
            )
            if is_public:
                request["auth"] = AuthContext(
                    authenticated=False,
                    role=Role.READONLY,
                    auth_method="anonymous",
                )
            else:
                request["auth"] = AuthContext(
                    authenticated=True,
                    user_id="alice",
                    role=Role.ADMIN,
                    auth_method="api_key",
                )
            return await handler(request)

        return _middleware

    def register_routes(self, app, prefix: str = "") -> None:
        self.registered_prefix = prefix


class _DummyTemplates:
    def list_templates(self, tag: str | None = None) -> list[dict[str, Any]]:
        return [{"name": "default", "tag": tag or "general"}]

    def render(self, name: str, variables: dict[str, Any], history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if name == "missing":
            raise KeyError("missing")
        if variables.get("invalid"):
            raise ValueError("invalid variables")
        return [{"role": "user", "content": variables.get("prompt", "hello")}] 


class _DummyPipelineExecutor:
    def list_pipelines(self) -> list[dict[str, str]]:
        return [{"name": "job-search"}]

    async def run_by_name(self, name: str, context: dict[str, Any]):
        if name == "ghost":
            return None
        return _DictRecord(name=name, context=context, status="completed")

    def list_runs(self, pipeline_name: str | None = None) -> list[dict[str, Any]]:
        return [{"pipeline": pipeline_name or "job-search"}]


class _DummyCache:
    def __init__(self) -> None:
        self.flushed = False

    async def stats(self) -> dict[str, Any]:
        return {"backend": "memory", "flushed": self.flushed}

    async def flush(self) -> None:
        self.flushed = True


class _DummyWorkflows:
    def list_workflows(self) -> list[dict[str, str]]:
        return [{"name": "triage"}]

    async def run(self, name: str, context: dict[str, Any]):
        if name == "missing":
            raise KeyError(name)
        return _DictRecord(name=name, context=context, status="completed")


class _DummyToolRegistry:
    def openai_schemas(self, category: str | None = None) -> list[dict[str, Any]]:
        return [{"name": "echo", "category": category or "general"}]

    def anthropic_schemas(self, category: str | None = None) -> list[dict[str, Any]]:
        return [{"name": "echo", "category": category or "general"}]

    def list_tools(self, category: str | None = None) -> list[dict[str, Any]]:
        return [{"name": "echo", "category": category or "general"}]

    def get(self, name: str) -> Any:
        return SimpleNamespace(requires_confirmation=False, name=name)

    async def call(self, call: Any, allow_confirmed_tools: bool = False):
        return _DictRecord(success=True, output={"tool": call.tool_name, "arguments": call.arguments})


class _DummyPersonaRegistry:
    def list_personas(self, tag: str = "") -> list[dict[str, str]]:
        return [{"name": "assistant", "tag": tag or "general"}]


class _DummyPersonaManager:
    def set_session_persona(self, session_id: str, name: str) -> bool:
        return name != "missing"

    def session_info(self, session_id: str) -> dict[str, str]:
        return {"session_id": session_id, "persona": "assistant"}


class _DummyEvaluator:
    def list_suites(self) -> list[dict[str, str]]:
        return [{"name": "smoke"}]

    def get_history(self, suite: str, model: str) -> dict[str, Any]:
        return {"suite": suite, "model": model, "runs": 1}

    def model_comparison(self, suite: str) -> dict[str, Any]:
        return {"suite": suite, "winner": "qwen3-coder-next:cloud"}


class _DummyGraph:
    def stats(self) -> dict[str, Any]:
        return {"nodes": 1, "edges": 0}

    def add_node(self, node_id: str, label: str, node_type: str) -> _DictRecord:
        return _DictRecord(node_id=node_id, label=label, node_type=node_type)

    def neighbours(self, name: str, direction: str = "both") -> list[_DictRecord]:
        return [_DictRecord(node_id=name, direction=direction)]

    def shortest_path(self, src: str, tgt: str) -> list[str]:
        return [src, "mid", tgt]


class _DummySandbox:
    async def run(self, code: str, language: Any):
        return _DictRecord(success=True, stdout=f"sandbox:{code}", language=str(language))

    def get_history(self, limit: int = 20) -> list[dict[str, Any]]:
        return [{"limit": limit, "status": "ok"}]

    def stats(self) -> dict[str, Any]:
        return {"runs": 1}


class _DummyVision:
    async def analyze(self, source: str, task: str = "describe", model: str | None = None):
        return _DictRecord(source=source, task=task, model=model or "auto")

    def get_vision_models(self) -> list[str]:
        return ["vision-test"]


class _DummyNotifier:
    def list_channels(self) -> list[str]:
        return ["console"]

    async def send(self, channel: str, title: str, body: str, priority: Any):
        return _DictRecord(channel=channel, title=title, body=body, priority=getattr(priority, "value", str(priority)))

    def delivery_stats(self) -> dict[str, int]:
        return {"sent": 1}

    def recent_deliveries(self, limit: int = 20) -> list[dict[str, Any]]:
        return [{"limit": limit, "channel": "console"}]


class _DummyRag:
    async def ingest_text(self, text: str, title: str = "api-doc", metadata: dict[str, Any] | None = None):
        return _DictRecord(doc_id="doc-1", text=text, title=title, metadata=metadata or {})

    async def retrieve(self, query: str, top_k: int = 5, doc_id: str | None = None) -> list[_DictRecord]:
        return [_DictRecord(query=query, top_k=top_k, doc_id=doc_id or "doc-1")]

    def generate_context(self, results: list[_DictRecord]) -> str:
        return "context" if results else ""

    def list_documents(self) -> list[dict[str, str]]:
        return [{"doc_id": "doc-1"}]

    def stats(self) -> dict[str, int]:
        return {"documents": 1}

    def delete_document(self, doc_id: str) -> bool:
        return doc_id == "doc-1"


class _DummyStructuredParser:
    async def parse(self, text: str, schema: Any):
        return _DictRecord(parsed=True, text=text, fields=len(getattr(schema, "fields", [])))


class _DummyConfigManager:
    def __init__(self) -> None:
        self.prefix = None

    def register_routes(self, app, prefix: str = "") -> None:
        self.prefix = prefix


class _DummyExporter:
    def __init__(self) -> None:
        self.prefix = None

    def register_routes(self, app, prefix: str = "") -> None:
        self.prefix = prefix


class _DummyAgent:
    def __init__(self) -> None:
        self.memory = _DummyMemory()
        self.heartbeat = SimpleNamespace(last_status={"ok": True})
        self.scheduler = SimpleNamespace(list_jobs=lambda: [{"name": "nightly"}])
        self.skills = SimpleNamespace(list_skills=lambda: [{"name": "search"}])
        self.llm = _DummyLLM()
        self.templates = _DummyTemplates()
        self.pipeline_executor = _DummyPipelineExecutor()
        self.cache = _DummyCache()
        self.workflows = _DummyWorkflows()
        self.tool_registry = _DummyToolRegistry()
        self.persona_registry = _DummyPersonaRegistry()
        self.persona_manager = _DummyPersonaManager()
        self.evaluator = _DummyEvaluator()
        self.knowledge_graph = _DummyGraph()
        self.sandbox = _DummySandbox()
        self.vision = _DummyVision()
        self.notifier = _DummyNotifier()
        self.rag = _DummyRag()
        self.structured_parser = _DummyStructuredParser()
        self.config_mgr = _DummyConfigManager()
        self.exporter = _DummyExporter()
        self.auth = _DummyAuth()
        self.tracer = Tracer(settings=TracingConfig())
        self.chat_calls: list[tuple[str, str, str]] = []

    async def chat(self, user_id: str, session_id: str, text: str) -> str:
        self.chat_calls.append((user_id, session_id, text))
        return f"reply:{user_id}:{session_id}:{text}"


async def _capture_api_app(monkeypatch: pytest.MonkeyPatch, agent: _DummyAgent, *, api_port: int = 8123, fallback_ports: list[int] | None = None):
    import aiohttp.web as aiohttp_web
    import agent.streaming as streaming_module

    captured: dict[str, Any] = {}
    fallback_ports = fallback_ports or [8124]

    class _FakeRunner:
        def __init__(self, app):
            self.app = app
            captured["app"] = app
            self.cleaned = False

        async def setup(self) -> None:
            captured["setup"] = True

        async def cleanup(self) -> None:
            self.cleaned = True
            captured["cleaned"] = True

    class _FakeSite:
        def __init__(self, runner, host: str, port: int):
            self.runner = runner
            self.host = host
            self.port = port
            captured.setdefault("ports", []).append(port)

        async def start(self) -> None:
            captured.setdefault("started_ports", []).append(self.port)

    original_runner = aiohttp_web.AppRunner
    original_site = aiohttp_web.TCPSite

    monkeypatch.setattr(streaming_module, "register_streaming_routes", lambda app, current_agent: None)
    monkeypatch.setattr(main.CONFIG, "API_HOST", "127.0.0.1", raising=False)
    monkeypatch.setattr(main.CONFIG, "API_PORT", api_port, raising=False)
    monkeypatch.setattr(main.CONFIG, "API_FALLBACK_PORTS", fallback_ports, raising=False)
    monkeypatch.setattr(main.CONFIG, "SEARXNG_URL", "http://127.0.0.1:18080", raising=False)

    aiohttp_web.AppRunner = _FakeRunner
    aiohttp_web.TCPSite = _FakeSite
    try:
        runner, port = await main.run_api(agent)
    finally:
        aiohttp_web.AppRunner = original_runner
        aiohttp_web.TCPSite = original_site

    return captured["app"], runner, port, captured


def test_main_helper_functions_cover_security_defaults_and_port_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    assert main._display_host("0.0.0.0") == "localhost"
    assert main._display_host("::") == "localhost"
    assert main._display_host("127.0.0.1") == "127.0.0.1"

    monkeypatch.setattr(main.CONFIG, "SEARXNG_URL", "http://127.0.0.1:9001", raising=False)
    monkeypatch.setattr(main.CONFIG, "API_PORT", 9001, raising=False)
    monkeypatch.setattr(main.CONFIG, "API_FALLBACK_PORTS", [9001, 9002, 9002], raising=False)
    assert main._api_bind_ports() == [9002]

    monkeypatch.setattr(main.CONFIG, "API_FALLBACK_PORTS", [9001], raising=False)
    with pytest.raises(RuntimeError, match="No usable API ports configured"):
        main._api_bind_ports()

    assert main._prompt_with_default("Admin user id", "admin", input_fn=lambda _prompt: "") == "admin"
    assert main._prompt_with_default("Admin user id", "admin", input_fn=lambda _prompt: "alice") == "alice"

    values = iter(["bootstrap-secret-value-0123456789", "different-bootstrap-secret-0123456789"])
    with pytest.raises(ValueError, match="Initial admin API key entries did not match"):
        main._prompt_confirmed_secret("Initial admin API key", secret_reader=lambda _prompt: next(values))

    auth_manager = SimpleNamespace(bootstrap_token="bootstrap-secret-value-0123456789")
    assert main._resolve_cli_bootstrap_token(auth_manager, "admin-api-key-value-0123456789") == "bootstrap-secret-value-0123456789"

    monkeypatch.setattr(main.CONFIG, "AUTH_BOOTSTRAP_TOKEN", "config-bootstrap-secret-0123456789", raising=False)
    assert main._resolve_cli_bootstrap_token(SimpleNamespace(bootstrap_token=""), "admin-api-key-value-0123456789") == "config-bootstrap-secret-0123456789"

    monkeypatch.setattr(main.CONFIG, "AUTH_BOOTSTRAP_TOKEN", "", raising=False)
    assert main._resolve_cli_bootstrap_token(None, "admin-api-key-value-0123456789") == "admin-api-key-value-0123456789"


@pytest.mark.asyncio
async def test_run_api_builds_core_routes_and_runtime_entrypoints(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _DummyAgent()
    app, runner, port, captured = await _capture_api_app(monkeypatch, agent)

    assert port == 8123
    assert captured["started_ports"] == [8123]
    assert "/dashboard" in agent.auth.public_paths
    assert "/cache/stats" in agent.auth.public_paths
    assert agent.auth.registered_prefix == ""
    assert agent.config_mgr.prefix == ""
    assert agent.exporter.prefix == ""
    assert agent.auth.audit_callback is not None

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        status_resp = await client.get("/status")
        status_body = await status_resp.json()

        dashboard_resp = await client.get("/dashboard")
        favicon_resp = await client.get("/favicon.ico")

        empty_chat_resp = await client.post("/chat", json={})
        empty_chat_body = await empty_chat_resp.json()

        forbidden_chat_resp = await client.post(
            "/chat",
            json={"message": "hello", "session_id": "user:bob:chat"},
        )
        forbidden_chat_body = await forbidden_chat_resp.json()

        chat_resp = await client.post(
            "/chat",
            json={"message": "hello", "session_id": "chat", "model": "qwen3-coder-next:cloud"},
        )
        chat_body = await chat_resp.json()

        route_resp = await client.post(
            "/route",
            json={"text": "write code", "session_id": "route", "has_image": True},
        )
        route_body = await route_resp.json()

        cache_stats_resp = await client.get("/cache/stats")
        cache_flush_resp = await client.post("/cache/flush")
        cache_flush_body = await cache_flush_resp.json()

        audit_resp = await client.get("/audit?limit=2")
        audit_body = await audit_resp.json()

        structured_bad_resp = await client.post("/structured", json={"text": "hi", "schema": "unknown"})
        structured_good_resp = await client.post("/structured", json={"text": "hi", "schema": "sentiment"})
        structured_good_body = await structured_good_resp.json()

        personas_resp = await client.get("/personas?tag=ops")
        persona_set_resp = await client.post("/personas/session/chat", json={"persona": "assistant"})
        persona_info_resp = await client.get("/personas/session/chat")
    finally:
        await client.close()

    assert status_resp.status == 200
    assert status_body["status"] == "running"
    assert status_body["health"] == {"ok": True}

    assert dashboard_resp.status == 200
    assert favicon_resp.status == 204

    assert empty_chat_resp.status == 400
    assert empty_chat_body == {"error": "message required"}

    assert forbidden_chat_resp.status == 403
    assert forbidden_chat_body["error"] == "forbidden"

    assert chat_resp.status == 200
    assert chat_body["response"].startswith("reply:alice:user:alice:chat:hello")
    assert chat_body["session_id"] == "user:alice:chat"
    assert chat_body["model"] == "qwen3-coder-next:cloud"
    assert agent.chat_calls[-1] == ("alice", "user:alice:chat", "hello")

    assert route_resp.status == 200
    assert route_body["task_type"] == "vision"
    assert route_body["model_id"] == "qwen3-coder-next:cloud"

    assert cache_stats_resp.status == 200
    assert cache_flush_resp.status == 200
    assert cache_flush_body == {"flushed": True}
    assert agent.cache.flushed is True

    assert audit_resp.status == 200
    assert audit_body["log"][0]["limit"] == 2

    assert structured_bad_resp.status == 400
    assert structured_good_resp.status == 200
    assert structured_good_body["parsed"] is True

    assert personas_resp.status == 200
    assert persona_set_resp.status == 200
    assert persona_info_resp.status == 200

    await runner.cleanup()


@pytest.mark.asyncio
async def test_run_api_exposes_models_rag_tools_and_admin_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _DummyAgent()
    app, runner, _port, _captured = await _capture_api_app(monkeypatch, agent, api_port=8125, fallback_ports=[8126])

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        models_resp = await client.get("/models")
        models_body = await models_resp.json()
        model_detail_resp = await client.get("/models/qwen3-coder-next:cloud")

        memories_resp = await client.get("/memories?q=threat-model")
        compare_resp = await client.post("/compare", json={"prompt": "compare", "models": ["model-a", "model-b"]})

        rag_ingest_resp = await client.post("/rag/ingest", json={"text": "hello", "title": "doc"})
        rag_query_resp = await client.post("/rag/query", json={"query": "hello", "top_k": 1})
        rag_docs_resp = await client.get("/rag/docs")
        rag_delete_resp = await client.delete("/rag/docs/doc-1")

        pipelines_resp = await client.get("/pipelines")
        pipeline_run_resp = await client.post("/pipelines/job-search/run", json={"context": {"query": "python"}})
        pipeline_missing_resp = await client.post("/pipelines/ghost/run", json={"context": {}})
        pipeline_runs_resp = await client.get("/pipelines/runs?pipeline=job-search")

        templates_resp = await client.get("/templates?tag=ops")
        template_render_resp = await client.post("/templates/default/render", json={"variables": {"prompt": "hi"}, "history": []})
        template_missing_resp = await client.post("/templates/missing/render", json={"variables": {}, "history": []})

        workflows_resp = await client.get("/workflows")
        workflow_run_resp = await client.post("/workflows/triage/run", json={"context": {"severity": "high"}})
        workflow_missing_resp = await client.post("/workflows/missing/run", json={"context": {}})

        tools_openai_resp = await client.get("/tools?format=openai")
        tool_call_resp = await client.post("/tools/call", json={"tool": "echo", "arguments": {"value": 1}})

        tracing_summary_resp = await client.get("/tracing/summary")
        tracing_spans_resp = await client.get("/tracing/spans?last_n=5")
        tracing_errors_resp = await client.get("/tracing/errors")

        eval_suites_resp = await client.get("/eval/suites")
        eval_history_resp = await client.get("/eval/history/smoke?model=qwen3-coder-next:cloud")
        eval_compare_resp = await client.get("/eval/compare/smoke")

        kg_stats_resp = await client.get("/kg/stats")
        kg_extract_resp = await client.post("/kg/extract", json={"text": "Payment Service"})
        kg_search_resp = await client.get("/kg/search?name=payment_service")
        kg_path_resp = await client.get("/kg/path?from=a&to=b")
        kg_export_resp = await client.get("/kg/export")

        sandbox_run_resp = await client.post("/sandbox/run", json={"code": "print(1)", "language": "python"})
        sandbox_history_resp = await client.get("/sandbox/history")

        vision_analyze_resp = await client.post("/vision/analyze", json={"source": "image.png", "task": "describe"})
        vision_models_resp = await client.get("/vision/models")

        notif_channels_resp = await client.get("/notifications/channels")
        notif_send_resp = await client.post(
            "/notifications/send",
            json={"channel": "console", "title": "Alert", "body": "Body", "priority": "bogus"},
        )
        notif_stats_resp = await client.get("/notifications/stats")
    finally:
        await client.close()

    assert models_resp.status == 200
    assert models_body["available_count"] >= 1
    assert any(model["id"] == "extra-local-model" for model in models_body["models"])
    assert model_detail_resp.status == 200

    assert memories_resp.status == 200
    assert compare_resp.status == 200

    assert rag_ingest_resp.status == 200
    assert rag_query_resp.status == 200
    assert rag_docs_resp.status == 200
    assert rag_delete_resp.status == 200

    assert pipelines_resp.status == 200
    assert pipeline_run_resp.status == 200
    assert pipeline_missing_resp.status == 404
    assert pipeline_runs_resp.status == 200

    assert templates_resp.status == 200
    assert template_render_resp.status == 200
    assert template_missing_resp.status == 404

    assert workflows_resp.status == 200
    assert workflow_run_resp.status == 200
    assert workflow_missing_resp.status == 404

    assert tools_openai_resp.status == 200
    assert tool_call_resp.status == 200

    assert tracing_summary_resp.status == 200
    assert tracing_spans_resp.status == 200
    assert tracing_errors_resp.status == 200

    assert eval_suites_resp.status == 200
    assert eval_history_resp.status == 200
    assert eval_compare_resp.status == 200

    assert kg_stats_resp.status == 200
    assert kg_extract_resp.status == 200
    assert kg_search_resp.status == 200
    assert kg_path_resp.status == 200
    assert kg_export_resp.status == 200

    assert sandbox_run_resp.status == 200
    assert sandbox_history_resp.status == 200

    assert vision_analyze_resp.status == 200
    assert vision_models_resp.status == 200

    assert notif_channels_resp.status == 200
    assert notif_send_resp.status == 200
    assert notif_stats_resp.status == 200

    await runner.cleanup()


@pytest.mark.asyncio
async def test_run_api_uses_fallback_port_when_primary_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    import aiohttp.web as aiohttp_web
    import agent.streaming as streaming_module

    agent = _DummyAgent()
    attempts: list[int] = []

    class _FakeRunner:
        def __init__(self, app):
            self.app = app
            self.cleaned = False

        async def setup(self) -> None:
            return None

        async def cleanup(self) -> None:
            self.cleaned = True

    class _FlakySite:
        def __init__(self, runner, host: str, port: int):
            self.runner = runner
            self.port = port

        async def start(self) -> None:
            attempts.append(self.port)
            if self.port == 8130:
                exc = OSError("port in use")
                exc.winerror = 10048
                exc.errno = 10048
                raise exc

    original_runner = aiohttp_web.AppRunner
    original_site = aiohttp_web.TCPSite

    monkeypatch.setattr(streaming_module, "register_streaming_routes", lambda app, current_agent: None)
    monkeypatch.setattr(main.CONFIG, "API_HOST", "127.0.0.1", raising=False)
    monkeypatch.setattr(main.CONFIG, "API_PORT", 8130, raising=False)
    monkeypatch.setattr(main.CONFIG, "API_FALLBACK_PORTS", [8131], raising=False)
    monkeypatch.setattr(main.CONFIG, "SEARXNG_URL", "http://127.0.0.1:18080", raising=False)

    aiohttp_web.AppRunner = _FakeRunner
    aiohttp_web.TCPSite = _FlakySite
    try:
        runner, port = await main.run_api(agent)
    finally:
        aiohttp_web.AppRunner = original_runner
        aiohttp_web.TCPSite = original_site

    assert attempts == [8130, 8131]
    assert port == 8131
    await runner.cleanup()


@pytest.mark.asyncio
async def test_main_create_admin_failure_exits_cleanly_without_secret_leakage(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    parser = SimpleNamespace(parse_args=lambda: SimpleNamespace(create_admin=True, mode="cli"))
    monkeypatch.setattr(main, "build_arg_parser", lambda: parser)

    def _raise_failure() -> None:
        raise ValueError("invalid bootstrap input")

    monkeypatch.setattr(main, "run_create_admin_bootstrap", _raise_failure)

    with pytest.raises(SystemExit) as excinfo:
        await main.main()

    stderr = capsys.readouterr().err
    assert excinfo.value.code == 1
    assert "Admin bootstrap failed: invalid bootstrap input" in stderr
    assert "unit-test-secret-key-0123456789abcdef" not in stderr


@pytest.mark.asyncio
async def test_main_cli_mode_runs_cli_and_stops_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    parser = SimpleNamespace(parse_args=lambda: SimpleNamespace(create_admin=False, mode="cli"))

    class _CliAgent:
        async def start(self) -> None:
            events.append("start")

        async def stop(self) -> None:
            events.append("stop")

    fake_agent_core = types.SimpleNamespace(OmniAgent=_CliAgent)

    async def _fake_run_cli(agent: Any) -> None:
        events.append("run_cli")

    monkeypatch.setattr(main, "build_arg_parser", lambda: parser)
    monkeypatch.setattr(main, "setup_logging", lambda: None)
    monkeypatch.setattr(main, "run_cli", _fake_run_cli)
    monkeypatch.setitem(sys.modules, "agent.core", fake_agent_core)

    await main.main()

    assert events == ["start", "run_cli", "stop"]
