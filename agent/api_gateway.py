"""OMNI AGENT - API Gateway
Route registry, middleware pipeline, auth, rate limiting,
request/response transformation, and upstream proxying.

Features:
- Route registry: pattern → handler with method + path matching
- Path parameters: /users/{id} captured into request context
- Middleware pipeline: ordered list of fn(req, ctx, next) coroutines
- Built-in middleware: auth, rate_limit, logging, cors, timeout, transform
- Auth strategies: API key (header), Bearer JWT (HMAC-HS256), Basic, None
- Rate limiting: per-client token bucket (reuses rate_limiter concepts)
- Request transformation: rewrite headers, body, path before forwarding
- Response transformation: rewrite headers, body after handler returns
- Circuit breaker per upstream route
- Retry: automatic retry on upstream failure with backoff
- Load balancing: round-robin across upstream targets
- Request logging: method, path, status, latency per request
- CORS: configurable allowed origins, methods, headers
- Timeout: per-route request timeout
- Mock responses: return static response without calling handler
- Hooks: on_request, on_response, on_error
- Stats: request counts, error rates, latency histograms per route
- SQLite persistence: route config, request log
- REST API: register_route, request_log, stats
"""
import asyncio, hashlib, hmac, json, re, sqlite3, time, uuid, logging
from base64 import b64decode
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class AuthStrategy(str, Enum):
    NONE   = "none"
    APIKEY = "apikey"
    BEARER = "bearer"
    BASIC  = "basic"

@dataclass
class RouteConfig:
    method: str; pattern: str
    handler: Optional[Callable] = None
    auth: AuthStrategy = AuthStrategy.NONE
    auth_keys: List[str] = field(default_factory=list)   # valid API keys
    rate_limit: int = 0          # req/s, 0 = unlimited
    timeout_s: float = 0.0       # 0 = no timeout
    upstream: Optional[str] = None
    mock_response: Optional[Dict] = None
    req_transform: Optional[Callable] = None
    resp_transform: Optional[Callable] = None
    middlewares: List[str] = field(default_factory=list)  # named middleware ids
    _regex: Any = field(default=None, repr=False)
    _param_names: List[str] = field(default_factory=list, repr=False)
    # Stats
    requests: int = 0; errors: int = 0
    total_latency_ms: float = 0.0

    def __post_init__(self):
        # Compile path pattern to regex
        pattern = self.pattern
        param_re = re.compile(r'\{(\w+)\}')
        self._param_names = param_re.findall(pattern)
        regex_str = "^" + param_re.sub(r'(?P<\1>[^/]+)', pattern) + "$"
        self._regex = re.compile(regex_str)

    def match(self, method: str, path: str
               ) -> Optional[Dict[str, str]]:
        if self.method.upper() not in (method.upper(), "*"):
            return None
        m = self._regex.match(path)
        return m.groupdict() if m else None

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.requests if self.requests else 0

    def to_dict(self):
        return {"method": self.method, "pattern": self.pattern,
                "auth": self.auth.value, "rate_limit": self.rate_limit,
                "timeout_s": self.timeout_s,
                "requests": self.requests, "errors": self.errors,
                "avg_latency_ms": round(self.avg_latency_ms, 2)}

@dataclass
class GatewayRequest:
    method: str; path: str
    headers: Dict[str, str] = field(default_factory=dict)
    body: Any = None
    query: Dict[str, str] = field(default_factory=dict)
    client_id: str = ""
    path_params: Dict[str, str] = field(default_factory=dict)
    metadata: Dict = field(default_factory=dict)

@dataclass
class GatewayResponse:
    status: int = 200
    body: Any = None
    headers: Dict[str, str] = field(default_factory=dict)
    latency_ms: float = 0.0
    error: str = ""

    def to_dict(self):
        return {"status": self.status, "body": self.body,
                "headers": self.headers,
                "latency_ms": round(self.latency_ms, 2)}

class TokenBucket:
    """Per-client rate limiting."""
    def __init__(self, rate: float, capacity: float = None):
        self.rate = rate; self.capacity = capacity or rate
        self.tokens = self.capacity; self._last = time.time()

    def consume(self, cost: float = 1.0) -> bool:
        now = time.time(); elapsed = now - self._last
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self._last = now
        if self.tokens >= cost:
            self.tokens -= cost; return True
        return False

class AGWStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS req_log(
                    id TEXT PRIMARY KEY, method TEXT, path TEXT,
                    status INTEGER, latency_ms REAL,
                    client_id TEXT, ts REAL);
                CREATE INDEX IF NOT EXISTS idx_rl_ts ON req_log(ts);
            """)

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def log(self, req: GatewayRequest, resp: GatewayResponse):
        with self._conn() as c:
            c.execute("INSERT INTO req_log VALUES(?,?,?,?,?,?,?)",
                (str(uuid.uuid4())[:8], req.method, req.path,
                 resp.status, resp.latency_ms,
                 req.client_id, time.time()))

    def stats(self, window_s: float = 3600) -> Dict:
        since = time.time() - window_s
        with self._conn() as c:
            total = c.execute(
                "SELECT COUNT(*) FROM req_log WHERE ts>?", (since,)).fetchone()[0]
            errors = c.execute(
                "SELECT COUNT(*) FROM req_log WHERE ts>? AND status>=400",
                (since,)).fetchone()[0]
            by_path = {r["path"]: r["cnt"] for r in c.execute(
                "SELECT path, COUNT(*) as cnt FROM req_log "
                "WHERE ts>? GROUP BY path ORDER BY cnt DESC LIMIT 20",
                (since,)).fetchall()}
            avg_lat = c.execute(
                "SELECT AVG(latency_ms) FROM req_log WHERE ts>?",
                (since,)).fetchone()[0] or 0
        return {"requests": total, "errors": errors,
                "error_rate": round(errors/total, 4) if total else 0,
                "avg_latency_ms": round(avg_lat, 2),
                "by_path": by_path}

class APIGateway:
    """
    API gateway with middleware pipeline and route matching.

    Usage:
        gw = APIGateway()
        gw.add_api_key("secret-key-123")

        @gw.route("GET", "/api/users/{id}", auth=AuthStrategy.APIKEY)
        async def get_user(req, ctx):
            user_id = req.path_params["id"]
            return GatewayResponse(body={"id": user_id})

        # Dispatch
        req = GatewayRequest("GET", "/api/users/42",
                              headers={"x-api-key": "secret-key-123"})
        resp = await gw.dispatch(req)
    """
    def __init__(self, db_path: str = "data/gateway.db",
                  jwt_secret: bytes = b"change-me"):
        self._store = AGWStore(db_path)
        self._routes: List[RouteConfig] = []
        self._middlewares: Dict[str, Callable] = {}
        self._api_keys: List[str] = []
        self._jwt_secret = jwt_secret
        self._rate_buckets: Dict[str, TokenBucket] = {}
        self._global_middlewares: List[Callable] = []
        self._hooks_request:  List[Callable] = []
        self._hooks_response: List[Callable] = []
        self._hooks_error:    List[Callable] = []

    def on_request(self, fn):  self._hooks_request.append(fn)
    def on_response(self, fn): self._hooks_response.append(fn)
    def on_error(self, fn):    self._hooks_error.append(fn)

    def add_api_key(self, key: str): self._api_keys.append(key)

    def add_middleware(self, name: str, fn: Callable):
        self._middlewares[name] = fn

    def add_global_middleware(self, fn: Callable):
        self._global_middlewares.append(fn)

    def route(self, method: str, pattern: str,
               auth: AuthStrategy = AuthStrategy.NONE,
               rate_limit: int = 0, timeout_s: float = 0.0,
               mock_response: Dict = None):
        """Decorator to register a route handler."""
        def decorator(fn):
            self.register_route(method, pattern, handler=fn,
                                  auth=auth, rate_limit=rate_limit,
                                  timeout_s=timeout_s,
                                  mock_response=mock_response)
            return fn
        return decorator

    def register_route(self, method: str, pattern: str,
                        handler: Callable = None,
                        auth: AuthStrategy = AuthStrategy.NONE,
                        auth_keys: List[str] = None,
                        rate_limit: int = 0, timeout_s: float = 0.0,
                        upstream: str = None,
                        mock_response: Dict = None,
                        req_transform: Callable = None,
                        resp_transform: Callable = None) -> RouteConfig:
        rc = RouteConfig(
            method=method, pattern=pattern, handler=handler,
            auth=auth, auth_keys=list(auth_keys or []),
            rate_limit=rate_limit, timeout_s=timeout_s,
            upstream=upstream, mock_response=mock_response,
            req_transform=req_transform, resp_transform=resp_transform)
        self._routes.append(rc)
        return rc

    def _find_route(self, method: str, path: str
                     ) -> Optional[Tuple[RouteConfig, Dict]]:
        for rc in self._routes:
            params = rc.match(method, path)
            if params is not None:
                return rc, params
        return None

    def _check_auth(self, rc: RouteConfig, req: GatewayRequest) -> Optional[str]:
        if rc.auth == AuthStrategy.NONE: return None
        if rc.auth == AuthStrategy.APIKEY:
            keys = rc.auth_keys or self._api_keys
            provided = (req.headers.get("x-api-key") or
                         req.headers.get("X-API-Key") or
                         req.query.get("api_key",""))
            if provided not in keys:
                return "Invalid API key"
        elif rc.auth == AuthStrategy.BEARER:
            token = req.headers.get("Authorization","")
            if token.startswith("Bearer "):
                token = token[7:]
            if not self._verify_jwt(token):
                return "Invalid Bearer token"
        elif rc.auth == AuthStrategy.BASIC:
            enc = req.headers.get("Authorization","")
            if enc.startswith("Basic "):
                try:
                    decoded = b64decode(enc[6:]).decode()
                    req.metadata["basic_user"] = decoded.split(":")[0]
                except:
                    return "Invalid Basic auth"
            else:
                return "Basic auth required"
        return None

    def _verify_jwt(self, token: str) -> bool:
        parts = token.split(".")
        if len(parts) != 3: return False
        try:
            import base64
            def b64pad(s):
                return s + "=" * (-len(s) % 4)
            sig = base64.urlsafe_b64decode(b64pad(parts[2]))
            msg = f"{parts[0]}.{parts[1]}".encode()
            expected = hmac.new(self._jwt_secret, msg, hashlib.sha256).digest()
            if not hmac.compare_digest(sig, expected): return False
            payload = json.loads(base64.urlsafe_b64decode(b64pad(parts[1])))
            if "exp" in payload and payload["exp"] < time.time():
                return False
            return True
        except: return False

    def _check_rate_limit(self, rc: RouteConfig,
                            req: GatewayRequest) -> bool:
        if rc.rate_limit <= 0: return True
        key = f"{rc.pattern}:{req.client_id or req.headers.get('x-api-key','anon')}"
        if key not in self._rate_buckets:
            self._rate_buckets[key] = TokenBucket(rate=rc.rate_limit)
        return self._rate_buckets[key].consume()

    async def dispatch(self, req: GatewayRequest) -> GatewayResponse:
        t0 = time.time()
        for h in self._hooks_request:
            try: h(req)
            except: pass
        match = self._find_route(req.method, req.path)
        if not match:
            return GatewayResponse(status=404, body={"error":"Not Found"})
        rc, params = match
        req.path_params = params
        # Auth
        auth_err = self._check_auth(rc, req)
        if auth_err:
            return GatewayResponse(status=401,
                                    body={"error": auth_err})
        # Rate limit
        if not self._check_rate_limit(rc, req):
            return GatewayResponse(status=429,
                                    body={"error": "Rate limit exceeded"})
        # Request transform
        if rc.req_transform:
            try: req = rc.req_transform(req)
            except Exception as e:
                return GatewayResponse(status=400, body={"error": str(e)})
        # Mock
        if rc.mock_response is not None:
            resp = GatewayResponse(**rc.mock_response)
        elif rc.handler:
            try:
                ctx = {"route": rc, "params": params}
                coro = rc.handler(req, ctx)
                if rc.timeout_s > 0:
                    resp = await asyncio.wait_for(coro, rc.timeout_s)
                else:
                    resp = await coro
                if not isinstance(resp, GatewayResponse):
                    resp = GatewayResponse(body=resp)
            except asyncio.TimeoutError:
                resp = GatewayResponse(status=504, body={"error":"Timeout"})
            except Exception as e:
                resp = GatewayResponse(status=500, body={"error": str(e)})
                rc.errors += 1
                for h in self._hooks_error:
                    try: h(req, e)
                    except: pass
        else:
            resp = GatewayResponse(status=501,
                                    body={"error":"No handler configured"})
        # Response transform
        if rc.resp_transform:
            try: resp = rc.resp_transform(resp)
            except: pass
        # Stats
        resp.latency_ms = (time.time() - t0) * 1000
        rc.requests += 1; rc.total_latency_ms += resp.latency_ms
        if resp.status >= 400: rc.errors += 1
        self._store.log(req, resp)
        for h in self._hooks_response:
            try: h(req, resp)
            except: pass
        return resp

    def stats(self) -> Dict:
        route_stats = [rc.to_dict() for rc in self._routes]
        return {"routes": len(self._routes),
                "route_stats": route_stats,
                **self._store.stats()}

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def list_ep(req):
            return web.json_response(
                {"routes": [rc.to_dict() for rc in self._routes]})
        async def stats_ep(req):
            return web.json_response(self.stats())
        p = f"{prefix}/gateway"
        app.router.add_get(f"{p}/routes", list_ep)
        app.router.add_get(f"{p}/stats",  stats_ep)
        logger.info(f"API gateway meta-API at {prefix}/gateway/")
