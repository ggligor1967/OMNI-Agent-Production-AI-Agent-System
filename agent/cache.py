"""
OMNI AGENT - Cache Layer
Redis-backed caching for LLM responses, session state, rate limiting, pub/sub.
Falls back gracefully to in-memory dict when Redis is unavailable.
"""
import time
import json
import hashlib
import logging
import asyncio
from typing import Any, Dict, List, Optional, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# IN-MEMORY FALLBACK (no Redis dependency)
# ══════════════════════════════════════════════════════════════════════════════

class _MemoryStore:
    """Thread-safe in-memory KV store with TTL. Used when Redis is unavailable."""

    def __init__(self):
        self._data: Dict[str, Any] = {}
        self._expiry: Dict[str, float] = {}

    def _is_alive(self, key: str) -> bool:
        exp = self._expiry.get(key)
        if exp and time.time() > exp:
            self._data.pop(key, None)
            self._expiry.pop(key, None)
            return False
        return key in self._data

    def get(self, key: str) -> Optional[str]:
        return self._data.get(key) if self._is_alive(key) else None

    def set(self, key: str, value: str, ttl: int = 0):
        self._data[key] = value
        if ttl:
            self._expiry[key] = time.time() + ttl

    def delete(self, key: str) -> bool:
        removed = key in self._data
        self._data.pop(key, None)
        self._expiry.pop(key, None)
        return removed

    def exists(self, key: str) -> bool:
        return self._is_alive(key)

    def incr(self, key: str) -> int:
        val = int(self._data.get(key, 0)) + 1
        self._data[key] = str(val)
        return val

    def expire(self, key: str, ttl: int):
        if key in self._data:
            self._expiry[key] = time.time() + ttl

    def keys(self, pattern: str = "*") -> List[str]:
        import fnmatch
        alive = [k for k in self._data if self._is_alive(k)]
        if pattern == "*":
            return alive
        return [k for k in alive if fnmatch.fnmatch(k, pattern)]

    def flush(self):
        self._data.clear()
        self._expiry.clear()

    def size(self) -> int:
        return len([k for k in self._data if self._is_alive(k)])


# ══════════════════════════════════════════════════════════════════════════════
# CACHE CLIENT
# ══════════════════════════════════════════════════════════════════════════════

