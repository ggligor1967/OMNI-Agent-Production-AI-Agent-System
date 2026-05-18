"""OMNI AGENT - Config Validator
Schema-driven configuration validation, diff, merge, migration,
and version history with rollback support.

Features:
- Schema definition: type, required, default, enum, range, pattern constraints
- Deep validation: nested dicts and lists with path-aware error reporting
- Default injection: auto-fill missing optional fields with defaults
- Diff: compare two configs and return added/removed/changed keys
- Merge: deep-merge two configs with conflict resolution strategies
- Migration: apply versioned migration functions to upgrade configs
- Version history: SQLite-persisted config snapshots with timestamps
- Rollback: restore any previous config version
- Environment overlay: apply env-var overrides to config
- Config linting: warn about deprecated keys, unknown fields, suspicious values
- REST API: validate, diff, merge, history, rollback
"""
import json, time, uuid, sqlite3, re, os, copy, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

# ── Schema types ──────────────────────────────────────────────────────────────

@dataclass
class FieldSchema:
    type: str                          # str | int | float | bool | list | dict | any
    required: bool = False
    default: Any = None
    enum: Optional[List] = None
    min_val: Optional[float] = None
    max_val: Optional[float] = None
    pattern: Optional[str] = None     # regex for string fields
    items: Optional["FieldSchema"] = None   # for list fields
    properties: Optional[Dict[str, "FieldSchema"]] = None  # for dict fields
    deprecated: bool = False
    description: str = ""

@dataclass
class ValidationError:
    path: str; message: str; severity: str = "error"  # error | warning
    def to_dict(self): return {"path": self.path, "message": self.message, "severity": self.severity}

@dataclass
class ValidationResult:
    valid: bool
    errors: List[ValidationError] = field(default_factory=list)
    warnings: List[ValidationError] = field(default_factory=list)
    config_with_defaults: Dict = field(default_factory=dict)

    def to_dict(self):
        return {"valid": self.valid,
                "errors": [e.to_dict() for e in self.errors],
                "warnings": [w.to_dict() for w in self.warnings],
                "error_count": len(self.errors),
                "warning_count": len(self.warnings)}

@dataclass
class ConfigVersion:
    id: str; name: str; config: Dict
    version: int; message: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self):
        return {"id": self.id, "name": self.name, "version": self.version,
                "message": self.message, "created_at": self.created_at}

# ── Validation logic ──────────────────────────────────────────────────────────

_TYPE_MAP = {"str": str, "string": str, "int": int, "integer": int,
              "float": float, "number": float, "bool": bool, "boolean": bool,
              "list": list, "array": list, "dict": dict, "object": dict}

def _validate_field(value: Any, schema: FieldSchema,
                     path: str) -> List[ValidationError]:
    errs = []
    if value is None:
        if schema.required:
            errs.append(ValidationError(path, "Required field is missing"))
        return errs

    # Type check
    if schema.type != "any":
        expected = _TYPE_MAP.get(schema.type)
        if expected and not isinstance(value, expected):
            # Allow int where float expected
            if not (schema.type in ("float","number") and isinstance(value, int)):
                errs.append(ValidationError(path,
                    f"Expected {schema.type}, got {type(value).__name__}"))
                return errs  # stop further checks

    # Enum
    if schema.enum is not None and value not in schema.enum:
        errs.append(ValidationError(path, f"Value {value!r} not in enum {schema.enum}"))

    # Range
    if schema.min_val is not None and isinstance(value, (int, float)):
        if value < schema.min_val:
            errs.append(ValidationError(path, f"Value {value} < min {schema.min_val}"))
    if schema.max_val is not None and isinstance(value, (int, float)):
        if value > schema.max_val:
            errs.append(ValidationError(path, f"Value {value} > max {schema.max_val}"))

    # Pattern
    if schema.pattern and isinstance(value, str):
        if not re.match(schema.pattern, value):
            errs.append(ValidationError(path, f"Value {value!r} does not match pattern {schema.pattern!r}"))

    # List items
    if isinstance(value, list) and schema.items:
        for i, item in enumerate(value):
            errs.extend(_validate_field(item, schema.items, f"{path}[{i}]"))

    # Dict properties
    if isinstance(value, dict) and schema.properties:
        for key, sub_schema in schema.properties.items():
            sub_val = value.get(key)
            sub_errs = _validate_field(sub_val, sub_schema, f"{path}.{key}")
            errs.extend(sub_errs)
        # Warn about unknown keys
        for key in value:
            if key not in schema.properties:
                errs.append(ValidationError(f"{path}.{key}",
                    f"Unknown field {key!r}", severity="warning"))

    # Deprecated
    if schema.deprecated:
        errs.append(ValidationError(path, f"Field {path!r} is deprecated", severity="warning"))

    return errs

def _inject_defaults(config: Dict, schema: Dict[str, FieldSchema], path="") -> Dict:
    result = dict(config)
    for key, fs in schema.items():
        if key not in result and not fs.required and fs.default is not None:
            result[key] = copy.deepcopy(fs.default)
        elif key in result and isinstance(result[key], dict) and fs.properties:
            result[key] = _inject_defaults(result[key], fs.properties, f"{path}.{key}")
    return result

