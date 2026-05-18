"""
OMNI AGENT - Tool Registry
Formal function-calling system with OpenAI-compatible schemas.
Tools are registered with typed parameter specs, validated at call time,
and can be used directly or injected into LLM tool-use payloads.

Features:
- Decorator-based tool registration (@tools.register)
- Typed parameter validation (str, int, float, bool, list, dict)
- OpenAI / Anthropic compatible tool schemas
- Async and sync tool execution
- Tool middleware (auth, logging, rate limiting)
- Tool categories and search
- Usage statistics
- Safe execution sandbox option
"""
import re
import time
import json
import asyncio
import inspect
import logging
import functools
from typing import Any, Callable, Dict, List, Optional, Union, get_type_hints
from dataclasses import dataclass, field
from enum import Enum
from agent.security_audit import AuditCallback, build_memory_audit_callback

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# PARAMETER TYPES
# ══════════════════════════════════════════════════════════════════════════════

class ParamType(str, Enum):
    STRING  = "string"
    INTEGER = "integer"
    NUMBER  = "number"
    BOOLEAN = "boolean"
    ARRAY   = "array"
    OBJECT  = "object"


PYTHON_TO_PARAM_TYPE = {
    str:   ParamType.STRING,
    int:   ParamType.INTEGER,
    float: ParamType.NUMBER,
    bool:  ParamType.BOOLEAN,
    list:  ParamType.ARRAY,
    dict:  ParamType.OBJECT,
}


@dataclass
class ToolParam:
    name: str
    type: ParamType
    description: str
    required: bool = True
    default: Any = None
    enum_values: List[str] = field(default_factory=list)
    example: Any = None

    def to_schema(self) -> Dict:
        schema: Dict[str, Any] = {"type": self.type.value}
        if self.description:
            schema["description"] = self.description
        if self.enum_values:
            schema["enum"] = self.enum_values
        if self.example is not None:
            schema["example"] = self.example
        return schema


# ══════════════════════════════════════════════════════════════════════════════
# TOOL DEFINITION
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ToolCall:
    tool_name: str
    arguments: Dict[str, Any]
    call_id: str = ""
    session_id: str = ""


@dataclass
class ToolResult:
    tool_name: str
    success: bool
    output: Any
    error: str = ""
    latency_ms: float = 0.0
    call_id: str = ""

    def to_dict(self) -> Dict:
        return {
            "tool": self.tool_name,
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "latency_ms": round(self.latency_ms, 1),
        }

    def as_message(self) -> Dict:
        """Format as a tool_result message for the LLM."""
        content = json.dumps(self.output) if not isinstance(self.output, str) else self.output
        if not self.success:
            content = f"Error: {self.error}"
        return {
            "role": "tool",
            "tool_use_id": self.call_id,
            "content": content,
        }


@dataclass
class RegisteredTool:
    name: str
    description: str
    fn: Callable
    params: List[ToolParam]
    category: str = "general"
    tags: List[str] = field(default_factory=list)
    enabled: bool = True
    requires_confirmation: bool = False
    timeout: float = 30.0
    call_count: int = 0
    error_count: int = 0
    total_latency_ms: float = 0.0

    def to_openai_schema(self) -> Dict:
        """OpenAI / Anthropic function-calling schema."""
        properties = {p.name: p.to_schema() for p in self.params}
        required = [p.name for p in self.params if p.required]
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                }
            }
        }

    def to_anthropic_schema(self) -> Dict:
        """Anthropic tool_use format."""
        properties = {p.name: p.to_schema() for p in self.params}
        required = [p.name for p in self.params if p.required]
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": required,
            }
        }

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "params": [p.name for p in self.params],
            "enabled": self.enabled,
            "call_count": self.call_count,
            "error_count": self.error_count,
            "avg_latency_ms": (round(self.total_latency_ms / self.call_count, 1)
                              if self.call_count else 0),
        }

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.call_count if self.call_count else 0.0

    @property
    def success_rate(self) -> float:
        if self.call_count == 0:
            return 1.0
        return 1.0 - (self.error_count / self.call_count)


# ══════════════════════════════════════════════════════════════════════════════
# VALIDATOR
# ══════════════════════════════════════════════════════════════════════════════

