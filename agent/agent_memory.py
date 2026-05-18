"""OMNI AGENT - Agent Memory
Long-term agent memory: store/retrieve facts with recency + relevance
decay, forgetting curve, and associative recall.

Features:
- Memory entry: content, embedding proxy (BOW), tags, source, importance
- Recency decay: exponential decay on retrieval score (half-life configurable)
- Relevance score: BOW cosine between query and memory content
- Forgetting curve: Ebbinghaus model — strength decays unless reinforced
- Reinforcement: accessing a memory resets its decay clock
- Importance weighting: explicit 0-1 importance multiplier per entry
- Associative recall: given an anchor memory, find most similar others
- Tag-based retrieval: filter memories by tag set
- Capacity management: evict weakest when max_entries exceeded
- Consolidation: merge near-duplicate memories, keeping highest importance
- Export / import: full memory dump for portability
- SQLite persistence: entries, access log
- REST API: store, recall, forget, reinforce, stats
"""
import math, re, sqlite3, time, uuid, json, logging
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

def _tokenize(text: str) -> List[str]:
    return re.findall(r'\b\w+\b', text.lower())

def _bow(tokens: List[str]) -> Dict[str, float]:
    d: Dict[str, float] = {}
    for t in tokens: d[t] = d.get(t, 0) + 1
    n = max(1, len(tokens))
    return {k: v / n for k, v in d.items()}

def _cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
    keys = set(a) & set(b)
    dot  = sum(a[k] * b[k] for k in keys)
    na   = math.sqrt(sum(v*v for v in a.values()))
    nb   = math.sqrt(sum(v*v for v in b.values()))
    return dot / max(1e-12, na * nb)

def _jaccard(a: str, b: str) -> float:
    sa = set(_tokenize(a)); sb = set(_tokenize(b))
    if not sa and not sb: return 1.0
    return len(sa & sb) / max(1, len(sa | sb))

def _ebbinghaus(strength: float, elapsed_s: float,
                 half_life_s: float = 86400.0) -> float:
    """Retention fraction after elapsed_s given strength and half-life."""
    k = math.log(2) / max(1.0, half_life_s)
    return strength * math.exp(-k * elapsed_s)

@dataclass
class MemoryEntry:
    id: str; content: str
    tags: List[str] = field(default_factory=list)
    source: str = ""
    importance: float = 0.5           # 0-1
    strength: float = 1.0             # Ebbinghaus strength, resets on access
    access_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    _bow_cache: Dict[str, float] = field(default_factory=dict)

    def __post_init__(self):
        if not self._bow_cache:
            self._bow_cache = _bow(_tokenize(self.content))

    def retention(self, half_life_s: float = 86400.0) -> float:
        elapsed = time.time() - self.last_accessed
        return _ebbinghaus(self.strength, elapsed, half_life_s)

    def reinforce(self):
        self.strength = min(2.0, self.strength + 0.3)
        self.last_accessed = time.time()
        self.access_count += 1

    def relevance(self, query_bow: Dict[str, float]) -> float:
        return _cosine(query_bow, self._bow_cache)

    def score(self, query_bow: Dict[str, float],
               half_life_s: float,
               recency_weight: float = 0.3) -> float:
        rel  = self.relevance(query_bow)
        ret  = self.retention(half_life_s)
        return (rel * (1 - recency_weight) + ret * recency_weight) * self.importance

    def to_dict(self):
        return {"id": self.id, "content": self.content,
                "tags": self.tags, "source": self.source,
                "type": "semantic",  # v20 compat
                "memory_type": "semantic",  # v20 compat
                "importance": round(self.importance, 3),
                "strength": round(self.strength, 3),
                "access_count": self.access_count,
                "retention": round(self.retention(), 4),
                "created_at": round(self.created_at, 1)}

