import logging
from pathlib import Path

import pytest


class _FakeRedisClient:
    def __init__(self, fail_ping: bool = False):
        self.fail_ping = fail_ping
        self.closed = False

    async def ping(self):
        if self.fail_ping:
            raise RuntimeError("redis unavailable")
        return True

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_memory_fallback_works_when_redis_connect_fails(monkeypatch):
    import redis.asyncio as redis_asyncio
    from agent.cache import CacheClient

    monkeypatch.setattr(
        redis_asyncio,
        "from_url",
        lambda *args, **kwargs: _FakeRedisClient(fail_ping=True),
    )

    cache = CacheClient("redis://cache.example:6379/0")
    backend = await cache.connect()

    assert backend == "memory"
    assert cache.backend == "memory"

    await cache.set("fallback-key", {"ok": True})
    assert await cache.get("fallback-key") == {"ok": True}


@pytest.mark.asyncio
async def test_redis_backend_initializes_with_redis_asyncio(monkeypatch):
    import redis.asyncio as redis_asyncio
    from agent.cache import CacheClient

    captured = {}
    fake_client = _FakeRedisClient()

    def fake_from_url(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return fake_client

    monkeypatch.setattr(redis_asyncio, "from_url", fake_from_url)

    cache = CacheClient("redis://cache.example:6379/9")
    backend = await cache.connect()

    assert backend == "redis"
    assert cache.backend == "redis"
    assert cache._redis is fake_client
    assert captured["url"] == "redis://cache.example:6379/9"
    assert captured["kwargs"]["decode_responses"] is True
    assert captured["kwargs"]["encoding"] == "utf-8"

    await cache.close()
    assert fake_client.closed is True


@pytest.mark.asyncio
async def test_redis_connection_failure_logs_explicit_fallback(monkeypatch, caplog):
    import redis.asyncio as redis_asyncio
    from agent.cache import CacheClient

    monkeypatch.setattr(
        redis_asyncio,
        "from_url",
        lambda *args, **kwargs: _FakeRedisClient(fail_ping=True),
    )

    cache = CacheClient()
    with caplog.at_level(logging.WARNING):
        backend = await cache.connect()

    assert backend == "memory"
    assert any(
        "falling back to in-memory store" in record.getMessage()
        for record in caplog.records
    )


def test_no_legacy_redis_import_remains_in_active_cache_path():
    cache_source = Path("agent/cache.py").read_text(encoding="utf-8")
    requirements = Path("requirements.txt").read_text(encoding="utf-8")
    legacy_name = "aio" + "redis"

    assert legacy_name not in cache_source
    assert "redis.asyncio" in cache_source
    assert legacy_name not in requirements
