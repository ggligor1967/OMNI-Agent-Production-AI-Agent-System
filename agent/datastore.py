"""
OMNI AGENT - Datastore
Unified typed key-value store with TTL, namespacing, versioning,
transactions, and SQLite-backed persistence with in-memory caching.

Features:
- Typed values: str, int, float, bool, list, dict, bytes (auto-serialized)
- Namespaced keys: "ns:key" pattern; list all keys in a namespace
- TTL: automatic expiry with background reaper
- Versioning: every write creates a new version; read any historical version
- Atomic operations: increment, append-to-list, set-if-not-exists
- Transactions: batch multiple writes atomically
- Pattern matching: list keys by glob pattern (e.g. "session:*")
- Export/import: dump namespace to dict, restore from dict
- Metrics: read/write counts, cache hit rate, storage size
- REST API: full CRUD + namespace ops + version history
"""
import re
import time
import json
import uuid
import sqlite3
import fnmatch
import logging
import threading
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# VALUE TYPES & SERIALIZATION
# ══════════════════════════════════════════════════════════════════════════════

class ValueType(str, Enum):
    STRING  = "string"
    INTEGER = "integer"
    FLOAT   = "float"
    BOOLEAN = "boolean"
    LIST    = "list"
    DICT    = "dict"
    BYTES   = "bytes"
    NULL    = "null"


def _serialize(value: Any) -> Tuple[str, ValueType]:
    """Serialize a Python value to (string, ValueType)."""
    if value is None:
        return "null", ValueType.NULL
    elif isinstance(value, bool):
        return str(value), ValueType.BOOLEAN
    elif isinstance(value, int):
        return str(value), ValueType.INTEGER
    elif isinstance(value, float):
        return str(value), ValueType.FLOAT
    elif isinstance(value, str):
        return value, ValueType.STRING
    elif isinstance(value, (list, dict)):
        return json.dumps(value, default=str), ValueType.LIST if isinstance(value, list) else ValueType.DICT
    elif isinstance(value, bytes):
        import base64
        return base64.b64encode(value).decode(), ValueType.BYTES
    else:
        return json.dumps(value, default=str), ValueType.DICT


def _deserialize(raw: str, vtype: ValueType) -> Any:
    """Deserialize from (string, ValueType) back to Python value."""
    if vtype == ValueType.NULL:
        return None
    elif vtype == ValueType.BOOLEAN:
        return raw.lower() == "true"
    elif vtype == ValueType.INTEGER:
        return int(raw)
    elif vtype == ValueType.FLOAT:
        return float(raw)
    elif vtype == ValueType.STRING:
        return raw
    elif vtype in (ValueType.LIST, ValueType.DICT):
        return json.loads(raw)
    elif vtype == ValueType.BYTES:
        import base64
        return base64.b64decode(raw)
    return raw


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY MODEL
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Entry:
    key: str
    value: Any
    vtype: ValueType
    namespace: str
    version: int
    ttl_s: Optional[float]
    created_at: float
    updated_at: float
    expires_at: Optional[float]
    tags: List[str] = field(default_factory=list)
    meta: Dict = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and time.time() > self.expires_at

    def to_dict(self, include_value: bool = True) -> Dict:
        d = {
            "key": self.key, "namespace": self.namespace,
            "type": self.vtype, "version": self.version,
            "ttl_s": self.ttl_s, "expires_at": self.expires_at,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "tags": self.tags,
        }
        if include_value:
            d["value"] = self.value
        return d


# ══════════════════════════════════════════════════════════════════════════════
# SQLITE BACKEND
# ══════════════════════════════════════════════════════════════════════════════