# ── Diff & Merge ──────────────────────────────────────────────────────────────

def _flatten(d: Dict, prefix="") -> Dict[str, Any]:
    out = {}
    for k, v in d.items():
        full = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, full))
        else:
            out[full] = v
    return out

def config_diff(a: Dict, b: Dict) -> Dict:
    fa = _flatten(a); fb = _flatten(b)
    added   = {k: fb[k] for k in fb if k not in fa}
    removed = {k: fa[k] for k in fa if k not in fb}
    changed = {k: {"from": fa[k], "to": fb[k]} for k in fa if k in fb and fa[k] != fb[k]}
    return {"added": added, "removed": removed, "changed": changed,
            "unchanged": sum(1 for k in fa if k in fb and fa[k] == fb[k])}

def _deep_merge(base: Dict, override: Dict,
                 strategy: str = "override") -> Dict:
    result = copy.deepcopy(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v, strategy)
        elif k in result and strategy == "keep_base":
            pass  # don't override
        else:
            result[k] = copy.deepcopy(v)
    return result

# ── Persistence ───────────────────────────────────────────────────────────────

class CVStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS config_versions(
                    id TEXT PRIMARY KEY, name TEXT, config TEXT,
                    version INTEGER, message TEXT DEFAULT '',
                    created_at REAL);
                CREATE INDEX IF NOT EXISTS idx_cv_name ON config_versions(name, version DESC);
            """)

    def save(self, cv: ConfigVersion):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO config_versions VALUES(?,?,?,?,?,?)",
                (cv.id, cv.name, json.dumps(cv.config), cv.version,
                 cv.message, cv.created_at))

    def get_latest(self, name: str) -> Optional[ConfigVersion]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM config_versions WHERE name=? ORDER BY version DESC LIMIT 1",
                             (name,)).fetchone()
        return self._r(row) if row else None

    def get_version(self, name: str, version: int) -> Optional[ConfigVersion]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM config_versions WHERE name=? AND version=?",
                             (name, version)).fetchone()
        return self._r(row) if row else None

    def list_versions(self, name: str) -> List[ConfigVersion]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM config_versions WHERE name=? ORDER BY version DESC",
                              (name,)).fetchall()
        return [self._r(r) for r in rows]

    def _r(self, row) -> ConfigVersion:
        return ConfigVersion(id=row["id"], name=row["name"],
                              config=json.loads(row["config"]),
                              version=row["version"], message=row["message"] or "",
                              created_at=row["created_at"])

    def stats(self):
        with self._conn() as c:
            nc = c.execute("SELECT COUNT(DISTINCT name) FROM config_versions").fetchone()[0]
            nv = c.execute("SELECT COUNT(*) FROM config_versions").fetchone()[0]
        return {"config_names": nc, "total_versions": nv}

class ConfigValidator:
    """
    Schema-driven config validation, diff, merge, migration, and versioning.

    Usage:
        cv = ConfigValidator()

        cv.define_schema("server", {
            "host": FieldSchema("str", required=True, pattern=r'^[\w.-]+$'),
            "port": FieldSchema("int", required=True, min_val=1, max_val=65535),
            "debug": FieldSchema("bool", default=False),
            "workers": FieldSchema("int", default=4, min_val=1, max_val=32),
            "log_level": FieldSchema("str", default="INFO",
                                      enum=["DEBUG","INFO","WARNING","ERROR"]),
        })

        result = cv.validate({"host":"localhost","port":8080}, schema="server")
        print(result.valid)                    # True
        print(result.config_with_defaults)     # {"host":..., "port":..., "debug":False, ...}
    """
    def __init__(self, db_path: str = "data/configs.db"):
        self._store = CVStore(db_path)
        self._schemas: Dict[str, Dict[str, FieldSchema]] = {}
        self._migrations: Dict[str, List[Callable]] = {}

    def define_schema(self, name: str, schema: Dict[str, FieldSchema]):
        self._schemas[name] = schema

    def validate(self, config: Dict, schema: str = None,
                  schema_def: Dict[str, FieldSchema] = None) -> ValidationResult:
        if schema_def is None:
            schema_def = self._schemas.get(schema, {}) if schema else {}
        errors = []; warnings = []
        for key, fs in schema_def.items():
            all_errs = _validate_field(config.get(key), fs, key)
            for e in all_errs:
                if e.severity == "warning": warnings.append(e)
                else: errors.append(e)
        # Warn about unknown top-level keys
        for key in config:
            if schema_def and key not in schema_def:
                warnings.append(ValidationError(key, f"Unknown field {key!r}", "warning"))
        config_with_defaults = _inject_defaults(config, schema_def) if schema_def else config
        return ValidationResult(valid=len(errors)==0, errors=errors,
                                  warnings=warnings,
                                  config_with_defaults=config_with_defaults)

    def diff(self, config_a: Dict, config_b: Dict) -> Dict:
        return config_diff(config_a, config_b)

    def merge(self, base: Dict, override: Dict,
               strategy: str = "override") -> Dict:
        return _deep_merge(base, override, strategy)

    def env_overlay(self, config: Dict, prefix: str = "APP_") -> Dict:
        """Apply environment variables with given prefix as config overrides."""
        result = copy.deepcopy(config)
        for key, val in os.environ.items():
            if key.startswith(prefix):
                cfg_key = key[len(prefix):].lower().replace("__", ".")
                parts = cfg_key.split(".")
                target = result
                for p in parts[:-1]:
                    target = target.setdefault(p, {})
                # Try to parse as JSON, fall back to string
                try: target[parts[-1]] = json.loads(val)
                except: target[parts[-1]] = val
        return result

    def save_version(self, name: str, config: Dict,
                      message: str = "") -> ConfigVersion:
        latest = self._store.get_latest(name)
        version = (latest.version + 1) if latest else 1
        cv = ConfigVersion(id=str(uuid.uuid4())[:10], name=name,
                            config=config, version=version, message=message)
        self._store.save(cv)
        logger.info(f"Config {name!r} v{version} saved")
        return cv

    def rollback(self, name: str, to_version: int) -> Optional[Dict]:
        cv = self._store.get_version(name, to_version)
        if not cv: return None
        # Save rollback as a new version
        latest = self._store.get_latest(name)
        new_cv = ConfigVersion(id=str(uuid.uuid4())[:10], name=name,
                                config=cv.config,
                                version=(latest.version + 1) if latest else 1,
                                message=f"Rollback to v{to_version}")
        self._store.save(new_cv)
        return cv.config

    def get_config(self, name: str, version: int = None) -> Optional[Dict]:
        cv = (self._store.get_version(name, version) if version
               else self._store.get_latest(name))
        return cv.config if cv else None

    def register_migration(self, schema_name: str, fn: Callable):
        self._migrations.setdefault(schema_name, []).append(fn)

    def migrate(self, config: Dict, schema_name: str) -> Tuple[Dict, int]:
        fns = self._migrations.get(schema_name, [])
        result = copy.deepcopy(config)
        for fn in fns:
            result = fn(result)
        return result, len(fns)

    def lint(self, config: Dict, schema: str = None) -> List[Dict]:
        warnings = []
        schema_def = self._schemas.get(schema, {}) if schema else {}
        flat = _flatten(config)
        for path, val in flat.items():
            # Suspicious value patterns
            if isinstance(val, str) and re.search(r'password|secret|token', path, re.I):
                if len(val) < 8:
                    warnings.append({"path": path, "message": "Possible weak credential value"})
            if isinstance(val, str) and val.lower() in ("todo","fixme","tbd","changeme"):
                warnings.append({"path": path, "message": f"Placeholder value: {val!r}"})
            if path.endswith("port") and isinstance(val, int):
                if not 1 <= val <= 65535:
                    warnings.append({"path": path, "message": f"Invalid port {val}"})
        # Deprecated fields
        for key, fs in schema_def.items():
            if fs.deprecated and key in config:
                warnings.append({"path": key, "message": f"Field {key!r} is deprecated"})
        return warnings

    def history(self, name: str) -> List[ConfigVersion]:
        return self._store.list_versions(name)

    def stats(self) -> Dict:
        s = self._store.stats()
        s["schemas_defined"] = len(self._schemas)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def validate_ep(req):
            d = await req.json()
            result = self.validate(d.get("config",{}), d.get("schema"))
            return web.json_response(result.to_dict())
        async def diff_ep(req):
            d = await req.json()
            return web.json_response(self.diff(d.get("a",{}), d.get("b",{})))
        async def merge_ep(req):
            d = await req.json()
            merged = self.merge(d.get("base",{}), d.get("override",{}),
                                 d.get("strategy","override"))
            return web.json_response({"merged": merged})
        async def save_ep(req):
            d = await req.json()
            cv = self.save_version(d["name"], d.get("config",{}), d.get("message",""))
            return web.json_response(cv.to_dict(), status=201)
        async def rollback_ep(req):
            d = await req.json()
            config = self.rollback(d["name"], int(d["version"]))
            if not config: return web.json_response({"error":"not found"},status=404)
            return web.json_response({"config": config})
        async def history_ep(req):
            name = req.match_info.get("name","")
            return web.json_response({"versions": [v.to_dict() for v in self.history(name)]})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/config"
        app.router.add_post(f"{p}/validate",   validate_ep)
        app.router.add_post(f"{p}/diff",       diff_ep)
        app.router.add_post(f"{p}/merge",      merge_ep)
        app.router.add_post(f"{p}/save",       save_ep)
        app.router.add_post(f"{p}/rollback",   rollback_ep)
        app.router.add_get( f"{p}/history/{{name}}", history_ep)
        app.router.add_get( f"{p}/stats",      stats_ep)
        logger.info(f"Config validator API at {prefix}/config/")
