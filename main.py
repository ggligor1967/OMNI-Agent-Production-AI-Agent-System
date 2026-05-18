"""
OMNI AGENT - Main Entrypoint
Run modes: telegram | cli | api | all
"""
import asyncio
import logging
import signal
import sys
import argparse
import getpass
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Any, Optional, TYPE_CHECKING
from urllib.parse import urlparse
from config import CONFIG

if TYPE_CHECKING:
    from agent.core import OmniAgent
    from aiohttp import web


_DEFAULT_SECRET = "CHANGE_ME_IN_PRODUCTION"


@dataclass
class AdminBootstrapResult:
    user_id: str
    key_id: str
    role: str
    api_key: str
    jwt: str


def _validate_security_config() -> None:
    secret = getattr(CONFIG, "SECRET_KEY", "")
    if not secret or secret == _DEFAULT_SECRET or len(secret) < 32:
        raise RuntimeError(
            "[SECURITY] SECRET_KEY is missing, default, or shorter than 32 chars. "
            "Set a strong SECRET_KEY before starting."
        )

    # B104 false positive: this branch explicitly rejects insecure public binds without auth.
    if getattr(CONFIG, "API_HOST", "") == "0.0.0.0" and not getattr(CONFIG, "AUTH_ENFORCE", True):  # nosec B104
        raise RuntimeError(
            "[SECURITY] Cannot bind to 0.0.0.0 with AUTH_ENFORCE=false. "
            "Use 127.0.0.1 for dev or enable authentication."
        )


_validate_security_config()


