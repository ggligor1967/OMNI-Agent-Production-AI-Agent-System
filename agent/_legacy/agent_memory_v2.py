"""OMNI Agent — Agent Memory V2: episodic, semantic, and working memory with consolidation."""
from __future__ import annotations
import hashlib, json, math, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class MemoryType(str, Enum):
    EPISODIC  = "episodic"    # events / conversations
    SEMANTIC  = "semantic"    # facts / knowledge
    WORKING   = "working"     # short-term scratchpad
    PROCEDURAL = "procedural" # how-to / skills
    EMOTIONAL  = "emotional"  # sentiment-tagged memories


class MemoryImportance(str, Enum):
    CRITICAL = "critical"
    HIGH     = "high"
    MEDIUM   = "medium"
    LOW      = "low"
    TRIVIAL  = "trivial"


IMPORTANCE_SCORE = {
    MemoryImportance.CRITICAL: 1.0,
    MemoryImportance.HIGH:     0.8,
    MemoryImportance.MEDIUM:   0.5,
    MemoryImportance.LOW:      0.3,
    MemoryImportance.TRIVIAL:  0.1,
}


def _cosine(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na  = math.sqrt(sum(x * x for x in a))
    nb  = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na > 0 and nb > 0 else 0.0


@dataclass
class MemoryEntry:
    memory_id: str
    memory_type: MemoryType
    content: str
    importance: MemoryImportance = MemoryImportance.MEDIUM
    embedding: Optional[List[float]] = None
    tags: List[str] = field(default_factory=list)
    source: str = ""
    session_id: str = ""
    agent_id: str = ""
    created_at: float = field(default_factory=time.time)
    accessed_at: float = field(default_factory=time.time)
    access_count: int = 0
    decay_rate: float = 0.01          # memory fades this much per hour
    reinforcement: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    consolidated: bool = False        # True after consolidation pass

    @property
    def age_hours(self) -> float:
        return (time.time() - self.created_at) / 3600

    @property
    def recency_score(self) -> float:
        """Exponential decay based on age."""
        return math.exp(-self.decay_rate * self.age_hours)

    @property
    def importance_score(self) -> float:
        return IMPORTANCE_SCORE.get(self.importance, 0.5)

    @property
    def salience(self) -> float:
        """Combined score: importance * recency + access bonus."""
        return (self.importance_score * 0.6
                + self.recency_score * 0.3
                + min(self.access_count / 10, 1.0) * 0.1
                + self.reinforcement * 0.1)

    def touch(self):
        self.accessed_at = time.time()
        self.access_count += 1

    def reinforce(self, amount: float = 0.1):
        self.reinforcement = min(1.0, self.reinforcement + amount)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "type": self.memory_type.value,
            "content": self.content[:120] + "…" if len(self.content) > 120 else self.content,
            "importance": self.importance.value,
            "tags": self.tags,
            "source": self.source,
            "age_hours": round(self.age_hours, 2),
            "access_count": self.access_count,
            "salience": round(self.salience, 4),
            "consolidated": self.consolidated,
        }


@dataclass
class WorkingMemorySlot:
    key: str
    value: Any
    set_at: float = field(default_factory=time.time)
    ttl_s: Optional[float] = None

    def is_expired(self) -> bool:
        if self.ttl_s is None:
            return False
        return (time.time() - self.set_at) > self.ttl_s


