"""OMNI AGENT - Data Validator
Schema-based validation: type checking, coercion, constraints,
nested schemas, custom validators, and detailed error reports.

Features:
- Field types: string, int, float, bool, list, dict, email, url,
    uuid, date, datetime, enum, any
- Required / optional fields with defaults
- Type coercion: string→int, string→float, string→bool, etc.
- Constraints: min/max (numbers), minlen/maxlen (strings/lists),
    pattern (regex), choices (enum), min_items/max_items (lists)
- Nested: dict field with nested schema; list of dicts
- Custom validators: register fn(value, field_name) → (bool, error_str)
- Multiple errors: collect all errors across fields (don't stop at first)
- Field aliases: accept alternate field names (e.g. "userId" for "user_id")
- Strip whitespace: optional auto-strip on string fields
- Unknown fields: ALLOW, IGNORE, or FORBID mode
- Default values: applied when field missing or None
- Computed fields: fn(cleaned_data) → value added post-validation
- Error format: [{field, code, message, value}]
- Schema inheritance: extend a base schema
- Schema registry: named schemas looked up by string
- Batch validate: validate list of dicts, return per-item results
- SQLite persistence: validation audit log (field_count, error_count, ts)
- REST API: validate, schema_register, schema_list, stats
"""
import json, re, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class UnknownFieldMode(str, Enum):
    ALLOW  = "allow"
    IGNORE = "ignore"
    FORBID = "forbid"

# ── Type coercion ─────────────────────────────────────────────────────────────
def _coerce(value: Any, type_name: str) -> Tuple[Any, Optional[str]]:
    try:
        if type_name == "string":
            return str(value), None
        if type_name == "int":
            if isinstance(value, bool): return None, "cannot coerce bool to int"
            return int(value), None
        if type_name == "float":
            return float(value), None
        if type_name == "bool":
            if isinstance(value, bool): return value, None
            if str(value).lower() in ("true","1","yes","on"):  return True, None
            if str(value).lower() in ("false","0","no","off"): return False, None
            return None, f"cannot coerce {value!r} to bool"
        if type_name == "list":
            if isinstance(value, (list, tuple)): return list(value), None
            return None, "expected list"
        if type_name == "dict":
            if isinstance(value, dict): return value, None
            return None, "expected dict"
    except (ValueError, TypeError) as e:
        return None, str(e)
    return value, None

# ── Format validators ──────────────────────────────────────────────────────────
_EMAIL_RE  = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
_URL_RE    = re.compile(r'^https?://')
_UUID_RE   = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
_DATE_RE   = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_DTIME_RE  = re.compile(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}')

def _check_format(value: Any, type_name: str) -> Optional[str]:
    s = str(value)
    if type_name == "email"    and not _EMAIL_RE.match(s): return "invalid email"
    if type_name == "url"      and not _URL_RE.match(s):   return "invalid URL"
    if type_name == "uuid"     and not _UUID_RE.match(s):  return "invalid UUID"
    if type_name == "date"     and not _DATE_RE.match(s):  return "invalid date (YYYY-MM-DD)"
    if type_name == "datetime" and not _DTIME_RE.match(s): return "invalid datetime"
    return None

@dataclass
class FieldError:
    field: str; code: str; message: str; value: Any = None

    def to_dict(self):
        return {"field": self.field, "code": self.code,
                "message": self.message,
                "value": str(self.value)[:80] if self.value is not None else None}

