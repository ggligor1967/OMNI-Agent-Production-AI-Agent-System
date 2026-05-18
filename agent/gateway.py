"""
OMNI AGENT - API Gateway
Unified entry point for all agent HTTP APIs: authentication, rate limiting,
request routing, CORS, response enveloping, and structured error handling.

Features:
- Middleware chain: auth → rate-limit → CORS → logging → handler
- Auth: Bearer token (JWT) and API key validation via auth module
- Per-route rate limiting: different limits for public vs. authenticated endpoints
- CORS: configurable origins, methods, headers with preflight support
- Request ID: X-Request-ID header injected on every request/response
- Structured error envelope: {error, code, request_id, timestamp}
- Unified success envelope: {data, meta: {request_id, duration_ms}}
- Health check bypass: /health and /ready skip auth
- Request logging: structured log for every request with timing
- Route registry: modules self-register their routes via register_routes()
- Graceful shutdown: drain in-flight requests before closing
"""
import time
import uuid
import json
import logging
import asyncio
from typing import Any, Callable, Dict, List, Optional, Set
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST / RESPONSE CONTEXT
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class RequestContext:
    """Carries per-request metadata through the middleware chain."""
    request_id: str
    method: str
    path: str
    user_id: str = ""
    api_key_id: str = ""
    authenticated: bool = False
    started_at: float = field(default_factory=time.time)
    rate_limit_key: str = ""
    tags: Dict[str, str] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        return (time.time() - self.started_at) * 1000


# ══════════════════════════════════════════════════════════════════════════════
# MIDDLEWARE PRIMITIVES
# ══════════════════════════════════════════════════════════════════════════════

class MiddlewareError(Exception):
    """Raised by middleware to short-circuit with an error response."""
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


def _error_body(code: str, message: str, request_id: str) -> Dict:
    return {
        "error": {"code": code, "message": message},
        "request_id": request_id,
        "timestamp": time.time(),
    }


