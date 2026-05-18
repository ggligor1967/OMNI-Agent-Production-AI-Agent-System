"""OMNI AGENT - Schema Validator
JSON Schema (draft-7 subset) + custom rules: type checking, range,
regex, enum, required fields, cross-field constraints, and coercion.

Features:
- Types: string, integer, number, boolean, array, object, null, any
- String: minLength, maxLength, pattern (regex), format (email, uri, date, uuid)
- Number: minimum, maximum, exclusiveMinimum, exclusiveMaximum, multipleOf
- Array: minItems, maxItems, items (element schema), uniqueItems
- Object: properties, required, additionalProperties, minProperties
- Enum: list of allowed values
- Const: exact value match
- anyOf, oneOf, allOf, not combiners
- Cross-field: dependencies(field → [required if field present]),
    if/then/else conditional schemas
- Coercion: optionally cast string→int/float/bool before validation
- Custom validators: register fn(value, ctx) → error_str | None
- Error collection: list of ValidationError with path + message
- Path tracking: JSON pointer style "$.field.subfield[0]"
- Strict mode vs permissive (additionalProperties handling)
- Schema compilation: pre-process for faster repeated validation
- SQLite persistence: validation history, error analytics
- REST API: validate, schema_info, coerce, stats
"""
import json, re, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

# ── Format validators ──────────────────────────────────────────────────────────
_EMAIL_RE    = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
_URI_RE      = re.compile(r'^https?://')
_DATE_RE     = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_UUID_RE     = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
_DATETIME_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}')
_IPV4_RE     = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')

_FORMATS = {
    "email":     lambda v: bool(_EMAIL_RE.match(v)),
    "uri":       lambda v: bool(_URI_RE.match(v)),
    "date":      lambda v: bool(_DATE_RE.match(v)),
    "date-time": lambda v: bool(_DATETIME_RE.match(v)),
    "uuid":      lambda v: bool(_UUID_RE.match(v)),
    "ipv4":      lambda v: bool(_IPV4_RE.match(v)),
}

@dataclass
class ValidationError:
    path: str; message: str; value: Any = None

    def to_dict(self): return {"path": self.path, "message": self.message}

    def __str__(self): return f"{self.path}: {self.message}"

def _type_of(v: Any) -> str:
    if isinstance(v, bool):  return "boolean"
    if isinstance(v, int):   return "integer"
    if isinstance(v, float): return "number"
    if isinstance(v, str):   return "string"
    if isinstance(v, list):  return "array"
    if isinstance(v, dict):  return "object"
    if v is None:             return "null"
    return "unknown"

def _coerce_value(v: Any, schema: Dict) -> Any:
    """Attempt type coercion from string values."""
    expected = schema.get("type")
    if not isinstance(v, str): return v
    if expected == "integer":
        try: return int(v)
        except: return v
    if expected == "number":
        try: return float(v)
        except: return v
    if expected == "boolean":
        if v.lower() in ("true","1","yes"):  return True
        if v.lower() in ("false","0","no"):  return False
    return v

class SVStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS validations(
                    id TEXT PRIMARY KEY, schema_name TEXT,
                    valid INTEGER, error_count INTEGER,
                    created_at REAL);
                CREATE INDEX IF NOT EXISTS idx_val_schema
                    ON validations(schema_name, created_at DESC);
            """)

    def log(self, schema_name: str, valid: bool, error_count: int):
        with self._conn() as c:
            c.execute("INSERT INTO validations VALUES(?,?,?,?,?)",
                (str(uuid.uuid4())[:8], schema_name, int(valid), error_count, time.time()))

    def stats(self, schema_name: str = None) -> Dict:
        with self._conn() as c:
            if schema_name:
                n  = c.execute(
                    "SELECT COUNT(*) FROM validations WHERE schema_name=?",
                    (schema_name,)).fetchone()[0]
                nv = c.execute(
                    "SELECT COUNT(*) FROM validations WHERE schema_name=? AND valid=1",
                    (schema_name,)).fetchone()[0]
            else:
                n  = c.execute("SELECT COUNT(*) FROM validations").fetchone()[0]
                nv = c.execute(
                    "SELECT COUNT(*) FROM validations WHERE valid=1").fetchone()[0]
        return {"total": n, "valid": nv,
                "invalid": n - nv,
                "pass_rate": round(nv / max(1, n), 4)}

class SchemaValidator:
    """
    JSON Schema (draft-7 subset) validator with coercion and custom rules.

    Usage:
        sv = SchemaValidator()
        user_schema = {
            "type": "object",
            "required": ["name", "age", "email"],
            "properties": {
                "name":  {"type": "string", "minLength": 1},
                "age":   {"type": "integer", "minimum": 0, "maximum": 150},
                "email": {"type": "string", "format": "email"},
                "tags":  {"type": "array", "items": {"type": "string"}}
            }
        }
        sv.register("user", user_schema)

        errors = sv.validate({"name":"Alice","age":30,"email":"alice@ex.com"}, "user")
        print(errors)  # []
    """
    def __init__(self, db_path: str = "data/schema.db",
                 strict: bool = False, coerce: bool = False):
        self._store = SVStore(db_path)
        self._schemas: Dict[str, Dict] = {}
        self._custom: Dict[str, List[Callable]] = {}
        self.strict = strict
        self.coerce = coerce

    def register(self, name: str, schema: Dict):
        self._schemas[name] = schema

    def add_custom(self, schema_name: str, fn: Callable):
        self._custom.setdefault(schema_name, []).append(fn)

    def validate(self, data: Any, schema_or_name: Union[str, Dict],
                  coerce: bool = None) -> List[ValidationError]:
        coerce = coerce if coerce is not None else self.coerce
        schema = (self._schemas[schema_or_name]
                   if isinstance(schema_or_name, str) else schema_or_name)
        errors: List[ValidationError] = []
        if coerce and isinstance(schema, dict):
            data = _coerce_value(data, schema)
        self._validate_node(data, schema, "$", errors, coerce)
        # Custom validators
        name = schema_or_name if isinstance(schema_or_name, str) else ""
        for fn in self._custom.get(name, []):
            try:
                msg = fn(data, schema)
                if msg: errors.append(ValidationError("$", msg, data))
            except Exception as e:
                errors.append(ValidationError("$", f"Custom validator error: {e}"))
        if name:
            self._store.log(name, not errors, len(errors))
        return errors

    def is_valid(self, data: Any, schema_or_name: Union[str, Dict]) -> bool:
        return len(self.validate(data, schema_or_name)) == 0

    def _validate_node(self, data: Any, schema: Dict,
                        path: str, errors: List, coerce: bool):
        if not isinstance(schema, dict): return
        # $ref not supported; skip
        # type check
        expected_type = schema.get("type")
        if expected_type:
            if coerce: data = _coerce_value(data, schema)
            actual = _type_of(data)
            if expected_type == "number" and actual == "integer":
                actual = "number"   # int satisfies number
            if actual != expected_type and expected_type != "any":
                errors.append(ValidationError(path,
                    f"Expected {expected_type}, got {actual}", data))
                return   # Further checks meaningless
        # enum
        if "enum" in schema and data not in schema["enum"]:
            errors.append(ValidationError(path,
                f"Value {data!r} not in enum {schema['enum']}", data))
        # const
        if "const" in schema and data != schema["const"]:
            errors.append(ValidationError(path,
                f"Expected const {schema['const']!r}, got {data!r}", data))
        # String keywords
        if isinstance(data, str):
            mn = schema.get("minLength")
            if mn is not None and len(data) < mn:
                errors.append(ValidationError(path, f"String too short (min {mn})", data))
            mx = schema.get("maxLength")
            if mx is not None and len(data) > mx:
                errors.append(ValidationError(path, f"String too long (max {mx})", data))
            pat = schema.get("pattern")
            if pat and not re.search(pat, data):
                errors.append(ValidationError(path, f"Doesn't match pattern {pat!r}", data))
            fmt = schema.get("format")
            if fmt and fmt in _FORMATS:
                if not _FORMATS[fmt](data):
                    errors.append(ValidationError(path, f"Invalid format: {fmt}", data))
        # Number keywords
        if isinstance(data, (int, float)) and not isinstance(data, bool):
            for kw, op, desc in [
                ("minimum",          lambda v,lim: v >= lim, "minimum"),
                ("maximum",          lambda v,lim: v <= lim, "maximum"),
                ("exclusiveMinimum", lambda v,lim: v >  lim, "exclusiveMinimum"),
                ("exclusiveMaximum", lambda v,lim: v <  lim, "exclusiveMaximum")]:
                if kw in schema and not op(data, schema[kw]):
                    errors.append(ValidationError(path,
                        f"Violates {desc} {schema[kw]}", data))
            mo = schema.get("multipleOf")
            if mo and data % mo != 0:
                errors.append(ValidationError(path, f"Not a multiple of {mo}", data))
        # Array keywords
        if isinstance(data, list):
            mn = schema.get("minItems")
            if mn is not None and len(data) < mn:
                errors.append(ValidationError(path, f"Array too short (min {mn})", data))
            mx = schema.get("maxItems")
            if mx is not None and len(data) > mx:
                errors.append(ValidationError(path, f"Array too long (max {mx})", data))
            if schema.get("uniqueItems") and len(data) != len(set(
                    json.dumps(i, sort_keys=True) for i in data)):
                errors.append(ValidationError(path, "Array items not unique", data))
            items_schema = schema.get("items")
            if items_schema:
                for i, item in enumerate(data):
                    self._validate_node(item, items_schema,
                                         f"{path}[{i}]", errors, coerce)
        # Object keywords
        if isinstance(data, dict):
            props = schema.get("properties", {})
            required = schema.get("required", [])
            for r in required:
                if r not in data:
                    errors.append(ValidationError(f"{path}.{r}",
                        f"Required field {r!r} missing"))
            for k, sub in props.items():
                if k in data:
                    self._validate_node(data[k], sub,
                                         f"{path}.{k}", errors, coerce)
            if self.strict or schema.get("additionalProperties") is False:
                for k in data:
                    if k not in props:
                        errors.append(ValidationError(f"{path}.{k}",
                            f"Additional property {k!r} not allowed"))
            mn = schema.get("minProperties")
            if mn is not None and len(data) < mn:
                errors.append(ValidationError(path, f"Too few properties (min {mn})", data))
            # dependencies
            for dep_field, dep_required in schema.get("dependencies", {}).items():
                if dep_field in data and isinstance(dep_required, list):
                    for req in dep_required:
                        if req not in data:
                            errors.append(ValidationError(f"{path}.{req}",
                                f"Required when {dep_field!r} is present"))
        # Combiners
        if "allOf" in schema:
            for sub in schema["allOf"]:
                self._validate_node(data, sub, path, errors, coerce)
        if "anyOf" in schema:
            any_errors = []
            for sub in schema["anyOf"]:
                sub_errs: List[ValidationError] = []
                self._validate_node(data, sub, path, sub_errs, coerce)
                if not sub_errs: break
                any_errors.extend(sub_errs)
            else:
                errors.append(ValidationError(path, "Fails all anyOf schemas", data))
        if "oneOf" in schema:
            passes = 0
            for sub in schema["oneOf"]:
                sub_errs: List[ValidationError] = []
                self._validate_node(data, sub, path, sub_errs, coerce)
                if not sub_errs: passes += 1
            if passes != 1:
                errors.append(ValidationError(path,
                    f"Must match exactly one of oneOf (matched {passes})", data))
        if "not" in schema:
            sub_errs: List[ValidationError] = []
            self._validate_node(data, schema["not"], path, sub_errs, coerce)
            if not sub_errs:
                errors.append(ValidationError(path, "Must NOT match 'not' schema", data))
        # if/then/else
        if "if" in schema:
            cond_errs: List[ValidationError] = []
            self._validate_node(data, schema["if"], path, cond_errs, coerce)
            if not cond_errs and "then" in schema:
                self._validate_node(data, schema["then"], path, errors, coerce)
            elif cond_errs and "else" in schema:
                self._validate_node(data, schema["else"], path, errors, coerce)

    def coerce_data(self, data: Any,
                     schema_or_name: Union[str, Dict]) -> Tuple[Any, List[ValidationError]]:
        schema = (self._schemas[schema_or_name]
                   if isinstance(schema_or_name, str) else schema_or_name)
        data = self._coerce_obj(data, schema)
        errors = self.validate(data, schema_or_name)
        return data, errors

    def _coerce_obj(self, data: Any, schema: Dict) -> Any:
        if isinstance(schema, dict) and "properties" in schema and isinstance(data, dict):
            for k, sub in schema["properties"].items():
                if k in data:
                    data[k] = _coerce_value(data[k], sub)
                    data[k] = self._coerce_obj(data[k], sub)
        elif isinstance(schema, dict) and "items" in schema and isinstance(data, list):
            data = [self._coerce_obj(_coerce_value(item, schema["items"]),
                                      schema["items"]) for item in data]
        else:
            data = _coerce_value(data, schema)
        return data

    def stats(self, schema_name: str = None) -> Dict:
        s = self._store.stats(schema_name)
        s["registered_schemas"] = len(self._schemas)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def validate_ep(req):
            d = await req.json()
            errors = self.validate(d["data"], d["schema"])
            return web.json_response({
                "valid": not errors,
                "errors": [e.to_dict() for e in errors]})
        async def coerce_ep(req):
            d = await req.json()
            coerced, errors = self.coerce_data(d["data"], d["schema"])
            return web.json_response({
                "coerced": coerced, "valid": not errors,
                "errors": [e.to_dict() for e in errors]})
        async def schemas_ep(req):
            return web.json_response(
                {"schemas": list(self._schemas.keys())})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/schema"
        app.router.add_post(f"{p}/validate", validate_ep)
        app.router.add_post(f"{p}/coerce",   coerce_ep)
        app.router.add_get( f"{p}/list",     schemas_ep)
        app.router.add_get( f"{p}/stats",    stats_ep)
        logger.info(f"Schema validator API at {prefix}/schema/")