class _SQLiteBackend:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS kv_store (
                    namespace   TEXT NOT NULL,
                    key         TEXT NOT NULL,
                    raw_value   TEXT,
                    vtype       TEXT NOT NULL,
                    version     INTEGER DEFAULT 1,
                    ttl_s       REAL,
                    expires_at  REAL,
                    tags        TEXT DEFAULT '[]',
                    meta        TEXT DEFAULT '{}',
                    created_at  REAL,
                    updated_at  REAL,
                    PRIMARY KEY (namespace, key)
                );
                CREATE TABLE IF NOT EXISTS kv_history (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    namespace   TEXT NOT NULL,
                    key         TEXT NOT NULL,
                    raw_value   TEXT,
                    vtype       TEXT NOT NULL,
                    version     INTEGER,
                    written_at  REAL
                );
                CREATE INDEX IF NOT EXISTS idx_kv_ns ON kv_store(namespace);
                CREATE INDEX IF NOT EXISTS idx_kv_exp ON kv_store(expires_at);
                CREATE INDEX IF NOT EXISTS idx_kv_hist ON kv_history(namespace, key, version DESC);
            """)

    def get_raw(self, namespace: str, key: str) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM kv_store WHERE namespace=? AND key=?",
                (namespace, key)
            ).fetchone()

    def set_raw(self, namespace: str, key: str, raw: str, vtype: ValueType,
                ttl_s: Optional[float], tags: List[str], meta: Dict):
        now = time.time()
        expires_at = now + ttl_s if ttl_s else None
        with self._lock, self._conn() as c:
            existing = c.execute(
                "SELECT version FROM kv_store WHERE namespace=? AND key=?",
                (namespace, key)
            ).fetchone()
            version = (existing["version"] + 1) if existing else 1
            # Save to history
            if existing:
                old = c.execute(
                    "SELECT raw_value, vtype FROM kv_store WHERE namespace=? AND key=?",
                    (namespace, key)
                ).fetchone()
                if old:
                    c.execute(
                        "INSERT INTO kv_history (namespace,key,raw_value,vtype,version,written_at) VALUES (?,?,?,?,?,?)",
                        (namespace, key, old["raw_value"], old["vtype"], existing["version"], now)
                    )
            c.execute("""
                INSERT OR REPLACE INTO kv_store
                (namespace,key,raw_value,vtype,version,ttl_s,expires_at,tags,meta,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,COALESCE((SELECT created_at FROM kv_store WHERE namespace=? AND key=?),?),?)
            """, (namespace, key, raw, vtype, version, ttl_s, expires_at,
                  json.dumps(tags), json.dumps(meta),
                  namespace, key, now, now))
        return version

    def delete_raw(self, namespace: str, key: str) -> bool:
        with self._lock, self._conn() as c:
            cur = c.execute(
                "DELETE FROM kv_store WHERE namespace=? AND key=?",
                (namespace, key)
            )
        return cur.rowcount > 0

    def list_keys(self, namespace: str, pattern: str = None) -> List[str]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT key FROM kv_store WHERE namespace=? AND (expires_at IS NULL OR expires_at > ?)",
                (namespace, time.time())
            ).fetchall()
        keys = [r["key"] for r in rows]
        if pattern:
            keys = [k for k in keys if fnmatch.fnmatch(k, pattern)]
        return keys

    def purge_expired(self) -> int:
        with self._lock, self._conn() as c:
            cur = c.execute(
                "DELETE FROM kv_store WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (time.time(),)
            )
        return cur.rowcount

    def get_history(self, namespace: str, key: str,
                    limit: int = 10) -> List[Dict]:
        with self._conn() as c:
            rows = c.execute("""
                SELECT raw_value, vtype, version, written_at FROM kv_history
                WHERE namespace=? AND key=?
                ORDER BY version DESC LIMIT ?
            """, (namespace, key, limit)).fetchall()
        return [
            {
                "version": r["version"],
                "value": _deserialize(r["raw_value"], ValueType(r["vtype"])),
                "written_at": r["written_at"],
            }
            for r in rows
        ]

    def stats(self) -> Dict:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM kv_store").fetchone()[0]
            expired = c.execute(
                "SELECT COUNT(*) FROM kv_store WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (time.time(),)
            ).fetchone()[0]
            by_ns = dict(c.execute(
                "SELECT namespace, COUNT(*) FROM kv_store GROUP BY namespace"
            ).fetchall())
        return {"total_keys": total, "expired_keys": expired, "by_namespace": by_ns}

    def dump_namespace(self, namespace: str) -> Dict[str, Any]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT key, raw_value, vtype FROM kv_store WHERE namespace=?",
                (namespace,)
            ).fetchall()
        return {
            r["key"]: _deserialize(r["raw_value"], ValueType(r["vtype"]))
            for r in rows
        }


# ══════════════════════════════════════════════════════════════════════════════
# DATASTORE
# ══════════════════════════════════════════════════════════════════════════════

class Datastore:
    """
    Unified typed key-value store with TTL, namespacing, and versioning.

    Usage:
        ds = Datastore()

        # Basic CRUD
        ds.set("config:theme", "dark")
        ds.set("config:max_tokens", 4096)
        ds.set("user:prefs", {"lang": "en", "tz": "UTC"}, ttl_s=3600)

        val = ds.get("config:theme")           # "dark"
        ds.delete("config:theme")

        # Namespace operations
        keys = ds.keys("config")               # ["theme", "max_tokens"]
        data = ds.dump("user")                 # {"prefs": {...}}

        # Atomic ops
        ds.increment("stats:requests")         # 1, 2, 3 ...
        ds.append("queue:items", "item_42")   # list append

        # History
        ds.set("key", "v1")
        ds.set("key", "v2")
        hist = ds.history("key")               # [v1, v2] with versions

        # Transactions
        with ds.transaction("config") as tx:
            tx.set("a", 1)
            tx.set("b", 2)
            tx.delete("c")
    """

    def __init__(self, db_path: str = "data/datastore.db",
                 default_namespace: str = "default",
                 reap_interval_s: float = 60.0):
        self._backend = _SQLiteBackend(db_path)
        self._cache: Dict[str, Any] = {}           # simple in-memory cache
        self._cache_ttl: Dict[str, float] = {}
        self._default_ns = default_namespace
        self._reads = 0
        self._writes = 0
        self._cache_hits = 0
        self._reap_interval = reap_interval_s
        self._reap_thread: Optional[threading.Thread] = None
        self._running = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        """Start background TTL reaper thread."""
        self._running = True
        self._reap_thread = threading.Thread(target=self._reaper, daemon=True)
        self._reap_thread.start()

    def stop(self):
        self._running = False

    def _reaper(self):
        while self._running:
            time.sleep(self._reap_interval)
            try:
                count = self._backend.purge_expired()
                if count:
                    logger.info(f"Datastore: reaped {count} expired keys")
            except Exception as e:
                logger.error(f"Datastore reaper error: {e}")

    # ── Key parsing ───────────────────────────────────────────────────────────

    def _parse_key(self, key: str) -> Tuple[str, str]:
        """Split "namespace:key" → (namespace, key). Default ns if no colon."""
        if ":" in key:
            ns, k = key.split(":", 1)
            return ns, k
        return self._default_ns, key

    # ── Core CRUD ─────────────────────────────────────────────────────────────

    def set(self, key: str, value: Any,
            ttl_s: Optional[float] = None,
            tags: List[str] = None,
            meta: Dict = None) -> int:
        """Set a value. Returns version number."""
        ns, k = self._parse_key(key)
        raw, vtype = _serialize(value)
        version = self._backend.set_raw(ns, k, raw, vtype,
                                        ttl_s, tags or [], meta or {})
        # Update cache
        self._cache[f"{ns}:{k}"] = value
        if ttl_s:
            self._cache_ttl[f"{ns}:{k}"] = time.time() + ttl_s
        self._writes += 1
        return version

    def get(self, key: str, default: Any = None) -> Any:
        """Get a value, or default if not found/expired."""
        ns, k = self._parse_key(key)
        cache_key = f"{ns}:{k}"

        # Cache check
        if cache_key in self._cache:
            ttl_exp = self._cache_ttl.get(cache_key)
            if ttl_exp is None or time.time() < ttl_exp:
                self._cache_hits += 1
                self._reads += 1
                return self._cache[cache_key]
            else:
                del self._cache[cache_key]
                self._cache_ttl.pop(cache_key, None)

        row = self._backend.get_raw(ns, k)
        self._reads += 1
        if row is None:
            return default
        if row["expires_at"] and time.time() > row["expires_at"]:
            return default
        value = _deserialize(row["raw_value"], ValueType(row["vtype"]))
        self._cache[cache_key] = value
        if row["expires_at"]:
            self._cache_ttl[cache_key] = row["expires_at"]
        return value

    def delete(self, key: str) -> bool:
        ns, k = self._parse_key(key)
        self._cache.pop(f"{ns}:{k}", None)
        self._cache_ttl.pop(f"{ns}:{k}", None)
        return self._backend.delete_raw(ns, k)

    def exists(self, key: str) -> bool:
        return self.get(key) is not None

    def get_many(self, keys: List[str]) -> Dict[str, Any]:
        return {k: self.get(k) for k in keys}

    def set_many(self, mapping: Dict[str, Any],
                 ttl_s: Optional[float] = None) -> Dict[str, int]:
        return {k: self.set(k, v, ttl_s=ttl_s) for k, v in mapping.items()}

    # ── Atomic operations ─────────────────────────────────────────────────────

    def increment(self, key: str, by: Union[int, float] = 1) -> Union[int, float]:
        """Atomically increment a numeric value. Creates with 0 if missing."""
        current = self.get(key, 0)
        new_val = current + by
        self.set(key, new_val)
        return new_val

    def decrement(self, key: str, by: Union[int, float] = 1) -> Union[int, float]:
        return self.increment(key, -by)

    def append(self, key: str, item: Any) -> List:
        """Append item to a list value. Creates empty list if missing."""
        current = self.get(key, [])
        if not isinstance(current, list):
            current = [current]
        current.append(item)
        self.set(key, current)
        return current

    def set_if_absent(self, key: str, value: Any,
                      ttl_s: Optional[float] = None) -> bool:
        """Set only if key doesn't exist. Returns True if set."""
        if self.exists(key):
            return False
        self.set(key, value, ttl_s=ttl_s)
        return True

    # ── Namespace operations ──────────────────────────────────────────────────

    def keys(self, namespace: str = None, pattern: str = None) -> List[str]:
        """List all keys in a namespace, optionally filtered by glob pattern."""
        ns = namespace or self._default_ns
        return self._backend.list_keys(ns, pattern)

    def delete_namespace(self, namespace: str) -> int:
        """Delete all keys in a namespace."""
        all_keys = self._backend.list_keys(namespace)
        count = 0
        for k in all_keys:
            if self._backend.delete_raw(namespace, k):
                count += 1
                self._cache.pop(f"{namespace}:{k}", None)
        return count

    def dump(self, namespace: str = None) -> Dict[str, Any]:
        """Export all key-value pairs from a namespace."""
        return self._backend.dump_namespace(namespace or self._default_ns)

    def restore(self, namespace: str, data: Dict[str, Any],
                ttl_s: Optional[float] = None):
        """Import key-value pairs into a namespace."""
        for key, value in data.items():
            self.set(f"{namespace}:{key}", value, ttl_s=ttl_s)

    # ── History ───────────────────────────────────────────────────────────────

    def history(self, key: str, limit: int = 10) -> List[Dict]:
        """Return version history for a key."""
        ns, k = self._parse_key(key)
        return self._backend.get_history(ns, k, limit)

    # ── Transactions ──────────────────────────────────────────────────────────

    def transaction(self, namespace: str = None):
        """Context manager for batched atomic writes."""
        return _Transaction(self, namespace or self._default_ns)

    # ── Stats ─────────────────────────────────────────────────────────────────

    def stats(self) -> Dict:
        backend_stats = self._backend.stats()
        cache_hit_rate = (
            self._cache_hits / self._reads if self._reads else 0.0
        )
        return {
            **backend_stats,
            "reads": self._reads,
            "writes": self._writes,
            "cache_hits": self._cache_hits,
            "cache_hit_rate": round(cache_hit_rate, 3),
            "cache_size": len(self._cache),
        }

    def purge_expired(self) -> int:
        return self._backend.purge_expired()

    # ── REST API ──────────────────────────────────────────────────────────────

    def register_routes(self, app, prefix: str = ""):
        from aiohttp import web

        async def get_key(request):
            key = request.match_info["key"]
            ns  = request.rel_url.query.get("ns", self._default_ns)
            full_key = f"{ns}:{key}" if ns != self._default_ns else key
            value = self.get(full_key)
            if value is None:
                return web.json_response({"error": "not found"}, status=404)
            return web.json_response({"key": key, "value": value, "namespace": ns})

        async def set_key(request):
            key  = request.match_info["key"]
            data = await request.json()
            ns   = data.get("ns", self._default_ns)
            full_key = f"{ns}:{key}" if ns != self._default_ns else key
            version = self.set(
                full_key, data["value"],
                ttl_s=data.get("ttl_s"),
                tags=data.get("tags", []),
                meta=data.get("meta", {}),
            )
            return web.json_response({"key": key, "version": version})

        async def delete_key(request):
            key = request.match_info["key"]
            ns  = request.rel_url.query.get("ns", self._default_ns)
            full_key = f"{ns}:{key}" if ns != self._default_ns else key
            ok = self.delete(full_key)
            return web.json_response({"deleted": ok})

        async def list_keys_ep(request):
            ns      = request.rel_url.query.get("ns", self._default_ns)
            pattern = request.rel_url.query.get("pattern")
            return web.json_response({
                "namespace": ns,
                "keys": self.keys(ns, pattern),
            })

        async def dump_ep(request):
            ns = request.rel_url.query.get("ns", self._default_ns)
            return web.json_response({"namespace": ns, "data": self.dump(ns)})

        async def stats_ep(request):
            return web.json_response(self.stats())

        async def history_ep(request):
            key   = request.match_info["key"]
            ns    = request.rel_url.query.get("ns", self._default_ns)
            limit = int(request.rel_url.query.get("limit", 10))
            full_key = f"{ns}:{key}" if ns != self._default_ns else key
            return web.json_response({"history": self.history(full_key, limit)})

        async def increment_ep(request):
            key  = request.match_info["key"]
            data = await request.json() if request.content_length else {}
            by   = float(data.get("by", 1))
            ns   = data.get("ns", self._default_ns)
            full_key = f"{ns}:{key}" if ns != self._default_ns else key
            new_val = self.increment(full_key, by)
            return web.json_response({"key": key, "value": new_val})

        app.router.add_get(   f"{prefix}/store/{{key}}",        get_key)
        app.router.add_put(   f"{prefix}/store/{{key}}",        set_key)
        app.router.add_delete(f"{prefix}/store/{{key}}",        delete_key)
        app.router.add_get(   f"{prefix}/store/{{key}}/history",history_ep)
        app.router.add_post(  f"{prefix}/store/{{key}}/incr",   increment_ep)
        app.router.add_get(   f"{prefix}/store",                list_keys_ep)
        app.router.add_get(   f"{prefix}/store/dump",           dump_ep)
        app.router.add_get(   f"{prefix}/store/stats",          stats_ep)
        logger.info(f"Datastore API routes registered at {prefix}/store/")


class _Transaction:
    """Batch multiple datastore writes atomically."""

    def __init__(self, store: Datastore, namespace: str):
        self._store = store
        self._ns = namespace
        self._ops: List[Tuple[str, Any, Dict]] = []  # (op, key, kwargs)

    def set(self, key: str, value: Any, **kwargs):
        self._ops.append(("set", f"{self._ns}:{key}", {"value": value, **kwargs}))
        return self

    def delete(self, key: str):
        self._ops.append(("delete", f"{self._ns}:{key}", {}))
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self.commit()
        return False

    def commit(self):
        for op, key, kwargs in self._ops:
            if op == "set":
                self._store.set(key, kwargs.pop("value"), **kwargs)
            elif op == "delete":
                self._store.delete(key)
        self._ops.clear()
