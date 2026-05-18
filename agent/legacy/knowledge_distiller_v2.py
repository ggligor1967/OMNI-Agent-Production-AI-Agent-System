"""OMNI Agent — Knowledge Distiller V2: extract, score, deduplicate, and index knowledge facts."""
from __future__ import annotations
import hashlib, re, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set


class FactType(str, Enum):
    FACT        = "fact"
    RULE        = "rule"
    DEFINITION  = "definition"
    EXAMPLE     = "example"
    RELATIONSHIP = "relationship"
    CONSTRAINT  = "constraint"
    UNKNOWN     = "unknown"


@dataclass
class KnowledgeFact:
    fact_id: str
    content: str
    fact_type: FactType = FactType.FACT
    source: str = ""
    confidence: float = 1.0      # 0.0 – 1.0
    importance: float = 0.5      # 0.0 – 1.0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    tags: List[str] = field(default_factory=list)
    related: List[str] = field(default_factory=list)  # fact_ids
    access_count: int = 0
    verified: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return hashlib.md5(self.content.strip().lower().encode()).hexdigest()

    @property
    def score(self) -> float:
        """Composite relevance score."""
        return self.confidence * 0.4 + self.importance * 0.4 + min(self.access_count / 10, 1.0) * 0.2

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "content": self.content,
            "fact_type": self.fact_type.value,
            "source": self.source,
            "confidence": self.confidence,
            "importance": self.importance,
            "score": round(self.score, 4),
            "tags": self.tags,
            "related": self.related,
            "verified": self.verified,
            "access_count": self.access_count,
        }


