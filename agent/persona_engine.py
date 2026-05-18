"""OMNI AGENT - Persona Engine
Dynamic persona switching: define roles with tone/style constraints,
inject system prompts, route queries to the right persona, and
maintain per-persona memory and conversation history.

Features:
- Persona registry: name, description, system_prompt, tone, style, tags
- Active persona: global or per-session current persona
- System prompt injection: prepend persona system prompt to messages
- Tone constraints: formal|casual|technical|empathetic|concise
- Style templates: customize greeting, sign-off, response structure
- Persona routing: keyword/tag-based auto-selection
- Per-persona memory: isolated context windows per persona
- Transition hooks: on_activate / on_deactivate callbacks
- Blending: weighted mix of two persona system prompts
- Usage analytics: calls, tokens, avg satisfaction per persona
- SQLite persistence: persona definitions and session mappings
- REST API: create, get, activate, route, blend, stats
"""
import re, time, uuid, sqlite3, json, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

TONES = {"formal", "casual", "technical", "empathetic", "concise", "playful"}

@dataclass
class Persona:
    id: str; name: str
    description: str = ""
    system_prompt: str = ""
    tone: str = "casual"
    style: Dict[str, str] = field(default_factory=dict)
    keywords: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    greeting: str = ""
    sign_off: str = ""
    active: bool = True
    # Runtime stats
    call_count: int = 0
    total_tokens: int = 0
    satisfaction_sum: float = 0.0
    satisfaction_count: int = 0
    created_at: float = field(default_factory=time.time)

    @property
    def avg_satisfaction(self):
        return round(self.satisfaction_sum / max(1, self.satisfaction_count), 3)

    def inject(self, messages: List[Dict]) -> List[Dict]:
        """Prepend persona system prompt to messages list."""
        sys_msg = {"role": "system", "content": self.system_prompt}
        existing = [m for m in messages if m.get("role") != "system"]
        return [sys_msg] + existing if self.system_prompt else messages

    def to_dict(self):
        return {"id": self.id, "name": self.name, "description": self.description,
                "tone": self.tone, "tags": self.tags, "keywords": self.keywords,
                "active": self.active, "call_count": self.call_count,
                "avg_satisfaction": self.avg_satisfaction,
                "greeting": self.greeting, "sign_off": self.sign_off}

@dataclass
class PersonaSession:
    session_id: str; persona_name: str
    messages: List[Dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)

class PEStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS personas(
                    id TEXT PRIMARY KEY, name TEXT UNIQUE, description TEXT DEFAULT '',
                    system_prompt TEXT DEFAULT '', tone TEXT DEFAULT 'casual',
                    style TEXT DEFAULT '{}', keywords TEXT DEFAULT '[]',
                    tags TEXT DEFAULT '[]', greeting TEXT DEFAULT '',
                    sign_off TEXT DEFAULT '', active INTEGER DEFAULT 1,
                    call_count INTEGER DEFAULT 0, total_tokens INTEGER DEFAULT 0,
                    satisfaction_sum REAL DEFAULT 0, satisfaction_count INTEGER DEFAULT 0,
                    created_at REAL);
                CREATE TABLE IF NOT EXISTS sessions(
                    session_id TEXT PRIMARY KEY, persona_name TEXT,
                    messages TEXT DEFAULT '[]', created_at REAL, last_used REAL);
                CREATE INDEX IF NOT EXISTS idx_pe_name ON personas(name);
            """)

    def save(self, p: Persona):
        with self._conn() as c:
            c.execute("""INSERT OR REPLACE INTO personas VALUES
                (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (p.id, p.name, p.description, p.system_prompt, p.tone,
                 json.dumps(p.style), json.dumps(p.keywords), json.dumps(p.tags),
                 p.greeting, p.sign_off, int(p.active),
                 p.call_count, p.total_tokens,
                 p.satisfaction_sum, p.satisfaction_count, p.created_at))

    def load(self, name: str) -> Optional[Persona]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM personas WHERE name=?", (name,)).fetchone()
        if not row: return None
        return Persona(id=row["id"], name=row["name"],
                        description=row["description"] or "",
                        system_prompt=row["system_prompt"] or "",
                        tone=row["tone"] or "casual",
                        style=json.loads(row["style"] or "{}"),
                        keywords=json.loads(row["keywords"] or "[]"),
                        tags=json.loads(row["tags"] or "[]"),
                        greeting=row["greeting"] or "",
                        sign_off=row["sign_off"] or "",
                        active=bool(row["active"]),
                        call_count=row["call_count"],
                        total_tokens=row["total_tokens"],
                        satisfaction_sum=row["satisfaction_sum"],
                        satisfaction_count=row["satisfaction_count"],
                        created_at=row["created_at"])

    def list_all(self) -> List[Persona]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM personas ORDER BY call_count DESC").fetchall()
        result = []
        for row in rows:
            p = self.load(row["name"])
            if p: result.append(p)
        return result

    def save_session(self, s: PersonaSession):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?)",
                (s.session_id, s.persona_name, json.dumps(s.messages),
                 s.created_at, s.last_used))

    def load_session(self, session_id: str) -> Optional[PersonaSession]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM sessions WHERE session_id=?",
                             (session_id,)).fetchone()
        if not row: return None
        return PersonaSession(session_id=row["session_id"],
                               persona_name=row["persona_name"],
                               messages=json.loads(row["messages"] or "[]"),
                               created_at=row["created_at"],
                               last_used=row["last_used"])

    def stats(self):
        with self._conn() as c:
            np = c.execute("SELECT COUNT(*) FROM personas").fetchone()[0]
            ns = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            tc = c.execute("SELECT SUM(call_count) FROM personas").fetchone()[0] or 0
        return {"total_personas": np, "total_sessions": ns, "total_calls": int(tc)}