class AMStore:
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
                    tags TEXT DEFAULT '[]', source TEXT DEFAULT '',
                    importance REAL DEFAULT 0.5, strength REAL DEFAULT 1.0,
                    access_count INTEGER DEFAULT 0,
                    created_at REAL, last_accessed REAL);
                CREATE TABLE IF NOT EXISTS access_log(
                    id TEXT PRIMARY KEY, memory_id TEXT,
                    query TEXT DEFAULT '', created_at REAL);
                CREATE INDEX IF NOT EXISTS idx_mem_imp ON memories(importance DESC);
            """)

    def save(self, m: MemoryEntry):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO memories VALUES(?,?,?,?,?,?,?,?,?)",
                (m.id, m.content, json.dumps(m.tags), m.source,
                 m.importance, m.strength, m.access_count,
                 m.created_at, m.last_accessed))

    def load_all(self) -> List[MemoryEntry]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM memories ORDER BY importance DESC").fetchall()
        out = []
        for r in rows:
            m = MemoryEntry(id=r["id"], content=r["content"],
                             tags=json.loads(r["tags"] or "[]"),
                             source=r["source"] or "",
                             importance=r["importance"], strength=r["strength"],
                             access_count=r["access_count"],
                             created_at=r["created_at"],
                             last_accessed=r["last_accessed"])
            out.append(m)
        return out

    def delete(self, mid: str):
        with self._conn() as c:
            c.execute("DELETE FROM memories WHERE id=?", (mid,))

    def log_access(self, memory_id: str, query: str):
        with self._conn() as c:
            c.execute("INSERT INTO access_log VALUES(?,?,?,?)",
                (str(uuid.uuid4())[:8], memory_id, query[:200], time.time()))

    def stats(self) -> Dict:
        with self._conn() as c:
            n = c.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            avg_imp = c.execute(
                "SELECT AVG(importance) FROM memories").fetchone()[0] or 0
            accesses = c.execute(
                "SELECT COUNT(*) FROM access_log").fetchone()[0]
        return {"total_memories": n, "total": n,  # v20 compat
                "avg_importance": round(avg_imp, 3),
                "total_accesses": accesses}

class AgentMemory:
    """
    Long-term agent memory with Ebbinghaus forgetting curve and associative recall.

    Usage:
        memory = AgentMemory(max_entries=500)

        memory.store("Python uses indentation to define code blocks",
                      tags=["python","syntax"], importance=0.8)
        memory.store("The capital of France is Paris",
                      tags=["geography"], importance=0.6)

        results = memory.recall("Python programming", top_k=3)
        for m, score in results:
            print(score, m.content)
    """
    def __init__(self, db_path: str = "data/memory.db",
                 max_entries: int = 1000,
                 working_capacity: int = None,
                 half_life_s: float = 86400.0,
                 recency_weight: float = 0.3,
                 dedup_threshold: float = 0.7):
        self._store = AMStore(db_path)
        self._memories: Dict[str, MemoryEntry] = {}
        self._max_entries = working_capacity if working_capacity is not None else max_entries
        self._half_life_s = half_life_s
        self._recency_weight = recency_weight
        self._dedup_threshold = dedup_threshold
        # Load persisted memories
        for m in self._store.load_all():
            self._memories[m.id] = m

    def store(self, content: str, tags: List[str] = None,
               source: str = "", importance: float = 0.5,
               dedup: bool = True) -> Optional[str]:
        if not content.strip(): return None

        # Dedup check
        if dedup:
            for m in self._memories.values():
                if _jaccard(content, m.content) >= self._dedup_threshold:
                    # Reinforce existing instead
                    m.reinforce()
                    m.importance = max(m.importance, importance)
                    self._store.save(m)
                    return m.id

        # Evict weakest if at capacity
        if len(self._memories) >= self._max_entries:
            self._evict()

        mid = str(uuid.uuid4())[:10]
        m = MemoryEntry(id=mid, content=content,
                         tags=tags or [], source=source,
                         importance=min(1.0, max(0.0, importance)))
        self._memories[mid] = m
        self._store.save(m)
        logger.debug(f"Memory stored: {mid!r}")
        return mid

    def recall(self, query: str, top_k: int = 5,
                tags: List[str] = None,
                min_score: float = 0.0) -> List[Tuple[MemoryEntry, float]]:
        qbow = _bow(_tokenize(query))
        candidates = list(self._memories.values())
        if tags:
            candidates = [m for m in candidates
                           if any(t in m.tags for t in tags)]
        scored = [(m, m.score(qbow, self._half_life_s, self._recency_weight))
                  for m in candidates]
        scored = [(m, s) for m, s in scored if s >= min_score]
        scored.sort(key=lambda x: -x[1])
        results = scored[:top_k]
        # Reinforce accessed memories
        for m, _ in results:
            m.reinforce()
            self._store.save(m)
            self._store.log_access(m.id, query)
        return results

    def get(self, mid: str) -> Optional[MemoryEntry]:
        return self._memories.get(mid)

    def forget(self, mid: str) -> bool:
        if mid not in self._memories: return False
        del self._memories[mid]
        self._store.delete(mid)
        return True

    def reinforce(self, mid: str) -> bool:
        m = self._memories.get(mid)
        if not m: return False
        m.reinforce(); self._store.save(m)
        return True

    def _evict(self):
        """Remove weakest memory (lowest retention × importance)."""
        if not self._memories: return
        weakest = min(self._memories.values(),
                       key=lambda m: m.retention(self._half_life_s) * m.importance)
        logger.debug(f"Evicting memory: {weakest.id!r}")
        del self._memories[weakest.id]
        self._store.delete(weakest.id)

    def consolidate(self) -> int:
        """Merge near-duplicate memories. Returns # merged."""
        ids = list(self._memories.keys()); merged = 0
        skip = set()
        for i in range(len(ids)):
            if ids[i] in skip: continue
            for j in range(i+1, len(ids)):
                if ids[j] in skip: continue
                ma = self._memories[ids[i]]
                mb = self._memories[ids[j]]
                if _jaccard(ma.content, mb.content) >= self._dedup_threshold:
                    # Keep higher importance
                    if mb.importance > ma.importance:
                        skip.add(ids[i]); break
                    else:
                        skip.add(ids[j])
                    merged += 1
        for mid in skip:
            self.forget(mid)
        return merged

    def associative_recall(self, anchor_id: str,
                            top_k: int = 5) -> List[Tuple[MemoryEntry, float]]:
        anchor = self._memories.get(anchor_id)
        if not anchor: return []
        return self.recall(anchor.content, top_k=top_k+1,
                            min_score=0.0)[1:top_k+1]  # skip self

    def list(self, tag: str = None, limit: int = 100) -> List[MemoryEntry]:
        ms = list(self._memories.values())
        if tag: ms = [m for m in ms if tag in m.tags]
        ms.sort(key=lambda m: -m.importance)
        return ms[:limit]

    def export(self) -> List[Dict]:
        return [m.to_dict() for m in self._memories.values()]

    def stats(self) -> Dict:
        s = self._store.stats()
        s["in_memory"] = len(self._memories)
        s["max_entries"] = self._max_entries
        s["half_life_s"] = self._half_life_s
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def store_ep(req):
            d = await req.json()
            mid = self.store(d["content"], d.get("tags",[]),
                              d.get("source",""),
                              float(d.get("importance",0.5)),
                              d.get("dedup",True))
            return web.json_response({"memory_id": mid}, status=201)
        async def recall_ep(req):
            d = await req.json()
            results = self.recall(d["query"], int(d.get("top_k",5)),
                                   d.get("tags"), float(d.get("min_score",0)))
            return web.json_response(
                {"results": [{"memory": m.to_dict(), "score": round(s,4)}
                              for m,s in results]})
        async def forget_ep(req):
            d = await req.json()
            ok = self.forget(d["memory_id"])
            return web.json_response({"forgotten": ok})
        async def reinforce_ep(req):
            d = await req.json()
            ok = self.reinforce(d["memory_id"])
            return web.json_response({"reinforced": ok})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/memory"
        app.router.add_post(f"{p}/store",     store_ep)
        app.router.add_post(f"{p}/recall",    recall_ep)
        app.router.add_post(f"{p}/forget",    forget_ep)
        app.router.add_post(f"{p}/reinforce", reinforce_ep)
        app.router.add_get( f"{p}/stats",     stats_ep)
        logger.info(f"Agent memory API at {prefix}/memory/")