class KnowledgeDistillerV2:
    """
    Ingests raw text, extracts typed facts, deduplicates by content hash,
    scores by confidence/importance/usage, and supports iterative refinement.
    """

    def __init__(self, db_path: str = ":memory:",
                 similarity_threshold: float = 0.85):
        self._facts: Dict[str, KnowledgeFact] = {}         # fact_id → fact
        self._hash_index: Dict[str, str] = {}               # content_hash → fact_id
        self._tag_index: Dict[str, Set[str]] = {}           # tag → set(fact_id)
        self._extractors: List[Callable[[str], List[Dict]]] = []
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._ingest_count = 0
        self._dedup_count = 0

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS kd_facts (
                fact_id TEXT PRIMARY KEY, content TEXT, fact_type TEXT,
                source TEXT, confidence REAL, importance REAL,
                created_at REAL, tags TEXT, verified INTEGER
            );
            CREATE TABLE IF NOT EXISTS kd_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT, ingested_at REAL, fact_count INTEGER
            );
        """)
        self._db.commit()

    # ── EXTRACTORS ────────────────────────────────────────────────────

    def add_extractor(self, fn: Callable[[str], List[Dict]]):
        """Register a custom extractor fn(text) → list of dicts with 'content' key."""
        self._extractors.append(fn)

    def _builtin_extract(self, text: str) -> List[Dict]:
        """Simple sentence-level extraction with heuristic typing."""
        facts = []
        sentences = re.split(r'(?<=[.!?])\s+', text.strip())
        for sent in sentences:
            sent = sent.strip()
            if len(sent) < 10:
                continue
            # Heuristic type classification
            lower = sent.lower()
            if lower.startswith(("if ", "when ", "always ", "never ", "must ")):
                ftype = FactType.RULE
            elif " is defined as " in lower or lower.startswith("a ") and " is " in lower:
                ftype = FactType.DEFINITION
            elif lower.startswith(("for example", "e.g.", "such as")):
                ftype = FactType.EXAMPLE
            elif " relates to " in lower or " depends on " in lower or " causes " in lower:
                ftype = FactType.RELATIONSHIP
            else:
                ftype = FactType.FACT
            facts.append({"content": sent, "fact_type": ftype})
        return facts

    # ── INGEST ────────────────────────────────────────────────────────

    def ingest(self, text: str, source: str = "",
               default_confidence: float = 0.8,
               default_importance: float = 0.5,
               tags: Optional[List[str]] = None) -> List[KnowledgeFact]:
        """Extract and store facts from raw text. Returns new (non-duplicate) facts."""
        import json
        raw_facts = self._builtin_extract(text)
        for extractor in self._extractors:
            try:
                raw_facts.extend(extractor(text))
            except Exception:
                pass

        new_facts = []
        for raw in raw_facts:
            content = raw.get("content", "").strip()
            if not content:
                continue
            ftype = raw.get("fact_type", FactType.UNKNOWN)
            if isinstance(ftype, str):
                ftype = FactType(ftype) if ftype in FactType._value2member_map_ else FactType.UNKNOWN

            fact = KnowledgeFact(
                fact_id=str(uuid.uuid4()),
                content=content,
                fact_type=ftype,
                source=source,
                confidence=raw.get("confidence", default_confidence),
                importance=raw.get("importance", default_importance),
                tags=list(tags or []),
            )
            stored = self._store(fact)
            if stored:
                new_facts.append(fact)
                self._ingest_count += 1
            else:
                self._dedup_count += 1

        self._db.execute(
            "INSERT INTO kd_sources (source,ingested_at,fact_count) VALUES (?,?,?)",
            (source, time.time(), len(new_facts)))
        self._db.commit()
        return new_facts

    def add_fact(self, content: str, fact_type: FactType = FactType.FACT,
                 source: str = "", confidence: float = 1.0,
                 importance: float = 0.5, tags: Optional[List[str]] = None,
                 verified: bool = False) -> Optional[KnowledgeFact]:
        """Manually add a single fact. Returns None if duplicate."""
        import json
        fact = KnowledgeFact(
            fact_id=str(uuid.uuid4()),
            content=content,
            fact_type=fact_type,
            source=source,
            confidence=confidence,
            importance=importance,
            tags=list(tags or []),
            verified=verified,
        )
        return fact if self._store(fact) else None

    def _store(self, fact: KnowledgeFact) -> bool:
        """Store fact if not duplicate. Returns True if stored."""
        import json
        h = fact.content_hash
        if h in self._hash_index:
            # Update confidence/importance if higher
            existing_id = self._hash_index[h]
            existing = self._facts[existing_id]
            if fact.confidence > existing.confidence:
                existing.confidence = fact.confidence
                existing.updated_at = time.time()
            return False
        self._facts[fact.fact_id] = fact
        self._hash_index[h] = fact.fact_id
        for tag in fact.tags:
            self._tag_index.setdefault(tag, set()).add(fact.fact_id)
        self._db.execute(
            "INSERT OR IGNORE INTO kd_facts VALUES (?,?,?,?,?,?,?,?,?)",
            (fact.fact_id, fact.content, fact.fact_type.value,
             fact.source, fact.confidence, fact.importance,
             fact.created_at, json.dumps(fact.tags), int(fact.verified)))
        self._db.commit()
        return True

    # ── QUERY ─────────────────────────────────────────────────────────

    def get(self, fact_id: str) -> Optional[KnowledgeFact]:
        fact = self._facts.get(fact_id)
        if fact:
            fact.access_count += 1
        return fact

    def search(self, query: str, top_k: int = 10,
               fact_type: Optional[FactType] = None,
               min_confidence: float = 0.0,
               tag: Optional[str] = None) -> List[KnowledgeFact]:
        """Keyword search over fact content."""
        query_lower = query.lower()
        candidates = list(self._facts.values())
        if fact_type:
            candidates = [f for f in candidates if f.fact_type == fact_type]
        if min_confidence > 0:
            candidates = [f for f in candidates if f.confidence >= min_confidence]
        if tag:
            tagged_ids = self._tag_index.get(tag, set())
            candidates = [f for f in candidates if f.fact_id in tagged_ids]
        scored = []
        for f in candidates:
            if query_lower in f.content.lower():
                scored.append((f, f.score))
        scored.sort(key=lambda x: x[1], reverse=True)
        results = [f for f, _ in scored[:top_k]]
        for f in results:
            f.access_count += 1
        return results

    def top_facts(self, n: int = 10,
                  fact_type: Optional[FactType] = None) -> List[KnowledgeFact]:
        """Return top-N facts by composite score."""
        facts = list(self._facts.values())
        if fact_type:
            facts = [f for f in facts if f.fact_type == fact_type]
        return sorted(facts, key=lambda f: f.score, reverse=True)[:n]

    def get_by_tag(self, tag: str) -> List[KnowledgeFact]:
        ids = self._tag_index.get(tag, set())
        return [self._facts[fid] for fid in ids if fid in self._facts]

    # ── REFINEMENT ────────────────────────────────────────────────────

    def update_fact(self, fact_id: str,
                    confidence: Optional[float] = None,
                    importance: Optional[float] = None,
                    verified: Optional[bool] = None,
                    tags: Optional[List[str]] = None) -> bool:
        fact = self._facts.get(fact_id)
        if fact is None:
            return False
        if confidence is not None:
            fact.confidence = max(0.0, min(1.0, confidence))
        if importance is not None:
            fact.importance = max(0.0, min(1.0, importance))
        if verified is not None:
            fact.verified = verified
        if tags is not None:
            # Update tag index
            for t in fact.tags:
                self._tag_index.get(t, set()).discard(fact_id)
            fact.tags = tags
            for t in tags:
                self._tag_index.setdefault(t, set()).add(fact_id)
        fact.updated_at = time.time()
        return True

    def delete_fact(self, fact_id: str) -> bool:
        fact = self._facts.pop(fact_id, None)
        if fact is None:
            return False
        self._hash_index.pop(fact.content_hash, None)
        for tag in fact.tags:
            self._tag_index.get(tag, set()).discard(fact_id)
        self._db.execute("DELETE FROM kd_facts WHERE fact_id=?", (fact_id,))
        self._db.commit()
        return True

    def link(self, fact_id_a: str, fact_id_b: str) -> bool:
        fa = self._facts.get(fact_id_a)
        fb = self._facts.get(fact_id_b)
        if not fa or not fb:
            return False
        if fact_id_b not in fa.related:
            fa.related.append(fact_id_b)
        if fact_id_a not in fb.related:
            fb.related.append(fact_id_a)
        return True

    def prune_low_confidence(self, threshold: float = 0.3) -> int:
        to_delete = [fid for fid, f in self._facts.items()
                     if f.confidence < threshold]
        for fid in to_delete:
            self.delete_fact(fid)
        return len(to_delete)

    # ── STATS ─────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        by_type: Dict[str, int] = {}
        for f in self._facts.values():
            by_type[f.fact_type.value] = by_type.get(f.fact_type.value, 0) + 1
        return {
            "total_facts": len(self._facts),
            "ingested": self._ingest_count,
            "deduplicated": self._dedup_count,
            "verified": sum(1 for f in self._facts.values() if f.verified),
            "by_type": by_type,
            "tags": len(self._tag_index),
        }