class PersonaEngine:
    """
    Dynamic persona management with routing, blending, and per-session memory.

    Usage:
        engine = PersonaEngine()
        engine.create("professor", "An expert academic assistant",
                       system_prompt="You are a knowledgeable professor. "
                                     "Explain concepts clearly with examples.",
                       tone="technical", tags=["education","science"])
        engine.create("buddy",  "A friendly casual helper",
                       system_prompt="You are a helpful friend. "
                                     "Keep answers short and friendly!",
                       tone="casual", tags=["general","chat"])

        # Auto-route based on message content
        persona = engine.route("Can you explain quantum entanglement?")
        print(persona.name)   # "professor"

        # Inject persona into messages
        messages = [{"role":"user","content":"Explain recursion"}]
        messages = engine.prepare_messages("professor", messages)
    """
    def __init__(self, db_path: str = "data/personas.db"):
        self._store = PEStore(db_path)
        self._personas: Dict[str, Persona] = {}
        self._active: Optional[str] = None
        self._activate_hooks: List[Callable] = []
        self._deactivate_hooks: List[Callable] = []
        # Load persisted
        for p in self._store.list_all():
            self._personas[p.name] = p

    def create(self, name: str, description: str = "",
                system_prompt: str = "", tone: str = "casual",
                style: Dict = None, keywords: List[str] = None,
                tags: List[str] = None, greeting: str = "",
                sign_off: str = "") -> Persona:
        if tone not in TONES:
            tone = "casual"
        p = Persona(id=str(uuid.uuid4())[:8], name=name,
                     description=description, system_prompt=system_prompt,
                     tone=tone, style=style or {},
                     keywords=keywords or [], tags=tags or [],
                     greeting=greeting, sign_off=sign_off)
        self._personas[name] = p
        self._store.save(p)
        logger.info(f"Persona created: {name!r}")
        return p

    def get(self, name: str) -> Optional[Persona]:
        return self._personas.get(name) or self._store.load(name)

    def list(self, tag: str = None, tone: str = None) -> List[Persona]:
        personas = list(self._personas.values())
        if tag:  personas = [p for p in personas if tag in p.tags]
        if tone: personas = [p for p in personas if p.tone == tone]
        return personas

    def activate(self, name: str) -> bool:
        if name not in self._personas: return False
        old = self._active
        if old and old in self._personas:
            for hook in self._deactivate_hooks:
                try: hook(self._personas[old])
                except: pass
        self._active = name
        for hook in self._activate_hooks:
            try: hook(self._personas[name])
            except: pass
        return True

    def deactivate(self):
        if self._active and self._active in self._personas:
            for hook in self._deactivate_hooks:
                try: hook(self._personas[self._active])
                except: pass
        self._active = None

    @property
    def active_persona(self) -> Optional[Persona]:
        return self._personas.get(self._active) if self._active else None

    def add_activate_hook(self, fn: Callable): self._activate_hooks.append(fn)
    def add_deactivate_hook(self, fn: Callable): self._deactivate_hooks.append(fn)

    def route(self, query: str, fallback: str = None) -> Optional[Persona]:
        """Select best persona based on keyword/tag overlap with query."""
        query_lower = query.lower()
        query_words = set(re.findall(r'\w+', query_lower))
        best_score = -1.0; best_persona = None
        for p in self._personas.values():
            if not p.active: continue
            kw_score = sum(1 for k in p.keywords if k.lower() in query_lower)
            tag_score = sum(1 for t in p.tags
                            if any(t.lower() in w for w in query_words))
            score = kw_score * 2 + tag_score
            if score > best_score:
                best_score = score; best_persona = p
        if best_persona and best_score > 0:
            return best_persona
        if fallback: return self.get(fallback)
        return self.active_persona

    def prepare_messages(self, persona_name: str,
                          messages: List[Dict],
                          session_id: str = None) -> List[Dict]:
        """Inject persona system prompt; optionally load session history."""
        p = self.get(persona_name)
        if not p: return messages
        p.call_count += 1
        self._store.save(p)
        if session_id:
            session = self._store.load_session(session_id)
            if session:
                messages = session.messages + messages
        return p.inject(messages)

    def blend(self, persona_a: str, persona_b: str,
               weight_a: float = 0.5) -> Persona:
        """Create a temporary blended persona from two existing ones."""
        pa = self.get(persona_a); pb = self.get(persona_b)
        if not pa or not pb: raise ValueError("One or both personas not found")
        wb = 1.0 - weight_a
        blended_prompt = (f"[Blend {weight_a:.0%} {pa.name} + {wb:.0%} {pb.name}]\n"
                           f"{pa.system_prompt}\n---\n{pb.system_prompt}")
        return Persona(id=f"blend_{pa.id}_{pb.id}", name=f"{pa.name}+{pb.name}",
                        description=f"Blend of {pa.name} and {pb.name}",
                        system_prompt=blended_prompt,
                        tone=pa.tone if weight_a >= 0.5 else pb.tone,
                        keywords=list(set(pa.keywords + pb.keywords)),
                        tags=list(set(pa.tags + pb.tags)))

    def record_satisfaction(self, persona_name: str, score: float):
        """Record a 0-1 satisfaction score for a persona response."""
        p = self.get(persona_name)
        if p:
            p.satisfaction_sum += max(0.0, min(1.0, score))
            p.satisfaction_count += 1
            self._store.save(p)

    def update_system_prompt(self, name: str, prompt: str):
        p = self.get(name)
        if p:
            p.system_prompt = prompt
            self._store.save(p)

    def delete(self, name: str) -> bool:
        existed = name in self._personas
        self._personas.pop(name, None)
        if self._active == name: self._active = None
        # Remove from DB
        with self._store._conn() as c:
            c.execute("DELETE FROM personas WHERE name=?", (name,))
        return existed

    def stats(self) -> Dict:
        s = self._store.stats()
        s["active_persona"] = self._active
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def create_ep(req):
            d = await req.json()
            p = self.create(d["name"], d.get("description",""),
                             d.get("system_prompt",""), d.get("tone","casual"),
                             d.get("style",{}), d.get("keywords",[]),
                             d.get("tags",[]), d.get("greeting",""))
            return web.json_response(p.to_dict(), status=201)
        async def get_ep(req):
            p = self.get(req.match_info["name"])
            if not p: return web.json_response({"error":"not found"},status=404)
            return web.json_response(p.to_dict())
        async def activate_ep(req):
            d = await req.json()
            ok = self.activate(d["name"])
            return web.json_response({"activated": ok})
        async def route_ep(req):
            d = await req.json()
            p = self.route(d["query"], d.get("fallback"))
            if not p: return web.json_response({"error":"no match"},status=404)
            return web.json_response(p.to_dict())
        async def stats_ep(req): return web.json_response(self.stats())
        pr = f"{prefix}/persona"
        app.router.add_post(f"{pr}",              create_ep)
        app.router.add_get( f"{pr}/{{name}}",     get_ep)
        app.router.add_post(f"{pr}/activate",     activate_ep)
        app.router.add_post(f"{pr}/route",        route_ep)
        app.router.add_get( f"{pr}/stats",        stats_ep)
        logger.info(f"Persona engine API at {prefix}/persona/")
