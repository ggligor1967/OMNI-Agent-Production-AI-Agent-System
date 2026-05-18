"""OMNI AGENT - Memory Store
Agent working memory with short/long-term tiers, associative retrieval,
decay scoring, and capacity-driven forgetting.

Features:
- Memory item: content, type, importance, timestamp, access_count, tags, embedding_hint
- Short-term: fixed-capacity buffer (FIFO eviction); rapid access
- Long-term: importance-weighted storage; slower decay
- Decay: score = importance × exp(-decay_rate × age_hours)
- Consolidation: move high-importance short-term items to long-term
- Retrieval: keyword search + recency boost + importance boost
- Relevance scoring: TF-like keyword match + recency + importance weighted sum
- Associative links: manual link(id1, id2, strength) for related memories
- Forgetting: remove items below score_threshold (configurable)
- Context window: return top-K relevant memories for a query
- Memory types: FACT, EPISODE, SKILL, GOAL, OBSERVATION, REFLECTION
- Working set: pin items that must always be in context
- Statistics: tier sizes, avg decay score, access patterns
- SQLite persistence: short_term, long_term, associations
- REST API: remember, recall, forget, consolidate, stats
"""
import json, math, re, sqlite3, time, uuid, logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class MemType(str, Enum):
    FACT        = "fact"
    EPISODE     = "episode"
    SKILL       = "skill"
    GOAL        = "goal"
    OBSERVATION = "observation"
    REFLECTION  = "reflection"

def _tokenize(text: str) -> List[str]:
    return re.findall(r'\w+', text.lower())

def _tf_score(query_tokens: List[str], content: str) -> float:
    content_tokens = _tokenize(content)
    if not content_tokens: return 0.0
    hits = sum(1 for t in query_tokens if t in content_tokens)
    return hits / max(1, len(query_tokens))

@dataclass
class MemoryItem:
    id: str; content: str
    mem_type: MemType = MemType.FACT
    importance: float = 0.5       # 0-1
    tags: List[str] = field(default_factory=list)
    metadata: Dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    access_count: int = 0
    pinned: bool = False
    tier: str = "short"           # "short" | "long"

    def decay_score(self, decay_rate: float = 0.1) -> float:
        age_hours = (time.time() - self.created_at) / 3600
        return self.importance * math.exp(-decay_rate * age_hours)

    def recency_score(self) -> float:
        age_s = time.time() - self.last_accessed
        return math.exp(-age_s / 3600)   # half-life 1 hour

    def relevance(self, query_tokens: List[str],
                   decay_rate: float = 0.1) -> float:
        tf = _tf_score(query_tokens, self.content)
        decay = self.decay_score(decay_rate)
        rec = self.recency_score()
        return tf * 0.5 + decay * 0.3 + rec * 0.2

    def touch(self):
        self.last_accessed = time.time()
        self.access_count += 1

    def to_dict(self):
        return {"id": self.id, "content": self.content,
                "type": self.mem_type.value,
                "importance": self.importance,
                "tags": self.tags, "tier": self.tier,
                "decay_score": round(self.decay_score(), 4),
                "access_count": self.access_count,
                "pinned": self.pinned,
                "created_at": round(self.created_at, 2),
                "last_accessed": round(self.last_accessed, 2)}

class MSStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS memories(
                    id TEXT PRIMARY KEY, content TEXT,
                    mem_type TEXT DEFAULT 'fact',
                    importance REAL DEFAULT 0.5,
                    tags TEXT DEFAULT '[]',
                    metadata TEXT DEFAULT '{}',
                    tier TEXT DEFAULT 'short',
                    pinned INTEGER DEFAULT 0,
                    access_count INTEGER DEFAULT 0,
                    created_at REAL, last_accessed REAL);
                CREATE TABLE IF NOT EXISTS associations(
                    id TEXT PRIMARY KEY,
                    a TEXT, b TEXT, strength REAL DEFAULT 0.5,
                    created_at REAL);
                CREATE INDEX IF NOT EXISTS idx_mem_tier ON memories(tier, importance DESC);
                CREATE INDEX IF NOT EXISTS idx_assoc_a  ON associations(a);
                CREATE INDEX IF NOT EXISTS idx_assoc_b  ON associations(b);
            """)

    def save(self, m: MemoryItem):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO memories VALUES"
                       "(?,?,?,?,?,?,?,?,?,?,?)",
                (m.id, m.content, m.mem_type.value, m.importance,
                 json.dumps(m.tags), json.dumps(m.metadata),
                 m.tier, int(m.pinned), m.access_count,
                 m.created_at, m.last_accessed))

    def delete(self, mid: str):
        with self._conn() as c:
            c.execute("DELETE FROM memories WHERE id=?", (mid,))

    def load_all(self) -> List[MemoryItem]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM memories").fetchall()
        items = []
        for r in rows:
            try:
                m = MemoryItem(id=r["id"], content=r["content"],
                                mem_type=MemType(r["mem_type"]),
                                importance=r["importance"],
                                tags=json.loads(r["tags"] or "[]"),
                                metadata=json.loads(r["metadata"] or "{}"),
                                tier=r["tier"], pinned=bool(r["pinned"]),
                                access_count=r["access_count"],
                                created_at=r["created_at"],
                                last_accessed=r["last_accessed"])
                items.append(m)
            except Exception: pass
        return items

    def save_association(self, a: str, b: str, strength: float):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO associations VALUES(?,?,?,?,?)",
                (f"{min(a,b)}:{max(a,b)}", a, b, strength, time.time()))

    def get_associated(self, mid: str) -> List[Tuple[str, float]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM associations WHERE a=? OR b=?",
                (mid, mid)).fetchall()
        result = []
        for r in rows:
            other = r["b"] if r["a"] == mid else r["a"]
            result.append((other, r["strength"]))
        return result

    def stats(self) -> Dict:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            short = c.execute(
                "SELECT COUNT(*) FROM memories WHERE tier='short'").fetchone()[0]
            lng   = c.execute(
                "SELECT COUNT(*) FROM memories WHERE tier='long'").fetchone()[0]
            assoc = c.execute("SELECT COUNT(*) FROM associations").fetchone()[0]
        return {"total": total, "short_term": short,
                "long_term": lng, "associations": assoc}

class MemoryStore:
    """
    Two-tier agent memory with decay scoring and associative retrieval.

    Usage:
        ms = MemoryStore(short_term_cap=50, long_term_cap=500)

        # Store memories
        ms.remember("Paris is the capital of France",
                      mem_type=MemType.FACT, importance=0.7)
        ms.remember("User prefers concise answers",
                      mem_type=MemType.OBSERVATION, importance=0.9)

        # Recall relevant context
        context = ms.recall("France capitals", top_k=5)
        for m in context:
            print(m.content, m.decay_score())
    """
    def __init__(self, db_path: str = "data/memory.db",
                 short_term_cap: int = 50,
                 long_term_cap: int = 500,
                 decay_rate: float = 0.1,
                 consolidation_threshold: float = 0.7,
                 forget_threshold: float = 0.01):
        self._store = MSStore(db_path)
        self._short: Dict[str, MemoryItem] = {}
        self._long:  Dict[str, MemoryItem] = {}
        self._pinned: Set[str] = set()
        self._assoc: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
        self.short_term_cap = short_term_cap
        self.long_term_cap  = long_term_cap
        self.decay_rate     = decay_rate
        self.consolidation_threshold = consolidation_threshold
        self.forget_threshold = forget_threshold
        # Load from DB
        for m in self._store.load_all():
            tier = self._short if m.tier == "short" else self._long
            tier[m.id] = m
            if m.pinned: self._pinned.add(m.id)

    def remember(self, content: str,
                  mem_type: MemType = MemType.FACT,
                  importance: float = 0.5,
                  tags: List[str] = None,
                  metadata: Dict = None,
                  pinned: bool = False,
                  memory_id: str = None) -> MemoryItem:
        mid = memory_id or str(uuid.uuid4())[:12]
        m = MemoryItem(id=mid, content=content, mem_type=mem_type,
                        importance=importance, tags=list(tags or []),
                        metadata=dict(metadata or {}), pinned=pinned,
                        tier="short")
        # Manage short-term capacity
        if len(self._short) >= self.short_term_cap:
            self._evict_short()
        self._short[mid] = m
        if pinned: self._pinned.add(mid)
        self._store.save(m)
        return m

    def _evict_short(self):
        """Remove lowest decay-score unpinned item from short-term."""
        candidates = [(mid, m.decay_score(self.decay_rate))
                       for mid, m in self._short.items()
                       if mid not in self._pinned]
        if not candidates: return
        worst_id = min(candidates, key=lambda x: x[1])[0]
        evicted = self._short.pop(worst_id)
        # Consolidate to long-term if important enough
        if evicted.importance >= self.consolidation_threshold:
            self._store.delete(worst_id)
            evicted.tier = "long"
            self._promote_to_long(evicted)
        else:
            self._store.delete(worst_id)

    def _promote_to_long(self, m: MemoryItem):
        if len(self._long) >= self.long_term_cap:
            self._evict_long()
        self._long[m.id] = m
        self._store.save(m)

    def _evict_long(self):
        candidates = [(mid, m.decay_score(self.decay_rate))
                       for mid, m in self._long.items()
                       if mid not in self._pinned]
        if not candidates: return
        worst_id = min(candidates, key=lambda x: x[1])[0]
        del self._long[worst_id]
        self._store.delete(worst_id)

    def consolidate(self) -> int:
        """Move high-importance short-term items to long-term. Returns count."""
        moved = 0
        for mid, m in list(self._short.items()):
            if m.importance >= self.consolidation_threshold:
                del self._short[mid]
                m.tier = "long"
                self._promote_to_long(m)
                moved += 1
        return moved

    def forget(self, score_threshold: float = None) -> int:
        """Remove items below score threshold. Returns count forgotten."""
        threshold = score_threshold or self.forget_threshold
        forgotten = 0
        for store in (self._short, self._long):
            to_del = [mid for mid, m in store.items()
                       if mid not in self._pinned
                       and m.decay_score(self.decay_rate) < threshold]
            for mid in to_del:
                del store[mid]
                self._store.delete(mid)
                forgotten += 1
        return forgotten

    def recall(self, query: str, top_k: int = 5,
                mem_type: MemType = None,
                tier: str = None,
                tag: str = None) -> List[MemoryItem]:
        """Return top-K most relevant memories for query."""
        query_tokens = _tokenize(query)
        all_items = list(self._short.values()) + list(self._long.values())
        # Filters
        if mem_type: all_items = [m for m in all_items if m.mem_type == mem_type]
        if tier:     all_items = [m for m in all_items if m.tier == tier]
        if tag:      all_items = [m for m in all_items if tag in m.tags]
        # Score and sort
        scored = sorted(all_items,
                         key=lambda m: m.relevance(query_tokens, self.decay_rate),
                         reverse=True)[:top_k]
        # Touch accessed items
        for m in scored: m.touch(); self._store.save(m)
        return scored

    def get(self, memory_id: str) -> Optional[MemoryItem]:
        m = self._short.get(memory_id) or self._long.get(memory_id)
        if m: m.touch(); self._store.save(m)
        return m

    def link(self, id1: str, id2: str, strength: float = 0.5):
        self._assoc[id1].append((id2, strength))
        self._assoc[id2].append((id1, strength))
        self._store.save_association(id1, id2, strength)

    def associated(self, memory_id: str) -> List[MemoryItem]:
        links = self._assoc.get(memory_id, [])
        items = []
        for (oid, _) in sorted(links, key=lambda x: -x[1]):
            m = self._short.get(oid) or self._long.get(oid)
            if m: items.append(m)
        return items

    def pin(self, memory_id: str):
        self._pinned.add(memory_id)
        m = self.get(memory_id)
        if m: m.pinned = True; self._store.save(m)

    def unpin(self, memory_id: str):
        self._pinned.discard(memory_id)
        m = self.get(memory_id)
        if m: m.pinned = False; self._store.save(m)

    def context_window(self, query: str, max_tokens: int = 2000,
                        top_k: int = 10) -> List[MemoryItem]:
        """Return memories fitting within token budget (est 4 chars/token)."""
        candidates = self.recall(query, top_k=top_k)
        result = []; used_tokens = 0
        for m in candidates:
            est = len(m.content) // 4
            if used_tokens + est > max_tokens: break
            result.append(m); used_tokens += est
        return result

    def stats(self) -> Dict:
        s = self._store.stats()
        s["in_memory_short"] = len(self._short)
        s["in_memory_long"]  = len(self._long)
        s["pinned"] = len(self._pinned)
        all_items = list(self._short.values()) + list(self._long.values())
        if all_items:
            scores = [m.decay_score(self.decay_rate) for m in all_items]
            s["avg_decay_score"] = round(sum(scores)/len(scores), 4)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def remember_ep(req):
            d = await req.json()
            m = self.remember(d["content"],
                               MemType(d.get("type","fact")),
                               float(d.get("importance",0.5)),
                               d.get("tags",[]),
                               pinned=d.get("pinned",False))
            return web.json_response(m.to_dict(), status=201)
        async def recall_ep(req):
            d = await req.json()
            items = self.recall(d["query"], d.get("top_k",5))
            return web.json_response({"memories": [m.to_dict() for m in items]})
        async def forget_ep(req):
            d = await req.json()
            n = self.forget(d.get("threshold"))
            return web.json_response({"forgotten": n})
        async def consolidate_ep(req):
            n = self.consolidate()
            return web.json_response({"consolidated": n})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/memory"
        app.router.add_post(f"{p}/remember",    remember_ep)
        app.router.add_post(f"{p}/recall",      recall_ep)
        app.router.add_post(f"{p}/forget",      forget_ep)
        app.router.add_post(f"{p}/consolidate", consolidate_ep)
        app.router.add_get( f"{p}/stats",       stats_ep)
        logger.info(f"Memory store API at {prefix}/memory/")
