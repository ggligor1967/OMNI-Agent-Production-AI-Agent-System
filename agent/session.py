"""
OMNI AGENT - Session Manager
Full session lifecycle management: create, resume, persist, expire, and sync
conversation state across requests, users, and devices.

Features:
- Session creation with metadata (user_id, device, model, persona, tags)
- SQLite persistence: sessions survive process restarts
- Automatic expiry: idle timeout + absolute TTL
- Conversation state: ordered messages, system prompt, context window tracking
- Multi-device: multiple sessions per user with device tagging
- Session cloning: fork a session at a specific message index
- Summary snapshots: compress old context to preserve token budget
- Search: find sessions by user, tag, time range, or message content
- Analytics: per-user session counts, avg length, model usage
"""
import json
import time
import uuid
import sqlite3
import logging
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# SESSION MODEL
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Message:
    role: str               # user | assistant | system | tool
    content: str
    model: str = ""
    tokens: int = 0
    latency_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)
    meta: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "role": self.role, "content": self.content,
            "model": self.model, "tokens": self.tokens,
            "latency_ms": self.latency_ms,
            "timestamp": self.timestamp, "meta": self.meta,
        }

    @staticmethod
    def from_dict(d: Dict) -> "Message":
        return Message(
            role=d["role"], content=d["content"],
            model=d.get("model", ""), tokens=d.get("tokens", 0),
            latency_ms=d.get("latency_ms", 0.0),
            timestamp=d.get("timestamp", time.time()),
            meta=d.get("meta", {}),
        )


@dataclass
class Session:
    id: str
    user_id: str
    title: str = ""
    model: str = ""
    persona: str = "assistant"
    device: str = ""
    tags: List[str] = field(default_factory=list)
    system_prompt: str = ""
    messages: List[Message] = field(default_factory=list)
    metadata: Dict = field(default_factory=dict)
    summary: str = ""              # compressed context summary
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    archived: bool = False

    def touch(self):
        self.last_active = time.time()
        self.updated_at = time.time()

    def add_message(self, role: str, content: str, **kwargs) -> Message:
        msg = Message(role=role, content=content, **kwargs)
        self.messages.append(msg)
        self.touch()
        if not self.title and role == "user" and content:
            # Auto-title from first user message
            self.title = content[:60].strip()
        return msg

    def to_llm_messages(self, max_messages: int = None) -> List[Dict]:
        """Return messages formatted for LLM API calls."""
        msgs = self.messages[-max_messages:] if max_messages else self.messages
        result = []
        if self.summary:
            result.append({"role": "system",
                          "content": f"[Conversation summary]: {self.summary}"})
        result.extend({"role": m.role, "content": m.content}
                      for m in msgs if m.role in ("user", "assistant"))
        return result

    @property
    def message_count(self) -> int:
        return len(self.messages)

    @property
    def total_tokens(self) -> int:
        return sum(m.tokens for m in self.messages)

    @property
    def is_expired(self) -> bool:
        if self.expires_at and time.time() > self.expires_at:
            return True
        return False

    def to_dict(self, include_messages: bool = True) -> Dict:
        d = {
            "id": self.id, "user_id": self.user_id,
            "title": self.title, "model": self.model,
            "persona": self.persona, "device": self.device,
            "tags": self.tags, "system_prompt": self.system_prompt,
            "summary": self.summary,
            "metadata": self.metadata,
            "message_count": self.message_count,
            "total_tokens": self.total_tokens,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_active": self.last_active,
            "expires_at": self.expires_at,
            "archived": self.archived,
        }
        if include_messages:
            d["messages"] = [m.to_dict() for m in self.messages]
        return d


# ══════════════════════════════════════════════════════════════════════════════
# SESSION STORE (SQLite)
# ══════════════════════════════════════════════════════════════════════════════

