"""
OMNI AGENT - Database Abstraction Layer
Unified async interface for SQLite (default) and PostgreSQL.
Switch via DB_BACKEND env var: 'sqlite' | 'postgres'
"""
import os
import json
import logging
import asyncio
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path

logger = logging.getLogger(__name__)

DB_BACKEND = os.getenv("DB_BACKEND", "sqlite")  # 'sqlite' | 'postgres'
POSTGRES_DSN = os.getenv(
    "POSTGRES_DSN",
    "postgresql://omni:omni@localhost:5432/omni_agent"
)


# ══════════════════════════════════════════════════════════════════════════════
# ABSTRACT INTERFACE
# ══════════════════════════════════════════════════════════════════════════════

class DBBackend(ABC):
    """Abstract async database backend."""

    @abstractmethod
    async def connect(self): ...

    @abstractmethod
    async def close(self): ...

    @abstractmethod
    async def execute(self, sql: str, params: Tuple = ()) -> None: ...

    @abstractmethod
    async def executemany(self, sql: str, params_list: List[Tuple]) -> None: ...

    @abstractmethod
    async def fetchone(self, sql: str, params: Tuple = ()) -> Optional[Dict]: ...

    @abstractmethod
    async def fetchall(self, sql: str, params: Tuple = ()) -> List[Dict]: ...

    @abstractmethod
    async def executescript(self, script: str) -> None: ...


# ══════════════════════════════════════════════════════════════════════════════
# SQLITE ASYNC BACKEND (via asyncio.to_thread)
# ══════════════════════════════════════════════════════════════════════════════

class SQLiteBackend(DBBackend):
    def __init__(self, db_path: str = "data/omni_agent.db"):
        import sqlite3
        self.db_path = db_path
        self._sqlite3 = sqlite3
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    def _sync_connect(self):
        conn = self._sqlite3.connect(self.db_path)
        conn.row_factory = self._sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    async def connect(self):
        logger.info(f"SQLite backend: {self.db_path}")

    async def close(self):
        pass  # SQLite connections are per-operation

    def _dict(self, row) -> Optional[Dict]:
        return dict(row) if row else None

    async def execute(self, sql: str, params: Tuple = ()):
        def _run():
            with self._sync_connect() as conn:
                conn.execute(sql, params)
                conn.commit()
        await asyncio.to_thread(_run)

    async def executemany(self, sql: str, params_list: List[Tuple]):
        def _run():
            with self._sync_connect() as conn:
                conn.executemany(sql, params_list)
                conn.commit()
        await asyncio.to_thread(_run)

    async def fetchone(self, sql: str, params: Tuple = ()) -> Optional[Dict]:
        def _run():
            conn = self._sync_connect()
            row = conn.execute(sql, params).fetchone()
            conn.close()
            return self._dict(row)
        return await asyncio.to_thread(_run)

    async def fetchall(self, sql: str, params: Tuple = ()) -> List[Dict]:
        def _run():
            conn = self._sync_connect()
            rows = conn.execute(sql, params).fetchall()
            conn.close()
            return [self._dict(r) for r in rows]
        return await asyncio.to_thread(_run)

    async def executescript(self, script: str):
        def _run():
            with self._sync_connect() as conn:
                conn.executescript(script)
        await asyncio.to_thread(_run)


# ══════════════════════════════════════════════════════════════════════════════
# POSTGRESQL ASYNC BACKEND (asyncpg)
# ══════════════════════════════════════════════════════════════════════════════

class PostgresBackend(DBBackend):
    def __init__(self, dsn: str = POSTGRES_DSN):
        self.dsn = dsn
        self._pool = None

    async def connect(self):
        try:
            import asyncpg
            self._pool = await asyncpg.create_pool(self.dsn, min_size=2, max_size=10)
            logger.info(f"PostgreSQL connected: {self.dsn.split('@')[-1]}")
        except ImportError:
            raise RuntimeError("asyncpg not installed. Run: pip install asyncpg")

    async def close(self):
        if self._pool:
            await self._pool.close()

    def _row_to_dict(self, record) -> Dict:
        return dict(record) if record else {}

    async def execute(self, sql: str, params: Tuple = ()):
        # Convert ? placeholders to $1,$2,... for asyncpg
        sql = self._convert_placeholders(sql)
        async with self._pool.acquire() as conn:
            await conn.execute(sql, *params)

    async def executemany(self, sql: str, params_list: List[Tuple]):
        sql = self._convert_placeholders(sql)
        async with self._pool.acquire() as conn:
            await conn.executemany(sql, params_list)

    async def fetchone(self, sql: str, params: Tuple = ()) -> Optional[Dict]:
        sql = self._convert_placeholders(sql)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(sql, *params)
        return self._row_to_dict(row) if row else None

    async def fetchall(self, sql: str, params: Tuple = ()) -> List[Dict]:
        sql = self._convert_placeholders(sql)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        return [self._row_to_dict(r) for r in rows]

    async def executescript(self, script: str):
        # Split on semicolons for postgres
        async with self._pool.acquire() as conn:
            await conn.execute(script)

    @staticmethod
    def _convert_placeholders(sql: str) -> str:
        """Convert SQLite ? placeholders to PostgreSQL $1, $2, ..."""
        import re
        counter = [0]
        def replace(m):
            counter[0] += 1
            return f"${counter[0]}"
        return re.sub(r'\?', replace, sql)