class AgentMemoryV2:
    """
    Structured agent memory system:
    - Episodic memory (events, conversations)
    - Semantic memory (facts, entities)
    - Working memory (scratchpad with TTL)
    - Procedural memory (skills, steps)
    - Retrieval by recency, importance, semantic similarity
    - Memory consolidation (dedup + summarize important)
    - Forgetting (decay-based cleanup)
    - SQLite persistence
    """

    def __init__(
        self,
        working_capacity: int = 20,
        embed_fn: Optional[Callable[[str], List[float]]] = None,
        db_path: str = ":memory:",
    ):
        self.working_capacity = working_capacity
        self._embed_fn = embed_fn or self._hash_embed(16)
        self._memories: Dict[str, MemoryEntry] = {}
        self._working: Dict[str, WorkingMemorySlot] = {}
        self._consolidation_hooks: List[Callable] = []
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._store_count = 0
        self._retrieve_count = 0

    @staticmethod
    def _hash_embed(dim: int) -> Callable[[str], List[float]]:
        def embed(text: str) -> List[float]:
            h = hashlib.md5(text.encode()).digest()
            raw = list(h) * (dim // 16 + 1)
            vec = [(b / 127.5) - 1.0 for b in raw[:dim]]
            n   = math.sqrt(sum(x * x for x in vec))
            return [x / n for x in vec] if n > 0 else vec
        return embed

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS amv2_memories (
                memory_id TEXT PRIMARY KEY, memory_type TEXT,
                content TEXT, importance TEXT, tags TEXT,
                source TEXT, session_id TEXT, agent_id TEXT,
                created_at REAL, accessed_at REAL, access_count INTEGER,
                reinforcement REAL, consolidated INTEGER, metadata TEXT
            );
            CREATE TABLE IF NOT EXISTS amv2_working (
                key TEXT PRIMARY KEY, value TEXT, set_at REAL, ttl_s REAL
            );
        """)
        self._db.commit()

    # ── STORE ─────────────────────────────────────────────────────────

    def store(self, content: str,
              memory_type: MemoryType = MemoryType.EPISODIC,
              importance: MemoryImportance = MemoryImportance.MEDIUM,
              tags: Optional[List[str]] = None,
              source: str = "",
              session_id: str = "",
              agent_id: str = "",
              embed: bool = True,
              metadata: Optional[Dict] = None,
              memory_id: Optional[str] = None) -> MemoryEntry:
        mid = memory_id or str(uuid.uuid4())
        emb = self._embed_fn(content) if embed else None
        entry = MemoryEntry(
            memory_id=mid, memory_type=memory_type,
            content=content, importance=importance,
            embedding=emb, tags=list(tags or []),
            source=source, session_id=session_id,
            agent_id=agent_id, metadata=metadata or {})
        self._memories[mid] = entry
        self._store_count += 1
        self._db.execute(
            "INSERT OR REPLACE INTO amv2_memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, memory_type.value, content, importance.value,
             json.dumps(tags or []), source, session_id, agent_id,
             entry.created_at, entry.accessed_at, 0, 0.0, 0,
             json.dumps(metadata or {})))
        self._db.commit()
        return entry

    # ── WORKING MEMORY ────────────────────────────────────────────────

    def set_working(self, key: str, value: Any, ttl_s: Optional[float] = None):
        self._evict_working()
        slot = WorkingMemorySlot(key=key, value=value, ttl_s=ttl_s)
        self._working[key] = slot
        self._db.execute(
            "INSERT OR REPLACE INTO amv2_working VALUES (?,?,?,?)",
            (key, json.dumps(value), slot.set_at, ttl_s))
        self._db.commit()

    def get_working(self, key: str, default: Any = None) -> Any:
        self._evict_working()
        slot = self._working.get(key)
        if slot and not slot.is_expired():
            return slot.value
        return default

    def delete_working(self, key: str):
        self._working.pop(key, None)
        self._db.execute("DELETE FROM amv2_working WHERE key=?", (key,))
        self._db.commit()

    def _evict_working(self):
        expired = [k for k, s in self._working.items() if s.is_expired()]
        for k in expired:
            del self._working[k]
        if len(self._working) > self.working_capacity:
            # Evict oldest
            oldest = sorted(self._working.items(), key=lambda kv: kv[1].set_at)
            for k, _ in oldest[:len(self._working) - self.working_capacity]:
                del self._working[k]

    def working_snapshot(self) -> Dict[str, Any]:
        self._evict_working()
        return {k: s.value for k, s in self._working.items()
                if not s.is_expired()}

    # ── RETRIEVE ──────────────────────────────────────────────────────

    def retrieve(self, query: str, top_k: int = 5,
                 memory_type: Optional[MemoryType] = None,
                 min_importance: Optional[MemoryImportance] = None,
                 tags: Optional[List[str]] = None,
                 strategy: str = "semantic") -> List[MemoryEntry]:
        """Retrieve memories by semantic similarity, recency, or salience."""
        self._retrieve_count += 1
        candidates = list(self._memories.values())

        if memory_type:
            candidates = [m for m in candidates if m.memory_type == memory_type]
        if min_importance:
            threshold = IMPORTANCE_SCORE[min_importance]
            candidates = [m for m in candidates
                          if m.importance_score >= threshold]
        if tags:
            candidates = [m for m in candidates
                          if any(t in m.tags for t in tags)]

        if strategy == "semantic":
            q_emb = self._embed_fn(query)
            scored = []
            for m in candidates:
                if m.embedding:
                    sim = _cosine(q_emb, m.embedding)
                else:
                    sim = 0.0
                scored.append((m, sim))
            scored.sort(key=lambda x: x[1], reverse=True)
            results = [m for m, _ in scored[:top_k]]

        elif strategy == "recency":
            results = sorted(candidates,
                             key=lambda m: m.accessed_at, reverse=True)[:top_k]

        elif strategy == "salience":
            results = sorted(candidates,
                             key=lambda m: m.salience, reverse=True)[:top_k]

        elif strategy == "importance":
            results = sorted(candidates,
                             key=lambda m: m.importance_score, reverse=True)[:top_k]
        else:
            results = candidates[:top_k]

        for m in results:
            m.touch()
        return results

    def get(self, memory_id: str) -> Optional[MemoryEntry]:
        m = self._memories.get(memory_id)
        if m:
            m.touch()
        return m

    def search_by_tag(self, tag: str) -> List[MemoryEntry]:
        return [m for m in self._memories.values() if tag in m.tags]

    # ── REINFORCE / FORGET ────────────────────────────────────────────

    def reinforce(self, memory_id: str, amount: float = 0.1):
        m = self._memories.get(memory_id)
        if m:
            m.reinforce(amount)

    def forget(self, memory_id: str) -> bool:
        if memory_id in self._memories:
            del self._memories[memory_id]
            self._db.execute("DELETE FROM amv2_memories WHERE memory_id=?",
                             (memory_id,))
            self._db.commit()
            return True
        return False

    def decay_pass(self, min_salience: float = 0.05) -> int:
        """Remove memories whose salience has decayed below threshold."""
        to_forget = [mid for mid, m in self._memories.items()
                     if m.salience < min_salience
                     and m.importance != MemoryImportance.CRITICAL]
        for mid in to_forget:
            self.forget(mid)
        return len(to_forget)

    # ── CONSOLIDATION ─────────────────────────────────────────────────

    def consolidate(self, summarise_fn: Optional[Callable[[List[str]], str]] = None,
                    min_count: int = 3) -> int:
        """
        Group similar episodic memories and consolidate into semantic facts.
        Returns number of consolidations performed.
        """
        episodic = [m for m in self._memories.values()
                    if m.memory_type == MemoryType.EPISODIC
                    and not m.consolidated]
        if len(episodic) < min_count:
            return 0

        # Simple clustering: group by tag overlap
        groups: Dict[str, List[MemoryEntry]] = {}
        for m in episodic:
            key = ",".join(sorted(m.tags)) if m.tags else "__untagged__"
            groups.setdefault(key, []).append(m)

        consolidated = 0
        for key, group in groups.items():
            if len(group) < min_count:
                continue
            texts = [m.content for m in group]
            if summarise_fn:
                summary = summarise_fn(texts)
            else:
                summary = f"Consolidated {len(texts)} memories: " + "; ".join(
                    t[:40] for t in texts[:3])
            self.store(summary, memory_type=MemoryType.SEMANTIC,
                       importance=MemoryImportance.HIGH,
                       tags=group[0].tags,
                       source="consolidation")
            for m in group:
                m.consolidated = True
            for fn in self._consolidation_hooks:
                try: fn(group, summary)
                except Exception: pass
            consolidated += 1

        return consolidated

    def on_consolidation(self, fn: Callable):
        self._consolidation_hooks.append(fn)

    # ── STATS ─────────────────────────────────────────────────────────

    def count(self, memory_type: Optional[MemoryType] = None) -> int:
        if memory_type:
            return sum(1 for m in self._memories.values()
                       if m.memory_type == memory_type)
        return len(self._memories)

    def stats(self) -> Dict[str, Any]:
        by_type: Dict[str, int] = {}
        for m in self._memories.values():
            by_type[m.memory_type.value] = by_type.get(m.memory_type.value, 0) + 1
        return {
            "total_memories": len(self._memories),
            "working_slots": len(self._working),
            "by_type": by_type,
            "stored": self._store_count,
            "retrieved": self._retrieve_count,
        }