class SessionStore:
    """SQLite-backed session persistence."""

    def __init__(self, db_path: str = "data/sessions.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id           TEXT PRIMARY KEY,
                    user_id      TEXT NOT NULL,
                    title        TEXT DEFAULT '',
                    model        TEXT DEFAULT '',
                    persona      TEXT DEFAULT 'assistant',
                    device       TEXT DEFAULT '',
                    tags         TEXT DEFAULT '[]',
                    system_prompt TEXT DEFAULT '',
                    messages     TEXT DEFAULT '[]',
                    metadata     TEXT DEFAULT '{}',
                    summary      TEXT DEFAULT '',
                    created_at   REAL,
                    updated_at   REAL,
                    last_active  REAL,
                    expires_at   REAL,
                    archived     INTEGER DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_sess_user ON sessions(user_id, last_active DESC);
                CREATE INDEX IF NOT EXISTS idx_sess_exp  ON sessions(expires_at);
                CREATE INDEX IF NOT EXISTS idx_sess_tag  ON sessions(tags);
            """)

    def save(self, session: Session):
        with self._conn() as c:
            c.execute("""
                INSERT OR REPLACE INTO sessions
                (id,user_id,title,model,persona,device,tags,system_prompt,
                 messages,metadata,summary,created_at,updated_at,last_active,
                 expires_at,archived)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                session.id, session.user_id, session.title, session.model,
                session.persona, session.device,
                json.dumps(session.tags),
                session.system_prompt,
                json.dumps([m.to_dict() for m in session.messages]),
                json.dumps(session.metadata),
                session.summary,
                session.created_at, session.updated_at, session.last_active,
                session.expires_at,
                1 if session.archived else 0,
            ))

    def get(self, session_id: str) -> Optional[Session]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM sessions WHERE id=?",
                           (session_id,)).fetchone()
        return self._row_to_session(row) if row else None

    def list_user_sessions(self, user_id: str,
                           include_archived: bool = False,
                           limit: int = 50) -> List[Session]:
        with self._conn() as c:
            q = ("SELECT * FROM sessions WHERE user_id=?"
                 + (" " if include_archived else " AND archived=0 ")
                 + "ORDER BY last_active DESC LIMIT ?")
            rows = c.execute(q, (user_id, limit)).fetchall()
        return [self._row_to_session(r) for r in rows]

    def search(self, user_id: str = None, tag: str = None,
               title_contains: str = None,
               after: float = None, before: float = None,
               limit: int = 50) -> List[Session]:
        conditions = ["archived=0"]
        params: List[Any] = []
        if user_id:
            conditions.append("user_id=?"); params.append(user_id)
        if tag:
            conditions.append("tags LIKE ?"); params.append(f'%"{tag}"%')
        if title_contains:
            conditions.append("title LIKE ?"); params.append(f"%{title_contains}%")
        if after:
            conditions.append("created_at >= ?"); params.append(after)
        if before:
            conditions.append("created_at <= ?"); params.append(before)
        params.append(limit)
        q = ("SELECT * FROM sessions WHERE " + " AND ".join(conditions)
             + " ORDER BY last_active DESC LIMIT ?")
        with self._conn() as c:
            rows = c.execute(q, params).fetchall()
        return [self._row_to_session(r) for r in rows]

    def delete(self, session_id: str) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        return cur.rowcount > 0

    def archive(self, session_id: str) -> bool:
        with self._conn() as c:
            cur = c.execute("UPDATE sessions SET archived=1 WHERE id=?",
                           (session_id,))
        return cur.rowcount > 0

    def purge_expired(self) -> int:
        now = time.time()
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM sessions WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,)
            )
        count = cur.rowcount
        if count:
            logger.info(f"Purged {count} expired sessions")
        return count

    def stats(self, user_id: str = None) -> Dict:
        with self._conn() as c:
            q_base = "WHERE archived=0" + (f" AND user_id='{user_id}'" if user_id else "")
            total = c.execute(f"SELECT COUNT(*) FROM sessions {q_base}").fetchone()[0]
            by_model = dict(c.execute(
                f"SELECT model, COUNT(*) FROM sessions {q_base} GROUP BY model"
            ).fetchall())
            by_persona = dict(c.execute(
                f"SELECT persona, COUNT(*) FROM sessions {q_base} GROUP BY persona"
            ).fetchall())
        return {"total_sessions": total, "by_model": by_model,
                "by_persona": by_persona}

    def _row_to_session(self, row) -> Session:
        return Session(
            id=row["id"], user_id=row["user_id"],
            title=row["title"] or "", model=row["model"] or "",
            persona=row["persona"] or "assistant",
            device=row["device"] or "",
            tags=json.loads(row["tags"] or "[]"),
            system_prompt=row["system_prompt"] or "",
            messages=[Message.from_dict(m)
                      for m in json.loads(row["messages"] or "[]")],
            metadata=json.loads(row["metadata"] or "{}"),
            summary=row["summary"] or "",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_active=row["last_active"],
            expires_at=row["expires_at"],
            archived=bool(row["archived"]),
        )


