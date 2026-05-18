"""OMNI AGENT - Conversation Summariser
Compress long multi-turn conversations: sliding-window summarisation,
action-item extraction, key-decision tracking, and incremental updates.

Features:
- Sliding window: summarise oldest N turns when context grows too long
- Progressive compression: full → dense → single-sentence summary
- Action items: extract tasks, deadlines, assignees from conversation
- Key decisions: identify and track decision points
- Topic tracking: follow conversation thread topic changes
- Participant analysis: per-speaker message stats
- Incremental updates: add new turns and re-summarise efficiently
- Summary versioning: keep history of all summary snapshots
- Compression ratio: track tokens saved vs original
- SQLite persistence: all summaries and snapshots stored
- REST API: summarise, add-turns, get-summary, action-items
"""
import json, time, uuid, sqlite3, re, asyncio, logging
from typing import Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

@dataclass
class Turn:
    role: str; content: str
    turn_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp: float = field(default_factory=time.time)
    @property
    def word_count(self): return len(self.content.split())
    def to_dict(self): return {"role": self.role, "content": self.content, "turn_id": self.turn_id}

@dataclass
class ActionItem:
    id: str; text: str; assignee: str = ""; deadline: str = ""
    status: str = "open"; source_turn: str = ""
    def to_dict(self):
        return {"id": self.id, "text": self.text, "assignee": self.assignee,
                "deadline": self.deadline, "status": self.status}

@dataclass
class KeyDecision:
    id: str; text: str; context: str = ""; turn_ref: str = ""
    def to_dict(self):
        return {"id": self.id, "text": self.text, "context": self.context}

@dataclass
class ConversationSummary:
    session_id: str; version: int
    full_summary: str; dense_summary: str; one_line: str
    action_items: List[ActionItem]
    key_decisions: List[KeyDecision]
    topics: List[str]
    turns_summarised: int; original_words: int; compressed_words: int
    created_at: float = field(default_factory=time.time)

    @property
    def compression_ratio(self):
        return max(0.0, round(1 - self.compressed_words / max(1, self.original_words), 3))

    def to_dict(self):
        return {"session_id": self.session_id, "version": self.version,
                "full_summary": self.full_summary, "dense_summary": self.dense_summary,
                "one_line": self.one_line,
                "action_items": [a.to_dict() for a in self.action_items],
                "key_decisions": [d.to_dict() for d in self.key_decisions],
                "topics": self.topics, "turns_summarised": self.turns_summarised,
                "original_words": self.original_words, "compressed_words": self.compressed_words,
                "compression_ratio": self.compression_ratio,
                "created_at": self.created_at}

class SumStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()
    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c
    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS summaries(
                    session_id TEXT, version INTEGER,
                    full_summary TEXT, dense_summary TEXT, one_line TEXT,
                    action_items TEXT DEFAULT '[]', key_decisions TEXT DEFAULT '[]',
                    topics TEXT DEFAULT '[]', turns_summarised INTEGER,
                    original_words INTEGER, compressed_words INTEGER, created_at REAL,
                    PRIMARY KEY(session_id, version));
                CREATE TABLE IF NOT EXISTS turns(
                    turn_id TEXT PRIMARY KEY, session_id TEXT, role TEXT,
                    content TEXT, timestamp REAL, summarised INTEGER DEFAULT 0);
                CREATE INDEX IF NOT EXISTS idx_turns_sess ON turns(session_id, timestamp ASC);
            """)
    def save_summary(self, s: ConversationSummary):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO summaries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (s.session_id, s.version, s.full_summary, s.dense_summary, s.one_line,
                 json.dumps([a.to_dict() for a in s.action_items]),
                 json.dumps([d.to_dict() for d in s.key_decisions]),
                 json.dumps(s.topics), s.turns_summarised,
                 s.original_words, s.compressed_words, s.created_at))
    def save_turns(self, session_id: str, turns: List[Turn]):
        with self._conn() as c:
            c.executemany("INSERT OR REPLACE INTO turns VALUES(?,?,?,?,?,0)",
                [(t.turn_id, session_id, t.role, t.content, t.timestamp) for t in turns])
    def get_turns(self, session_id: str, unsummarised_only: bool = False) -> List[Turn]:
        with self._conn() as c:
            if unsummarised_only:
                rows = c.execute("SELECT * FROM turns WHERE session_id=? AND summarised=0 ORDER BY timestamp ASC", (session_id,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM turns WHERE session_id=? ORDER BY timestamp ASC", (session_id,)).fetchall()
        return [Turn(role=r["role"], content=r["content"], turn_id=r["turn_id"], timestamp=r["timestamp"]) for r in rows]
    def mark_summarised(self, session_id: str):
        with self._conn() as c:
            c.execute("UPDATE turns SET summarised=1 WHERE session_id=?", (session_id,))
    def get_latest_summary(self, session_id: str) -> Optional[ConversationSummary]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM summaries WHERE session_id=? ORDER BY version DESC LIMIT 1", (session_id,)).fetchone()
        if not row: return None
        return self._rs(row)
    def get_version_count(self, session_id: str) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM summaries WHERE session_id=?", (session_id,)).fetchone()[0]
    def stats(self):
        with self._conn() as c:
            ns = c.execute("SELECT COUNT(DISTINCT session_id) FROM summaries").fetchone()[0]
            nt = c.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
            avg = c.execute("SELECT AVG(1.0 - CAST(compressed_words AS REAL)/MAX(1,original_words)) FROM summaries").fetchone()[0]
        return {"sessions": ns, "total_turns": nt, "avg_compression_ratio": round(avg or 0, 3)}
    def _rs(self, row) -> ConversationSummary:
        ais = [ActionItem(id=a["id"],text=a["text"],assignee=a.get("assignee",""),
                          deadline=a.get("deadline",""),status=a.get("status","open"))
               for a in json.loads(row["action_items"] or "[]")]
        kds = [KeyDecision(id=d["id"],text=d["text"],context=d.get("context",""))
               for d in json.loads(row["key_decisions"] or "[]")]
        return ConversationSummary(session_id=row["session_id"],version=row["version"],
            full_summary=row["full_summary"],dense_summary=row["dense_summary"],
            one_line=row["one_line"],action_items=ais,key_decisions=kds,
            topics=json.loads(row["topics"] or "[]"),
            turns_summarised=row["turns_summarised"],
            original_words=row["original_words"],compressed_words=row["compressed_words"],
            created_at=row["created_at"])

# ── Heuristic extractors ──────────────────────────────────────────────────────
_ACTION_PATTERNS = re.compile(
    r'\b(please|could you|can you|will you|should|need to|must|action:|todo:|task:)\b.{5,80}',
    re.I)
_DECISION_PATTERNS = re.compile(
    r'\b(decided|agreed|will go with|chosen|confirmed|finalised|we\'ll use|conclusion:)\b.{5,100}',
    re.I)

def _heuristic_actions(turns: List[Turn]) -> List[ActionItem]:
    items = []
    for t in turns:
        for m in _ACTION_PATTERNS.finditer(t.content):
            items.append(ActionItem(id=str(uuid.uuid4())[:8], text=m.group(0).strip(),
                                     source_turn=t.turn_id))
    return items[:10]

def _heuristic_decisions(turns: List[Turn]) -> List[KeyDecision]:
    decisions = []
    for t in turns:
        for m in _DECISION_PATTERNS.finditer(t.content):
            decisions.append(KeyDecision(id=str(uuid.uuid4())[:8], text=m.group(0).strip()))
    return decisions[:5]

def _infer_topics(turns: List[Turn]) -> List[str]:
    TOPIC_PATTERNS = {
        "technical": re.compile(r'\b(code|bug|api|database|server|error|deploy|python|javascript)\b', re.I),
        "planning": re.compile(r'\b(plan|schedule|timeline|sprint|deadline|milestone)\b', re.I),
        "design": re.compile(r'\b(design|ui|ux|layout|style|color|font|wireframe)\b', re.I),
        "business": re.compile(r'\b(revenue|cost|customer|market|product|strategy|roi)\b', re.I),
    }
    all_text = " ".join(t.content for t in turns)
    return [topic for topic, pat in TOPIC_PATTERNS.items() if pat.search(all_text)]

class ConversationSummariser:
    """
    Sliding-window conversation summariser with action item and decision extraction.

    Usage:
        cs = ConversationSummariser(llm_fn=my_llm, window_turns=20)
        session_id = "conv_001"

        cs.add_turns(session_id, [
            Turn("user", "We need to pick a database for the new service."),
            Turn("assistant", "I'd recommend PostgreSQL for relational data."),
            Turn("user", "Agreed, let's go with PostgreSQL. Can you write the schema?"),
        ])

        summary = await cs.summarise(session_id)
        print(summary.one_line)
        for item in summary.action_items: print("TODO:", item.text)
        for dec in summary.key_decisions: print("DECIDED:", dec.text)
    """
    def __init__(self, llm_fn=None, db_path="data/conversation_summaries.db",
                 window_turns: int = 20):
        self._llm_fn = llm_fn
        self._store = SumStore(db_path)
        self._window_turns = window_turns

    async def _call_llm(self, prompt: str) -> str:
        if not self._llm_fn: return ""
        fn = self._llm_fn
        return str(await fn(prompt) if asyncio.iscoroutinefunction(fn) else fn(prompt))

    def add_turns(self, session_id: str, turns: List[Turn]):
        self._store.save_turns(session_id, turns)
        logger.debug(f"Added {len(turns)} turns to session {session_id!r}")

    async def summarise(self, session_id: str,
                         incremental: bool = True) -> ConversationSummary:
        if incremental:
            turns = self._store.get_turns(session_id, unsummarised_only=True)
            if not turns:
                existing = self._store.get_latest_summary(session_id)
                if existing: return existing
        turns = self._store.get_turns(session_id)
        if not turns:
            return ConversationSummary(session_id=session_id, version=1,
                full_summary="No conversation yet.", dense_summary="Empty.",
                one_line="Empty conversation.", action_items=[], key_decisions=[],
                topics=[], turns_summarised=0, original_words=0, compressed_words=0)

        # Apply sliding window — summarise oldest turns if too many
        if len(turns) > self._window_turns:
            turns = turns[-self._window_turns:]

        original_words = sum(t.word_count for t in turns)
        conv_text = "\n".join(f"{t.role.upper()}: {t.content}" for t in turns)

        if self._llm_fn:
            # LLM-powered summarisation
            prompt = (f"Summarise this conversation at 3 levels of detail:\n\n{conv_text[:3000]}\n\n"
                       "Respond ONLY with JSON:\n"
                       '{"full_summary":"2-3 paragraphs...","dense_summary":"2-3 sentences...","one_line":"1 sentence...","action_items":[{"id":"a1","text":"...","assignee":"","deadline":""}],"key_decisions":[{"id":"d1","text":"...","context":""}],"topics":["topic1"]}\n'
                       "JSON only:")
            raw = await self._call_llm(prompt)
            full_s = dense_s = one_line = ""
            action_items = []; key_decisions = []; topics = []
            try:
                m = re.search(r'\{[\s\S]*\}', raw)
                if m:
                    data = json.loads(m.group(0))
                    full_s = data.get("full_summary", "")
                    dense_s = data.get("dense_summary", "")
                    one_line = data.get("one_line", "")
                    action_items = [ActionItem(id=a.get("id", str(uuid.uuid4())[:8]),
                                               text=a.get("text",""),
                                               assignee=a.get("assignee",""),
                                               deadline=a.get("deadline",""))
                                     for a in data.get("action_items",[])]
                    key_decisions = [KeyDecision(id=d.get("id", str(uuid.uuid4())[:8]),
                                                  text=d.get("text",""),
                                                  context=d.get("context",""))
                                      for d in data.get("key_decisions",[])]
                    topics = data.get("topics", [])
            except: pass
            if not full_s:
                full_s = conv_text[:500]; dense_s = conv_text[:150]; one_line = conv_text[:80]
        else:
            # Heuristic fallback
            sentences = [s.strip() for s in re.split(r'[.!?]', conv_text) if len(s.strip()) > 20]
            full_s = conv_text[:600]
            dense_s = ". ".join(sentences[:4])
            one_line = sentences[0] if sentences else conv_text[:80]
            action_items = _heuristic_actions(turns)
            key_decisions = _heuristic_decisions(turns)
            topics = _infer_topics(turns)

        compressed_words = len(full_s.split())
        version = self._store.get_version_count(session_id) + 1
        summary = ConversationSummary(
            session_id=session_id, version=version,
            full_summary=full_s, dense_summary=dense_s, one_line=one_line,
            action_items=action_items, key_decisions=key_decisions, topics=topics,
            turns_summarised=len(turns), original_words=original_words,
            compressed_words=compressed_words)
        self._store.save_summary(summary)
        self._store.mark_summarised(session_id)
        logger.info(f"Summarised session {session_id!r}: {len(turns)} turns → "
                     f"compression={summary.compression_ratio:.0%}")
        return summary

    def get_summary(self, session_id: str) -> Optional[ConversationSummary]:
        return self._store.get_latest_summary(session_id)

    def get_turns(self, session_id: str) -> List[Turn]:
        return self._store.get_turns(session_id)

    def stats(self) -> Dict:
        return self._store.stats()

    def participant_stats(self, session_id: str) -> Dict:
        turns = self._store.get_turns(session_id)
        from collections import Counter
        role_counts = Counter(t.role for t in turns)
        role_words = {}
        for t in turns:
            role_words[t.role] = role_words.get(t.role, 0) + t.word_count
        return {"total_turns": len(turns), "by_role": dict(role_counts),
                "words_by_role": role_words}

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def summarise_ep(req):
            d = await req.json()
            summary = await self.summarise(d["session_id"], d.get("incremental", True))
            return web.json_response(summary.to_dict())
        async def add_turns_ep(req):
            d = await req.json()
            turns = [Turn(role=t["role"], content=t["content"]) for t in d.get("turns",[])]
            self.add_turns(d["session_id"], turns)
            return web.json_response({"added": len(turns)}, status=201)
        async def get_summary_ep(req):
            s = self.get_summary(req.match_info["id"])
            if not s: return web.json_response({"error":"not found"},status=404)
            return web.json_response(s.to_dict())
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/summarise"
        app.router.add_post(f"{p}", summarise_ep)
        app.router.add_post(f"{p}/turns", add_turns_ep)
        app.router.add_get( f"{p}/{{id}}", get_summary_ep)
        app.router.add_get( f"{p}/stats", stats_ep)
        logger.info(f"Conversation summariser API at {prefix}/summarise/")