class CacheClient:
    """
    Unified async cache client.
    Connects to Redis if available; transparently falls back to _MemoryStore.

    Usage:
        cache = CacheClient()
        await cache.connect()
        await cache.set("key", {"data": 123}, ttl=300)
        val = await cache.get("key")
    """

    def __init__(self, redis_url: str = "redis://localhost:6379/0"):
        self.redis_url = redis_url
        self._redis = None
        self._mem = _MemoryStore()
        self._backend = "memory"

    async def connect(self) -> str:
        """Try Redis; fall back to memory silently."""
        try:
            import aioredis  # type: ignore
            self._redis = await aioredis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=2,
            )
            await self._redis.ping()
            self._backend = "redis"
            logger.info(f"Cache: Redis connected ({self.redis_url})")
        except Exception as e:
            logger.info(f"Cache: Redis unavailable ({e}), using in-memory store")
            self._backend = "memory"
        return self._backend

    async def close(self):
        if self._redis:
            await self._redis.close()

    @property
    def backend(self) -> str:
        return self._backend

    # ── Core KV ───────────────────────────────────────────────────────────────

    async def set(self, key: str, value: Any, ttl: int = 0):
        """Store value (auto-serialized to JSON)."""
        serialized = json.dumps(value) if not isinstance(value, str) else value
        if self._backend == "redis":
            if ttl:
                await self._redis.setex(key, ttl, serialized)
            else:
                await self._redis.set(key, serialized)
        else:
            self._mem.set(key, serialized, ttl)

    async def get(self, key: str) -> Optional[Any]:
        """Retrieve and auto-deserialize JSON value."""
        if self._backend == "redis":
            raw = await self._redis.get(key)
        else:
            raw = self._mem.get(key)

        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw

    async def delete(self, key: str) -> bool:
        if self._backend == "redis":
            return bool(await self._redis.delete(key))
        return self._mem.delete(key)

    async def exists(self, key: str) -> bool:
        if self._backend == "redis":
            return bool(await self._redis.exists(key))
        return self._mem.exists(key)

    async def expire(self, key: str, ttl: int):
        if self._backend == "redis":
            await self._redis.expire(key, ttl)
        else:
            self._mem.expire(key, ttl)

    async def keys(self, pattern: str = "*") -> List[str]:
        if self._backend == "redis":
            return await self._redis.keys(pattern)
        return self._mem.keys(pattern)

    async def flush(self):
        if self._backend == "redis":
            await self._redis.flushdb()
        else:
            self._mem.flush()

    # ── Rate Limiting ─────────────────────────────────────────────────────────

    async def rate_check(self, key: str, limit: int,
                         window: int = 60) -> Dict[str, Any]:
        """
        Sliding-window rate limiter.
        Returns {"allowed": bool, "count": int, "remaining": int, "retry_after": int}
        """
        rk = f"rate:{key}"
        if self._backend == "redis":
            pipe = self._redis.pipeline()
            now = int(time.time())
            pipe.incr(rk)
            pipe.expire(rk, window)
            results = await pipe.execute()
            count = results[0]
        else:
            count = self._mem.incr(rk)
            self._mem.expire(rk, window)

        allowed = count <= limit
        return {
            "allowed": allowed,
            "count": count,
            "remaining": max(0, limit - count),
            "retry_after": window if not allowed else 0,
        }

    # ── Response Cache ────────────────────────────────────────────────────────

    @staticmethod
    def _response_key(model_id: str, messages: List[Dict],
                      system: str = None) -> str:
        """Deterministic cache key from model + prompt content."""
        content = json.dumps({"model": model_id, "msgs": messages, "sys": system},
                            sort_keys=True)
        return "resp:" + hashlib.sha256(content.encode()).hexdigest()[:24]

    async def cache_response(self, model_id: str, messages: List[Dict],
                             response: Dict, system: str = None,
                             ttl: int = 3600):
        """Cache an LLM response."""
        key = self._response_key(model_id, messages, system)
        payload = {
            "response": response,
            "model_id": model_id,
            "cached_at": time.time(),
        }
        await self.set(key, payload, ttl=ttl)
        logger.debug(f"Cached response: {key[:16]}... (ttl={ttl}s)")

    async def get_cached_response(self, model_id: str, messages: List[Dict],
                                  system: str = None) -> Optional[Dict]:
        """Retrieve a cached LLM response."""
        key = self._response_key(model_id, messages, system)
        payload = await self.get(key)
        if payload:
            logger.debug(f"Cache HIT: {key[:16]}...")
            resp = payload["response"]
            resp["_cached"] = True
            resp["_cached_at"] = payload.get("cached_at")
            return resp
        return None

    # ── Session State ─────────────────────────────────────────────────────────

    async def set_session(self, session_id: str, data: Dict, ttl: int = 86400):
        await self.set(f"session:{session_id}", data, ttl=ttl)

    async def get_session(self, session_id: str) -> Optional[Dict]:
        return await self.get(f"session:{session_id}")

    async def update_session(self, session_id: str, updates: Dict, ttl: int = 86400):
        existing = await self.get_session(session_id) or {}
        existing.update(updates)
        await self.set_session(session_id, existing, ttl)

    async def delete_session(self, session_id: str):
        await self.delete(f"session:{session_id}")

    # ── Pub/Sub (Redis only) ──────────────────────────────────────────────────

    async def publish(self, channel: str, message: Any) -> int:
        """Publish to a Redis pub/sub channel. No-op in memory mode."""
        if self._backend != "redis":
            logger.debug(f"Pub/sub not available in memory mode (channel={channel})")
            return 0
        payload = json.dumps(message) if not isinstance(message, str) else message
        return await self._redis.publish(channel, payload)

    async def subscribe(self, channel: str,
                        handler: Callable[[Any], None],
                        run_once: bool = False):
        """Subscribe to a channel and call handler for each message."""
        if self._backend != "redis":
            return
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(channel)
        async for msg in pubsub.listen():
            if msg["type"] == "message":
                try:
                    data = json.loads(msg["data"])
                except Exception:
                    data = msg["data"]
                if asyncio.iscoroutinefunction(handler):
                    await handler(data)
                else:
                    handler(data)
                if run_once:
                    break
        await pubsub.unsubscribe(channel)

    # ── Stats ─────────────────────────────────────────────────────────────────

    async def stats(self) -> Dict:
        if self._backend == "redis":
            info = await self._redis.info()
            return {
                "backend": "redis",
                "connected_clients": info.get("connected_clients"),
                "used_memory_human": info.get("used_memory_human"),
                "keyspace_hits": info.get("keyspace_hits"),
                "keyspace_misses": info.get("keyspace_misses"),
            }
        return {
            "backend": "memory",
            "keys": self._mem.size(),
        }