# ── v20 backward-compatibility shims ──────────────────────────────────────────
class MemoryType:
    SEMANTIC = "semantic"; EPISODIC = "episodic"
    WORKING  = "working";  PROCEDURAL = "procedural"

import dataclasses as _dc

@_dc.dataclass
class Memory:
    """v20 compat: named memory record."""
    id: str = ""; content: str = ""
    memory_type: str = MemoryType.SEMANTIC
    tags: list = _dc.field(default_factory=list)
    importance: float = 0.5; access_count: int = 0
    source: str = ""

    def to_dict(self):
        return {"id": self.id, "content": self.content,
                "memory_type": self.memory_type, "tags": self.tags,
                "importance": self.importance}

# Patch AgentMemory with v20 methods
def _remember(self, content, memory_type=MemoryType.SEMANTIC,
               tags=None, importance=0.5, **_kw):
    """v20 alias for store()."""
    mid = self.store(content, tags=tags or [], importance=importance)
    m = Memory(id=mid, content=content, memory_type=memory_type,
                tags=tags or [], importance=importance)
    return m

AgentMemory.remember = _remember

def _recall_v20(self, query, top_k=5, tags=None, memory_type=None, **_kw):
    """v20 alias for recall(), returns Memory objects."""
    results = self.recall(query, top_k=top_k, tags=tags)
    return [Memory(id=m.id, content=m.content, tags=m.tags,
                    importance=m.importance, access_count=m.access_count)
             for m, _ in results]

AgentMemory.recall_memories = _recall_v20

def _boost_importance(self, mid, delta=0.1):
    """v20: increase importance of a memory."""
    m = self.get(mid)
    if m: m.importance = min(1.0, m.importance + delta); self._store.save(m)

AgentMemory.boost_importance = _boost_importance

def _forget_below(self, threshold=0.3):
    """v20: forget all memories with importance below threshold."""
    ids = [m.id for m in self._memories.values() if m.importance < threshold]
    for mid in ids: self.forget(mid)
    return len(ids)

AgentMemory.forget_below_importance = _forget_below

def _clear_type(self, memory_type):
    """v20: clear all memories of a given type (noop for typing compat)."""
    pass

AgentMemory.clear_type = _clear_type

def _working_capacity(self):
    return self._max_entries

AgentMemory.working_capacity = property(_working_capacity)
