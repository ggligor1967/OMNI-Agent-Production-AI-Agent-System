"""OMNI AGENT - Conversation Router
Intent-based conversation routing: classify incoming messages, dispatch
to registered handlers, carry conversation context across turns.

Features:
- Intent registry: name, patterns (regex + keywords), handler fn, priority
- Multi-classifier: regex → keyword → embedding-similarity (BOW cosine)
- Confidence scoring: normalised 0-1 per intent
- Fallback chain: ordered list of intents to try if primary fails
- Context carry-over: per-session state dict passed to every handler
- Slot filling: extract named entities from message using patterns
- Handler response: structured dict with reply, actions, next_intent
- Conversation history: last N turns stored per session
- Pre/post hooks: middleware for logging, auth, rate-limit
- Multi-turn: handler can set next_expected_intent for guided flows
- SQLite persistence: sessions and routing decisions
- REST API: route, session, history, stats
"""
import re, time, uuid, sqlite3, json, math, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

def _bow_cosine(a: str, b: str) -> float:
    wa = re.findall(r'\b\w+\b', a.lower())
    wb = re.findall(r'\b\w+\b', b.lower())
    if not wa or not wb: return 0.0
    vocab = set(wa) | set(wb)
    va = {w: wa.count(w) for w in vocab}
    vb = {w: wb.count(w) for w in vocab}
    dot = sum(va[w] * vb[w] for w in vocab)
    na  = math.sqrt(sum(v*v for v in va.values()))
    nb  = math.sqrt(sum(v*v for v in vb.values()))
    return dot / max(1e-12, na * nb)

@dataclass
class Intent:
    id: str; name: str
    description: str = ""
    patterns: List[str] = field(default_factory=list)   # regex patterns
    keywords: List[str] = field(default_factory=list)
    examples: List[str] = field(default_factory=list)   # for similarity
    handler: Optional[Callable] = None
    priority: int = 5   # lower = higher priority
    slots: Dict[str, str] = field(default_factory=dict) # slot_name → regex
    fallback_intents: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    call_count: int = 0; match_count: int = 0

    @property
    def match_rate(self):
        return round(self.match_count / max(1, self.call_count), 4)

    def to_dict(self):
        return {"id": self.id, "name": self.name,
                "description": self.description,
                "priority": self.priority, "tags": self.tags,
                "call_count": self.call_count, "match_rate": self.match_rate}

@dataclass
class RoutingDecision:
    message: str; intent_name: str
    confidence: float; method: str  # regex|keyword|similarity|fallback
    slots: Dict[str, Any] = field(default_factory=dict)
    session_id: str = ""; created_at: float = field(default_factory=time.time)

    def to_dict(self):
        return {"intent": self.intent_name, "confidence": round(self.confidence, 4),
                "method": self.method, "slots": self.slots,
                "message_preview": self.message[:80]}

@dataclass
class HandlerResponse:
    reply: str = ""
    actions: List[Dict] = field(default_factory=list)
    next_intent: Optional[str] = None
    context_updates: Dict = field(default_factory=dict)
    data: Any = None

    def to_dict(self):
        return {"reply": self.reply, "actions": self.actions,
                "next_intent": self.next_intent, "data": self.data}

@dataclass
class ConversationSession:
    id: str; history: List[Dict] = field(default_factory=list)
    context: Dict = field(default_factory=dict)
    next_expected_intent: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def add_turn(self, message: str, intent: str, reply: str):
        self.history.append({"message": message, "intent": intent,
                              "reply": reply, "ts": time.time()})
        if len(self.history) > 50: self.history.pop(0)
        self.updated_at = time.time()

    def to_dict(self):
        return {"id": self.id, "context": self.context,
                "next_expected": self.next_expected_intent,
                "history_turns": len(self.history),
                "recent": self.history[-3:]}

class CRStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS sessions(
                    id TEXT PRIMARY KEY, history TEXT DEFAULT '[]',
                    context TEXT DEFAULT '{}',
                    next_expected TEXT DEFAULT '',
                    created_at REAL, updated_at REAL);
                CREATE TABLE IF NOT EXISTS decisions(
                    id TEXT PRIMARY KEY, session_id TEXT, intent TEXT,
                    confidence REAL, method TEXT, message TEXT DEFAULT '',
                    created_at REAL);
                CREATE INDEX IF NOT EXISTS idx_dec_sess ON decisions(session_id, created_at DESC);
            """)

    def save_session(self, s: ConversationSession):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?,?)",
                (s.id, json.dumps(s.history), json.dumps(s.context),
                 s.next_expected_intent or '', s.created_at, s.updated_at))

    def load_session(self, sid: str) -> Optional[ConversationSession]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if not row: return None
        return ConversationSession(id=row["id"],
                                    history=json.loads(row["history"] or "[]"),
                                    context=json.loads(row["context"] or "{}"),
                                    next_expected_intent=row["next_expected"] or None,
                                    created_at=row["created_at"],
                                    updated_at=row["updated_at"])

    def log_decision(self, d: RoutingDecision):
        with self._conn() as c:
            c.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?)",
                (str(uuid.uuid4())[:8], d.session_id, d.intent_name,
                 d.confidence, d.method, d.message[:200], d.created_at))

    def stats(self) -> Dict:
        with self._conn() as c:
            nd = c.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
            ns = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            by_intent = dict(c.execute(
                "SELECT intent, COUNT(*) FROM decisions GROUP BY intent "
                "ORDER BY COUNT(*) DESC LIMIT 10").fetchall())
        return {"total_decisions": nd, "total_sessions": ns,
                "top_intents": by_intent}

class ConversationRouter:
    """
    Intent classifier and conversation dispatcher.

    Usage:
        router = ConversationRouter()

        router.register("greet",
                         patterns=[r'\b(hi|hello|hey)\b'],
                         keywords=["hello","hi","hey"],
                         handler=lambda msg, ctx: HandlerResponse(reply="Hello!"),
                         priority=1)

        router.register("help",
                         keywords=["help","assist","support"],
                         handler=lambda msg, ctx: HandlerResponse(reply="How can I help?"))

        decision, response = await router.route("Hello there!", session_id="sess1")
        print(response.reply)  # "Hello!"
    """
    def __init__(self, db_path: str = "data/router.db",
                 similarity_threshold: float = 0.3,
                 fallback_reply: str = "I'm not sure how to help with that.",
                 **_kwargs):
        self._store = CRStore(db_path)
        self._intents: Dict[str, Intent] = {}
        self._sessions: Dict[str, ConversationSession] = {}
        self._pre_hooks: List[Callable] = []
        self._post_hooks: List[Callable] = []
        self._sim_threshold = similarity_threshold
        self._fallback_reply = fallback_reply
        self._compiled: Dict[str, List[re.Pattern]] = {}

    def register(self, name: str, description: str = "",
                  patterns: List[str] = None,
                  keywords: List[str] = None,
                  examples: List[str] = None,
                  handler: Callable = None,
                  priority: int = 5,
                  slots: Dict[str, str] = None,
                  fallback_intents: List[str] = None,
                  tags: List[str] = None) -> Intent:
        intent = Intent(id=str(uuid.uuid4())[:8], name=name,
                         description=description,
                         patterns=patterns or [],
                         keywords=keywords or [],
                         examples=examples or [],
                         handler=handler, priority=priority,
                         slots=slots or {},
                         fallback_intents=fallback_intents or [],
                         tags=tags or [])
        if name in self._intents:
            raise ValueError(f"Intent {name!r} already registered")
        self._intents[name] = intent
        # Pre-compile patterns
        self._compiled[name] = [re.compile(p, re.I) for p in intent.patterns]
        return intent

    def add_pre_hook(self, fn: Callable): self._pre_hooks.append(fn)
    def add_post_hook(self, fn: Callable): self._post_hooks.append(fn)

    def _classify(self, message: str,
                   session: ConversationSession) -> Tuple[str, float, str]:
        """Returns (intent_name, confidence, method)."""
        msg_lower = message.lower()

        # 0. Respect guided next-expected intent
        if session.next_expected_intent:
            name = session.next_expected_intent
            if name in self._intents:
                return name, 0.95, "guided"

        # 1. Regex
        candidates = sorted(self._intents.values(), key=lambda i: i.priority)
        for intent in candidates:
            for pattern in self._compiled.get(intent.name, []):
                if pattern.search(message):
                    return intent.name, 0.9, "regex"

        # 2. Keyword
        best_kw: Optional[Tuple[str, int]] = None
        for intent in candidates:
            hits = sum(1 for kw in intent.keywords if kw.lower() in msg_lower)
            if hits > 0 and (best_kw is None or hits > best_kw[1]):
                best_kw = (intent.name, hits)
        if best_kw:
            conf = min(0.85, 0.4 + best_kw[1] * 0.15)
            return best_kw[0], conf, "keyword"

        # 3. Similarity to examples
        best_sim: Optional[Tuple[str, float]] = None
        for intent in candidates:
            for ex in intent.examples:
                sim = _bow_cosine(message, ex)
                if sim >= self._sim_threshold:
                    if best_sim is None or sim > best_sim[1]:
                        best_sim = (intent.name, sim)
        if best_sim:
            return best_sim[0], best_sim[1], "similarity"

        return "fallback", 0.0, "fallback"

    def _extract_slots(self, intent: Intent, message: str) -> Dict[str, Any]:
        slots = {}
        for slot_name, pattern in intent.slots.items():
            m = re.search(pattern, message, re.I)
            if m:
                slots[slot_name] = m.group(1) if m.lastindex else m.group(0)
        return slots

    def get_or_create_session(self, session_id: str) -> ConversationSession:
        if session_id in self._sessions:
            return self._sessions[session_id]
        s = self._store.load_session(session_id)
        if s:
            self._sessions[session_id] = s; return s
        s = ConversationSession(id=session_id)
        self._sessions[session_id] = s
        return s

    async def route(self, message: str,
                     session_id: str = None) -> Tuple[RoutingDecision, HandlerResponse]:
        session_id = session_id or str(uuid.uuid4())[:8]
        session = self.get_or_create_session(session_id)

        # Pre-hooks
        for hook in self._pre_hooks:
            try:
                message = hook(message, session) or message
            except: pass

        intent_name, confidence, method = self._classify(message, session)
        intent = self._intents.get(intent_name)
        slots = self._extract_slots(intent, message) if intent else {}

        decision = RoutingDecision(message=message, intent_name=intent_name,
                                    confidence=confidence, method=method,
                                    slots=slots, session_id=session_id)
        self._store.log_decision(decision)

        # Invoke handler
        response = HandlerResponse(reply=self._fallback_reply)
        if intent and intent.handler:
            try:
                import asyncio as _asyncio
                import inspect as _inspect
                handler = intent.handler
                sig = _inspect.signature(handler)
                kwargs: Dict = {}
                if "message" in sig.parameters:  kwargs["message"] = message
                if "ctx"     in sig.parameters:  kwargs["ctx"]     = session.context
                if "slots"   in sig.parameters:  kwargs["slots"]   = slots
                if _asyncio.iscoroutinefunction(handler):
                    result = await handler(**kwargs)
                else:
                    result = handler(**kwargs)
                if isinstance(result, HandlerResponse):
                    response = result
                elif isinstance(result, str):
                    response = HandlerResponse(reply=result)
                elif isinstance(result, dict):
                    response = HandlerResponse(**{k: v for k, v in result.items()
                                                    if k in HandlerResponse.__dataclass_fields__})
            except Exception as e:
                logger.warning(f"Handler error for {intent_name!r}: {e}")

        # Update session
        session.context.update(response.context_updates)
        session.next_expected_intent = response.next_intent
        session.add_turn(message, intent_name, response.reply)
        self._store.save_session(session)

        # Update intent stats
        if intent:
            intent.call_count += 1
            if method != "fallback": intent.match_count += 1

        # Post-hooks
        for hook in self._post_hooks:
            try: hook(decision, response, session)
            except: pass

        return decision, response

    def list_intents(self, tag: str = None) -> List[Intent]:
        intents = list(self._intents.values())
        if tag: intents = [i for i in intents if tag in i.tags]
        return sorted(intents, key=lambda i: i.priority)

    def get_session(self, session_id: str) -> Optional[ConversationSession]:
        return self._sessions.get(session_id) or self._store.load_session(session_id)

    def stats(self) -> Dict:
        s = self._store.stats()
        s["registered_intents"] = len(self._intents)
        s["active_sessions"] = len(self._sessions)
        return s


    # ── v13 backward-compat methods ─────────────────────────────────────────
    def register_handler(self, name: str, handler):
        """v13 compat: attach handler to existing or new intent."""
        if name in self._intents:
            self._intents[name].handler = handler
        else:
            self.register(name, handler=handler)

    def register_intent(self, name: str, patterns=None, handler=None,
                         priority: int = 5, **kwargs) -> "Intent":
        """v13 compat alias for register()."""
        return self.register(name, patterns=patterns or [],
                              keywords=kwargs.get('keywords', []),
                              handler=handler, priority=priority,
                              tags=kwargs.get('tags', []))

    async def dispatch(self, message: str, session_id: str = None):
        """v13 compat: returns reply string."""
        decision, response = await self.route(message, session_id)
        return response.reply

    def disable_intent(self, name: str):
        """v13 compat: mark intent disabled."""
        if name in self._intents:
            self._intents[name].call_count = -1  # sentinel

    def enable_intent(self, name: str):
        """v13 compat: re-enable intent."""
        if name in self._intents and self._intents[name].call_count == -1:
            self._intents[name].call_count = 0

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def route_ep(req):
            d = await req.json()
            decision, response = await self.route(d["message"], d.get("session_id"))
            return web.json_response({"decision": decision.to_dict(),
                                       "response": response.to_dict()})
        async def session_ep(req):
            s = self.get_session(req.match_info["session_id"])
            if not s: return web.json_response({"error":"not found"},status=404)
            return web.json_response(s.to_dict())
        async def intents_ep(req):
            return web.json_response({"intents":[i.to_dict() for i in self.list_intents()]})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/router"
        app.router.add_post(f"{p}/route",                route_ep)
        app.router.add_get( f"{p}/session/{{session_id}}", session_ep)
        app.router.add_get( f"{p}/intents",              intents_ep)
        app.router.add_get( f"{p}/stats",                stats_ep)
        logger.info(f"Conversation router API at {prefix}/router/")

# ── Backward-compatibility shims (v13 API) ───────────────────────────────────
from dataclasses import dataclass as _dc
@_dc
class RoutingTarget:
    """v13 compat stub."""
    name: str = ""; handler: object = None
    priority: int = 5; patterns: list = None
    model: str = ""
    def __init__(self, name="", handler=None, priority=5, patterns=None, **kwargs):
        self.name=name; self.handler=handler; self.priority=priority
        self.patterns=patterns or []
        for k,v in kwargs.items(): setattr(self, k, v)

class MatchStrategy:
    EXACT = "exact"; PREFIX = "prefix"
    REGEX = "regex"; SEMANTIC = "semantic"
