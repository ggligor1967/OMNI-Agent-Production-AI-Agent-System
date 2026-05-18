"""OMNI Agent — Config Store: typed, validated, versioned configuration with watchers."""
from __future__ import annotations
import json, os, re, sqlite3, threading, time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Type, Union


class ConfigType(str, Enum):
    STRING  = "string"
    INT     = "int"
    FLOAT   = "float"
    BOOL    = "bool"
    LIST    = "list"
    DICT    = "dict"
    SECRET  = "secret"   # stored but masked in display


class ConfigError(Exception):
    pass


@dataclass
class ConfigSchema:
    key: str
    type: ConfigType
    default: Any = None
    required: bool = False
    description: str = ""
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    choices: Optional[List[Any]] = None
    pattern: Optional[str] = None   # regex for strings
    tags: List[str] = field(default_factory=list)

    def coerce(self, value: Any) -> Any:
        if value is None:
            return self.default
        if self.type == ConfigType.STRING or self.type == ConfigType.SECRET:
            return str(value)
        if self.type == ConfigType.INT:
            return int(value)
        if self.type == ConfigType.FLOAT:
            return float(value)
        if self.type == ConfigType.BOOL:
            if isinstance(value, bool):
                return value
            return str(value).lower() in ("true", "1", "yes", "on")
        if self.type == ConfigType.LIST:
            if isinstance(value, list):
                return value
            return json.loads(str(value))
        if self.type == ConfigType.DICT:
            if isinstance(value, dict):
                return value
            return json.loads(str(value))
        return value

    def validate(self, value: Any) -> List[str]:
        errors = []
        if value is None:
            if self.required:
                errors.append(f"'{self.key}' is required")
            return errors
        if self.type in (ConfigType.INT, ConfigType.FLOAT):
            if self.min_value is not None and value < self.min_value:
                errors.append(f"'{self.key}' must be >= {self.min_value}")
            if self.max_value is not None and value > self.max_value:
                errors.append(f"'{self.key}' must be <= {self.max_value}")
        if self.choices is not None and value not in self.choices:
            errors.append(f"'{self.key}' must be one of {self.choices}")
        if self.pattern and self.type in (ConfigType.STRING, ConfigType.SECRET):
            if not re.match(self.pattern, str(value)):
                errors.append(f"'{self.key}' must match pattern {self.pattern}")
        return errors


@dataclass
class ConfigVersion:
    version: int
    key: str
    value: Any
    changed_at: float = field(default_factory=time.time)
    changed_by: str = "system"
    comment: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "key": self.key,
            "value": value_display(self.key, self.value),
            "changed_at": self.changed_at,
            "changed_by": self.changed_by,
            "comment": self.comment,
        }


def value_display(key: str, value: Any) -> Any:
    """Mask secrets."""
    if "secret" in key.lower() or "password" in key.lower() or "token" in key.lower():
        return "***"
    return value