class ToolParamValidator:
    """Validates and coerces tool arguments against ToolParam specs."""

    def validate(self, args: Dict, params: List[ToolParam]):
        from typing import Tuple
        coerced = {}
        errors = []

        param_map = {p.name: p for p in params}

        # Check required params
        for p in params:
            if p.required and p.name not in args:
                if p.default is not None:
                    coerced[p.name] = p.default
                else:
                    errors.append(f"Required parameter '{p.name}' missing")
                continue

            val = args.get(p.name, p.default)
            if val is None and not p.required:
                if p.default is not None:
                    coerced[p.name] = p.default
                continue

            try:
                coerced[p.name] = self._coerce(val, p)
            except (ValueError, TypeError) as e:
                errors.append(f"Parameter '{p.name}': {e}")

        # Pass through unknown params
        for k, v in args.items():
            if k not in coerced:
                coerced[k] = v

        return coerced, errors

    def _coerce(self, value: Any, param: ToolParam) -> Any:
        t = param.type
        if t == ParamType.STRING:
            return str(value)
        elif t == ParamType.INTEGER:
            return int(float(str(value)))
        elif t == ParamType.NUMBER:
            return float(str(value))
        elif t == ParamType.BOOLEAN:
            if isinstance(value, bool):
                return value
            s = str(value).lower()
            if s in ("true", "yes", "1"):
                return True
            if s in ("false", "no", "0"):
                return False
            raise ValueError(f"Cannot convert '{value}' to boolean")
        elif t == ParamType.ARRAY:
            if isinstance(value, list):
                return value
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, list):
                        return parsed
                except Exception:
                    pass
                return [v.strip() for v in value.split(",") if v.strip()]
            return list(value)
        elif t == ParamType.OBJECT:
            if isinstance(value, dict):
                return value
            if isinstance(value, str):
                return json.loads(value)
            return dict(value)
        return value



# ══════════════════════════════════════════════════════════════════════════════
# MIDDLEWARE
# ══════════════════════════════════════════════════════════════════════════════

MiddlewareFn = Callable[[ToolCall, "ToolRegistry"], Any]


def parse_confirmation_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "confirmed"}


def confirmation_policy(tool: Optional[RegisteredTool], role: Any,
                        confirmed: bool) -> tuple[bool, str]:
    if tool is None or not tool.requires_confirmation:
        return True, ""

    if not confirmed:
        return False, "Tool requires explicit confirmation"

    role_value = getattr(role, "value", str(role or "")).lower()
    if role_value not in {"admin", "developer"}:
        return False, "Confirmed tool execution requires admin or developer role"

    return True, ""


def logging_middleware(call: ToolCall, registry: "ToolRegistry") -> None:
    """Log every tool invocation."""
    logger.info(f"Tool call: {call.tool_name}({list(call.arguments.keys())})"
               f" [session={call.session_id}]")


def rate_limit_middleware(max_per_minute: int = 60):
    """Factory: returns middleware that rate-limits tool calls."""
    _counts: Dict[str, List[float]] = {}

    def _middleware(call: ToolCall, registry: "ToolRegistry") -> None:
        now = time.time()
        window = 60.0
        key = f"{call.session_id}:{call.tool_name}"
        _counts.setdefault(key, [])
        # Prune old timestamps
        _counts[key] = [t for t in _counts[key] if now - t < window]
        if len(_counts[key]) >= max_per_minute:
            raise RuntimeError(
                f"Rate limit: tool '{call.tool_name}' called too frequently"
            )
        _counts[key].append(now)

    return _middleware


# ══════════════════════════════════════════════════════════════════════════════
# TOOL REGISTRY
# ══════════════════════════════════════════════════════════════════════════════

