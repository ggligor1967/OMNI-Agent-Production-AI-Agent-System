"""OMNI Agent — Config Manager V2: hierarchical config, env override, hot reload."""
from __future__ import annotations
import json, os, re, sqlite3, time, uuid
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, Union


class ConfigFormat(str, Enum):
    DICT  = "dict"
    JSON  = "json"
    ENV   = "env"


class ConfigSource(str, Enum):
    DEFAULT  = "default"
    FILE     = "file"
    ENV      = "env"
    RUNTIME  = "runtime"
    OVERRIDE = "override"


@dataclass
class ConfigEntry:
    key: str
    value: Any
    source: ConfigSource = ConfigSource.DEFAULT
    description: str = ""
    secret: bool = False
    validators: List[Callable[[Any], Optional[str]]] = field(default_factory=list)
    on_change: Optional[Callable[[Any, Any], None]] = None
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        val = "***" if self.secret else self.value
        return {"key": self.key, "value": val,
                "source": self.source.value,
                "description": self.description}


@dataclass
class ConfigSnapshot:
    snapshot_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    data: Dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    label: str = ""


class ConfigManagerV2:
    """
    Hierarchical config manager:
    - Multi-layer config: default → file → env → runtime → override
    - Dot-notation key access (app.db.host)
    - Environment variable override (PREFIX_APP_DB_HOST)
    - JSON config loading/dumping
    - Per-key validation on set
    - Secret keys (masked in dump/export)
    - Change callbacks per key
    - Watch for changed keys (diff between snapshots)
    - Snapshot and rollback
    - Config schema (required keys + types)
    - Interpolation: ${other.key} references
    - Namespace isolation
    - SQLite persistence for change history
    """

    def __init__(self, env_prefix: str = "OMNI",
                 db_path: str = ":memory:"):
        self._layers: Dict[ConfigSource, Dict[str, Any]] = {
            src: {} for src in ConfigSource}
        self._meta:     Dict[str, ConfigEntry] = {}
        self._snapshots: List[ConfigSnapshot] = []
        self._env_prefix = env_prefix.upper().rstrip("_")
        self._required:  List[str] = []
        self._schema:    Dict[str, type] = {}
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS cm_changes (
                change_id TEXT PRIMARY KEY, key TEXT,
                old_value TEXT, new_value TEXT,
                source TEXT, ts REAL
            );
        """)
        self._db.commit()

    # ── LAYER LOADING ─────────────────────────────────────────────────

    def load_defaults(self, data: Dict[str, Any]):
        self._merge(ConfigSource.DEFAULT, data)

    def load_json(self, json_str: str):
        data = json.loads(json_str)
        self._merge(ConfigSource.FILE, data)

    def load_env(self, prefix: Optional[str] = None):
        p = (prefix or self._env_prefix).upper() + "_"
        for k, v in os.environ.items():
            if k.startswith(p):
                key = k[len(p):].replace("__", ".").replace("_", ".").lower()
                self._set_layer(ConfigSource.ENV, key, v)

    def load_dict(self, data: Dict[str, Any],
                   source: ConfigSource = ConfigSource.RUNTIME):
        self._merge(source, data)

    def override(self, key: str, value: Any):
        self._set_layer(ConfigSource.OVERRIDE, key, value)

    def _merge(self, src: ConfigSource, data: Dict[str, Any],
                prefix: str = ""):
        for k, v in data.items():
            full_key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                self._merge(src, v, full_key)
            else:
                self._set_layer(src, full_key, v)

    def _set_layer(self, src: ConfigSource, key: str, value: Any):
        self._layers[src][key] = value

    # ── GET / SET ─────────────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        """Highest-priority layer wins: override > runtime > env > file > default."""
        for src in [ConfigSource.OVERRIDE, ConfigSource.RUNTIME,
                    ConfigSource.ENV, ConfigSource.FILE,
                    ConfigSource.DEFAULT]:
            if key in self._layers[src]:
                val = self._layers[src][key]
                return self._interpolate(val)
        return default

    def set(self, key: str, value: Any,
             source: ConfigSource = ConfigSource.RUNTIME,
             validate: bool = True) -> bool:
        meta = self._meta.get(key)
        if validate and meta and meta.validators:
            for fn in meta.validators:
                err = fn(value)
                if err:
                    raise ValueError(f"Validation failed for '{key}': {err}")
        if validate and key in self._schema:
            expected = self._schema[key]
            try:
                value = expected(value)
            except Exception:
                raise TypeError(f"Key '{key}' expects {expected.__name__}")

        old = self.get(key)
        self._set_layer(source, key, value)

        if meta and meta.on_change and old != value:
            try: meta.on_change(old, value)
            except Exception: pass

        self._db.execute(
            "INSERT INTO cm_changes VALUES (?,?,?,?,?,?)",
            (str(uuid.uuid4())[:8], key,
             json.dumps(old, default=str),
             json.dumps(value, default=str),
             source.value, time.time()))
        self._db.commit()
        return True

    def delete(self, key: str) -> bool:
        deleted = False
        for layer in self._layers.values():
            if key in layer:
                del layer[key]; deleted = True
        return deleted

    def _interpolate(self, value: Any) -> Any:
        if not isinstance(value, str): return value
        def repl(m):
            ref = m.group(1)
            return str(self.get(ref, m.group(0)))
        return re.sub(r"\$\{([^}]+)\}", repl, value)

    # ── METADATA ──────────────────────────────────────────────────────

    def register(self, key: str,
                  description: str = "",
                  secret: bool = False,
                  validators: Optional[List[Callable]] = None,
                  on_change: Optional[Callable] = None,
                  required: bool = False,
                  schema_type: Optional[type] = None):
        self._meta[key] = ConfigEntry(
            key=key, value=self.get(key),
            description=description, secret=secret,
            validators=list(validators or []),
            on_change=on_change)
        if required:
            self._required.append(key)
        if schema_type:
            self._schema[key] = schema_type

    def validate_required(self) -> List[str]:
        return [k for k in self._required if self.get(k) is None]

    # ── NAMESPACE ─────────────────────────────────────────────────────

    def namespace(self, prefix: str) -> "ConfigNamespace":
        return ConfigNamespace(self, prefix)

    # ── SNAPSHOT / ROLLBACK ───────────────────────────────────────────

    def snapshot(self, label: str = "") -> ConfigSnapshot:
        snap = ConfigSnapshot(data=self.dump(), label=label)
        self._snapshots.append(snap)
        return snap

    def rollback(self, snapshot_id: str) -> bool:
        snap = next((s for s in self._snapshots
                     if s.snapshot_id == snapshot_id), None)
        if not snap: return False
        self._layers[ConfigSource.RUNTIME].clear()
        for k, v in snap.data.items():
            self._set_layer(ConfigSource.RUNTIME, k, v)
        return True

    def diff(self, snap_id_a: str, snap_id_b: str) -> Dict[str, Tuple]:
        a = next((s for s in self._snapshots if s.snapshot_id == snap_id_a), None)
        b = next((s for s in self._snapshots if s.snapshot_id == snap_id_b), None)
        if not a or not b: return {}
        all_keys = set(a.data) | set(b.data)
        return {k: (a.data.get(k), b.data.get(k))
                for k in all_keys if a.data.get(k) != b.data.get(k)}

    # ── DUMP / EXPORT ─────────────────────────────────────────────────

    def dump(self, include_secrets: bool = False) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        for src in [ConfigSource.DEFAULT, ConfigSource.FILE,
                    ConfigSource.ENV, ConfigSource.RUNTIME,
                    ConfigSource.OVERRIDE]:
            merged.update(self._layers[src])
        if not include_secrets:
            for k, meta in self._meta.items():
                if meta.secret and k in merged:
                    merged[k] = "***"
        return merged

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.dump(), indent=indent, default=str)

    def change_history(self, key: Optional[str] = None,
                        limit: int = 50) -> List[Dict]:
        q = ("SELECT key,old_value,new_value,source,ts FROM cm_changes "
             "ORDER BY ts DESC LIMIT ?")
        rows = self._db.execute(q, (limit,)).fetchall()
        result = [{"key": r[0], "old": r[1], "new": r[2],
                   "source": r[3], "ts": r[4]} for r in rows]
        if key: result = [r for r in result if r["key"] == key]
        return result

    def stats(self) -> Dict[str, Any]:
        return {
            "total_keys": len(self.dump()),
            "snapshots": len(self._snapshots),
            "layers": {src.value: len(layer)
                       for src, layer in self._layers.items()},
        }


class ConfigNamespace:
    """Scoped view of a ConfigManagerV2 under a prefix."""

    def __init__(self, manager: ConfigManagerV2, prefix: str):
        self._mgr = manager
        self._prefix = prefix.rstrip(".")

    def _key(self, key: str) -> str:
        return f"{self._prefix}.{key}"

    def get(self, key: str, default: Any = None) -> Any:
        return self._mgr.get(self._key(key), default)

    def set(self, key: str, value: Any, **kw) -> bool:
        return self._mgr.set(self._key(key), value, **kw)

    def dump(self) -> Dict[str, Any]:
        prefix = self._prefix + "."
        return {k[len(prefix):]: v
                for k, v in self._mgr.dump().items()
                if k.startswith(prefix)}
