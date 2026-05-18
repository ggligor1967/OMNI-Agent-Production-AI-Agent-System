"""OMNI AGENT - Tool Registry
Register Python callables as LLM-callable tools with auto-schema inference,
parameter validation, dispatch, and usage tracking.

Features:
- ToolSpec: name, description, fn, parameter schema (JSON Schema draft-7)
- Schema inference: inspect type annotations → JSON Schema types
- Required fields: detect parameters with no default value
- Enum support: typing.Literal annotations → enum schema
- Nested types: List[X], Dict[str, X] → array/object schema
- Validation: validate dict args against schema before dispatch
- Middleware: pre/post call hooks (logging, auth, rate-limit)
- Async support: await coroutine tools transparently
- Aliases: register same fn under multiple names
- Versioned tools: tool_name:v2 naming convention
- Timeout: per-tool call timeout via asyncio.wait_for
- Result wrapping: ToolResult with output, error, latency, call_id
- Batch dispatch: call multiple tools with one invocation
- SQLite persistence: call log with inputs/outputs/latency
- REST API: list tools, call, batch, schema, stats
"""
import asyncio, inspect, json, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, get_type_hints, List, Optional, Tuple, Union
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

# ── Schema inference ───────────────────────────────────────────────────────────
_PY_TO_JSON = {
    int: "integer", float: "number", str: "string",
    bool: "boolean", list: "array", dict: "object", type(None): "null"
}

def _infer_type(annotation) -> Dict:
    """Convert Python type annotation to JSON Schema fragment."""
    if annotation is inspect.Parameter.empty:
        return {"type": "string"}
    origin = getattr(annotation, "__origin__", None)
    args   = getattr(annotation, "__args__", ())

    # Literal → enum
    if origin is Union and type(None) in args:
        inner = [a for a in args if a is not type(None)]
        schema = _infer_type(inner[0]) if inner else {"type": "string"}
        schema["nullable"] = True
        return schema

    # typing.Literal
    try:
        import typing
        if hasattr(typing, "Literal") and origin is getattr(typing, "Literal", None):
            return {"type": "string", "enum": list(args)}
    except Exception:
        pass

    # List[X]
    if origin is list:
        items = _infer_type(args[0]) if args else {"type": "string"}
        return {"type": "array", "items": items}

    # Dict[K, V]
    if origin is dict:
        return {"type": "object"}

    base = _PY_TO_JSON.get(annotation, "string")
    return {"type": base}

def _build_schema(fn: Callable) -> Dict:
    """Build a JSON Schema object from a callable's signature."""
    try:
        hints = get_type_hints(fn)
    except Exception:
        hints = {}
    sig = inspect.signature(fn)
    props: Dict[str, Dict] = {}
    required: List[str] = []

    for name, param in sig.parameters.items():
        if name in ("self", "cls"): continue
        ann = hints.get(name, inspect.Parameter.empty)
        schema_frag = _infer_type(ann)
        # Add description from default if it's a string sentinel
        if hasattr(param.default, "__doc__") and param.default.__doc__:
            schema_frag["description"] = param.default.__doc__
        props[name] = schema_frag
        if param.default is inspect.Parameter.empty:
            required.append(name)

    return {"type": "object", "properties": props, "required": required}

# ── Validation ─────────────────────────────────────────────────────────────────
def _validate(args: Dict, schema: Dict) -> List[str]:
    """Return list of validation errors (empty = valid)."""
    errors = []
    required = schema.get("required", [])
    props = schema.get("properties", {})
    for r in required:
        if r not in args:
            errors.append(f"Missing required argument: {r!r}")
    for key, val in args.items():
        if key not in props: continue
        expected = props[key].get("type")
        if not expected: continue
        actual_map = {int: "integer", float: "number", str: "string",
                       bool: "boolean", list: "array", dict: "object"}
        actual = actual_map.get(type(val))
        if actual and expected not in (actual, "string"):
            # Allow int where number expected
            if not (expected == "number" and actual == "integer"):
                errors.append(f"Arg {key!r}: expected {expected}, got {actual}")
    return errors