# ══════════════════════════════════════════════════════════════════════════════
# FACTORY
# ══════════════════════════════════════════════════════════════════════════════

def create_backend(backend: str = None, **kwargs) -> DBBackend:
    """Factory function — returns configured DB backend."""
    b = backend or DB_BACKEND
    if b == "postgres":
        return PostgresBackend(kwargs.get("dsn", POSTGRES_DSN))
    return SQLiteBackend(kwargs.get("db_path", "data/omni_agent.db"))


# ══════════════════════════════════════════════════════════════════════════════
# HIGH-LEVEL ASYNC DB OPERATIONS
# ══════════════════════════════════════════════════════════════════════════════

class AsyncDB:
    """
    High-level async database interface used by the rest of the system.
    Wraps DBBackend with schema management and typed helpers.
    """

    SCHEMA = """
        CREATE TABLE IF NOT EXISTS conversations (
            id          SERIAL PRIMARY KEY,
            session_id  TEXT NOT NULL,
            role        TEXT NOT NULL,
            content     TEXT NOT NULL,
            metadata    TEXT DEFAULT '{}',
            ts          DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
        );
        CREATE TABLE IF NOT EXISTS memories (
            id          SERIAL PRIMARY KEY,
            key         TEXT UNIQUE NOT NULL,
            value       TEXT NOT NULL,
            category    TEXT DEFAULT 'general',
            importance  INTEGER DEFAULT 5,
            source      TEXT DEFAULT 'agent',
            created_at  DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
            updated_at  DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
        );
        CREATE TABLE IF NOT EXISTS agent_state (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_at  DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id          SERIAL PRIMARY KEY,
            action      TEXT NOT NULL,
            actor       TEXT DEFAULT 'system',
            details     TEXT DEFAULT '{}',
            ts          DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
        );
        CREATE INDEX IF NOT EXISTS idx_conv_session ON conversations(session_id);
        CREATE INDEX IF NOT EXISTS idx_mem_cat ON memories(category);
    """

    def __init__(self, backend: DBBackend = None):
        self.db = backend or create_backend()

    async def setup(self):
        await self.db.connect()
        await self.db.executescript(self.SCHEMA)
        logger.info("AsyncDB schema ready.")

    async def teardown(self):
        await self.db.close()

    # ── Conversations ──────────────────────────────────────────────────────

    async def add_message(self, session_id: str, role: str,
                          content: str, metadata: dict = None):
        await self.db.execute(
            "INSERT INTO conversations (session_id, role, content, metadata) "
            "VALUES (?, ?, ?, ?)",
            (session_id, role, content, json.dumps(metadata or {}))
        )

    async def get_history(self, session_id: str, limit: int = 50) -> List[Dict]:
        rows = await self.db.fetchall(
            "SELECT role, content, metadata FROM conversations "
            "WHERE session_id=? ORDER BY ts DESC LIMIT ?",
            (session_id, limit)
        )
        return list(reversed(rows))

    # ── Memories ────────────────────────────────────────────────────────────

    async def save_memory(self, key: str, value: Any, category: str = "general",
                          importance: int = 5):
        val = json.dumps(value) if not isinstance(value, str) else value
        await self.db.execute(
            "INSERT INTO memories (key, value, category, importance) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "importance=excluded.importance",
            (key, val, category, importance)
        )

    async def get_memory(self, key: str) -> Optional[Any]:
        row = await self.db.fetchone("SELECT value FROM memories WHERE key=?", (key,))
        if row:
            try:
                return json.loads(row["value"])
            except Exception:
                return row["value"]
        return None

    async def search_memories(self, query: str, limit: int = 10) -> List[Dict]:
        return await self.db.fetchall(
            "SELECT key, value, category, importance FROM memories "
            "WHERE value LIKE ? ORDER BY importance DESC LIMIT ?",
            (f"%{query}%", limit)
        )

    # ── State ────────────────────────────────────────────────────────────────

    async def set_state(self, key: str, value: Any):
        await self.db.execute(
            "INSERT INTO agent_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value))
        )

    async def get_state(self, key: str, default=None) -> Any:
        row = await self.db.fetchone("SELECT value FROM agent_state WHERE key=?", (key,))
        if row:
            try:
                return json.loads(row["value"])
            except Exception:
                return row["value"]
        return default

    # ── Audit ────────────────────────────────────────────────────────────────

    async def audit(self, action: str, actor: str = "system", details: dict = None):
        await self.db.execute(
            "INSERT INTO audit_log (action, actor, details) VALUES (?, ?, ?)",
            (action, actor, json.dumps(details or {}))
        )