@dataclass
class FieldSchema:
    name: str
    type: str = "any"
    required: bool = False
    default: Any = None
    coerce: bool = False
    strip: bool = True
    aliases: List[str] = field(default_factory=list)
    # Constraints
    min_val: Optional[float] = None
    max_val: Optional[float] = None
    minlen: Optional[int] = None
    maxlen: Optional[int] = None
    pattern: Optional[str] = None
    choices: Optional[List] = None
    min_items: Optional[int] = None
    max_items: Optional[int] = None
    # Nested
    item_schema: Optional["Schema"] = None   # for list of dicts
    nested_schema: Optional["Schema"] = None  # for dict field
    # Custom
    validators: List[Callable] = field(default_factory=list)

    def validate_value(self, value: Any, path: str
                        ) -> Tuple[Any, List[FieldError]]:
        errors: List[FieldError] = []

        # Strip strings
        if self.strip and isinstance(value, str):
            value = value.strip()

        # Coerce
        if self.coerce and self.type not in ("any","list","dict",
                                              "email","url","uuid",
                                              "date","datetime","enum"):
            value, err = _coerce(value, self.type)
            if err:
                errors.append(FieldError(path, "coerce_error", err, value))
                return value, errors

        # Type check
        if self.type != "any":
            type_map = {
                "string": str, "int": int, "float": (int, float),
                "bool": bool, "list": list, "dict": dict}
            expected = type_map.get(self.type)
            if expected and not isinstance(value, expected):
                errors.append(FieldError(path, "type_error",
                    f"expected {self.type}, got {type(value).__name__}", value))
                return value, errors
            fmt_err = _check_format(value, self.type)
            if fmt_err:
                errors.append(FieldError(path, "format_error", fmt_err, value))
                return value, errors

        # Constraints
        if self.choices is not None and value not in self.choices:
            errors.append(FieldError(path, "choices_error",
                f"must be one of {self.choices}", value))

        if self.pattern and isinstance(value, str):
            if not re.search(self.pattern, value):
                errors.append(FieldError(path, "pattern_error",
                    f"must match pattern {self.pattern!r}", value))

        if self.min_val is not None and isinstance(value, (int,float)):
            if value < self.min_val:
                errors.append(FieldError(path, "min_error",
                    f"must be >= {self.min_val}", value))

        if self.max_val is not None and isinstance(value, (int,float)):
            if value > self.max_val:
                errors.append(FieldError(path, "max_error",
                    f"must be <= {self.max_val}", value))

        if self.minlen is not None and hasattr(value, "__len__"):
            if len(value) < self.minlen:
                errors.append(FieldError(path, "minlen_error",
                    f"length must be >= {self.minlen}", value))

        if self.maxlen is not None and hasattr(value, "__len__"):
            if len(value) > self.maxlen:
                errors.append(FieldError(path, "maxlen_error",
                    f"length must be <= {self.maxlen}", value))

        if self.min_items is not None and isinstance(value, list):
            if len(value) < self.min_items:
                errors.append(FieldError(path, "min_items_error",
                    f"need >= {self.min_items} items", value))

        if self.max_items is not None and isinstance(value, list):
            if len(value) > self.max_items:
                errors.append(FieldError(path, "max_items_error",
                    f"need <= {self.max_items} items", value))

        # Nested dict schema
        if self.nested_schema and isinstance(value, dict):
            result = self.nested_schema.validate(value, prefix=path)
            errors.extend(result.errors)
            value = result.data

        # List of dicts
        if self.item_schema and isinstance(value, list):
            new_list = []
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    result = self.item_schema.validate(
                        item, prefix=f"{path}[{i}]")
                    errors.extend(result.errors)
                    new_list.append(result.data)
                else:
                    new_list.append(item)
            value = new_list

        # Custom validators
        for fn in self.validators:
            try:
                ok, msg = fn(value, path)
                if not ok:
                    errors.append(FieldError(path, "custom_error", msg, value))
            except Exception as e:
                errors.append(FieldError(path, "validator_error", str(e), value))

        return value, errors

@dataclass
class ValidationResult:
    data: Dict; errors: List[FieldError]; valid: bool

    def to_dict(self):
        return {"valid": self.valid, "data": self.data,
                "errors": [e.to_dict() for e in self.errors]}

class Schema:
    def __init__(self, fields: List[FieldSchema] = None,
                  unknown: UnknownFieldMode = UnknownFieldMode.IGNORE,
                  parent: "Schema" = None):
        self._fields: Dict[str, FieldSchema] = {}
        self._computed: List[Tuple[str, Callable]] = []
        self._unknown = unknown
        if parent:
            self._fields.update(parent._fields)
            self._computed.extend(parent._computed)
        for f in (fields or []):
            self._fields[f.name] = f

    def add_field(self, f: FieldSchema): self._fields[f.name] = f

    def add_computed(self, name: str, fn: Callable):
        self._computed.append((name, fn))

    def validate(self, data: Dict,
                  prefix: str = "") -> ValidationResult:
        errors: List[FieldError] = []
        result: Dict = {}

        # Resolve aliases
        resolved = {}
        for name, fs in self._fields.items():
            if name in data:
                resolved[name] = data[name]
            else:
                for alias in fs.aliases:
                    if alias in data:
                        resolved[name] = data[alias]; break

        # Validate each field
        for name, fs in self._fields.items():
            path = f"{prefix}.{name}" if prefix else name
            if name in resolved:
                value, ferrs = fs.validate_value(resolved[name], path)
                errors.extend(ferrs)
                result[name] = value
            elif fs.required:
                errors.append(FieldError(path, "required",
                    f"field '{name}' is required"))
            elif fs.default is not None:
                result[name] = (fs.default() if callable(fs.default)
                                 else fs.default)

        # Unknown fields
        known = set(self._fields.keys())
        all_aliases = {alias for fs in self._fields.values()
                        for alias in fs.aliases}
        for key in data:
            if key not in known and key not in all_aliases:
                if self._unknown == UnknownFieldMode.FORBID:
                    errors.append(FieldError(key, "unknown_field",
                        f"unknown field '{key}'"))
                elif self._unknown == UnknownFieldMode.ALLOW:
                    result[key] = data[key]

        # Computed fields
        if not errors:
            for name, fn in self._computed:
                try: result[name] = fn(result)
                except Exception as e:
                    errors.append(FieldError(name, "computed_error", str(e)))

        return ValidationResult(data=result, errors=errors,
                                 valid=len(errors) == 0)

class DVStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS audit(
                    id TEXT PRIMARY KEY, schema_name TEXT,
                    field_count INTEGER, error_count INTEGER, ts REAL);
            """)

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def log(self, schema_name: str, n_fields: int, n_errors: int):
        with self._conn() as c:
            c.execute("INSERT INTO audit VALUES(?,?,?,?,?)",
                (str(uuid.uuid4())[:8], schema_name,
                 n_fields, n_errors, time.time()))

    def stats(self) -> Dict:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
            errors = c.execute(
                "SELECT SUM(error_count) FROM audit").fetchone()[0] or 0
            by_schema = {r["schema_name"]: r["cnt"] for r in c.execute(
                "SELECT schema_name, COUNT(*) as cnt FROM audit "
                "GROUP BY schema_name").fetchall()}
        return {"validations": total, "total_errors": errors,
                "by_schema": by_schema}

class DataValidator:
    """
    Schema-based data validator with coercion and nested schemas.

    Usage:
        dv = DataValidator()
        dv.register_schema("user", Schema([
            FieldSchema("name",  type="string",  required=True, maxlen=100),
            FieldSchema("email", type="email",   required=True),
            FieldSchema("age",   type="int",     coerce=True, min_val=0, max_val=150),
            FieldSchema("role",  type="string",  choices=["admin","user","guest"],
                         default="user"),
        ]))

        result = dv.validate("user", {"name":"Alice","email":"a@b.com","age":"30"})
        if result.valid:
            print(result.data)  # {"name":"Alice","email":"a@b.com","age":30,"role":"user"}
        else:
            for err in result.errors:
                print(err.field, err.message)
    """
    def __init__(self, db_path: str = "data/validator.db"):
        self._store = DVStore(db_path)
        self._schemas: Dict[str, Schema] = {}

    def register_schema(self, name: str, schema: Schema) -> Schema:
        self._schemas[name] = schema
        return schema

    def get_schema(self, name: str) -> Optional[Schema]:
        return self._schemas.get(name)

    def validate(self, schema_or_name, data: Dict) -> ValidationResult:
        if isinstance(schema_or_name, str):
            schema = self._schemas.get(schema_or_name)
            if not schema:
                raise KeyError(f"Schema '{schema_or_name}' not found")
            name = schema_or_name
        else:
            schema = schema_or_name; name = "inline"
        result = schema.validate(data)
        self._store.log(name, len(schema._fields), len(result.errors))
        return result

    def validate_batch(self, schema_or_name,
                        items: List[Dict]) -> List[ValidationResult]:
        return [self.validate(schema_or_name, item) for item in items]

    def quick_validate(self, data: Dict,
                        rules: Dict[str, Any]) -> ValidationResult:
        """Inline schema from a simple dict spec."""
        fields = []
        for name, spec in rules.items():
            if isinstance(spec, str):
                fields.append(FieldSchema(name=name, type=spec,
                                           required=True, coerce=True))
            elif isinstance(spec, dict):
                fields.append(FieldSchema(
                    name=name,
                    type=spec.get("type","any"),
                    required=spec.get("required", False),
                    coerce=spec.get("coerce", True),
                    default=spec.get("default"),
                    min_val=spec.get("min"),
                    max_val=spec.get("max"),
                    choices=spec.get("choices"),
                    minlen=spec.get("minlen"),
                    maxlen=spec.get("maxlen"),
                    pattern=spec.get("pattern")))
        return Schema(fields).validate(data)

    def stats(self) -> Dict:
        s = self._store.stats()
        s["registered_schemas"] = len(self._schemas)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def validate_ep(req):
            d = await req.json()
            try:
                result = self.validate(d["schema"], d["data"])
                return web.json_response(result.to_dict())
            except KeyError as e:
                return web.json_response({"error": str(e)}, status=404)
        async def quick_ep(req):
            d = await req.json()
            result = self.quick_validate(d["data"], d["rules"])
            return web.json_response(result.to_dict())
        async def list_ep(req):
            return web.json_response({"schemas": list(self._schemas.keys())})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/validate"
        app.router.add_post(f"{p}/run",   validate_ep)
        app.router.add_post(f"{p}/quick", quick_ep)
        app.router.add_get( f"{p}/list",  list_ep)
        app.router.add_get( f"{p}/stats", stats_ep)
        logger.info(f"Data validator API at {prefix}/validate/")