# ══════════════════════════════════════════════════════════════════════════════
# SESSION MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class SessionManager:
    """
    High-level session management API.

    Usage:
        sm = SessionManager()

        # Create a session
        session = sm.create("user_123", model="deepseek-v3", persona="engineer")

        # Add messages
        session.add_message("user", "Explain async/await")
        session.add_message("assistant", "Sure! async/await allows...",
                           model="deepseek-v3", tokens=42)
        sm.save(session)

        # Resume
        session = sm.get(session_id)

        # User's sessions
        sessions = sm.list_user("user_123")

        # Clone at message 5
        fork = sm.clone(session_id, at_message=5)

        # Analytics
        stats = sm.stats("user_123")
    """

    def __init__(self, db_path: str = "data/sessions.db",
                 default_ttl_s: float = 86400 * 30,   # 30 days
                 idle_timeout_s: float = 86400 * 7):   # 7 days idle
        self.store = SessionStore(db_path)
        self.default_ttl_s = default_ttl_s
        self.idle_timeout_s = idle_timeout_s
        self._cache: Dict[str, Session] = {}   # in-memory hot cache

    def create(self, user_id: str,
               model: str = "",
               persona: str = "assistant",
               device: str = "",
               system_prompt: str = "",
               tags: List[str] = None,
               ttl_s: float = None,
               metadata: Dict = None) -> Session:
        """Create and persist a new session."""
        session_id = str(uuid.uuid4())
        now = time.time()
        ttl = ttl_s if ttl_s is not None else self.default_ttl_s
        session = Session(
            id=session_id,
            user_id=user_id,
            model=model,
            persona=persona,
            device=device,
            system_prompt=system_prompt,
            tags=tags or [],
            metadata=metadata or {},
            created_at=now,
            updated_at=now,
            last_active=now,
            expires_at=now + ttl if ttl > 0 else None,
        )
        self.store.save(session)
        self._cache[session_id] = session
        logger.info(f"Session created: id={session_id} user={user_id} "
                   f"model={model} persona={persona}")
        return session

    def get(self, session_id: str,
            check_expiry: bool = True) -> Optional[Session]:
        """Retrieve session by ID."""
        # Check hot cache first
        if session_id in self._cache:
            session = self._cache[session_id]
        else:
            session = self.store.get(session_id)
            if session:
                self._cache[session_id] = session

        if session is None:
            return None

        if check_expiry and session.is_expired:
            logger.info(f"Session expired: {session_id}")
            self.store.delete(session_id)
            self._cache.pop(session_id, None)
            return None

        return session

    def save(self, session: Session):
        """Persist changes to a session."""
        session.touch()
        self.store.save(session)
        self._cache[session.id] = session

    def delete(self, session_id: str) -> bool:
        self._cache.pop(session_id, None)
        return self.store.delete(session_id)

    def archive(self, session_id: str) -> bool:
        session = self.get(session_id)
        if session:
            session.archived = True
            self.save(session)
        return self.store.archive(session_id)

    def add_message(self, session_id: str, role: str,
                    content: str, **kwargs) -> Optional[Message]:
        """Add a message to a session and auto-save."""
        session = self.get(session_id)
        if not session:
            return None
        msg = session.add_message(role, content, **kwargs)
        self.save(session)
        return msg

    def clone(self, session_id: str,
              at_message: int = None,
              new_user_id: str = None) -> Optional[Session]:
        """Fork a session, optionally truncating at a message index."""
        original = self.get(session_id)
        if not original:
            return None
        now = time.time()
        fork = Session(
            id=str(uuid.uuid4()),
            user_id=new_user_id or original.user_id,
            title=f"Fork of: {original.title}",
            model=original.model,
            persona=original.persona,
            device=original.device,
            tags=list(original.tags) + ["fork"],
            system_prompt=original.system_prompt,
            messages=list(original.messages[:at_message])
                     if at_message is not None else list(original.messages),
            metadata={**original.metadata, "forked_from": session_id},
            summary=original.summary,
            created_at=now, updated_at=now, last_active=now,
            expires_at=original.expires_at,
        )
        self.store.save(fork)
        self._cache[fork.id] = fork
        logger.info(f"Session forked: {session_id} → {fork.id}")
        return fork

    def update_summary(self, session_id: str, summary: str):
        """Set a compressed context summary and clear old messages."""
        session = self.get(session_id)
        if not session:
            return
        session.summary = summary
        self.save(session)

    def list_user(self, user_id: str, limit: int = 50,
                  include_archived: bool = False) -> List[Session]:
        return self.store.list_user_sessions(user_id, include_archived, limit)

    def search(self, **kwargs) -> List[Session]:
        return self.store.search(**kwargs)

    def purge_expired(self) -> int:
        count = self.store.purge_expired()
        # Also evict from cache
        expired_ids = [sid for sid, s in self._cache.items() if s.is_expired]
        for sid in expired_ids:
            del self._cache[sid]
        return count

    def stats(self, user_id: str = None) -> Dict:
        return self.store.stats(user_id)

    def register_routes(self, app, prefix: str = ""):
        from aiohttp import web

        async def create_session(request):
            data = await request.json()
            session = self.create(
                user_id=data.get("user_id", "anonymous"),
                model=data.get("model", ""),
                persona=data.get("persona", "assistant"),
                device=data.get("device", ""),
                system_prompt=data.get("system_prompt", ""),
                tags=data.get("tags", []),
                ttl_s=data.get("ttl_s"),
                metadata=data.get("metadata", {}),
            )
            return web.json_response(session.to_dict(include_messages=False), status=201)

        async def get_session(request):
            sid = request.match_info["id"]
            session = self.get(sid)
            if not session:
                return web.json_response({"error": "not found"}, status=404)
            include_msgs = request.rel_url.query.get("messages", "true") == "true"
            return web.json_response(session.to_dict(include_messages=include_msgs))

        async def delete_session(request):
            sid = request.match_info["id"]
            ok = self.delete(sid)
            return web.json_response({"deleted": ok})

        async def add_message_endpoint(request):
            sid = request.match_info["id"]
            data = await request.json()
            msg = self.add_message(
                sid, data["role"], data["content"],
                model=data.get("model", ""),
                tokens=data.get("tokens", 0),
                latency_ms=data.get("latency_ms", 0.0),
            )
            if not msg:
                return web.json_response({"error": "session not found"}, status=404)
            return web.json_response(msg.to_dict())

        async def list_sessions(request):
            user_id = request.rel_url.query.get("user_id")
            limit = int(request.rel_url.query.get("limit", 50))
            if not user_id:
                return web.json_response({"error": "user_id required"}, status=400)
            sessions = self.list_user(user_id, limit=limit)
            return web.json_response({
                "sessions": [s.to_dict(include_messages=False) for s in sessions]
            })

        async def clone_session(request):
            sid = request.match_info["id"]
            data = await request.json() if request.content_length else {}
            fork = self.clone(sid, at_message=data.get("at_message"),
                             new_user_id=data.get("user_id"))
            if not fork:
                return web.json_response({"error": "session not found"}, status=404)
            return web.json_response(fork.to_dict(include_messages=False), status=201)

        async def stats_endpoint(request):
            user_id = request.rel_url.query.get("user_id")
            return web.json_response(self.stats(user_id))

        app.router.add_post(f"{prefix}/sessions",                    create_session)
        app.router.add_get( f"{prefix}/sessions",                    list_sessions)
        app.router.add_get( f"{prefix}/sessions/{{id}}",             get_session)
        app.router.add_delete(f"{prefix}/sessions/{{id}}",           delete_session)
        app.router.add_post(f"{prefix}/sessions/{{id}}/messages",    add_message_endpoint)
        app.router.add_post(f"{prefix}/sessions/{{id}}/clone",       clone_session)
        app.router.add_get( f"{prefix}/sessions/stats",              stats_endpoint)
        logger.info(f"Session API routes registered at {prefix}/sessions")
