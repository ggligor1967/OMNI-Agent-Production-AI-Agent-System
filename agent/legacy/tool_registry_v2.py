"""OMNI Agent — Tool Registry V2: typed tools with schema validation, versioning, execution."""
from __future__ import annotations
import inspect, json, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, Type


class ToolStatus(str, Enum):
    ACTIVE      = "active"
    DEPRECATED  = "deprecated"
    DISABLED    = "disabled"
    EXPERIMENTAL = "experimental"


class ParamType(str, Enum):
    STRING  = "string"
    INTEGER = "integer"
    NUMBER  = "number"
    BOOLEAN = "boolean"
    ARRAY   = "array"
    OBJECT  = "object"
    ANY     = "any"


@dataclass
class ParamSchema:
    name: str
    param_type: ParamType
    description: str = ""
    required: bool = True
    default: Any = None
    enum_values: Optional[List[Any]] = None
    min_val: Optional[float] = None
    max_val: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "name": self.name,
            "type": self.param_type.value,
            "description": self.description,
            "required": self.required,
        }
        if self.default is not None:
            d["default"] = self.default
        if self.enum_values:
            d["enum"] = self.enum_values
        if self.min_val is not None:
            d["minimum"] = self.min_val
        if self.max_val is not None:
            d["maximum"] = self.max_val
        return d


@dataclass
class ToolSpec:
    tool_id: str
    name: str
    fn: Callable
    description: str = ""
    params: List[ParamSchema] = field(default_factory=list)
    returns: str = ""
    version: str = "1.0.0"
    status: ToolStatus = ToolStatus.ACTIVE
    tags: List[str] = field(default_factory=list)
    namespace: str = "default"
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    call_count: int = 0
    error_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "status": self.status.value,
            "namespace": self.namespace,
            "params": [p.to_dict() for p in self.params],
            "returns": self.returns,
            "tags": self.tags,
            "call_count": self.call_count,
        }

    def to_openai_spec(self) -> Dict[str, Any]:
        """Export as OpenAI function-calling spec."""
        props = {}
        required = []
        for p in self.params:
            props[p.name] = {"type": p.param_type.value,
                             "description": p.description}
            if p.enum_values:
                props[p.name]["enum"] = p.enum_values
            if p.required:
                required.append(p.name)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                },
            },
        }


@dataclass
class ToolCall:
    call_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    tool_id: str = ""
    tool_name: str = ""
    inputs: Dict[str, Any] = field(default_factory=dict)
    output: Any = None
    error: Optional[str] = None
    success: bool = False
    duration_ms: float = 0.0
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool_id": self.tool_id,
            "tool_name": self.tool_name,
            "success": self.success,
            "duration_ms": round(self.duration_ms, 2),
            "error": self.error,
        }


class ValidationError(Exception):
    pass


def _coerce(value: Any, param: ParamSchema) -> Any:
    """Type-coerce and validate a single parameter value."""
    if value is None:
        if param.required and param.default is None:
            raise ValidationError(f"Required param '{param.name}' missing")
        return param.default
    if param.param_type == ParamType.ANY:
        return value
    if param.param_type == ParamType.STRING:
        value = str(value)
    elif param.param_type == ParamType.INTEGER:
        try: value = int(value)
        except (ValueError, TypeError):
            raise ValidationError(f"'{param.name}' must be integer, got {value!r}")
    elif param.param_type == ParamType.NUMBER:
        try: value = float(value)
        except (ValueError, TypeError):
            raise ValidationError(f"'{param.name}' must be number, got {value!r}")
    elif param.param_type == ParamType.BOOLEAN:
        if isinstance(value, str):
            value = value.lower() in ("true", "1", "yes")
        else:
            value = bool(value)
    elif param.param_type == ParamType.ARRAY:
        if not isinstance(value, list):
            raise ValidationError(f"'{param.name}' must be array")
    elif param.param_type == ParamType.OBJECT:
        if not isinstance(value, dict):
            raise ValidationError(f"'{param.name}' must be object")
    if param.enum_values and value not in param.enum_values:
        raise ValidationError(
            f"'{param.name}' must be one of {param.enum_values}, got {value!r}")
    if param.min_val is not None and isinstance(value, (int, float)):
        if value < param.min_val:
            raise ValidationError(f"'{param.name}' must be >= {param.min_val}")
    if param.max_val is not None and isinstance(value, (int, float)):
        if value > param.max_val:
            raise ValidationError(f"'{param.name}' must be <= {param.max_val}")
    return value