class ToolRegistry:
    """
    Central registry for all agent tools.

    Register via decorator:
        @tools.register(
            description="Search the web",
            params=[ToolParam("query", ParamType.STRING, "Search query")],
            category="research"
        )
        async def web_search(query: str) -> list:
            ...

    Or programmatically:
        tools.add(RegisteredTool(...))

    Execute:
        result = await tools.call(ToolCall("web_search", {"query": "AI news"}))

    Export schemas for LLM:
        schemas = tools.openai_schemas()
        schemas = tools.anthropic_schemas()
    """

    def __init__(self, audit_callback: Optional[AuditCallback] = None):
        self._tools: Dict[str, RegisteredTool] = {}
        self._middleware: List[MiddlewareFn] = [logging_middleware]
        self._validator = ToolParamValidator()
        self._audit_callback = audit_callback

    def _audit(self, action: str, actor: str, details: Dict[str, Any]) -> None:
        if not self._audit_callback:
            return
        try:
            self._audit_callback(action, actor or "system", details)
        except Exception as exc:
            logger.warning("Security audit callback failed for %s: %s", action, exc)

    # ── Registration ──────────────────────────────────────────────────────────

    def register(
        self,
        description: str,
        params: List[ToolParam] = None,
        category: str = "general",
        tags: List[str] = None,
        timeout: float = 30.0,
        requires_confirmation: bool = False,
        name: str = None,
    ):
        """Decorator factory for registering tool functions."""
        def decorator(fn: Callable) -> Callable:
            tool_name = name or fn.__name__

            # Auto-infer params from type hints if not provided
            inferred_params = params or self._infer_params(fn)

            tool = RegisteredTool(
                name=tool_name,
                description=description,
                fn=fn,
                params=inferred_params,
                category=category,
                tags=tags or [],
                timeout=timeout,
                requires_confirmation=requires_confirmation,
            )
            self._tools[tool_name] = tool
            logger.debug(f"Tool registered: '{tool_name}' ({category})")

            @functools.wraps(fn)
            async def wrapper(*args, **kwargs):
                if asyncio.iscoroutinefunction(fn):
                    return await fn(*args, **kwargs)
                return await asyncio.to_thread(fn, *args, **kwargs)
            return wrapper

        return decorator

    def add(self, tool: RegisteredTool):
        """Register a pre-built RegisteredTool."""
        self._tools[tool.name] = tool

    def _infer_params(self, fn: Callable) -> List[ToolParam]:
        """Infer ToolParam specs from Python function signature + type hints."""
        hints = {}
        try:
            hints = get_type_hints(fn)
        except Exception:
            pass

        sig = inspect.signature(fn)
        params = []

        for pname, param in sig.parameters.items():
            if pname in ("self", "ctx"):
                continue
            py_type = hints.get(pname, str)
            param_type = PYTHON_TO_PARAM_TYPE.get(py_type, ParamType.STRING)
            required = param.default is inspect.Parameter.empty
            default = None if required else param.default
            params.append(ToolParam(
                name=pname,
                type=param_type,
                description=pname.replace("_", " "),
                required=required,
                default=default,
            ))
        return params

    # ── Execution ─────────────────────────────────────────────────────────────

    async def call(self, tool_call: ToolCall,
                   skip_middleware: bool = False,
                   allow_confirmed_tools: bool = False) -> ToolResult:
        """Execute a tool call with validation, middleware, and timeout."""
        tool = self._tools.get(tool_call.tool_name)
        actor = tool_call.session_id or "system"
        audit_details = {
            "tool": tool_call.tool_name,
            "session_id": tool_call.session_id,
            "arg_keys": sorted(tool_call.arguments.keys()),
        }
        if not tool:
            self._audit("security.tool_execution_rejected", actor, {
                **audit_details,
                "reason": "tool_not_found",
            })
            return ToolResult(
                tool_name=tool_call.tool_name, success=False,
                output=None,
                error=f"Tool '{tool_call.tool_name}' not found. "
                      f"Available: {list(self._tools.keys())}",
                call_id=tool_call.call_id,
            )

        if not tool.enabled:
            self._audit("security.tool_execution_rejected", actor, {
                **audit_details,
                "reason": "tool_disabled",
            })
            return ToolResult(
                tool_name=tool_call.tool_name, success=False,
                output=None, error="Tool is disabled",
                call_id=tool_call.call_id,
            )

        if tool.requires_confirmation and not allow_confirmed_tools:
            self._audit("security.tool_execution_rejected", actor, {
                **audit_details,
                "reason": "confirmation_required",
            })
            return ToolResult(
                tool_name=tool_call.tool_name,
                success=False,
                output=None,
                error=("Tool requires an explicit confirmation flow and is "
                       "not available for unconfirmed execution"),
                call_id=tool_call.call_id,
            )

        # Run middleware
        if not skip_middleware:
            for mw in self._middleware:
                try:
                    if asyncio.iscoroutinefunction(mw):
                        await mw(tool_call, self)
                    else:
                        mw(tool_call, self)
                except Exception as e:
                    return ToolResult(
                        tool_name=tool_call.tool_name, success=False,
                        output=None, error=f"Middleware rejected: {e}",
                        call_id=tool_call.call_id,
                    )

        # Validate arguments
        coerced_args, errors = self._validator.validate(tool_call.arguments, tool.params)
        if errors:
            self._audit("security.tool_execution_rejected", actor, {
                **audit_details,
                "reason": "validation_failed",
                "errors": errors,
            })
            return ToolResult(
                tool_name=tool_call.tool_name, success=False,
                output=None, error=f"Validation: {'; '.join(errors)}",
                call_id=tool_call.call_id,
            )

        # Execute
        start = time.time()
        tool.call_count += 1
        self._audit("security.tool_execution", actor, {
            **audit_details,
            "requires_confirmation": tool.requires_confirmation,
            "confirmed_execution": bool(tool.requires_confirmation and allow_confirmed_tools),
        })

        try:
            fn = tool.fn
            if asyncio.iscoroutinefunction(fn):
                output = await asyncio.wait_for(
                    fn(**coerced_args), timeout=tool.timeout
                )
            else:
                output = await asyncio.wait_for(
                    asyncio.to_thread(fn, **coerced_args),
                    timeout=tool.timeout,
                )

            latency_ms = (time.time() - start) * 1000
            tool.total_latency_ms += latency_ms
            logger.debug(f"Tool '{tool_call.tool_name}' OK ({latency_ms:.0f}ms)")
            self._audit("security.tool_execution_result", actor, {
                **audit_details,
                "success": True,
                "latency_ms": round(latency_ms, 1),
            })

            return ToolResult(
                tool_name=tool_call.tool_name, success=True,
                output=output, latency_ms=latency_ms,
                call_id=tool_call.call_id,
            )

        except asyncio.TimeoutError:
            tool.error_count += 1
            err = f"Tool '{tool_call.tool_name}' timed out after {tool.timeout}s"
            logger.error(err)
            self._audit("security.tool_execution_result", actor, {
                **audit_details,
                "success": False,
                "error": err,
            })
            return ToolResult(tool_name=tool_call.tool_name, success=False,
                            output=None, error=err, call_id=tool_call.call_id)

        except Exception as e:
            tool.error_count += 1
            err = f"{type(e).__name__}: {e}"
            logger.error(f"Tool '{tool_call.tool_name}' error: {err}")
            self._audit("security.tool_execution_result", actor, {
                **audit_details,
                "success": False,
                "error": err,
            })
            return ToolResult(tool_name=tool_call.tool_name, success=False,
                            output=None, error=err, call_id=tool_call.call_id)

    async def call_batch(self, calls: List[ToolCall],
                         parallel: bool = True) -> List[ToolResult]:
        """Execute multiple tool calls, optionally in parallel."""
        if parallel:
            return list(await asyncio.gather(*[self.call(c) for c in calls]))
        return [await self.call(c) for c in calls]

    # ── LLM Integration ───────────────────────────────────────────────────────

    def parse_llm_tool_calls(self, tool_calls_data: List[Dict],
                              session_id: str = "") -> List[ToolCall]:
        """Parse OpenAI-style tool_calls from LLM response into ToolCall objects."""
        calls = []
        for tc in tool_calls_data:
            fn = tc.get("function", tc)
            name = fn.get("name", "")
            raw_args = fn.get("arguments", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(
                tool_name=name,
                arguments=args,
                call_id=tc.get("id", ""),
                session_id=session_id,
            ))
        return calls

    async def run_llm_tool_calls(self, tool_calls_data: List[Dict],
                                  session_id: str = "") -> List[ToolResult]:
        """Full pipeline: parse → validate → execute all tool calls from LLM."""
        calls = self.parse_llm_tool_calls(tool_calls_data, session_id)
        return await self.call_batch(calls, parallel=True)

    # ── Middleware ────────────────────────────────────────────────────────────

    def use(self, middleware: MiddlewareFn):
        """Add middleware to the execution chain."""
        self._middleware.append(middleware)

    # ── Query ─────────────────────────────────────────────────────────────────

    def get(self, name: str) -> Optional[RegisteredTool]:
        return self._tools.get(name)

    def list_tools(self, category: Optional[str] = None,
                   enabled_only: bool = True) -> List[Dict]:
        tools = list(self._tools.values())
        if category:
            tools = [t for t in tools if t.category == category]
        if enabled_only:
            tools = [t for t in tools if t.enabled]
        return [t.to_dict() for t in sorted(tools, key=lambda t: t.name)]

    def openai_schemas(self, category: Optional[str] = None) -> List[Dict]:
        """Export all enabled tools as OpenAI function-calling schemas."""
        tools = self._tools.values()
        if category:
            tools = [t for t in tools if t.category == category]
        return [t.to_openai_schema() for t in tools if t.enabled]

    def anthropic_schemas(self, category: Optional[str] = None) -> List[Dict]:
        """Export all enabled tools as Anthropic tool schemas."""
        tools = self._tools.values()
        if category:
            tools = [t for t in tools if t.category == category]
        return [t.to_anthropic_schema() for t in tools if t.enabled]

    def enable(self, name: str): 
        if name in self._tools:
            self._tools[name].enabled = True

    def disable(self, name: str):
        if name in self._tools:
            self._tools[name].enabled = False

    def stats(self) -> List[Dict]:
        return [t.to_dict() for t in self._tools.values()]

    def categories(self) -> List[str]:
        return list({t.category for t in self._tools.values()})

    def search(self, query: str) -> List[RegisteredTool]:
        q = query.lower()
        return [
            t for t in self._tools.values()
            if q in t.name.lower() or q in t.description.lower()
            or any(q in tag for tag in t.tags)
        ]


