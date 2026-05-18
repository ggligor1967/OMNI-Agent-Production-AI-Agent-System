"""OMNI AGENT - LLM Router
Multi-provider LLM routing: register providers, select by strategy,
enforce rate limits, cache responses, and track cost + latency.

Features:
- Provider registry: name, model, base_url, api_key_env, cost per token
- Routing strategies: round-robin, least-latency, cost-optimized, random
- Fallback chain: ordered list of providers to try on failure
- Response caching: hash(prompt+model) → cached response with TTL
- Rate limiting: per-provider RPM and TPM caps with sliding window
- Latency tracking: EWMA p50 per provider, auto-deprioritise slow ones
- Cost tracking: input+output tokens × price per 1M, per-session totals
- Health: mark provider degraded/down after N consecutive errors
- Dry-run mode: return mock response without calling provider
- Request tagging: attach metadata to each request for analytics
- SQLite persistence: request log, cost summaries
- REST API: route, providers, stats, cost-report
"""
import asyncio, hashlib, json, os, sqlite3, time, uuid, logging, random
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

def _hash_prompt(model: str, messages: List[Dict]) -> str:
    key = model + json.dumps(messages, sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()[:16]

def _ewma(old: float, new: float, alpha: float = 0.15) -> float:
    return alpha * new + (1 - alpha) * old

@dataclass
class ProviderSpec:
    id: str; name: str
    model: str; base_url: str = ""
    api_key_env: str = ""
    input_cost_per_1m: float  = 1.0   # USD
    output_cost_per_1m: float = 3.0
    rpm_limit: int  = 60
    tpm_limit: int  = 100_000
    priority: int   = 5
    tags: List[str] = field(default_factory=list)
    # Runtime
    latency_p50: float = 200.0   # ms
    total_requests: int = 0
    total_errors: int   = 0
    total_input_tokens:  int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    consecutive_errors: int = 0
    healthy: bool = True
    _rpm_window: List[float] = field(default_factory=list)
    _tpm_window: List[Tuple[float, int]] = field(default_factory=list)

    @property
    def error_rate(self):
        return self.total_errors / max(1, self.total_requests)

    def within_rate_limits(self, tokens: int = 0) -> bool:
        now = time.time()
        self._rpm_window = [t for t in self._rpm_window if now - t < 60]
        self._tpm_window = [(t, tk) for t, tk in self._tpm_window if now - t < 60]
        rpm_ok = len(self._rpm_window) < self.rpm_limit
        tpm_ok = sum(tk for _, tk in self._tpm_window) + tokens <= self.tpm_limit
        return rpm_ok and tpm_ok

    def record_request(self, tokens_in: int, tokens_out: int,
                        latency_ms: float, error: bool):
        now = time.time()
        self._rpm_window.append(now)
        self._tpm_window.append((now, tokens_in))
        self.total_requests += 1
        if error:
            self.total_errors += 1; self.consecutive_errors += 1
            if self.consecutive_errors >= 5: self.healthy = False
        else:
            self.consecutive_errors = 0
            self.latency_p50 = _ewma(self.latency_p50, latency_ms)
            cost = (tokens_in * self.input_cost_per_1m +
                    tokens_out * self.output_cost_per_1m) / 1_000_000
            self.total_cost_usd += cost
            self.total_input_tokens  += tokens_in
            self.total_output_tokens += tokens_out

    def to_dict(self):
        return {"id": self.id, "name": self.name, "model": self.model,
                "healthy": self.healthy, "latency_p50": round(self.latency_p50, 1),
                "error_rate": round(self.error_rate, 4),
                "total_requests": self.total_requests,
                "total_cost_usd": round(self.total_cost_usd, 6),
                "priority": self.priority, "tags": self.tags}

@dataclass
class RouteRequest:
    messages: List[Dict]; max_tokens: int = 1024
    temperature: float = 0.7; stream: bool = False
    tags: Dict = field(default_factory=dict)
    session_id: str = ""; cache_ttl: float = 0.0

@dataclass
class RouteResponse:
    provider: str; model: str
    content: str; input_tokens: int; output_tokens: int
    latency_ms: float; cost_usd: float; cached: bool = False
    request_id: str = field(default_factory=lambda: str(uuid.uuid4())[:10])

    def to_dict(self):
        return {"provider": self.provider, "model": self.model,
                "content": self.content[:200],
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "latency_ms": round(self.latency_ms, 1),
                "cost_usd": round(self.cost_usd, 8),
                "cached": self.cached, "request_id": self.request_id}

class LLMStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS requests(
                    id TEXT PRIMARY KEY, provider TEXT, model TEXT,
                    input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
                    latency_ms REAL DEFAULT 0, cost_usd REAL DEFAULT 0,
                    cached INTEGER DEFAULT 0, error TEXT DEFAULT '',
                    session_id TEXT DEFAULT '', created_at REAL);
                CREATE TABLE IF NOT EXISTS cache(
                    hash TEXT PRIMARY KEY, content TEXT,
                    input_tokens INTEGER, output_tokens INTEGER,
                    expire_at REAL, created_at REAL);
                CREATE INDEX IF NOT EXISTS idx_req_prov ON requests(provider, created_at DESC);
            """)

    def log(self, resp: RouteResponse, error: str = "", session_id: str = ""):
        with self._conn() as c:
            c.execute("INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (resp.request_id, resp.provider, resp.model,
                 resp.input_tokens, resp.output_tokens,
                 resp.latency_ms, resp.cost_usd, int(resp.cached),
                 error, session_id, time.time()))

    def cache_get(self, h: str) -> Optional[Dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM cache WHERE hash=? AND expire_at>?",
                (h, time.time())).fetchone()
        return dict(row) if row else None

    def cache_set(self, h: str, content: str,
                  in_tok: int, out_tok: int, ttl: float):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?,?,?,?)",
                (h, content, in_tok, out_tok, time.time()+ttl, time.time()))

    def cost_report(self, provider: str = None) -> Dict:
        with self._conn() as c:
            if provider:
                row = c.execute(
                    "SELECT SUM(cost_usd) tot, SUM(input_tokens) ti, "
                    "SUM(output_tokens) to2, COUNT(*) n "
                    "FROM requests WHERE provider=?", (provider,)).fetchone()
            else:
                row = c.execute(
                    "SELECT SUM(cost_usd) tot, SUM(input_tokens) ti, "
                    "SUM(output_tokens) to2, COUNT(*) n FROM requests").fetchone()
        return {"total_cost": round(row[0] or 0, 6),
                "input_tokens": row[1] or 0, "output_tokens": row[2] or 0,
                "requests": row[3] or 0}

    def stats(self):
        with self._conn() as c:
            n = c.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
            nc = c.execute(
                "SELECT COUNT(*) FROM cache WHERE expire_at>?",
                (time.time(),)).fetchone()[0]
        return {"total_requests": n, "cache_entries": nc}

class LLMRouter:
    """
    Multi-provider LLM router with caching, cost tracking, and fallback.

    Usage:
        router = LLMRouter()
        router.register("openai-gpt4o", model="gpt-4o",
                         input_cost_per_1m=5.0, output_cost_per_1m=15.0)
        router.register("anthropic-sonnet", model="claude-sonnet-4-6",
                         input_cost_per_1m=3.0, output_cost_per_1m=15.0)
        router.set_fallback(["openai-gpt4o", "anthropic-sonnet"])

        # Provide a call_fn to actually invoke the LLM
        async def my_call(provider, request):
            # ... call the API ...
            return RouteResponse(...)

        router.set_call_fn(my_call)
        response = await router.route(RouteRequest(messages=[...]))
    """
    def __init__(self, db_path: str = "data/llm_router.db",
                 strategy: str = "round_robin",
                 dry_run: bool = False):
        self._store = LLMStore(db_path)
        self._providers: Dict[str, ProviderSpec] = {}
        self._strategy = strategy
        self._dry_run = dry_run
        self._fallback_chain: List[str] = []
        self._call_fn: Optional[Callable] = None
        self._rr_index = 0
        self._session_costs: Dict[str, float] = {}

    def register(self, name: str, model: str = "",
                  base_url: str = "", api_key_env: str = "",
                  input_cost_per_1m: float = 1.0,
                  output_cost_per_1m: float = 3.0,
                  rpm_limit: int = 60, tpm_limit: int = 100_000,
                  priority: int = 5, tags: List[str] = None) -> ProviderSpec:
        spec = ProviderSpec(id=str(uuid.uuid4())[:8], name=name,
                             model=model or name, base_url=base_url,
                             api_key_env=api_key_env,
                             input_cost_per_1m=input_cost_per_1m,
                             output_cost_per_1m=output_cost_per_1m,
                             rpm_limit=rpm_limit, tpm_limit=tpm_limit,
                             priority=priority, tags=tags or [])
        self._providers[name] = spec
        if not self._fallback_chain:
            self._fallback_chain = [name]
        logger.info(f"LLM provider registered: {name!r}")
        return spec

    def set_fallback(self, chain: List[str]):
        self._fallback_chain = chain

    def set_call_fn(self, fn: Callable):
        self._call_fn = fn

    def set_strategy(self, strategy: str):
        self._strategy = strategy

    def _select_provider(self, req: RouteRequest,
                          exclude: List[str] = None) -> Optional[ProviderSpec]:
        exclude = exclude or []
        candidates = [p for p in self._providers.values()
                       if p.healthy and p.name not in exclude
                       and p.within_rate_limits()]
        if not candidates: return None
        if self._strategy == "round_robin":
            idx = self._rr_index % len(candidates)
            self._rr_index += 1
            return candidates[idx]
        if self._strategy == "least_latency":
            return min(candidates, key=lambda p: p.latency_p50)
        if self._strategy == "cost_optimized":
            return min(candidates,
                        key=lambda p: p.input_cost_per_1m + p.output_cost_per_1m)
        if self._strategy == "priority":
            return min(candidates, key=lambda p: p.priority)
        if self._strategy == "random":
            return random.choice(candidates)
        return candidates[0]

    async def route(self, req: RouteRequest,
                     provider_name: str = None) -> RouteResponse:
        # Check cache first
        _cache_key = _hash_prompt("", req.messages)  # stable, provider-agnostic
        if req.cache_ttl > 0:
            h = _cache_key
            cached = self._store.cache_get(h)
            if cached:
                return RouteResponse(
                    provider="cache", model="cached",
                    content=cached["content"],
                    input_tokens=cached["input_tokens"],
                    output_tokens=cached["output_tokens"],
                    latency_ms=0.0, cost_usd=0.0, cached=True)

        if self._dry_run:
            # Return mock without calling provider
            if provider_name:
                prov = provider_name
            else:
                selected = self._select_provider(req)
                prov = selected.name if selected else (
                    self._fallback_chain[0] if self._fallback_chain else "mock")
            spec = self._providers.get(prov)
            dry_resp = RouteResponse(provider=prov,
                                      model=spec.model if spec else "mock",
                                      content="[dry-run mock response]",
                                      input_tokens=10, output_tokens=20,
                                      latency_ms=1.0, cost_usd=0.0)
            if req.cache_ttl > 0:
                self._store.cache_set(_cache_key, dry_resp.content, 10, 20, req.cache_ttl)
            return dry_resp

        # Determine provider order
        if provider_name:
            order = [provider_name] + [n for n in self._fallback_chain
                                        if n != provider_name]
        else:
            selected = self._select_provider(req)
            order = ([selected.name] if selected else []) + [
                n for n in self._fallback_chain
                if n != (selected.name if selected else "")]

        last_err = None
        for pname in order:
            spec = self._providers.get(pname)
            if not spec or not spec.healthy: continue
            start = time.time()
            try:
                if self._call_fn:
                    resp = await self._call_fn(spec, req)
                else:
                    # No call_fn set: simulate
                    await asyncio.sleep(0.01)
                    resp = RouteResponse(provider=pname, model=spec.model,
                                          content="[no call_fn set]",
                                          input_tokens=len(str(req.messages))//4,
                                          output_tokens=50,
                                          latency_ms=10.0, cost_usd=0.0)
                ms = (time.time() - start) * 1000
                resp.latency_ms = ms
                cost = (resp.input_tokens  * spec.input_cost_per_1m +
                        resp.output_tokens * spec.output_cost_per_1m) / 1_000_000
                resp.cost_usd = cost
                spec.record_request(resp.input_tokens, resp.output_tokens, ms, False)

                if req.session_id:
                    self._session_costs[req.session_id] = (
                        self._session_costs.get(req.session_id, 0) + cost)

                if req.cache_ttl > 0:
                    self._store.cache_set(_cache_key, resp.content,
                                          resp.input_tokens, resp.output_tokens,
                                          req.cache_ttl)
                self._store.log(resp, session_id=req.session_id)
                return resp

            except Exception as e:
                ms = (time.time() - start) * 1000
                spec.record_request(0, 0, ms, True)
                last_err = str(e)
                logger.warning(f"Provider {pname!r} failed: {e}")

        raise RuntimeError(f"All providers failed. Last: {last_err}")

    def providers(self, healthy_only: bool = False) -> List[ProviderSpec]:
        ps = list(self._providers.values())
        return [p for p in ps if p.healthy] if healthy_only else ps

    def cost_report(self, provider: str = None) -> Dict:
        return self._store.cost_report(provider)

    def session_cost(self, session_id: str) -> float:
        return round(self._session_costs.get(session_id, 0.0), 8)

    def reset_provider(self, name: str):
        spec = self._providers.get(name)
        if spec: spec.healthy = True; spec.consecutive_errors = 0

    def stats(self) -> Dict:
        s = self._store.stats()
        s["providers"] = len(self._providers)
        s["healthy"] = sum(1 for p in self._providers.values() if p.healthy)
        s["strategy"] = self._strategy
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def route_ep(req):
            d = await req.json()
            rr = RouteRequest(messages=d["messages"],
                               max_tokens=int(d.get("max_tokens",1024)),
                               cache_ttl=float(d.get("cache_ttl",0)),
                               session_id=d.get("session_id",""))
            resp = await self.route(rr, d.get("provider"))
            return web.json_response(resp.to_dict())
        async def providers_ep(req):
            return web.json_response(
                {"providers": [p.to_dict() for p in self.providers()]})
        async def cost_ep(req):
            prov = req.rel_url.query.get("provider")
            return web.json_response(self.cost_report(prov))
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/llm"
        app.router.add_post(f"{p}/route",     route_ep)
        app.router.add_get( f"{p}/providers", providers_ep)
        app.router.add_get( f"{p}/cost",      cost_ep)
        app.router.add_get( f"{p}/stats",     stats_ep)
        logger.info(f"LLM router API at {prefix}/llm/")
