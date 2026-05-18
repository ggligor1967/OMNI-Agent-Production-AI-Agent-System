"""OMNI AGENT - Conversation Manager
Multi-turn conversation session management with message history,
context window trimming, summarization triggers, and persistence.

Features:
- Session: id, user_id, metadata, message list, created/updated timestamps
- Message roles: user, assistant, system, tool
- Context window: enforce max_messages and max_tokens limits
- Trim strategy: drop oldest non-system messages or summarize
- Summarization trigger: fire hook when token budget is near exceeded
- System prompt pinning: system messages always retained
- Branch: fork a session at a given message index
- Search: full-text search across message content
- Tags: label sessions for filtering
- Turn counter, avg response length stats per session
- Export: JSON dump of session
- SQLite persistence: sessions, messages
- REST API: create, append, get, search, stats
"""
import json, re, sqlite3, time, uuid, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

def _est_tokens(text: str) -> int:
    return max(1, len(text) // 4)

@dataclass
class Message:
    id: str
    role: str               # user | assistant | system | tool
    content: str
    name: str = ""          # optional name (tool name, persona)
    metadata: Dict = field(default_factory=dict)
    token_count: int = 0
    created_at: float = field(default_factory=time.time)

    def __post_init__(self):
        if not self.token_count:
            self.token_count = _est_tokens(self.content)

    def to_dict(self):
        return {"id": self.id, "role": self.role,
                "content": self.content, "name": self.name,
                "token_count": self.token_count,
                "metadata": self.metadata,
                "created_at": round(self.created_at, 2)}

@dataclass
class Session:
    id: str
    user_id: str = "anonymous"
    title: str = ""
    system_prompt: str = ""
    messages: List[Message] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    metadata: Dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def total_tokens(self) -> int:
        return sum(m.token_count for m in self.messages)

    @property
    def turn_count(self) -> int:
        return sum(1 for m in self.messages if m.role == "user")

    @property
    def last_message(self) -> Optional[Message]:
        return self.messages[-1] if self.messages else None

    def to_dict(self, include_messages: bool = False):
        d = {"id": self.id, "user_id": self.user_id,
             "title": self.title,
             "total_tokens": self.total_tokens,
             "turn_count": self.turn_count,
             "message_count": len(self.messages),
             "tags": self.tags, "metadata": self.metadata,
             "created_at": round(self.created_at, 2),
             "updated_at": round(self.updated_at, 2)}
        if include_messages:
            d["messages"] = [m.to_dict() for m in self.messages]
        return d

class CMStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS sessions(
                    id TEXT PRIMARY KEY, user_id TEXT DEFAULT 'anonymous',
                    title TEXT DEFAULT '', system_prompt TEXT DEFAULT '',
                    tags TEXT DEFAULT '[]', metadata TEXT DEFAULT '{}',
                    created_at REAL, updated_at REAL);
                CREATE TABLE IF NOT EXISTS messages(
                    id TEXT PRIMARY KEY, session_id TEXT,
                    role TEXT, content TEXT, name TEXT DEFAULT '',
                    token_count INTEGER DEFAULT 0,
                    metadata TEXT DEFAULT '{}',
                    created_at REAL);
                CREATE INDEX IF NOT EXISTS idx_msg_sess
                    ON messages(session_id, created_at ASC);
                CREATE INDEX IF NOT EXISTS idx_sess_user
                    ON sessions(user_id, updated_at DESC);
            """)

    def save_session(self, s: Session):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?,?,?,?)",
                (s.id, s.user_id, s.title, s.system_prompt,
                 json.dumps(s.tags), json.dumps(s.metadata),
                 s.created_at, s.updated_at))

    def save_message(self, session_id: str, m: Message):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO messages VALUES(?,?,?,?,?,?,?,?)",
                (m.id, session_id, m.role, m.content, m.name,
                 m.token_count, json.dumps(m.metadata), m.created_at))

    def load_session(self, session_id: str) -> Optional[Dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def load_messages(self, session_id: str) -> List[Dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM messages WHERE session_id=? "
                "ORDER BY created_at ASC", (session_id,)).fetchall()
        return [dict(r) for r in rows]

    def search_messages(self, query: str, session_id: str = None,
                         limit: int = 20) -> List[Dict]:
        with self._conn() as c:
            if session_id:
                rows = c.execute(
                    "SELECT * FROM messages WHERE session_id=? "
                    "AND content LIKE ? ORDER BY created_at DESC LIMIT ?",
                    (session_id, f"%{query}%", limit)).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM messages WHERE content LIKE ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (f"%{query}%", limit)).fetchall()
        return [dict(r) for r in rows]

    def list_sessions(self, user_id: str = None, tag: str = None,
                       limit: int = 20) -> List[Dict]:
        with self._conn() as c:
            if user_id and tag:
                rows = c.execute(
                    "SELECT * FROM sessions WHERE user_id=? AND tags LIKE ? "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (user_id, f'%"{tag}"%', limit)).fetchall()
            elif user_id:
                rows = c.execute(
                    "SELECT * FROM sessions WHERE user_id=? "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (user_id, limit)).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?",
                    (limit,)).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> Dict:
        with self._conn() as c:
            ns = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            nm = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            avg = c.execute(
                "SELECT AVG(token_count) FROM messages").fetchone()[0] or 0
        return {"sessions": ns, "messages": nm,
                "avg_tokens_per_message": round(avg, 1)}

class ConversationManager:
    """
    Multi-turn conversation session manager with context window trimming.

    Usage:
        cm = ConversationManager(max_tokens=4096)

        sess = cm.create("alice", system_prompt="You are a helpful assistant.")
        cm.append(sess.id, "user", "Hello!")
        cm.append(sess.id, "assistant", "Hi! How can I help you?")

        context = cm.get_context(sess.id)  # list of dicts for LLM API
        print(f"Turns: {sess.turn_count}, Tokens: {sess.total_tokens}")
    """
    def __init__(self, db_path: str = "data/conversations.db",
                 max_messages: int = 100,
                 max_tokens: int = 8000,
                 trim_strategy: str = "drop_oldest",
                 summarize_threshold: float = 0.85):
        self._store = CMStore(db_path)
        self._sessions: Dict[str, Session] = {}
        self.max_messages = max_messages
        self.max_tokens = max_tokens
        self.trim_strategy = trim_strategy
        self.summarize_threshold = summarize_threshold
        self._hooks: Dict[str, List[Callable]] = {
            "on_summarize": [], "on_message": [], "on_trim": []}

    def create(self, user_id: str = "anonymous",
                system_prompt: str = "",
                title: str = "",
                tags: List[str] = None,
                metadata: Dict = None,
                session_id: str = None) -> Session:
        sid = session_id or str(uuid.uuid4())[:14]
        sess = Session(id=sid, user_id=user_id,
                        system_prompt=system_prompt,
                        title=title or f"Session {sid[:6]}",
                        tags=list(tags or []),
                        metadata=dict(metadata or {}))
        if system_prompt:
            msg = Message(id=str(uuid.uuid4())[:8],
                           role="system", content=system_prompt)
            sess.messages.append(msg)
            self._store.save_message(sid, msg)
        self._sessions[sid] = sess
        self._store.save_session(sess)
        return sess

    def get(self, session_id: str) -> Optional[Session]:
        if session_id in self._sessions:
            return self._sessions[session_id]
        # Load from DB
        row = self._store.load_session(session_id)
        if not row: return None
        msgs = [Message(id=r["id"], role=r["role"], content=r["content"],
                         name=r["name"] or "",
                         token_count=r["token_count"],
                         metadata=json.loads(r["metadata"] or "{}"),
                         created_at=r["created_at"])
                 for r in self._store.load_messages(session_id)]
        sess = Session(id=row["id"], user_id=row["user_id"],
                        title=row["title"],
                        system_prompt=row["system_prompt"],
                        messages=msgs,
                        tags=json.loads(row["tags"] or "[]"),
                        metadata=json.loads(row["metadata"] or "{}"),
                        created_at=row["created_at"],
                        updated_at=row["updated_at"])
        self._sessions[session_id] = sess
        return sess

    def append(self, session_id: str, role: str, content: str,
                name: str = "", metadata: Dict = None) -> Optional[Message]:
        sess = self.get(session_id)
        if not sess: return None
        msg = Message(id=str(uuid.uuid4())[:8], role=role,
                       content=content, name=name,
                       metadata=dict(metadata or {}))
        sess.messages.append(msg)
        sess.updated_at = time.time()
        self._store.save_message(session_id, msg)
        self._store.save_session(sess)
        # Fire hook
        for h in self._hooks["on_message"]:
            try: h(sess, msg)
            except: pass
        # Check limits
        self._maybe_trim(sess)
        return msg

    def _maybe_trim(self, sess: Session):
        needs_trim = (len(sess.messages) > self.max_messages
                       or sess.total_tokens > self.max_tokens)
        if not needs_trim: return

        # Summarize trigger
        if sess.total_tokens > self.max_tokens * self.summarize_threshold:
            for h in self._hooks["on_summarize"]:
                try: h(sess)
                except: pass

        if self.trim_strategy == "drop_oldest":
            # Keep system messages + recent messages
            system_msgs = [m for m in sess.messages if m.role == "system"]
            other_msgs  = [m for m in sess.messages if m.role != "system"]
            # Drop from oldest non-system until within limits
            while (len(sess.messages) > self.max_messages
                    or sess.total_tokens > self.max_tokens):
                if not other_msgs: break
                removed = other_msgs.pop(0)
                sess.messages = system_msgs + other_msgs
                for h in self._hooks["on_trim"]:
                    try: h(sess, removed)
                    except: pass

    def get_context(self, session_id: str,
                     max_messages: int = None) -> List[Dict]:
        """Return messages formatted for LLM API call."""
        sess = self.get(session_id)
        if not sess: return []
        msgs = sess.messages
        if max_messages:
            # Always include system, then last N
            sys_msgs = [m for m in msgs if m.role == "system"]
            other    = [m for m in msgs if m.role != "system"]
            msgs = sys_msgs + other[-max_messages:]
        return [{"role": m.role, "content": m.content,
                  **({"name": m.name} if m.name else {})}
                 for m in msgs]

    def branch(self, session_id: str, at_index: int,
                new_user_id: str = None) -> Optional[Session]:
        sess = self.get(session_id)
        if not sess: return None
        new_sess = self.create(
            user_id=new_user_id or sess.user_id,
            system_prompt=sess.system_prompt,
            title=f"Branch of {sess.title[:30]}",
            tags=list(sess.tags))
        # Copy messages up to index
        non_sys = [m for m in sess.messages if m.role != "system"]
        for m in non_sys[:at_index]:
            self.append(new_sess.id, m.role, m.content, m.name)
        return new_sess

    def delete(self, session_id: str) -> bool:
        sess = self._sessions.pop(session_id, None)
        return sess is not None

    def search(self, query: str, session_id: str = None,
                limit: int = 20) -> List[Dict]:
        return self._store.search_messages(query, session_id, limit)

    def list_sessions(self, user_id: str = None,
                       tag: str = None, limit: int = 20) -> List[Dict]:
        return self._store.list_sessions(user_id, tag, limit)

    def on(self, event: str, fn: Callable):
        if event in self._hooks: self._hooks[event].append(fn)

    def export(self, session_id: str) -> Optional[Dict]:
        sess = self.get(session_id)
        if not sess: return None
        return sess.to_dict(include_messages=True)

    def stats(self) -> Dict:
        s = self._store.stats()
        s["active_sessions"] = len(self._sessions)
        s["max_tokens"] = self.max_tokens
        s["max_messages"] = self.max_messages
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def create_ep(req):
            d = await req.json()
            sess = self.create(d.get("user_id","anonymous"),
                                d.get("system_prompt",""),
                                d.get("title",""),
                                d.get("tags",[]))
            return web.json_response(sess.to_dict(), status=201)
        async def append_ep(req):
            d = await req.json()
            msg = self.append(d["session_id"], d["role"], d["content"],
                               d.get("name",""))
            if not msg: return web.json_response({"error":"not found"},status=404)
            return web.json_response(msg.to_dict(), status=201)
        async def get_ep(req):
            sess = self.get(req.match_info["sid"])
            if not sess: return web.json_response({"error":"not found"},status=404)
            return web.json_response(sess.to_dict(include_messages=True))
        async def search_ep(req):
            q = req.rel_url.query
            return web.json_response(
                {"results": self.search(q.get("q",""))})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/conv"
        app.router.add_post(f"{p}/create",        create_ep)
        app.router.add_post(f"{p}/append",        append_ep)
        app.router.add_get( f"{p}/{{sid}}",       get_ep)
        app.router.add_get( f"{p}/search",        search_ep)
        app.router.add_get( f"{p}/stats",         stats_ep)
        logger.info(f"Conversation manager API at {prefix}/conv/")