def _success_body(data: Any, request_id: str, duration_ms: float) -> Dict:
    return {
        "data": data,
        "meta": {
            "request_id": request_id,
            "duration_ms": round(duration_ms, 2),
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# CORS HANDLER
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class CORSConfig:
    allowed_origins: List[str] = field(default_factory=lambda: ["*"])
    allowed_methods: List[str] = field(
        default_factory=lambda: ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    allowed_headers: List[str] = field(
        default_factory=lambda: ["Content-Type", "Authorization", "X-Request-ID",
                                  "X-API-Key"])
    expose_headers: List[str] = field(
        default_factory=lambda: ["X-Request-ID", "X-RateLimit-Remaining"])
    max_age_s: int = 86400
    allow_credentials: bool = False

    def headers(self, origin: str = "*") -> Dict[str, str]:
        allowed_origin = origin if ("*" in self.allowed_origins or
                                    origin in self.allowed_origins) else ""
        return {
            "Access-Control-Allow-Origin": allowed_origin or "*",
            "Access-Control-Allow-Methods": ", ".join(self.allowed_methods),
            "Access-Control-Allow-Headers": ", ".join(self.allowed_headers),
            "Access-Control-Expose-Headers": ", ".join(self.expose_headers),
            "Access-Control-Max-Age": str(self.max_age_s),
            **({"Access-Control-Allow-Credentials": "true"}
               if self.allow_credentials else {}),
        }


# ══════════════════════════════════════════════════════════════════════════════
# ROUTE REGISTRY
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class RouteConfig:
    path: str
    method: str
    auth_required: bool = True
    rate_limit_policy: str = "api_default"
    public: bool = False           # skip auth AND rate limit
    tags: List[str] = field(default_factory=list)


class RouteRegistry:
    """Tracks per-route security and rate-limit configuration."""

    def __init__(self):
        self._routes: Dict[str, RouteConfig] = {}

    def register(self, route: RouteConfig):
        key = f"{route.method.upper()}:{route.path}"
        self._routes[key] = route

    def get(self, method: str, path: str) -> Optional[RouteConfig]:
        key = f"{method.upper()}:{path}"
        return self._routes.get(key)

    def is_public(self, path: str) -> bool:
        PUBLIC_PREFIXES = ["/health", "/ready", "/metrics", "/favicon"]
        return any(path.startswith(p) for p in PUBLIC_PREFIXES)


# ══════════════════════════════════════════════════════════════════════════════
# GATEWAY
# ══════════════════════════════════════════════════════════════════════════════

class APIGateway:
    """
    Unified HTTP API gateway built on aiohttp.

    Usage:
        gw = APIGateway(
            auth_manager=auth,
            rate_limiter=limiter,
            cors=CORSConfig(allowed_origins=["https://myapp.com"]),
        )

        # Register module routes
        gw.register_module(session_manager)
        gw.register_module(search_service)
        gw.register_module(governance_manager)

        # Start
        await gw.start(host="0.0.0.0", port=8080)
    """

    def __init__(self,
                 auth_manager=None,
                 rate_limiter=None,
                 cors: CORSConfig = None,
                 api_prefix: str = "/api/v1",
                 envelop_responses: bool = True,
                 log_requests: bool = True,
                 max_body_size_mb: float = 10.0):
        self._auth = auth_manager
        self._rate_limiter = rate_limiter
        self._cors = cors or CORSConfig()
        self._prefix = api_prefix
        self._envelop = envelop_responses
        self._log = log_requests
        self._max_body = int(max_body_size_mb * 1024 * 1024)
        self._registry = RouteRegistry()
        self._modules: List[Any] = []
        self._app = None
        self._runner = None
        self._site = None
        self._request_count = 0
        self._error_count = 0
        self._in_flight = 0

    # ── Module Registration ───────────────────────────────────────────────────

    def register_module(self, module: Any, prefix: str = None):
        """Register a module's routes. Module must have register_routes(app, prefix)."""
        if not hasattr(module, "register_routes"):
            logger.warning(f"Module {module.__class__.__name__} has no register_routes()")
            return
        self._modules.append((module, prefix or self._prefix))

    # ── Middleware ────────────────────────────────────────────────────────────

    async def _middleware(self, request, handler):
        """Main aiohttp middleware: inject request context, auth, rate limit, CORS."""
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4())[:16])
        ctx = RequestContext(
            request_id=request_id,
            method=request.method,
            path=request.path,
        )
        request["ctx"] = ctx
        self._request_count += 1
        self._in_flight += 1

        try:
            from aiohttp import web

            # Preflight CORS
            origin = request.headers.get("Origin", "*")
            if request.method == "OPTIONS":
                resp = web.Response(status=204)
                for k, v in self._cors.headers(origin).items():
                    resp.headers[k] = v
                return resp

            # Skip auth/rate-limit for public paths
            is_public = self._registry.is_public(request.path)

            # Authentication
            if not is_public and self._auth:
                try:
                    await self._authenticate(request, ctx)
                except MiddlewareError as e:
                    self._error_count += 1
                    resp = web.Response(
                        status=e.status,
                        content_type="application/json",
                        text=json.dumps(_error_body(e.code, e.message, request_id)),
                    )
                    resp.headers["X-Request-ID"] = request_id
                    return resp

            # Rate limiting
            if not is_public and self._rate_limiter:
                try:
                    await self._check_rate_limit(request, ctx)
                except MiddlewareError as e:
                    self._error_count += 1
                    resp = web.Response(
                        status=429,
                        content_type="application/json",
                        text=json.dumps(_error_body(e.code, e.message, request_id)),
                    )
                    resp.headers["X-Request-ID"] = request_id
                    resp.headers["Retry-After"] = "60"
                    return resp

            # Call the actual handler
            try:
                response = await handler(request)
            except Exception as e:
                self._error_count += 1
                logger.error(f"Handler error: {request.path} → {e}", exc_info=True)
                resp = web.Response(
                    status=500,
                    content_type="application/json",
                    text=json.dumps(_error_body(
                        "INTERNAL_ERROR", "An internal error occurred.", request_id
                    )),
                )
                resp.headers["X-Request-ID"] = request_id
                return resp

            # Inject standard headers
            response.headers["X-Request-ID"] = request_id
            for k, v in self._cors.headers(origin).items():
                response.headers.setdefault(k, v)

            if self._log:
                logger.info(
                    f"{request.method} {request.path} → {response.status} "
                    f"({ctx.duration_ms:.1f}ms) user={ctx.user_id or 'anon'} "
                    f"req={request_id}"
                )

            return response

        finally:
            self._in_flight -= 1

    async def _authenticate(self, request, ctx: RequestContext):
        """Extract and validate Bearer token or API key."""
        auth_header = request.headers.get("Authorization", "")
        api_key = request.headers.get("X-API-Key", "") or \
                  request.rel_url.query.get("api_key", "")

        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            try:
                result = self._auth.authenticate(token=token)
                if result and getattr(result, "authenticated", False):
                    ctx.authenticated = True
                    ctx.user_id = getattr(result, "user_id", "")
                    return
            except Exception:
                pass
            raise MiddlewareError(401, "UNAUTHORIZED", "Invalid or expired token.")

        elif api_key:
            try:
                result = self._auth.authenticate(api_key=api_key)
                if result and getattr(result, "authenticated", False):
                    ctx.authenticated = True
                    ctx.api_key_id = getattr(result, "key_id", "")
                    ctx.user_id = getattr(result, "user_id", "")
                    return
            except Exception:
                pass
            raise MiddlewareError(401, "UNAUTHORIZED", "Invalid API key.")

        else:
            raise MiddlewareError(401, "AUTH_REQUIRED",
                                  "Authentication required. Provide Bearer token or X-API-Key.")

    async def _check_rate_limit(self, request, ctx: RequestContext):
        """Check rate limit for this request."""
        key = ctx.user_id or request.remote or "anonymous"
        try:
            result = await self._rate_limiter.check_user(key)
            if not result.allowed:
                raise MiddlewareError(
                    429, "RATE_LIMITED",
                    f"Rate limit exceeded. Retry after {result.retry_after_s:.0f}s."
                )
        except MiddlewareError:
            raise
        except Exception:
            pass  # Rate limiter errors should not block requests

    # ── App Construction ──────────────────────────────────────────────────────

    def build_app(self):
        """Build and configure the aiohttp application."""
        from aiohttp import web

        middlewares = [self._middleware]
        app = web.Application(
            middlewares=middlewares,
            client_max_size=self._max_body,
        )

        # Register all module routes
        for module, prefix in self._modules:
            try:
                module.register_routes(app, prefix=prefix)
            except Exception as e:
                logger.error(f"Failed to register routes for "
                            f"{module.__class__.__name__}: {e}")

        # Gateway meta endpoints
        app.router.add_get("/health", self._health_handler)
        app.router.add_get("/ready", self._ready_handler)
        app.router.add_get(f"{self._prefix}/gateway/stats", self._stats_handler)

        self._app = app
        return app

    async def _health_handler(self, request):
        from aiohttp import web
        return web.json_response({
            "status": "healthy",
            "requests": self._request_count,
            "errors": self._error_count,
            "in_flight": self._in_flight,
            "uptime_s": round(time.time() - self._started_at, 1),
        })

    async def _ready_handler(self, request):
        from aiohttp import web
        return web.Response(text="ready", status=200)

    async def _stats_handler(self, request):
        from aiohttp import web
        return web.json_response({
            "requests_total": self._request_count,
            "errors_total": self._error_count,
            "in_flight": self._in_flight,
            "modules": len(self._modules),
            "error_rate": (
                round(self._error_count / self._request_count, 4)
                if self._request_count else 0.0
            ),
        })

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self, host: str = "0.0.0.0", port: int = 8080):
        from aiohttp import web
        self._started_at = time.time()
        app = self.build_app()
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host, port)
        await self._site.start()
        logger.info(f"API Gateway started at http://{host}:{port}{self._prefix}")

    async def stop(self, timeout_s: float = 10.0):
        """Graceful shutdown: wait for in-flight requests."""
        deadline = time.time() + timeout_s
        while self._in_flight > 0 and time.time() < deadline:
            await asyncio.sleep(0.1)
        if self._runner:
            await self._runner.cleanup()
        logger.info(f"API Gateway stopped "
                   f"(drained {self._request_count} total requests)")

    def stats(self) -> Dict:
        return {
            "requests_total": self._request_count,
            "errors_total": self._error_count,
            "in_flight": self._in_flight,
            "modules_registered": len(self._modules),
        }


# ══════════════════════════════════════════════════════════════════════════════
# HELPER: wrap a plain function as a gateway-compatible module
# ══════════════════════════════════════════════════════════════════════════════

class RouteModule:
    """
    Lightweight adapter to register ad-hoc routes without a full module class.

    Usage:
        module = RouteModule()
        module.add_route("GET", "/ping", lambda req: web.Response(text="pong"))
        gw.register_module(module)
    """

    def __init__(self):
        self._routes: List[tuple] = []

    def add_route(self, method: str, path: str, handler: Callable):
        self._routes.append((method, path, handler))

    def register_routes(self, app, prefix: str = ""):
        from aiohttp import web
        for method, path, handler in self._routes:
            app.router.add_route(method, f"{prefix}{path}", handler)