# ══════════════════════════════════════════════════════════════════════════════
# DEFAULT BUILT-IN TOOLS
# ══════════════════════════════════════════════════════════════════════════════

def build_default_tools(agent) -> ToolRegistry:
    """Build and return a ToolRegistry pre-loaded with all standard agent tools."""
    audit_callback = build_memory_audit_callback(agent.memory) if hasattr(agent, "memory") else None
    tools = ToolRegistry(audit_callback=audit_callback)

    @tools.register(
        description="Search the web using SearXNG or DuckDuckGo",
        params=[
            ToolParam("query", ParamType.STRING, "Search query"),
            ToolParam("num_results", ParamType.INTEGER, "Number of results",
                     required=False, default=5),
        ],
        category="research", tags=["web", "search"],
    )
    async def web_search(query: str, num_results: int = 5) -> List[Dict]:
        return await agent.scraper.search(query, num_results=num_results)

    @tools.register(
        description="Fetch and extract content from a URL",
        params=[
            ToolParam("url", ParamType.STRING, "URL to fetch"),
            ToolParam("extract_links", ParamType.BOOLEAN, "Include links",
                     required=False, default=False),
        ],
        category="research", tags=["web", "scrape"],
    )
    async def web_fetch(url: str, extract_links: bool = False) -> Dict:
        result = await agent.scraper.fetch(url)
        if not extract_links:
            result.pop("links", None)
        return result

    @tools.register(
        description="Execute Python code in a sandboxed environment",
        params=[
            ToolParam("code", ParamType.STRING, "Python code to execute"),
            ToolParam("safe_mode", ParamType.BOOLEAN, "Enable sandbox restrictions",
                     required=False, default=True),
        ],
        category="code", tags=["python", "execute"],
        requires_confirmation=True,
    )
    async def execute_python(code: str, safe_mode: bool = True) -> Dict:
        if not safe_mode:
            return {
                "success": False,
                "error": "Unsafe in-process execution is disabled for tool calls",
            }
        if not hasattr(agent, "sandbox"):
            return {"success": False, "error": "Sandbox unavailable"}
        result = await agent.sandbox.run_python(code)
        return result.to_dict()

    @tools.register(
        description="Store a key-value memory",
        params=[
            ToolParam("key", ParamType.STRING, "Memory key"),
            ToolParam("value", ParamType.STRING, "Value to store"),
            ToolParam("category", ParamType.STRING, "Category",
                     required=False, default="agent"),
            ToolParam("importance", ParamType.INTEGER, "Importance 1-10",
                     required=False, default=5),
        ],
        category="memory",
        requires_confirmation=True,
    )
    async def remember(key: str, value: str, category: str = "agent",
                      importance: int = 5) -> str:
        agent.memory.save_memory(key, value, category=category, importance=importance)
        return f"Stored: {key}"

    @tools.register(
        description="Retrieve a value from memory by key",
        params=[ToolParam("key", ParamType.STRING, "Memory key")],
        category="memory",
    )
    async def recall(key: str) -> Any:
        return agent.memory.get_memory(key)

    @tools.register(
        description="Semantic search across all stored memories",
        params=[
            ToolParam("query", ParamType.STRING, "Search query"),
            ToolParam("limit", ParamType.INTEGER, "Max results",
                     required=False, default=5),
        ],
        category="memory",
    )
    async def search_memory(query: str, limit: int = 5) -> List[Dict]:
        return agent.memory.search_memories(query, limit=limit)

    @tools.register(
        description="Analyze text for sentiment, keywords, and entities",
        params=[ToolParam("text", ParamType.STRING, "Text to analyze")],
        category="analysis",
    )
    async def analyze_text(text: str) -> Dict:
        return agent.analyzer.analyze(text)

    @tools.register(
        description="Ingest a document into the RAG vector store",
        params=[
            ToolParam("text", ParamType.STRING, "Document text"),
            ToolParam("title", ParamType.STRING, "Document title",
                     required=False, default="untitled"),
        ],
        category="rag", tags=["document", "ingest"],
        requires_confirmation=True,
    )
    async def rag_ingest(text: str, title: str = "untitled") -> Dict:
        doc = await agent.rag.ingest_text(text, title=title)
        return doc.to_dict()

    @tools.register(
        description="Retrieve relevant context from the RAG knowledge base",
        params=[
            ToolParam("query", ParamType.STRING, "Query to search for"),
            ToolParam("top_k", ParamType.INTEGER, "Number of results",
                     required=False, default=5),
        ],
        category="rag",
    )
    async def rag_search(query: str, top_k: int = 5) -> List[Dict]:
        results = await agent.rag.retrieve(query, top_k=top_k)
        return [r.to_dict() for r in results]

    @tools.register(
        description="Run a named pipeline with a context dict",
        params=[
            ToolParam("name", ParamType.STRING, "Pipeline name"),
            ToolParam("context", ParamType.OBJECT, "Context JSON",
                     required=False, default={}),
        ],
        category="pipeline",
        requires_confirmation=True,
    )
    async def run_pipeline(name: str, context: dict = {}) -> Dict:
        run = await agent.pipeline_executor.run_by_name(name, context)
        return run.to_dict() if run else {"error": f"Pipeline '{name}' not found"}

    @tools.register(
        description="Run the improved ADR tanker Switzerland job search and export reports",
        params=[
            ToolParam(
                "export_format",
                ParamType.STRING,
                "Export format",
                required=False,
                default="html",
                enum_values=["json", "csv", "html", "all"],
            ),
            ToolParam(
                "verbose",
                ParamType.BOOLEAN,
                "Enable detailed console logging",
                required=False,
                default=False,
            ),
            ToolParam(
                "output_dir",
                ParamType.STRING,
                "Optional custom output directory",
                required=False,
                default="",
            ),
        ],
        category="automation",
        tags=["jobs", "search", "adr", "switzerland"],
        timeout=900.0,
        requires_confirmation=True,
        name="run_job_search_tank_adr_improved",
    )
    async def run_job_search_tank_adr_improved(
        export_format: str = "html",
        verbose: bool = False,
        output_dir: str = "",
    ) -> Dict:
        from job_search_tank_adr_improved import run_search_with_summary

        export_format = (export_format or "html").strip().lower()
        if export_format not in {"json", "csv", "html", "all"}:
            raise ValueError("export_format must be one of: json, csv, html, all")

        return await run_search_with_summary(
            export_format=export_format,
            verbose=verbose,
            output_dir=output_dir or None,
        )

    @tools.register(
        description="Get current system status and health",
        params=[],
        category="system",
    )
    async def system_status() -> Dict:
        return {
            "models": len(agent.llm.router.list_all_models()),
            "memory_sessions": len(agent.memory.list_sessions()),
            "cache_backend": agent.cache.backend,
            "rag_docs": agent.rag.stats()["documents"],
            "pipelines": len(agent.pipeline_executor.list_pipelines()),
        }

    return tools