def setup_logging() -> None:
    Path(CONFIG.LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
    stream_handler = logging.StreamHandler(sys.stdout)
    if hasattr(stream_handler.stream, 'reconfigure'):
        stream_handler.stream.reconfigure(encoding='utf-8', errors='replace')  # type: ignore[union-attr]
    handlers: list[logging.Handler] = [
        stream_handler,
        logging.FileHandler(CONFIG.LOG_FILE, encoding='utf-8'),
    ]
    logging.basicConfig(
        level=getattr(logging, CONFIG.LOG_LEVEL, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


async def run_cli(agent: 'OmniAgent') -> None:
    """Enhanced Rich CLI with model selector, RAG, pipelines, and templates."""
    from agent.cli import EnhancedCLI
    cli = EnhancedCLI(agent)
    await cli.run()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OMNI Agent")
    parser.add_argument("--mode", choices=["cli", "telegram", "api", "all"],
                       default="cli", help="Run mode")
    parser.add_argument(
        "--create-admin",
        action="store_true",
        help="Bootstrap the first admin API identity locally using the existing auth model",
    )
    return parser


def _prompt_with_default(prompt: str, default: str,
                         input_fn: Callable[[str], str] = input) -> str:
    value = input_fn(f"{prompt} [{default}]: ").strip()
    return value or default


def _prompt_confirmed_secret(label: str,
                             secret_reader: Callable[[str], str] = getpass.getpass) -> str:
    from agent.auth import validate_secret_value

    secret = secret_reader(f"{label}: ")
    confirm = secret_reader(f"Confirm {label.lower()}: ")
    if secret != confirm:
        raise ValueError(f"{label} entries did not match")
    return validate_secret_value(secret, label=label)


def _resolve_cli_bootstrap_token(auth_manager: Any,
                                 admin_api_key: str) -> str:
    from agent.auth import validate_secret_value

    configured_token = ""
    if auth_manager is not None:
        configured_token = getattr(auth_manager, "bootstrap_token", "") or ""
    if not configured_token:
        configured_token = getattr(CONFIG, "AUTH_BOOTSTRAP_TOKEN", "") or ""
    if configured_token:
        return validate_secret_value(configured_token, label="bootstrap token")
    return admin_api_key


def run_create_admin_bootstrap(auth_manager: Any = None,
                               input_fn: Callable[[str], str] = input,
                               secret_reader: Callable[[str], str] = getpass.getpass,
                               output_stream: Any = None) -> AdminBootstrapResult:
    from agent.auth import AuthManager

    output_stream = output_stream or sys.stdout
    user_id = _prompt_with_default("Admin user id", "admin", input_fn)
    name = _prompt_with_default("Admin display name", "Bootstrap Admin", input_fn)
    admin_api_key = _prompt_confirmed_secret("Initial admin API key", secret_reader)
    bootstrap_token = _resolve_cli_bootstrap_token(auth_manager, admin_api_key)

    auth = auth_manager or AuthManager(
        secret=CONFIG.SECRET_KEY,
        db_path="data/auth.db",
        enforce_auth=CONFIG.AUTH_ENFORCE,
        bootstrap_token=bootstrap_token,
    )

    raw_key, key, token = auth.bootstrap_admin_identity(
        bootstrap_token,
        user_id=user_id,
        name=name,
        admin_api_key=admin_api_key,
    )

    print(
        f"Initial admin created: user_id={key.user_id} key_id={key.key_id} role={key.role.value}",
        file=output_stream,
    )
    print(
        "Use the admin API key you entered to authenticate; obtain JWTs through the existing auth token flow.",
        file=output_stream,
    )

    return AdminBootstrapResult(
        user_id=key.user_id,
        key_id=key.key_id,
        role=key.role.value,
        api_key=raw_key,
        jwt=token,
    )


def _display_host(host: str) -> str:
    # B104 false positive: display-only normalization of the host label; no socket binding occurs here.
    return "localhost" if host in {"0.0.0.0", "::"} else host  # nosec B104


def _api_bind_ports() -> list[int]:
    logger = logging.getLogger(__name__)
    searxng_port = urlparse(CONFIG.SEARXNG_URL).port if CONFIG.SEARXNG_URL else None
    bind_ports: list[int] = []
    for port in [CONFIG.API_PORT, *CONFIG.API_FALLBACK_PORTS]:
        if port in bind_ports:
            continue
        if searxng_port is not None and port == searxng_port:
            logger.warning(
                "Skipping API port %s because SEARXNG_URL is configured on the same port.",
                port,
            )
            continue
        bind_ports.append(port)
    if not bind_ports:
        raise RuntimeError(
            "No usable API ports configured. Adjust API_PORT/API_FALLBACK_PORTS so they do not overlap SEARXNG_URL."
        )
    return bind_ports


async def run_api(agent: 'OmniAgent') -> tuple['web.AppRunner', int]:
    """Minimal aiohttp REST API server."""
    from aiohttp import web
    from agent.auth import auth_context_from_request, effective_user_id, scoped_session_id

    async def chat_endpoint(request: web.Request) -> web.Response:
        data = await request.json()
        ctx = auth_context_from_request(request)
        user_id = effective_user_id(
            ctx,
            requested_user_id=data.get("user_id", "api_user"),
            default_user_id="api_user",
        )
        default_session_id = f"api:{user_id}" if not ctx.authenticated else "api"
        try:
            session_id = scoped_session_id(
                ctx,
                requested_session_id=data.get("session_id", ""),
                default_session_id=default_session_id,
            )
        except PermissionError as exc:
            return web.json_response(
                {"error": "forbidden", "detail": str(exc)},
                status=403,
            )
        text = data.get("message", "")
        model_override = data.get("model")          # optional model override
        if not text:
            return web.json_response({"error": "message required"}, status=400)
        # Apply model override to router if provided
        if "model" in data:
            if model_override:
                agent.llm.router.set_session_model(session_id, model_override)
            else:
                agent.llm.router.clear_session_model(session_id)
        response = await agent.chat(user_id, session_id, text)
        routed = agent.llm.router.get_session_model(session_id) or "auto"
        return web.json_response({
            "response": response,
            "session_id": session_id,
            "model": routed,
        })

    async def status_endpoint(request: web.Request) -> web.Response:
        return web.json_response({
            "status": agent.memory.get_state("agent_status"),
            "health": agent.heartbeat.last_status,
            "jobs": agent.scheduler.list_jobs(),
            "skills": agent.skills.list_skills(),
            "router": agent.llm.get_router_summary(),
            "model_stats": agent.llm.get_stats(),
        })

    async def memories_endpoint(request: web.Request) -> web.Response:
        query = request.rel_url.query.get("q", "")
        if query:
            results = agent.memory.search_memories(query)
        else:
            results = agent.memory.get_memories_by_category("general")
        return web.json_response({"memories": results})

    async def models_endpoint(request: web.Request) -> web.Response:
        """GET /models — full model catalog (registry + any extra Ollama models)"""
        from agent.model_registry import MODELS, summary_table
        # All models actually present in Ollama right now
        all_ollama: set = await agent.llm._list_ollama_models(cache_seconds=0)
        models = []
        for row in summary_table():
            model_row = dict(row)
            model_row["available"] = model_row["id"] in all_ollama
            models.append(model_row)
        # Add Ollama models that are NOT in the static registry
        known_ids = {m["id"] for m in models}
        for ollama_id in sorted(all_ollama - known_ids):
            models.append({
                "id": ollama_id,
                "display_name": ollama_id,
                "provider": "Local/Ollama",
                "context_k": None,
                "tier": "balanced",
                "capabilities": [],
                "capability_count": 0,
                "best_for": [],
                "available": True,
            })
        total_available = len([m for m in models if m["available"]])
        return web.json_response({
            "count": len(models),
            "available_count": total_available,
            "models": models,
        })

    async def model_detail_endpoint(request: web.Request) -> web.Response:
        """GET /models/{model_id} — single model details"""
        from agent.model_registry import get_model
        model_id = request.match_info.get("model_id", "")
        spec = get_model(model_id)
        if not spec:
            return web.json_response({"error": "Model not found"}, status=404)
        return web.json_response(spec.to_dict())

    async def route_endpoint(request: web.Request) -> web.Response:
        """POST /route — preview routing decision"""
        data = await request.json()
        text = data.get("text", "")
        ctx = auth_context_from_request(request)
        try:
            session_id = scoped_session_id(
                ctx,
                requested_session_id=data.get("session_id", ""),
                default_session_id="route",
            )
        except PermissionError as exc:
            return web.json_response(
                {"error": "forbidden", "detail": str(exc)},
                status=403,
            )
        has_image = data.get("has_image", False)
        decision = agent.llm.router.route(text, session_id=session_id,
                                           has_image=has_image)
        return web.json_response({
            "model_id": decision.model_id,
            "model_name": decision.model_spec.display_name,
            "task_type": decision.task_type.value,
            "confidence": decision.confidence,
            "reason": decision.reason,
            "fallback_chain": decision.fallback_chain,
        })

    async def compare_endpoint(request: web.Request) -> web.Response:
        """POST /compare — run same prompt on multiple models"""
        data = await request.json()
        text = data.get("prompt") or data.get("message", "")
        model_ids = data.get("models", CONFIG.MODEL_COMPARE_IDS)
        if not text:
            return web.json_response({"error": "prompt required"}, status=400)
        messages = [{"role": "user", "content": text}]
        import time
        start = time.time()
        raw = await agent.llm.chat_parallel(messages, model_ids=model_ids)
        total_ms = int((time.time() - start) * 1000)
        results = [
            {
                "model": mid,
                "response": r.get("content", ""),
                "error": r.get("error"),
                "latency_ms": total_ms,
                "tokens": r.get("eval_count") or r.get("usage", {}).get("completion_tokens"),
            }
            for mid, r in raw.items()
        ]
        return web.json_response({"prompt": text, "results": results})

    # ── RAG endpoints ─────────────────────────────────────────────────────────

    async def rag_ingest_endpoint(request: web.Request) -> web.Response:
        """POST /rag/ingest — ingest a text document"""
        data = await request.json()
        text = data.get("text", "")
        title = data.get("title", "api-doc")
        if not text:
            return web.json_response({"error": "text required"}, status=400)
        doc = await agent.rag.ingest_text(text, title=title,
                                          metadata=data.get("metadata", {}))
        return web.json_response(doc.to_dict())

    async def rag_query_endpoint(request: web.Request) -> web.Response:
        """POST /rag/query — retrieve relevant chunks"""
        data = await request.json()
        query = data.get("query", "")
        top_k = int(data.get("top_k", 5))
        doc_id = data.get("doc_id")
        if not query:
            return web.json_response({"error": "query required"}, status=400)
        results = await agent.rag.retrieve(query, top_k=top_k, doc_id=doc_id)
        return web.json_response({
            "query": query,
            "results": [r.to_dict() for r in results],
            "context": agent.rag.generate_context(results),
        })

    async def rag_docs_endpoint(request: web.Request) -> web.Response:
        """GET /rag/docs — list ingested documents"""
        return web.json_response({"documents": agent.rag.list_documents(),
                                  "stats": agent.rag.stats()})

    async def rag_delete_endpoint(request: web.Request) -> web.Response:
        """DELETE /rag/docs/{doc_id}"""
        doc_id = request.match_info.get("doc_id", "")
        ok = agent.rag.delete_document(doc_id)
        return web.json_response({"deleted": ok, "doc_id": doc_id})

    # ── Pipeline endpoints ────────────────────────────────────────────────────

    async def pipelines_endpoint(request: web.Request) -> web.Response:
        """GET /pipelines — list registered pipelines"""
        return web.json_response({
            "pipelines": agent.pipeline_executor.list_pipelines()
        })

    async def pipeline_run_endpoint(request: web.Request) -> web.Response:
        """POST /pipelines/{name}/run"""
        name = request.match_info.get("name", "")
        data = await request.json() if request.content_length else {}
        context = data.get("context", {})
        run = await agent.pipeline_executor.run_by_name(name, context)
        if not run:
            return web.json_response({"error": f"Pipeline '{name}' not found"}, status=404)
        return web.json_response(run.to_dict())

    async def pipeline_runs_endpoint(request: web.Request) -> web.Response:
        """GET /pipelines/runs"""
        name = request.rel_url.query.get("pipeline")
        return web.json_response({
            "runs": agent.pipeline_executor.list_runs(pipeline_name=name)
        })

    # ── Template endpoints ────────────────────────────────────────────────────

    async def templates_endpoint(request: web.Request) -> web.Response:
        """GET /templates — list all prompt templates"""
        tag = request.rel_url.query.get("tag")
        return web.json_response({"templates": agent.templates.list_templates(tag=tag)})

    async def template_render_endpoint(request: web.Request) -> web.Response:
        """POST /templates/{name}/render"""
        name = request.match_info.get("name", "")
        data = await request.json()
        variables = data.get("variables", {})
        history = data.get("history", [])
        try:
            messages = agent.templates.render(name, variables, history)
            return web.json_response({"messages": messages})
        except KeyError as e:
            return web.json_response({"error": str(e)}, status=404)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)

    # ── Cache endpoint ────────────────────────────────────────────────────────

    async def cache_stats_endpoint(request: web.Request) -> web.Response:
        """GET /cache/stats"""
        stats = await agent.cache.stats()
        return web.json_response(stats)

    async def cache_flush_endpoint(request: web.Request) -> web.Response:
        """POST /cache/flush"""
        await agent.cache.flush()
        return web.json_response({"flushed": True})

    async def audit_endpoint(request: web.Request) -> web.Response:
        limit = int(request.rel_url.query.get("limit", "50"))
        log = agent.memory.get_audit_log(limit=limit)
        return web.json_response({"log": log})

    # ── Workflows ─────────────────────────────────────────────────────────────

    async def workflows_endpoint(request: web.Request) -> web.Response:
        """GET /workflows"""
        return web.json_response({"workflows": agent.workflows.list_workflows()})

    async def workflow_run_endpoint(request: web.Request) -> web.Response:
        """POST /workflows/{name}/run"""
        name = request.match_info.get("name", "")
        data = await request.json() if request.content_length else {}
        try:
            run = await agent.workflows.run(name, data.get("context", {}))
            return web.json_response(run.to_dict())
        except KeyError as e:
            return web.json_response({"error": str(e)}, status=404)

    # ── Tools Registry ────────────────────────────────────────────────────────

    async def tools_endpoint(request: web.Request) -> web.Response:
        """GET /tools"""
        category = request.rel_url.query.get("category")
        fmt = request.rel_url.query.get("format", "list")
        if fmt == "openai":
            return web.json_response({"tools": agent.tool_registry.openai_schemas(category)})
        if fmt == "anthropic":
            return web.json_response({"tools": agent.tool_registry.anthropic_schemas(category)})
        return web.json_response({"tools": agent.tool_registry.list_tools(category)})

    async def tool_call_endpoint(request: web.Request) -> web.Response:
        """POST /tools/call"""
        data = await request.json()
        from agent.tools_registry import ToolCall
        from agent.tools_registry import confirmation_policy, parse_confirmation_flag
        ctx = auth_context_from_request(request)
        try:
            session_id = scoped_session_id(
                ctx,
                requested_session_id=data.get("session_id", "api"),
                default_session_id="api",
            )
        except PermissionError as exc:
            return web.json_response(
                {"error": "forbidden", "detail": str(exc)},
                status=403,
            )
        tool = agent.tool_registry.get(data.get("tool", ""))
        confirmed = parse_confirmation_flag(data.get("confirmed", False))
        allow_confirmed_tools, rejection_detail = confirmation_policy(
            tool,
            ctx.role,
            confirmed,
        )
        if not allow_confirmed_tools and rejection_detail:
            status = 403 if confirmed else 400
            error = "forbidden" if confirmed else "confirmation_required"
            return web.json_response(
                {"error": error, "detail": rejection_detail},
                status=status,
            )
        call = ToolCall(
            tool_name=data.get("tool", ""),
            arguments=data.get("arguments", {}),
            session_id=session_id,
        )
        result = await agent.tool_registry.call(
            call,
            allow_confirmed_tools=bool(tool and tool.requires_confirmation and confirmed),
        )
        return web.json_response(result.to_dict())

    # ── Tracing ───────────────────────────────────────────────────────────────

    async def tracing_summary_endpoint(request: web.Request) -> web.Response:
        """GET /tracing/summary"""
        return web.json_response(agent.tracer.summary())

    async def tracing_spans_endpoint(request: web.Request) -> web.Response:
        """GET /tracing/spans?last_n=50"""
        last_n = int(request.rel_url.query.get("last_n", "50"))
        spans = agent.tracer.get_spans(last_n=last_n)
        return web.json_response({"spans": [s.to_dict() for s in spans]})

    async def tracing_errors_endpoint(request: web.Request) -> web.Response:
        """GET /tracing/errors"""
        return web.json_response({"errors": agent.tracer.recent_errors(limit=20)})

    # ── Structured Output ─────────────────────────────────────────────────────

    async def structured_endpoint(request: web.Request) -> web.Response:
        """POST /structured"""
        from agent.structured_output import SENTIMENT_SCHEMA, ENTITY_SCHEMA, PLAN_SCHEMA
        named = {"sentiment": SENTIMENT_SCHEMA, "entities": ENTITY_SCHEMA, "plan": PLAN_SCHEMA}
        data: Dict[str, Any] = await request.json()
        text: str = data.get("text", "")
        schema_name: str = data.get("schema", "")
        schema = named.get(schema_name)
        if not schema:
            return web.json_response(
                {"error": f"Unknown schema '{schema_name}'. Available: {list(named.keys())}"},
                status=400)
        if not text:
            return web.json_response({"error": "text required"}, status=400)
        result = await agent.structured_parser.parse(text, schema)
        return web.json_response(result.to_dict())

    from agent.dashboard import register_dashboard

    async def favicon_endpoint(request: web.Request) -> web.Response:
        return web.Response(status=204)

    app = web.Application(middlewares=[
        agent.auth.middleware(public_paths=["/status", "/health", "/auth/bootstrap", "/dashboard", "/favicon.ico", "/cache/stats", "/audit"])
    ])
    # Core
    app.router.add_post("/chat", chat_endpoint)
    app.router.add_get("/status", status_endpoint)
    app.router.add_get("/memories", memories_endpoint)
    app.router.add_get("/audit", audit_endpoint)
    # Models
    app.router.add_get("/models", models_endpoint)
    app.router.add_get("/models/{model_id}", model_detail_endpoint)
    app.router.add_post("/route", route_endpoint)
    app.router.add_post("/compare", compare_endpoint)
    # RAG
    app.router.add_post("/rag/ingest", rag_ingest_endpoint)
    app.router.add_post("/rag/query", rag_query_endpoint)
    app.router.add_get("/rag/docs", rag_docs_endpoint)
    app.router.add_delete("/rag/docs/{doc_id}", rag_delete_endpoint)
    # Pipelines
    app.router.add_get("/pipelines", pipelines_endpoint)
    app.router.add_post("/pipelines/{name}/run", pipeline_run_endpoint)
    app.router.add_get("/pipelines/runs", pipeline_runs_endpoint)
    # Templates
    app.router.add_get("/templates", templates_endpoint)
    app.router.add_post("/templates/{name}/render", template_render_endpoint)
    # Cache
    app.router.add_get("/cache/stats", cache_stats_endpoint)
    app.router.add_get("/favicon.ico", favicon_endpoint)
    app.router.add_post("/cache/flush", cache_flush_endpoint)
    # Workflows
    app.router.add_get("/workflows", workflows_endpoint)
    app.router.add_post("/workflows/{name}/run", workflow_run_endpoint)
    # Tools Registry
    app.router.add_get("/tools", tools_endpoint)
    app.router.add_post("/tools/call", tool_call_endpoint)
    # Tracing
    app.router.add_get("/tracing/summary", tracing_summary_endpoint)
    app.router.add_get("/tracing/spans", tracing_spans_endpoint)
    app.router.add_get("/tracing/errors", tracing_errors_endpoint)
    # Structured output
    app.router.add_post("/structured", structured_endpoint)

    # ── Personas ──────────────────────────────────────────────────────────────

    async def personas_endpoint(request: web.Request) -> web.Response:
        tag = request.rel_url.query.get("tag", "")
        return web.json_response({"personas": agent.persona_registry.list_personas(tag)})

    async def persona_set_endpoint(request: web.Request) -> web.Response:
        ctx = auth_context_from_request(request)
        try:
            session_id = scoped_session_id(
                ctx,
                requested_session_id=request.match_info.get("session_id", ""),
                default_session_id="persona",
            )
        except PermissionError as exc:
            return web.json_response(
                {"error": "forbidden", "detail": str(exc)},
                status=403,
            )
        data = await request.json()
        name = data.get("persona", "assistant")
        ok = agent.persona_manager.set_session_persona(session_id, name)
        if not ok:
            return web.json_response({"error": f"Unknown persona '{name}'"}, status=400)
        return web.json_response(agent.persona_manager.session_info(session_id))

    async def persona_info_endpoint(request: web.Request) -> web.Response:
        ctx = auth_context_from_request(request)
        try:
            session_id = scoped_session_id(
                ctx,
                requested_session_id=request.match_info.get("session_id", ""),
                default_session_id="persona",
            )
        except PermissionError as exc:
            return web.json_response(
                {"error": "forbidden", "detail": str(exc)},
                status=403,
            )
        return web.json_response(agent.persona_manager.session_info(session_id))

    # ── Evaluation ────────────────────────────────────────────────────────────

    async def eval_suites_endpoint(request: web.Request) -> web.Response:
        return web.json_response({"suites": agent.evaluator.list_suites()})

    async def eval_history_endpoint(request: web.Request) -> web.Response:
        suite = request.match_info.get("suite", "")
        model = request.rel_url.query.get("model")
        return web.json_response(agent.evaluator.get_history(suite, model or ""))

    async def eval_compare_endpoint(request: web.Request) -> web.Response:
        suite = request.match_info.get("suite", "")
        return web.json_response(agent.evaluator.model_comparison(suite))

    # ── Knowledge Graph ───────────────────────────────────────────────────────

    async def kg_stats_endpoint(request: web.Request) -> web.Response:
        return web.json_response(agent.knowledge_graph.stats())

    async def kg_extract_endpoint(request: web.Request) -> web.Response:
        data = await request.json()
        text = data.get("text", "")
        if not text:
            return web.json_response({"error": "text required"}, status=400)
        # Parse text for entity names and add as nodes
        node = agent.knowledge_graph.add_node(
            node_id=text[:20].lower().replace(" ", "_"),
            label=text[:50],
            node_type="extracted",
        )
        return web.json_response({"node": node.to_dict()})

    async def kg_search_endpoint(request: web.Request) -> web.Response:
        name = request.rel_url.query.get("name", "")
        if not name:
            return web.json_response({"error": "name required"}, status=400)
        nodes = agent.knowledge_graph.neighbours(name, direction="both")
        return web.json_response({"neighbours": [n.to_dict() for n in nodes]})

    async def kg_path_endpoint(request: web.Request) -> web.Response:
        src = request.rel_url.query.get("from", "")
        tgt = request.rel_url.query.get("to", "")
        path = agent.knowledge_graph.shortest_path(src, tgt)
        if not path:
            return web.json_response({"error": "no path found"}, status=404)
        return web.json_response({"path": path})

    async def kg_export_endpoint(request: web.Request) -> web.Response:
        return web.json_response(agent.knowledge_graph.stats())

    # ── Config API ────────────────────────────────────────────────────────────
    agent.config_mgr.register_routes(app, prefix="")

    # Register all new routes
    app.router.add_get("/personas", personas_endpoint)
    app.router.add_post("/personas/session/{session_id}", persona_set_endpoint)
    app.router.add_get("/personas/session/{session_id}", persona_info_endpoint)
    app.router.add_get("/eval/suites", eval_suites_endpoint)
    app.router.add_get("/eval/history/{suite}", eval_history_endpoint)
    app.router.add_get("/eval/compare/{suite}", eval_compare_endpoint)
    app.router.add_get("/kg/stats", kg_stats_endpoint)
    app.router.add_post("/kg/extract", kg_extract_endpoint)
    app.router.add_get("/kg/search", kg_search_endpoint)
    app.router.add_get("/kg/path", kg_path_endpoint)
    app.router.add_get("/kg/export", kg_export_endpoint)

    # ── Sandbox ──────────────────────────────────────────────────────────────

    async def sandbox_run_endpoint(request: web.Request) -> web.Response:
        """POST /sandbox/run — execute code in secure sandbox"""
        data = await request.json()
        code = data.get("code", "")
        language = data.get("language", "python")
        if not code:
            return web.json_response({"error": "code required"}, status=400)
        from agent.sandbox import ExecLanguage
        try:
            lang = ExecLanguage(language)
        except ValueError:
            lang = ExecLanguage.PYTHON
        result = await agent.sandbox.run(code, language=lang)
        return web.json_response(result.to_dict())

    async def sandbox_history_endpoint(request: web.Request) -> web.Response:
        return web.json_response({"history": agent.sandbox.get_history(limit=20),
                                  "stats": agent.sandbox.stats()})

    app.router.add_post("/sandbox/run", sandbox_run_endpoint)
    app.router.add_get("/sandbox/history", sandbox_history_endpoint)

    # ── Multimodal / Vision ───────────────────────────────────────────────────

    async def vision_analyze_endpoint(request: web.Request) -> web.Response:
        """POST /vision/analyze — analyze image"""
        data = await request.json()
        source = data.get("source", "")
        task = data.get("task", "describe")
        model = data.get("model")
        if not source:
            return web.json_response({"error": "source required"}, status=400)
        result = await agent.vision.analyze(source, task=task, model=model)
        return web.json_response(result.to_dict())

    async def vision_models_endpoint(request: web.Request) -> web.Response:
        return web.json_response({"models": agent.vision.get_vision_models()})

    app.router.add_post("/vision/analyze", vision_analyze_endpoint)
    app.router.add_get("/vision/models", vision_models_endpoint)

    # ── Auth ──────────────────────────────────────────────────────────────────
    agent.auth.register_routes(app, prefix="")

    # ── Export ────────────────────────────────────────────────────────────────
    agent.exporter.register_routes(app, prefix="")

    # ── Notifications ─────────────────────────────────────────────────────────

    async def notif_channels_endpoint(request: web.Request) -> web.Response:
        return web.json_response({"channels": agent.notifier.list_channels()})

    async def notif_send_endpoint(request: web.Request) -> web.Response:
        data = await request.json()
        channel = data.get("channel", "console")
        title = data.get("title", "Notification")
        body = data.get("body", "")
        priority_str = data.get("priority", "normal")
        from agent.notifications import Priority
        try:
            priority = Priority(priority_str)
        except ValueError:
            priority = Priority.NORMAL
        rec = await agent.notifier.send(channel, title, body, priority)
        return web.json_response(rec.to_dict())

    async def notif_stats_endpoint(request: web.Request) -> web.Response:
        return web.json_response({
            "stats": agent.notifier.delivery_stats(),
            "recent": agent.notifier.recent_deliveries(limit=20),
        })

    app.router.add_get("/notifications/channels", notif_channels_endpoint)
    app.router.add_post("/notifications/send", notif_send_endpoint)
    app.router.add_get("/notifications/stats", notif_stats_endpoint)

    # Streaming SSE
    from agent.streaming import register_streaming_routes
    register_streaming_routes(app, agent)
    register_dashboard(app, agent)

    runner = web.AppRunner(app)
    await runner.setup()
    logger = logging.getLogger(__name__)
    last_error: Optional[OSError] = None
    bind_host = CONFIG.API_HOST
    for bind_port in _api_bind_ports():
        try:
            site = web.TCPSite(runner, bind_host, bind_port)
            await site.start()
            if bind_port != CONFIG.API_PORT:
                logger.warning(
                    "Preferred API port %s unavailable. Using fallback port %s.",
                    CONFIG.API_PORT,
                    bind_port,
                )
            logger.info(
                "API server running on http://%s:%s",
                bind_host,
                bind_port,
            )
            return runner, bind_port
        except OSError as exc:
            last_error = exc
            port_in_use = (
                getattr(exc, "winerror", None) == 10048
                or exc.errno in {48, 98, 10048}
            )
            if not port_in_use:
                await runner.cleanup()
                raise
            logger.warning("API port %s unavailable: %s", bind_port, exc)

    await runner.cleanup()
    tried_ports = ", ".join(str(port) for port in _api_bind_ports())
    raise RuntimeError(
        f"Unable to start API server. Tried ports: {tried_ports}. "
        "Set API_PORT or API_FALLBACK_PORTS to free ports."
    ) from last_error


async def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.create_admin:
        try:
            run_create_admin_bootstrap()
        except (PermissionError, RuntimeError, ValueError) as exc:
            print(f"[SECURITY] Admin bootstrap failed: {exc}", file=sys.stderr)
            raise SystemExit(1)
        return

    setup_logging()
    logger = logging.getLogger("main")
    logger.info(f"Starting OMNI Agent in mode: {args.mode}")

    from agent.core import OmniAgent
    agent = OmniAgent()
    await agent.start()

    # Graceful shutdown
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def _signal_handler():
        logger.info("Shutdown signal received.")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass  # Windows

    tasks = []
    api_port: Optional[int] = None

    if args.mode in ("telegram", "all"):
        if not CONFIG.TELEGRAM_TOKEN:
            logger.warning("TELEGRAM_TOKEN not set. Skipping Telegram mode.")
        else:
            from agent.telegram_bot import TelegramBot
            bot = TelegramBot(memory=agent.memory, agent_handler=agent.chat)
            tasks.append(asyncio.create_task(bot.start_polling()))

    if args.mode in ("api", "all"):
        runner, api_port = await run_api(agent)

    if args.mode in ("cli",):
        await run_cli(agent)
        stop_event.set()
    else:
        if api_port is not None:
            dashboard_url = f"http://{_display_host(CONFIG.API_HOST)}:{api_port}/dashboard"
            print(
                f"\n🚀 OMNI Agent running in '{args.mode}' mode at {dashboard_url}. "
                "Press Ctrl+C to stop.\n"
            )
        else:
            print(f"\n🚀 OMNI Agent running in '{args.mode}' mode. Press Ctrl+C to stop.\n")
        await stop_event.wait()

    for task in tasks:
        task.cancel()

    if args.mode in ("api", "all"):
        await runner.cleanup()

    await agent.stop()
    logger.info("OMNI Agent shut down cleanly.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.getLogger("main").info("Keyboard interrupt received. Exiting.")
    except Exception:
        logging.getLogger("main").exception("OMNI Agent crashed.")
        raise