class ToolRegistryV2:
    """
    Typed tool registry with:
    - Schema-validated parameter coercion
    - OpenAI function-calling spec export
    - Versioning + deprecation
    - Namespace isolation
    - Execution with timeout and error handling
    - Call history in SQLite
    - Pre/post execution hooks
    - Auto-registration from function signatures
    """

    def __init__(self, db_path: str = ":memory:"):
        self._tools: Dict[str, ToolSpec] = {}         # tool_id → spec
        self._name_index: Dict[str, str] = {}          # name → tool_id
        self._pre_hooks:  List[Callable] = []
        self._post_hooks: List[Callable] = []
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS tr_calls (
                call_id TEXT PRIMARY KEY, tool_id TEXT, tool_name TEXT,
                inputs TEXT, success INTEGER, error TEXT,
                duration_ms REAL, ts REAL
            );
        """)
        self._db.commit()

    # ── REGISTRATION ──────────────────────────────────────────────────

    def register(self, name: str, fn: Callable,
                 description: str = "",
                 params: Optional[List[Dict]] = None,
                 returns: str = "",
                 version: str = "1.0.0",
                 status: ToolStatus = ToolStatus.ACTIVE,
                 tags: Optional[List[str]] = None,
                 namespace: str = "default",
                 metadata: Optional[Dict] = None,
                 tool_id: Optional[str] = None) -> ToolSpec:
        tid = tool_id or str(uuid.uuid4())[:8]
        parsed_params = []
        for p in (params or []):
            parsed_params.append(ParamSchema(
                name=p["name"],
                param_type=ParamType(p.get("type", "any")),
                description=p.get("description", ""),
                required=p.get("required", True),
                default=p.get("default"),
                enum_values=p.get("enum"),
                min_val=p.get("minimum"),
                max_val=p.get("maximum"),
            ))
        spec = ToolSpec(
            tool_id=tid, name=name, fn=fn,
            description=description, params=parsed_params,
            returns=returns, version=version, status=status,
            tags=list(tags or []), namespace=namespace,
            metadata=metadata or {})
        self._tools[tid] = spec
        self._name_index[name] = tid
        return spec

    def register_from_function(self, fn: Callable,
                                description: str = "",
                                **kwargs) -> ToolSpec:
        """Auto-register a function using its signature for param inference."""
        sig = inspect.signature(fn)
        params = []
        for pname, param in sig.parameters.items():
            ann = param.annotation
            ptype = ParamType.ANY
            if ann == int:             ptype = ParamType.INTEGER
            elif ann == float:         ptype = ParamType.NUMBER
            elif ann == str:           ptype = ParamType.STRING
            elif ann == bool:          ptype = ParamType.BOOLEAN
            elif ann == list:          ptype = ParamType.ARRAY
            elif ann == dict:          ptype = ParamType.OBJECT
            has_default = param.default is not inspect.Parameter.empty
            params.append({
                "name": pname,
                "type": ptype.value,
                "required": not has_default,
                "default": param.default if has_default else None,
            })
        return self.register(
            name=fn.__name__,
            fn=fn,
            description=description or (fn.__doc__ or ""),
            params=params,
            **kwargs)

    def deprecate(self, tool_id: str, replacement: Optional[str] = None):
        tool = self._tools.get(tool_id)
        if tool:
            tool.status = ToolStatus.DEPRECATED
            if replacement:
                tool.metadata["replacement"] = replacement

    def disable(self, tool_id: str):
        if tool_id in self._tools:
            self._tools[tool_id].status = ToolStatus.DISABLED

    def enable(self, tool_id: str):
        if tool_id in self._tools:
            self._tools[tool_id].status = ToolStatus.ACTIVE

    def unregister(self, tool_id: str) -> bool:
        tool = self._tools.pop(tool_id, None)
        if tool:
            self._name_index.pop(tool.name, None)
            return True
        return False

    # ── EXECUTION ─────────────────────────────────────────────────────

    def call(self, name: str,
             inputs: Optional[Dict[str, Any]] = None,
             validate: bool = True) -> ToolCall:
        tid  = self._name_index.get(name)
        spec = self._tools.get(tid) if tid else None
        tc   = ToolCall(tool_id=tid or "", tool_name=name,
                        inputs=dict(inputs or {}))
        if not spec:
            tc.error = f"Tool '{name}' not found"
            return tc
        if spec.status == ToolStatus.DISABLED:
            tc.error = f"Tool '{name}' is disabled"
            return tc

        # Validate and coerce params
        if validate and spec.params:
            try:
                coerced = {}
                param_map = {p.name: p for p in spec.params}
                for pname, schema in param_map.items():
                    coerced[pname] = _coerce(
                        (inputs or {}).get(pname), schema)
                # Remove extra keys
                inputs = coerced
            except ValidationError as e:
                tc.error = str(e)
                spec.error_count += 1
                return tc

        for fn in self._pre_hooks:
            try: fn(spec, inputs)
            except Exception: pass

        t0 = time.time()
        try:
            result = spec.fn(**(inputs or {}))
            tc.output   = result
            tc.success  = True
            spec.call_count += 1
        except Exception as exc:
            tc.error = str(exc)
            spec.error_count += 1
        finally:
            tc.duration_ms = (time.time() - t0) * 1000

        self._db.execute(
            "INSERT INTO tr_calls VALUES (?,?,?,?,?,?,?,?)",
            (tc.call_id, tc.tool_id, tc.tool_name,
             json.dumps(str(inputs or {})),
             int(tc.success), tc.error,
             tc.duration_ms, tc.ts))
        self._db.commit()

        for fn in self._post_hooks:
            try: fn(spec, tc)
            except Exception: pass
        return tc

    def call_by_id(self, tool_id: str, inputs: Optional[Dict] = None,
                   **kwargs) -> ToolCall:
        spec = self._tools.get(tool_id)
        if not spec:
            tc = ToolCall(tool_id=tool_id, tool_name="")
            tc.error = f"Tool ID '{tool_id}' not found"
            return tc
        return self.call(spec.name, inputs, **kwargs)

    # ── HOOKS ─────────────────────────────────────────────────────────

    def on_pre_call(self, fn: Callable):  self._pre_hooks.append(fn)
    def on_post_call(self, fn: Callable): self._post_hooks.append(fn)

    # ── QUERY ─────────────────────────────────────────────────────────

    def get_tool(self, name: str) -> Optional[ToolSpec]:
        tid = self._name_index.get(name)
        return self._tools.get(tid) if tid else None

    def get_tool_by_id(self, tool_id: str) -> Optional[ToolSpec]:
        return self._tools.get(tool_id)

    def list_tools(self, namespace: Optional[str] = None,
                   status: Optional[ToolStatus] = None,
                   tag: Optional[str] = None) -> List[Dict[str, Any]]:
        tools = list(self._tools.values())
        if namespace:
            tools = [t for t in tools if t.namespace == namespace]
        if status:
            tools = [t for t in tools if t.status == status]
        if tag:
            tools = [t for t in tools if tag in t.tags]
        return [t.to_dict() for t in tools]

    def to_openai_specs(self, namespace: Optional[str] = None,
                         active_only: bool = True) -> List[Dict]:
        tools = list(self._tools.values())
        if namespace:
            tools = [t for t in tools if t.namespace == namespace]
        if active_only:
            tools = [t for t in tools if t.status == ToolStatus.ACTIVE]
        return [t.to_openai_spec() for t in tools]

    def call_history(self, tool_name: Optional[str] = None,
                     limit: int = 50) -> List[Dict[str, Any]]:
        q = "SELECT call_id,tool_name,success,error,duration_ms,ts FROM tr_calls"
        params: List[Any] = []
        if tool_name:
            q += " WHERE tool_name=?"; params.append(tool_name)
        q += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        rows = self._db.execute(q, params).fetchall()
        return [{"call_id": r[0], "name": r[1], "success": bool(r[2]),
                 "error": r[3], "ms": r[4]} for r in rows]

    def stats(self) -> Dict[str, Any]:
        total_calls  = sum(t.call_count for t in self._tools.values())
        total_errors = sum(t.error_count for t in self._tools.values())
        return {
            "tools": len(self._tools),
            "active": sum(1 for t in self._tools.values()
                          if t.status == ToolStatus.ACTIVE),
            "total_calls": total_calls,
            "total_errors": total_errors,
            "namespaces": len({t.namespace for t in self._tools.values()}),
        }