class ConfigStore:
    """
    Typed, validated configuration store with:
    - Schema enforcement and coercion
    - Environment variable override (prefix-based)
    - Version history per key
    - Change watchers
    - Namespace support (dotted keys)
    - SQLite persistence
    """

    def __init__(self, env_prefix: str = "OMNI_",
                 db_path: str = ":memory:"):
        self._env_prefix = env_prefix
        self._schemas: Dict[str, ConfigSchema] = {}
        self._values:  Dict[str, Any] = {}
        self._history: Dict[str, List[ConfigVersion]] = {}
        self._watchers: Dict[str, List[Callable]] = {}     # key → [fn]
        self._global_watchers: List[Callable] = []
        self._lock = threading.RLock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS cs_values (
                key TEXT PRIMARY KEY, value TEXT, type TEXT, version INTEGER
            );
            CREATE TABLE IF NOT EXISTS cs_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT, value TEXT, version INTEGER,
                changed_at REAL, changed_by TEXT, comment TEXT
            );
        """)
        self._db.commit()

    # ── SCHEMA ────────────────────────────────────────────────────────

    def define(self, key: str, type: ConfigType,
               default: Any = None, required: bool = False,
               description: str = "", min_value: Optional[float] = None,
               max_value: Optional[float] = None,
               choices: Optional[List[Any]] = None,
               pattern: Optional[str] = None,
               tags: Optional[List[str]] = None) -> ConfigSchema:
        schema = ConfigSchema(
            key=key, type=type, default=default, required=required,
            description=description, min_value=min_value, max_value=max_value,
            choices=choices, pattern=pattern, tags=list(tags or []))
        self._schemas[key] = schema
        # Set default immediately
        if default is not None and key not in self._values:
            self._values[key] = schema.coerce(default)
        return schema

    def define_many(self, definitions: List[Dict]) -> List[ConfigSchema]:
        return [self.define(**d) for d in definitions]

    # ── READ ──────────────────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            # Environment override takes highest priority
            env_key = self._env_prefix + key.upper().replace(".", "_")
            env_val = os.environ.get(env_key)
            if env_val is not None:
                schema = self._schemas.get(key)
                return schema.coerce(env_val) if schema else env_val
            val = self._values.get(key)
            if val is None:
                schema = self._schemas.get(key)
                if schema and schema.default is not None:
                    return schema.coerce(schema.default)
                return default
            return val

    def get_typed(self, key: str, t: Type) -> Any:
        val = self.get(key)
        if val is not None and not isinstance(val, t):
            try:
                return t(val)
            except Exception:
                pass
        return val

    def get_all(self, namespace: Optional[str] = None) -> Dict[str, Any]:
        keys = self._values.keys()
        if namespace:
            keys = [k for k in keys if k.startswith(namespace + ".")]
        return {k: value_display(k, self.get(k)) for k in keys}

    def get_namespace(self, ns: str) -> Dict[str, Any]:
        prefix = ns + "."
        return {k[len(prefix):]: self.get(k)
                for k in self._values if k.startswith(prefix)}

    # ── WRITE ─────────────────────────────────────────────────────────

    def set(self, key: str, value: Any,
            changed_by: str = "system", comment: str = "") -> Any:
        with self._lock:
            schema = self._schemas.get(key)
            if schema:
                coerced = schema.coerce(value)
                errors  = schema.validate(coerced)
                if errors:
                    raise ConfigError("; ".join(errors))
                value = coerced
            old_val = self._values.get(key)
            self._values[key] = value
            # Version history
            hist = self._history.setdefault(key, [])
            version = len(hist) + 1
            cv = ConfigVersion(version=version, key=key, value=value,
                               changed_by=changed_by, comment=comment)
            hist.append(cv)
            self._db.execute(
                "INSERT OR REPLACE INTO cs_values VALUES (?,?,?,?)",
                (key, json.dumps(value),
                 schema.type.value if schema else "unknown", version))
            self._db.execute(
                "INSERT INTO cs_history (key,value,version,changed_at,changed_by,comment) "
                "VALUES (?,?,?,?,?,?)",
                (key, json.dumps(value), version, cv.changed_at, changed_by, comment))
            self._db.commit()
            # Notify watchers
            if value != old_val:
                for fn in self._watchers.get(key, []):
                    try: fn(key, old_val, value)
                    except Exception: pass
                for fn in self._global_watchers:
                    try: fn(key, old_val, value)
                    except Exception: pass
            return value

    def set_many(self, values: Dict[str, Any], changed_by: str = "system"):
        for key, val in values.items():
            self.set(key, val, changed_by=changed_by)

    def delete(self, key: str) -> bool:
        with self._lock:
            if key not in self._values:
                return False
            del self._values[key]
            self._db.execute("DELETE FROM cs_values WHERE key=?", (key,))
            self._db.commit()
            return True

    def reset(self, key: str) -> Any:
        """Reset key to schema default."""
        schema = self._schemas.get(key)
        if schema and schema.default is not None:
            return self.set(key, schema.default, changed_by="reset")
        return self.delete(key)

    # ── WATCHERS ──────────────────────────────────────────────────────

    def watch(self, key: str, fn: Callable[[str, Any, Any], None]):
        self._watchers.setdefault(key, []).append(fn)

    def watch_all(self, fn: Callable[[str, Any, Any], None]):
        self._global_watchers.append(fn)

    # ── HISTORY ───────────────────────────────────────────────────────

    def history(self, key: str) -> List[Dict[str, Any]]:
        return [v.to_dict() for v in self._history.get(key, [])]

    def rollback(self, key: str, steps: int = 1) -> Any:
        hist = self._history.get(key, [])
        if len(hist) <= steps:
            raise ConfigError(f"Not enough history to rollback '{key}' by {steps}")
        target = hist[-(steps + 1)]
        return self.set(key, target.value, changed_by="rollback",
                        comment=f"rollback to v{target.version}")

    # ── VALIDATION ────────────────────────────────────────────────────

    def validate_all(self) -> Dict[str, List[str]]:
        errors = {}
        for key, schema in self._schemas.items():
            val    = self.get(key)
            errs   = schema.validate(val)
            if errs:
                errors[key] = errs
        return errors

    # ── EXPORT / IMPORT ───────────────────────────────────────────────

    def export(self) -> Dict[str, Any]:
        return {k: value_display(k, v) for k, v in self._values.items()}

    def import_values(self, data: Dict[str, Any], changed_by: str = "import"):
        for key, val in data.items():
            self.set(key, val, changed_by=changed_by)

    # ── STATS ─────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        return {
            "keys": len(self._values),
            "schemas": len(self._schemas),
            "watchers": sum(len(v) for v in self._watchers.values()),
            "global_watchers": len(self._global_watchers),
            "env_prefix": self._env_prefix,
        }
