"""OMNI Agent — Memory Compressor: hierarchical conversation memory with tiered summarization."""
from __future__ import annotations
import hashlib, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class MemoryTier(str, Enum):
    HOT    = "hot"       # recent raw turns (verbatim)
    WARM   = "warm"      # rolling summaries (compressed)
    COLD   = "cold"      # long-term condensed facts
    FROZEN = "frozen"    # permanent anchors / user profile


@dataclass
class MemoryEntry:
    entry_id: str
    tier: MemoryTier
    content: str
    role: str = "assistant"         # user | assistant | system | summary
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    token_count: int = 0
    importance: float = 0.5
    access_count: int = 0
    tags: List[str] = field(default_factory=list)
    source_ids: List[str] = field(default_factory=list)   # entries this was derived from
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def age_s(self) -> float:
        return time.time() - self.created_at

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "tier": self.tier.value,
            "role": self.role,
            "content": self.content[:120] + "…" if len(self.content) > 120 else self.content,
            "token_count": self.token_count,
            "importance": self.importance,
            "access_count": self.access_count,
            "age_s": round(self.age_s, 1),
        }


class MemoryCompressor:
    """
    Three-tier memory manager for LLM conversations.

    HOT  → raw recent turns (bounded by hot_limit)
    WARM → rolling summary windows (compressed from HOT)
    COLD → distilled long-term facts (compressed from WARM)
    FROZEN → immutable anchors

    When HOT fills up, oldest entries are summarised into WARM.
    When WARM fills up, oldest summaries are distilled into COLD.
    """

    def __init__(
        self,
        hot_limit: int  = 20,
        warm_limit: int = 10,
        cold_limit: int = 20,
        summarize_fn: Optional[Callable[[List[MemoryEntry]], str]] = None,
        db_path: str = ":memory:",
    ):
        self.hot_limit  = hot_limit
        self.warm_limit = warm_limit
        self.cold_limit = cold_limit
        self._summarize = summarize_fn or self._default_summarize
        self._entries: Dict[str, MemoryEntry] = {}
        self._by_tier: Dict[MemoryTier, List[str]] = {
            t: [] for t in MemoryTier}
        self._compress_count = 0
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS mc_entries (
                entry_id TEXT PRIMARY KEY, tier TEXT, role TEXT,
                content TEXT, created_at REAL, token_count INTEGER,
                importance REAL, tags TEXT, source_ids TEXT
            );
            CREATE TABLE IF NOT EXISTS mc_compress_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, from_tier TEXT, to_tier TEXT,
                n_entries INTEGER, summary_len INTEGER
            );
        """)
        self._db.commit()

    # ── WRITE ─────────────────────────────────────────────────────────

    def add(self, content: str, role: str = "user",
            tier: MemoryTier = MemoryTier.HOT,
            importance: float = 0.5,
            tags: Optional[List[str]] = None,
            token_count: Optional[int] = None,
            metadata: Optional[Dict] = None) -> MemoryEntry:
        import json
        tc = token_count if token_count is not None else len(content.split())
        entry = MemoryEntry(
            entry_id=str(uuid.uuid4()),
            tier=tier,
            content=content,
            role=role,
            importance=importance,
            token_count=tc,
            tags=list(tags or []),
            metadata=metadata or {},
        )
        self._entries[entry.entry_id] = entry
        self._by_tier[tier].append(entry.entry_id)
        self._db.execute(
            "INSERT INTO mc_entries VALUES (?,?,?,?,?,?,?,?,?)",
            (entry.entry_id, tier.value, role, content, entry.created_at,
             tc, importance, json.dumps(entry.tags), json.dumps([])))
        self._db.commit()
        self._maybe_compress()
        return entry

    def add_frozen(self, content: str, tags: Optional[List[str]] = None) -> MemoryEntry:
        return self.add(content, role="system", tier=MemoryTier.FROZEN,
                        importance=1.0, tags=tags)

    # ── COMPRESSION ───────────────────────────────────────────────────

    def _maybe_compress(self):
        if len(self._by_tier[MemoryTier.HOT]) > self.hot_limit:
            self._compress_tier(MemoryTier.HOT, MemoryTier.WARM)
        if len(self._by_tier[MemoryTier.WARM]) > self.warm_limit:
            self._compress_tier(MemoryTier.WARM, MemoryTier.COLD)
        if len(self._by_tier[MemoryTier.COLD]) > self.cold_limit:
            self._evict_cold()

    def _compress_tier(self, from_tier: MemoryTier, to_tier: MemoryTier):
        """Summarise oldest half of from_tier into one to_tier entry."""
        ids = self._by_tier[from_tier]
        n = max(2, len(ids) // 2)
        to_compress_ids = ids[:n]
        entries = [self._entries[eid] for eid in to_compress_ids if eid in self._entries]
        if not entries:
            return
        summary_text = self._summarize(entries)
        avg_importance = sum(e.importance for e in entries) / len(entries)
        summary_entry = MemoryEntry(
            entry_id=str(uuid.uuid4()),
            tier=to_tier,
            content=summary_text,
            role="summary",
            importance=min(1.0, avg_importance * 1.1),
            token_count=len(summary_text.split()),
            source_ids=to_compress_ids,
        )
        # Remove compressed entries
        for eid in to_compress_ids:
            self._entries.pop(eid, None)
        self._by_tier[from_tier] = ids[n:]
        # Store summary
        import json
        self._entries[summary_entry.entry_id] = summary_entry
        self._by_tier[to_tier].append(summary_entry.entry_id)
        self._db.execute(
            "INSERT INTO mc_entries VALUES (?,?,?,?,?,?,?,?,?)",
            (summary_entry.entry_id, to_tier.value, "summary",
             summary_text, summary_entry.created_at,
             summary_entry.token_count, summary_entry.importance,
             json.dumps([]), json.dumps(to_compress_ids)))
        self._db.execute(
            "INSERT INTO mc_compress_log (ts,from_tier,to_tier,n_entries,summary_len) VALUES (?,?,?,?,?)",
            (time.time(), from_tier.value, to_tier.value, n, len(summary_text)))
        self._db.commit()
        self._compress_count += 1

    def _evict_cold(self):
        """Remove oldest (lowest importance) cold entries beyond limit."""
        ids = self._by_tier[MemoryTier.COLD]
        if len(ids) <= self.cold_limit:
            return
        scored = [(eid, self._entries[eid].importance)
                  for eid in ids if eid in self._entries]
        scored.sort(key=lambda x: x[1])
        to_remove = [eid for eid, _ in scored[:len(ids) - self.cold_limit]]
        for eid in to_remove:
            self._entries.pop(eid, None)
        self._by_tier[MemoryTier.COLD] = [eid for eid in ids if eid not in set(to_remove)]

    @staticmethod
    def _default_summarize(entries: List[MemoryEntry]) -> str:
        lines = [f"[{e.role}] {e.content[:200]}" for e in entries]
        return "Summary of prior conversation: " + " | ".join(lines)

    def force_compress(self):
        """Manually trigger compression pass."""
        self._maybe_compress()

    # ── READ ──────────────────────────────────────────────────────────

    def get(self, entry_id: str) -> Optional[MemoryEntry]:
        e = self._entries.get(entry_id)
        if e:
            e.access_count += 1
        return e

    def get_tier(self, tier: MemoryTier) -> List[MemoryEntry]:
        return [self._entries[eid] for eid in self._by_tier[tier]
                if eid in self._entries]

    def get_context(self, max_tokens: int = 2000) -> List[MemoryEntry]:
        """
        Build context window: FROZEN + COLD + WARM + HOT (newest last),
        trimmed to max_tokens.
        """
        ordered: List[MemoryEntry] = []
        for tier in [MemoryTier.FROZEN, MemoryTier.COLD,
                     MemoryTier.WARM, MemoryTier.HOT]:
            ordered.extend(self.get_tier(tier))
        # Newest last
        ordered.sort(key=lambda e: e.created_at)
        result, tokens = [], 0
        for e in reversed(ordered):
            if tokens + e.token_count > max_tokens:
                break
            result.append(e)
            tokens += e.token_count
        return list(reversed(result))

    def search(self, query: str, tier: Optional[MemoryTier] = None) -> List[MemoryEntry]:
        q = query.lower()
        candidates = (self.get_tier(tier) if tier
                      else list(self._entries.values()))
        return [e for e in candidates if q in e.content.lower()]

    def delete(self, entry_id: str) -> bool:
        entry = self._entries.pop(entry_id, None)
        if not entry:
            return False
        tier_ids = self._by_tier[entry.tier]
        if entry_id in tier_ids:
            tier_ids.remove(entry_id)
        self._db.execute("DELETE FROM mc_entries WHERE entry_id=?", (entry_id,))
        self._db.commit()
        return True

    def clear_tier(self, tier: MemoryTier):
        for eid in list(self._by_tier[tier]):
            self._entries.pop(eid, None)
        self._by_tier[tier] = []

    def token_count_total(self) -> int:
        return sum(e.token_count for e in self._entries.values())

    def token_count_tier(self, tier: MemoryTier) -> int:
        return sum(self._entries[eid].token_count
                   for eid in self._by_tier[tier] if eid in self._entries)

    def compress_log(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self._db.execute(
            "SELECT ts,from_tier,to_tier,n_entries,summary_len FROM mc_compress_log "
            "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [{"ts": r[0], "from": r[1], "to": r[2],
                 "n": r[3], "summary_len": r[4]} for r in rows]

    def stats(self) -> Dict[str, Any]:
        return {
            "total_entries": len(self._entries),
            "compress_count": self._compress_count,
            "total_tokens": self.token_count_total(),
            "by_tier": {t.value: len(ids) for t, ids in self._by_tier.items()},
        }
