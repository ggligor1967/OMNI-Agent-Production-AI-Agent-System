"""
OMNI AGENT - Persistent Memory System
Stores: conversation history, semantic memories, agent state, skills
"""
import sqlite3
import json
import hashlib
import time
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any
from pathlib import Path

logger = logging.getLogger(__name__)


class MemoryDB:
    """SQLite-backed persistent memory with semantic search support."""

    def __init__(self, db_path: str = "data/omni_agent.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id  TEXT NOT NULL,
                    role        TEXT NOT NULL CHECK(role IN ('user','assistant','system','tool')),
                    content     TEXT NOT NULL,
                    metadata    TEXT DEFAULT '{}',
                    ts          REAL DEFAULT (unixepoch('now','subsec'))
                );

                CREATE TABLE IF NOT EXISTS memories (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    key         TEXT UNIQUE NOT NULL,
                    value       TEXT NOT NULL,
                    category    TEXT DEFAULT 'general',
                    importance  INTEGER DEFAULT 5 CHECK(importance BETWEEN 1 AND 10),
                    source      TEXT DEFAULT 'agent',
                    embedding   BLOB,
                    created_at  REAL DEFAULT (unixepoch('now','subsec')),
                    updated_at  REAL DEFAULT (unixepoch('now','subsec'))
                );

                CREATE TABLE IF NOT EXISTS skills (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    name        TEXT UNIQUE NOT NULL,
                    description TEXT,
                    code        TEXT NOT NULL,
                    triggers    TEXT DEFAULT '[]',
                    enabled     INTEGER DEFAULT 1,
                    version     TEXT DEFAULT '1.0.0',
                    created_at  REAL DEFAULT (unixepoch('now','subsec'))
                );

                CREATE TABLE IF NOT EXISTS hooks (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    event       TEXT NOT NULL,
                    handler     TEXT NOT NULL,
                    priority    INTEGER DEFAULT 5,
                    enabled     INTEGER DEFAULT 1,
                    metadata    TEXT DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    name        TEXT UNIQUE NOT NULL,
                    cron_expr   TEXT NOT NULL,
                    handler     TEXT NOT NULL,
                    last_run    REAL,
                    next_run    REAL,
                    enabled     INTEGER DEFAULT 1,
                    run_count   INTEGER DEFAULT 0,
                    metadata    TEXT DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS agent_state (
                    key         TEXT PRIMARY KEY,
                    value       TEXT NOT NULL,
                    updated_at  REAL DEFAULT (unixepoch('now','subsec'))
                );

                CREATE TABLE IF NOT EXISTS audit_log (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    action      TEXT NOT NULL,
                    actor       TEXT DEFAULT 'system',
                    details     TEXT DEFAULT '{}',
                    ts          REAL DEFAULT (unixepoch('now','subsec'))
                );

                CREATE INDEX IF NOT EXISTS idx_conv_session ON conversations(session_id);
                CREATE INDEX IF NOT EXISTS idx_mem_category ON memories(category);
                CREATE INDEX IF NOT EXISTS idx_hooks_event ON hooks(event);
            """)
        logger.info(f"Memory DB initialized at {self.db_path}")

    # ── Conversations ──────────────────────────────────────────────────────────

    def add_message(self, session_id: str, role: str, content: str, metadata: dict = None):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO conversations (session_id, role, content, metadata) VALUES (?,?,?,?)",
                (session_id, role, content, json.dumps(metadata or {}))
            )

    def get_history(self, session_id: str, limit: int = 50) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT role, content, metadata, ts FROM conversations "
                "WHERE session_id=? ORDER BY ts DESC LIMIT ?",
                (session_id, limit)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def clear_session(self, session_id: str):
        with self._conn() as conn:
            conn.execute("DELETE FROM conversations WHERE session_id=?", (session_id,))

    def list_sessions(self) -> List[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT session_id FROM conversations ORDER BY session_id"
            ).fetchall()
        return [r[0] for r in rows]

    # ── Semantic Memories ──────────────────────────────────────────────────────

    def save_memory(self, key: str, value: Any, category: str = "general",
                    importance: int = 5, source: str = "agent"):
        serialized = json.dumps(value) if not isinstance(value, str) else value
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO memories (key, value, category, importance, source)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    importance=excluded.importance,
                    updated_at=unixepoch('now','subsec')
            """, (key, serialized, category, importance, source))

    def get_memory(self, key: str) -> Optional[Any]:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM memories WHERE key=?", (key,)).fetchone()
        if row:
            try:
                return json.loads(row[0])
            except json.JSONDecodeError:
                return row[0]
        return None

    def search_memories(self, query: str, category: Optional[str] = None, limit: int = 10) -> List[Dict]:
        """Simple keyword search (replace with vector search if embeddings available)."""
        sql = "SELECT key, value, category, importance FROM memories WHERE value LIKE ?"
        params = [f"%{query}%"]
        if category:
            sql += " AND category=?"
            params.append(category)
        sql += " ORDER BY importance DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_memories_by_category(self, category: str) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT key, value, importance FROM memories WHERE category=? ORDER BY importance DESC",
                (category,)
            ).fetchall()
        return [dict(r) for r in rows]

    def _deserialize_memory_value(self, raw_value: str) -> Any:
        try:
            return json.loads(raw_value)
        except (TypeError, json.JSONDecodeError):
            return raw_value

    def get_all_memories(self, category: Optional[str] = None) -> List[Dict]:
        sql = (
            "SELECT id, key, value, category, importance, source, created_at, updated_at "
            "FROM memories"
        )
        params: List[Any] = []
        if category:
            sql += " WHERE category=?"
            params.append(category)
        sql += " ORDER BY importance DESC, updated_at DESC, key ASC"

        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()

        memories = []
        for row in rows:
            item = dict(row)
            item["value"] = self._deserialize_memory_value(item.get("value"))
            memories.append(item)
        return memories

    def export(self, category: Optional[str] = None) -> List[Dict]:
        return self.get_all_memories(category=category)

    # ── Agent State ────────────────────────────────────────────────────────────

    def set_state(self, key: str, value: Any):
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO agent_state (key, value, updated_at) VALUES (?,?,unixepoch('now','subsec'))",
                (key, json.dumps(value))
            )

    def get_state(self, key: str, default=None) -> Any:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM agent_state WHERE key=?", (key,)).fetchone()
        if row:
            try:
                return json.loads(row[0])
            except Exception:
                return row[0]
        return default

    # ── Audit Log ──────────────────────────────────────────────────────────────

    def audit(self, action: str, actor: str = "system", details: dict = None):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO audit_log (action, actor, details) VALUES (?,?,?)",
                (action, actor, json.dumps(details or {}))
            )

    def get_audit_log(self, limit: int = 100) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT action, actor, details, ts FROM audit_log ORDER BY ts DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
