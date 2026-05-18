"""OMNI AGENT - Config Manager
Hierarchical configuration system: defaults → environment variables →
config files → runtime overrides, with typed values, watchers, and validation.

Features:
- Layers (priority low→high): DEFAULT, FILE, ENV, RUNTIME
- Typed values: string, int, float, bool, json (list/dict)
- Key path: dot-notation access (e.g. "database.host")
- Defaults: built-in fallback per key
- File sources: JSON, TOML-lite (key=value), INI-style; auto-reload on change
- Environment variables: configurable prefix (e.g. APP_DATABASE__HOST)
- Runtime overrides: set/delete in memory without touching files
- Schema validation: required keys, type constraints, min/max ranges
- Watchers: fn(key, old_val, new_val) called on change
- Namespaces: group keys under a prefix (e.g. "db", "server")
- Secrets masking: mark keys as secret; masked in exports/logs
- Snapshot: export current resolved config as dict
- Diff: compare two snapshots
- SQLite persistence: change history, snapshot archive
- REST API: get, set, delete, snapshot, diff, reload
"""
import json, os, re, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Type
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class Layer(str, Enum):
    DEFAULT = "default"; FILE    = "file"
    ENV     = "env";     RUNTIME = "runtime"

class ValueType(str, Enum):
    STRING = "string"; INT    = "int"
    FLOAT  = "float";  BOOL   = "bool"
    JSON   = "json"

def _cast(value: Any, vtype: ValueType) -> Any:
    if value is None: return None
    if vtype == ValueType.STRING: return str(value)
    if vtype == ValueType.INT:    return int(value)
    if vtype == ValueType.FLOAT:  return float(value)
    if vtype == ValueType.BOOL:
        if isinstance(value, bool): return value
        return str(value).lower() in ("true","1","yes","on")
    if vtype == ValueType.JSON:
        if isinstance(value, (dict,list)): return value
        return json.loads(str(value))
    return value

@dataclass
class ConfigKey:
    path: str                             # dot-notation key
    value_type: ValueType = ValueType.STRING
    default: Any = None
    required: bool = False
    secret: bool = False
    description: str = ""
    min_val: Any = None; max_val: Any = None
    allowed_values: List[Any] = field(default_factory=list)

    def validate(self, value: Any) -> Optional[str]:
        if value is None and self.required:
            return f"Required key '{self.path}' is missing"
        if value is None: return None
        if self.allowed_values and value not in self.allowed_values:
            return f"'{self.path}' value {value!r} not in {self.allowed_values}"
        if self.min_val is not None:
            try:
                if value < self.min_val:
                    return f"'{self.path}' {value} < min {self.min_val}"
            except TypeError: pass
        if self.max_val is not None:
            try:
                if value > self.max_val:
                    return f"'{self.path}' {value} > max {self.max_val}"
            except TypeError: pass
        return None

class CMStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS changes(
                    id TEXT PRIMARY KEY, key TEXT,
                    layer TEXT, old_val TEXT,
                    new_val TEXT, ts REAL);
                CREATE TABLE IF NOT EXISTS snapshots(
                    id TEXT PRIMARY KEY, data TEXT,
                    created_at REAL);
                CREATE INDEX IF NOT EXISTS idx_ch_key
                    ON changes(key, ts DESC);
            """)

    def log_change(self, key: str, layer: str,
                    old_val: Any, new_val: Any):
        with self._conn() as c:
            c.execute("INSERT INTO changes VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4())[:8], key, layer,
                 json.dumps(old_val, default=str),
                 json.dumps(new_val, default=str), time.time()))

    def save_snapshot(self, data: Dict) -> str:
        snap_id = str(uuid.uuid4())[:8]
        with self._conn() as c:
            c.execute("INSERT INTO snapshots VALUES(?,?,?)",
                (snap_id, json.dumps(data, default=str), time.time()))
        return snap_id

    def load_snapshot(self, snap_id: str) -> Optional[Dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT data FROM snapshots WHERE id=?", (snap_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def recent_changes(self, limit: int = 50) -> List[Dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM changes ORDER BY ts DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

def _parse_json_file(path: str) -> Dict:
    with open(path) as f: return json.load(f)

def _parse_env_file(path: str, prefix: str = "") -> Dict:
    """Parse KEY=value lines, applying optional prefix filter."""
    result = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"): continue
            if "=" not in line: continue
            k, _, v = line.partition("=")
            k = k.strip().lower()
            if prefix and not k.startswith(prefix.lower()): continue
            if prefix: k = k[len(prefix):]
            k = k.replace("__", ".")
            result[k] = v.strip()
    return result

class ConfigManager:
    """
    Hierarchical config with defaults, files, env vars, and runtime overrides.

    Usage:
        cm = ConfigManager(env_prefix="APP_")
        cm.define("server.host", ValueType.STRING, default="localhost")
        cm.define("server.port", ValueType.INT,    default=8080, min_val=1, max_val=65535)
        cm.define("db.password", ValueType.STRING, secret=True, required=True)

        cm.load_env()   # reads APP_SERVER__HOST, APP_SERVER__PORT, etc.
        cm.set("server.port", 9090, layer=Layer.RUNTIME)

        host = cm.get("server.host")          # "localhost"
        port = cm.get("server.port")          # 9090 (runtime overrides env)
        cfg  = cm.snapshot()                  # full dict of resolved values
    """
    def __init__(self, db_path: str = "data/config.db",
                 env_prefix: str = ""):
        self._store = CMStore(db_path)
        self._env_prefix = env_prefix
        self._schema:   Dict[str, ConfigKey] = {}
        self._layers:   Dict[Layer, Dict[str, Any]] = {l: {} for l in Layer}
        self._secrets:  Set[str] = set()
        self._watchers: Dict[str, List[Callable]] = {}  # key → [fn]
        self._global_watchers: List[Callable] = []

    def define(self, path: str,
                value_type: ValueType = ValueType.STRING,
                default: Any = None,
                required: bool = False,
                secret: bool = False,
                description: str = "",
                min_val: Any = None, max_val: Any = None,
                allowed_values: List[Any] = None) -> ConfigKey:
        ck = ConfigKey(path=path, value_type=value_type,
                        default=default, required=required,
                        secret=secret, description=description,
                        min_val=min_val, max_val=max_val,
                        allowed_values=list(allowed_values or []))
        self._schema[path] = ck
        if default is not None:
            self._layers[Layer.DEFAULT][path] = _cast(default, value_type)
        if secret: self._secrets.add(path)
        return ck

    def _resolve(self, path: str) -> Tuple[Any, Layer]:
        """Return (value, layer) for the highest-priority layer that has the key."""
        for layer in reversed(list(Layer)):  # RUNTIME → ENV → FILE → DEFAULT
            if path in self._layers[layer]:
                return self._layers[layer][path], layer
        return None, Layer.DEFAULT

    def get(self, path: str, default: Any = None) -> Any:
        val, _ = self._resolve(path)
        if val is None: return default
        ck = self._schema.get(path)
        if ck: return _cast(val, ck.value_type)
        return val

    def get_int(self, path: str, default: int = 0) -> int:
        return int(self.get(path, default) or default)

    def get_bool(self, path: str, default: bool = False) -> bool:
        v = self.get(path, default)
        return _cast(v, ValueType.BOOL)

    def get_ns(self, namespace: str) -> Dict[str, Any]:
        """Return all resolved values under a namespace prefix."""
        prefix = namespace.rstrip(".") + "."
        keys = set()
        for layer_data in self._layers.values():
            keys.update(k for k in layer_data if k.startswith(prefix))
        return {k[len(prefix):]: self.get(k) for k in keys}

    def set(self, path: str, value: Any,
             layer: Layer = Layer.RUNTIME,
             author: str = "system"):
        old_val, _ = self._resolve(path)
        ck = self._schema.get(path)
        if ck: value = _cast(value, ck.value_type)
        self._layers[layer][path] = value
        self._store.log_change(path, layer.value, old_val, value)
        self._fire_watchers(path, old_val, value)

    def delete(self, path: str, layer: Layer = Layer.RUNTIME):
        old_val, _ = self._resolve(path)
        self._layers[layer].pop(path, None)
        new_val, _ = self._resolve(path)
        if new_val != old_val:
            self._store.log_change(path, layer.value, old_val, new_val)
            self._fire_watchers(path, old_val, new_val)

    def load_json_file(self, path: str, flatten_sep: str = "."):
        """Load a JSON config file; nested dicts become dot-notation keys."""
        raw = _parse_json_file(path)
        def _flatten(d, prefix=""):
            for k, v in d.items():
                full = f"{prefix}{k}" if not prefix else f"{prefix}{flatten_sep}{k}"
                if isinstance(v, dict): _flatten(v, full)
                else:
                    ck = self._schema.get(full)
                    self._layers[Layer.FILE][full] = (
                        _cast(v, ck.value_type) if ck else v)
        _flatten(raw)

    def load_env(self, env: Dict[str, str] = None):
        """Read environment variables (or provided dict) matching prefix."""
        source = env if env is not None else dict(os.environ)
        prefix = self._env_prefix.upper()
        for k, v in source.items():
            if prefix and not k.upper().startswith(prefix): continue
            key = k[len(prefix):].lower().replace("__", ".")
            ck = self._schema.get(key)
            self._layers[Layer.ENV][key] = (
                _cast(v, ck.value_type) if ck else v)

    def load_env_file(self, path: str):
        data = _parse_env_file(path, self._env_prefix)
        for k, v in data.items():
            ck = self._schema.get(k)
            self._layers[Layer.ENV][k] = _cast(v, ck.value_type) if ck else v

    def watch(self, path: str, fn: Callable):
        self._watchers.setdefault(path, []).append(fn)

    def watch_all(self, fn: Callable): self._global_watchers.append(fn)

    def _fire_watchers(self, path: str, old: Any, new: Any):
        for fn in self._watchers.get(path, []):
            try: fn(path, old, new)
            except: pass
        for fn in self._global_watchers:
            try: fn(path, old, new)
            except: pass

    def validate(self) -> List[str]:
        errors = []
        for path, ck in self._schema.items():
            val = self.get(path)
            err = ck.validate(val)
            if err: errors.append(err)
        return errors

    def snapshot(self, mask_secrets: bool = True) -> Dict[str, Any]:
        all_keys: Set[str] = set()
        for layer_data in self._layers.values():
            all_keys.update(layer_data.keys())
        result = {}
        for k in sorted(all_keys):
            val = self.get(k)
            if mask_secrets and k in self._secrets:
                val = "***"
            result[k] = val
        return result

    def save_snapshot(self) -> str:
        return self._store.save_snapshot(self.snapshot())

    def diff(self, snap_id: str) -> Dict[str, Dict]:
        old_snap = self._store.load_snapshot(snap_id)
        if not old_snap: return {}
        current = self.snapshot()
        changes = {}
        all_keys = set(old_snap.keys()) | set(current.keys())
        for k in all_keys:
            o, n = old_snap.get(k), current.get(k)
            if o != n: changes[k] = {"old": o, "new": n}
        return changes

    def stats(self) -> Dict:
        return {"schema_keys": len(self._schema),
                "secrets": len(self._secrets),
                "watchers": sum(len(v) for v in self._watchers.values()),
                "layers": {l.value: len(d) for l, d in self._layers.items()}}

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def get_ep(req):
            key = req.match_info["key"].replace("~", ".")
            val = self.get(key)
            masked = key in self._secrets
            return web.json_response({"key": key,
                "value": "***" if masked else val, "found": val is not None})
        async def set_ep(req):
            d = await req.json()
            self.set(d["key"], d["value"], Layer(d.get("layer","runtime")))
            return web.json_response({"set": True})
        async def snapshot_ep(req):
            return web.json_response({"snapshot": self.snapshot()})
        async def validate_ep(req):
            errs = self.validate()
            return web.json_response({"valid": not errs, "errors": errs})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/config"
        app.router.add_get( f"{p}/{{key}}",  get_ep)
        app.router.add_post(f"{p}/set",      set_ep)
        app.router.add_get( f"{p}/snapshot", snapshot_ep)
        app.router.add_get( f"{p}/validate", validate_ep)
        app.router.add_get( f"{p}/stats",    stats_ep)
        logger.info(f"Config manager API at {prefix}/config/")

    # ── Hot-reload file watcher ───────────────────────────────────────────────

    async def start_watcher(self, file_path: str, interval: float = 10.0):
        """Start a background task that polls a .env file for changes."""
        import asyncio
        self._watcher_path = file_path
        self._watcher_running = True
        self._watcher_mtime: float = 0.0
        async def _watch():
            while self._watcher_running:
                try:
                    p = Path(file_path)
                    if p.exists():
                        mtime = p.stat().st_mtime
                        if mtime != self._watcher_mtime:
                            self._watcher_mtime = mtime
                            self.load_env_file(file_path)
                            logger.info(f"Config hot-reloaded from {file_path}")
                except Exception as e:
                    logger.debug(f"Config watcher error: {e}")
                await asyncio.sleep(interval)
        self._watcher_task = asyncio.create_task(_watch())
        logger.debug(f"Config watcher started for {file_path} (interval={interval}s)")

    async def stop_watcher(self):
        """Stop the hot-reload file watcher task."""
        self._watcher_running = False
        task = getattr(self, '_watcher_task', None)
        if task:
            task.cancel()

    def flag(self, key: str, default: bool = False) -> bool:
        """Convenience: read a boolean feature flag."""
        return self.get_bool(key, default)