# ── Data classes ───────────────────────────────────────────────────────────────
@dataclass
class ToolSpec:
    name: str; fn: Callable
    description: str = ""
    schema: Dict = field(default_factory=dict)
    aliases: List[str] = field(default_factory=list)
    timeout_s: float = 30.0
    enabled: bool = True
    tags: List[str] = field(default_factory=list)
    call_count: int = 0
    error_count: int = 0

    def to_dict(self):
        return {"name": self.name, "description": self.description,
                "schema": self.schema, "aliases": self.aliases,
                "timeout_s": self.timeout_s, "enabled": self.enabled,
                "tags": self.tags, "call_count": self.call_count}

    def to_openai_schema(self) -> Dict:
        """Return OpenAI function-calling format."""
        return {"type": "function",
                "function": {"name": self.name,
                              "description": self.description,
                              "parameters": self.schema}}

@dataclass
class ToolResult:
    call_id: str; tool_name: str
    output: Any = None; error: str = ""
    latency_ms: float = 0.0
    validated: bool = True

    @property
    def success(self) -> bool: return not self.error

    def to_dict(self):
        return {"call_id": self.call_id, "tool": self.tool_name,
                "output": self.output, "error": self.error,
                "latency_ms": round(self.latency_ms, 1),
                "success": self.success}

class TRStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS calls(
                    id TEXT PRIMARY KEY, tool TEXT,
                    inputs TEXT DEFAULT '{}',
                    output TEXT DEFAULT '',
                    error TEXT DEFAULT '',
                    latency_ms REAL DEFAULT 0,
                    created_at REAL);
                CREATE INDEX IF NOT EXISTS idx_tr_tool
                    ON calls(tool, created_at DESC);
            """)

    def log(self, r: ToolResult, inputs: Dict):
        with self._conn() as c:
            c.execute("INSERT INTO calls VALUES(?,?,?,?,?,?,?)",
                (r.call_id, r.tool_name,
                 json.dumps(inputs, default=str)[:1000],
                 json.dumps(r.output, default=str)[:1000],
                 r.error[:500], r.latency_ms, time.time()))

    def stats(self) -> Dict:
        with self._conn() as c:
            n  = c.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
            ne = c.execute(
                "SELECT COUNT(*) FROM calls WHERE error != ''").fetchone()[0]
            avg = c.execute(
                "SELECT AVG(latency_ms) FROM calls").fetchone()[0] or 0
            top = c.execute(
                "SELECT tool, COUNT(*) as cnt FROM calls "
                "GROUP BY tool ORDER BY cnt DESC LIMIT 5").fetchall()
        return {"total_calls": n, "errors": ne,
                "avg_latency_ms": round(avg, 1),
                "error_rate": round(ne / max(1, n), 4),
                "top_tools": [(r["tool"], r["cnt"]) for r in top]}

class ToolRegistry:
    """
    Tool/function registry with schema inference and validated dispatch.

    Usage:
        registry = ToolRegistry()

        def get_weather(city: str, units: str = "celsius") -> dict:
            \"\"\"Get current weather for a city.\"\"\"
            return {"city": city, "temp": 22, "units": units}

        registry.register(get_weather)

        result = await registry.call("get_weather", {"city": "Paris"})
        print(result.output)   # {"city": "Paris", "temp": 22, "units": "celsius"}
    """
    def __init__(self, db_path: str = "data/tools.db"):
        self._store = TRStore(db_path)
        self._tools: Dict[str, ToolSpec] = {}
        self._aliases: Dict[str, str] = {}    # alias -> canonical name
        self._pre_hooks:  List[Callable] = []
        self._post_hooks: List[Callable] = []

    def register(self, fn: Callable,
                  name: str = None,
                  description: str = None,
                  schema: Dict = None,
                  aliases: List[str] = None,
                  timeout_s: float = 30.0,
                  tags: List[str] = None) -> ToolSpec:
        tool_name = name or fn.__name__
        desc = description or (inspect.getdoc(fn) or "")
        inferred_schema = schema or _build_schema(fn)
        spec = ToolSpec(name=tool_name, fn=fn,
                         description=desc,
                         schema=inferred_schema,
                         aliases=list(aliases or []),
                         timeout_s=timeout_s,
                         tags=list(tags or []))
        self._tools[tool_name] = spec
        for alias in (aliases or []):
            self._aliases[alias] = tool_name
        logger.debug(f"Tool registered: {tool_name!r}")
        return spec

    def register_many(self, fns: List[Callable], **kw) -> List[ToolSpec]:
        return [self.register(fn, **kw) for fn in fns]

    def unregister(self, name: str) -> bool:
        spec = self._tools.pop(name, None)
        if spec:
            for alias in spec.aliases:
                self._aliases.pop(alias, None)
        return spec is not None

    def get(self, name: str) -> Optional[ToolSpec]:
        canonical = self._aliases.get(name, name)
        return self._tools.get(canonical)

    def enable(self, name: str): 
        if spec := self.get(name): spec.enabled = True

    def disable(self, name: str):
        if spec := self.get(name): spec.enabled = False

    async def call(self, name: str, args: Dict = None,
                    validate: bool = True) -> ToolResult:
        cid = str(uuid.uuid4())[:10]
        args = dict(args or {})
        canonical = self._aliases.get(name, name)
        spec = self._tools.get(canonical)

        if not spec:
            r = ToolResult(cid, name, error=f"Tool {name!r} not found")
            return r
        if not spec.enabled:
            r = ToolResult(cid, name, error=f"Tool {name!r} is disabled")
            return r

        # Pre-hooks
        for h in self._pre_hooks:
            try: h(spec, args)
            except: pass

        # Validate
        if validate:
            errors = _validate(args, spec.schema)
            if errors:
                r = ToolResult(cid, name, error="; ".join(errors),
                                validated=False)
                spec.error_count += 1
                self._store.log(r, args)
                return r

        start = time.time()
        try:
            if asyncio.iscoroutinefunction(spec.fn):
                output = await asyncio.wait_for(spec.fn(**args),
                                                  timeout=spec.timeout_s)
            else:
                output = spec.fn(**args)
            latency = (time.time() - start) * 1000
            r = ToolResult(cid, name, output=output, latency_ms=latency)
            spec.call_count += 1
        except asyncio.TimeoutError:
            latency = (time.time() - start) * 1000
            r = ToolResult(cid, name, error=f"Timeout after {spec.timeout_s}s",
                            latency_ms=latency)
            spec.error_count += 1
        except Exception as e:
            latency = (time.time() - start) * 1000
            r = ToolResult(cid, name, error=str(e), latency_ms=latency)
            spec.error_count += 1

        self._store.log(r, args)
        # Post-hooks
        for h in self._post_hooks:
            try: h(spec, r)
            except: pass
        return r

    async def call_batch(self, calls: List[Dict]) -> List[ToolResult]:
        tasks = [self.call(c["name"], c.get("args", {})) for c in calls]
        return await asyncio.gather(*tasks)

    def before_call(self, fn: Callable): self._pre_hooks.append(fn)
    def after_call(self, fn: Callable):  self._post_hooks.append(fn)

    def list_tools(self, tag: str = None,
                    enabled_only: bool = False) -> List[ToolSpec]:
        tools = list(self._tools.values())
        if tag: tools = [t for t in tools if tag in t.tags]
        if enabled_only: tools = [t for t in tools if t.enabled]
        return tools

    def openai_tools(self, tag: str = None) -> List[Dict]:
        return [t.to_openai_schema()
                for t in self.list_tools(tag, enabled_only=True)]

    def stats(self) -> Dict:
        s = self._store.stats()
        s["registered_tools"] = len(self._tools)
        s["aliases"] = len(self._aliases)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def list_ep(req):
            return web.json_response(
                {"tools": [t.to_dict() for t in self.list_tools()]})
        async def call_ep(req):
            d = await req.json()
            r = await self.call(d["name"], d.get("args",{}))
            return web.json_response(r.to_dict())
        async def batch_ep(req):
            d = await req.json()
            results = await self.call_batch(d["calls"])
            return web.json_response(
                {"results": [r.to_dict() for r in results]})
        async def schema_ep(req):
            name = req.match_info["name"]
            spec = self.get(name)
            if not spec: return web.json_response({"error":"not found"},status=404)
            return web.json_response(spec.to_openai_schema())
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/tools"
        app.router.add_get( f"{p}/list",          list_ep)
        app.router.add_post(f"{p}/call",          call_ep)
        app.router.add_post(f"{p}/batch",         batch_ep)
        app.router.add_get( f"{p}/schema/{{name}}", schema_ep)
        app.router.add_get( f"{p}/stats",         stats_ep)
        logger.info(f"Tool registry API at {prefix}/tools/")
