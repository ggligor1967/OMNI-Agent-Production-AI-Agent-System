"""OMNI Agent — Knowledge Base V2: structured KB with CRUD, search, relations, versions."""
from __future__ import annotations
import hashlib, json, math, re, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class EntryType(str, Enum):
    FACT      = "fact"
    RULE      = "rule"
    CONCEPT   = "concept"
    PROCEDURE = "procedure"
    EXAMPLE   = "example"
    QUESTION  = "question"
    ANSWER    = "answer"
    REFERENCE = "reference"


class RelationType(str, Enum):
    IS_A         = "is_a"
    HAS_PART     = "has_part"
    RELATED_TO   = "related_to"
    DEPENDS_ON   = "depends_on"
    CONTRADICTS  = "contradicts"
    SUPPORTS     = "supports"
    DERIVED_FROM = "derived_from"
    EXAMPLE_OF   = "example_of"


@dataclass
class KBEntry:
    entry_id: str
    title: str
    content: str
    entry_type: EntryType = EntryType.FACT
    tags: List[str] = field(default_factory=list)
    source: str = ""
    confidence: float = 1.0       # 0.0–1.0
    embedding: Optional[List[float]] = None
    version: int = 1
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    created_by: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    _content_hash: str = field(default="", init=False, repr=False)

    def __post_init__(self):
        self._content_hash = hashlib.md5(self.content.encode()).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "title": self.title,
            "content": self.content[:300],
            "type": self.entry_type.value,
            "tags": self.tags,
            "confidence": self.confidence,
            "version": self.version,
        }


@dataclass
class KBRelation:
    relation_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    from_id: str = ""
    to_id: str = ""
    relation_type: RelationType = RelationType.RELATED_TO
    weight: float = 1.0
    description: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "relation_id": self.relation_id,
            "from": self.from_id, "to": self.to_id,
            "type": self.relation_type.value,
            "weight": self.weight,
        }


@dataclass
class SearchResult:
    entry: KBEntry
    score: float
    match_type: str   # "keyword" | "semantic" | "hybrid"
    snippet: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {**self.entry.to_dict(),
                "score": round(self.score, 4),
                "match_type": self.match_type,
                "snippet": self.snippet}


def _tokenize(text: str) -> List[str]:
    return re.findall(r"\b\w+\b", text.lower())


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b): return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na  = math.sqrt(sum(x * x for x in a))
    nb  = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class KnowledgeBaseV2:
    """
    Structured knowledge base:
    - CRUD for entries with typed content (facts/rules/concepts/examples…)
    - Keyword search (TF-IDF-style scoring)
    - Semantic search (cosine similarity on embeddings)
    - Hybrid ranking (keyword + semantic fusion)
    - Typed relations between entries (IS_A, HAS_PART, etc.)
    - Relation traversal (BFS/DFS)
    - Confidence scoring per entry
    - Entry versioning (full history)
    - Tag-based filtering and browsing
    - Duplicate detection (content hash)
    - Conflict detection (CONTRADICTS relations)
    - Pluggable embedding function
    - SQLite persistence
    """

    def __init__(self, embed_fn: Optional[Callable[[str], List[float]]] = None,
                 db_path: str = ":memory:"):
        self._entries:   Dict[str, KBEntry] = {}
        self._relations: Dict[str, KBRelation] = {}
        self._history:   Dict[str, List[Dict]] = {}   # entry_id → versions
        self._index:     Dict[str, List[str]] = {}    # term → [entry_ids]
        self.embed_fn    = embed_fn
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS kb_entries (
                entry_id TEXT PRIMARY KEY, title TEXT, content TEXT,
                entry_type TEXT, tags TEXT, source TEXT,
                confidence REAL, version INTEGER,
                created_at REAL, updated_at REAL
            );
            CREATE TABLE IF NOT EXISTS kb_relations (
                relation_id TEXT PRIMARY KEY,
                from_id TEXT, to_id TEXT, relation_type TEXT,
                weight REAL, description TEXT, created_at REAL
            );
            CREATE TABLE IF NOT EXISTS kb_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id TEXT, version INTEGER, content TEXT,
                updated_at REAL
            );
        """)
        self._db.commit()

    # ── CRUD ─────────────────────────────────────────────────────────

    def add(self, title: str, content: str,
            entry_type: EntryType = EntryType.FACT,
            tags: Optional[List[str]] = None,
            source: str = "",
            confidence: float = 1.0,
            created_by: str = "",
            embedding: Optional[List[float]] = None,
            entry_id: Optional[str] = None,
            metadata: Optional[Dict] = None) -> KBEntry:
        eid = entry_id or str(uuid.uuid4())[:10]
        if embedding is None and self.embed_fn:
            try: embedding = self.embed_fn(f"{title} {content}")
            except Exception: pass

        e = KBEntry(
            entry_id=eid, title=title, content=content,
            entry_type=entry_type, tags=list(tags or []),
            source=source, confidence=confidence,
            embedding=embedding, created_by=created_by,
            metadata=metadata or {})
        self._entries[eid] = e
        self._index_entry(e)
        self._persist_entry(e)
        return e

    def get(self, entry_id: str) -> Optional[KBEntry]:
        return self._entries.get(entry_id)

    def update(self, entry_id: str,
               content: Optional[str] = None,
               title: Optional[str] = None,
               confidence: Optional[float] = None,
               tags: Optional[List[str]] = None,
               embedding: Optional[List[float]] = None) -> Optional[KBEntry]:
        e = self._entries.get(entry_id)
        if not e: return None
        # Save history
        self._history.setdefault(entry_id, []).append({
            "version": e.version, "content": e.content,
            "updated_at": e.updated_at})
        self._db.execute(
            "INSERT INTO kb_history (entry_id,version,content,updated_at) "
            "VALUES (?,?,?,?)",
            (entry_id, e.version, e.content[:2000], e.updated_at))
        self._db.commit()

        if content is not None:
            self._remove_from_index(e)
            e.content = content
            e._content_hash = hashlib.md5(content.encode()).hexdigest()
            if self.embed_fn:
                try: e.embedding = self.embed_fn(f"{e.title} {content}")
                except Exception: pass
        if title      is not None: e.title      = title
        if confidence is not None: e.confidence = confidence
        if tags       is not None: e.tags        = list(tags)
        if embedding  is not None: e.embedding   = embedding
        e.version    += 1
        e.updated_at  = time.time()
        self._index_entry(e)
        self._persist_entry(e)
        return e

    def delete(self, entry_id: str) -> bool:
        e = self._entries.pop(entry_id, None)
        if not e: return False
        self._remove_from_index(e)
        self._db.execute("DELETE FROM kb_entries WHERE entry_id=?", (entry_id,))
        self._db.commit()
        return True

    def list_entries(self, entry_type: Optional[EntryType] = None,
                      tag: Optional[str] = None,
                      min_confidence: float = 0.0) -> List[Dict]:
        entries = list(self._entries.values())
        if entry_type:
            entries = [e for e in entries if e.entry_type == entry_type]
        if tag:
            entries = [e for e in entries if tag in e.tags]
        if min_confidence > 0:
            entries = [e for e in entries if e.confidence >= min_confidence]
        return [e.to_dict() for e in entries]

    # ── INDEXING ─────────────────────────────────────────────────────

    def _index_entry(self, e: KBEntry):
        for term in set(_tokenize(f"{e.title} {e.content}")):
            self._index.setdefault(term, [])
            if e.entry_id not in self._index[term]:
                self._index[term].append(e.entry_id)

    def _remove_from_index(self, e: KBEntry):
        for term in set(_tokenize(f"{e.title} {e.content}")):
            lst = self._index.get(term, [])
            if e.entry_id in lst: lst.remove(e.entry_id)

    # ── SEARCH ───────────────────────────────────────────────────────

    def search(self, query: str,
               top_k: int = 10,
               entry_type: Optional[EntryType] = None,
               tag: Optional[str] = None,
               min_confidence: float = 0.0,
               mode: str = "hybrid",
               query_embedding: Optional[List[float]] = None) -> List[SearchResult]:
        q_tokens = _tokenize(query)
        q_emb    = query_embedding

        if q_emb is None and self.embed_fn and mode in ("semantic", "hybrid"):
            try: q_emb = self.embed_fn(query)
            except Exception: pass

        # Filter candidates
        candidates = [e for e in self._entries.values()
                      if e.confidence >= min_confidence
                      and (not entry_type or e.entry_type == entry_type)
                      and (not tag or tag in e.tags)]

        keyword_scores: Dict[str, float] = {}
        semantic_scores: Dict[str, float] = {}

        if mode in ("keyword", "hybrid"):
            N     = len(self._entries) or 1
            for e in candidates:
                sc = 0.0
                for term in q_tokens:
                    tf = (_tokenize(f"{e.title} {e.content}").count(term))
                    df = len(self._index.get(term, []))
                    if tf > 0 and df > 0:
                        sc += math.log(1 + tf) * math.log(N / df)
                if sc > 0:
                    keyword_scores[e.entry_id] = sc

        if mode in ("semantic", "hybrid") and q_emb:
            for e in candidates:
                if e.embedding:
                    sc = _cosine(q_emb, e.embedding)
                    if sc > 0:
                        semantic_scores[e.entry_id] = sc

        # Combine
        all_ids = set(keyword_scores) | set(semantic_scores)
        final: Dict[str, float] = {}
        for eid in all_ids:
            kw  = keyword_scores.get(eid, 0.0)
            sem = semantic_scores.get(eid, 0.0)
            if mode == "keyword":   final[eid] = kw
            elif mode == "semantic": final[eid] = sem
            else:                    final[eid] = 0.5 * kw + 0.5 * sem

        sorted_ids = sorted(final, key=lambda x: -final[x])[:top_k]
        results = []
        for eid in sorted_ids:
            e = self._entries.get(eid)
            if not e: continue
            snippet = self._snippet(e.content, q_tokens)
            mt = ("semantic" if eid in semantic_scores and eid not in keyword_scores
                  else "keyword" if eid in keyword_scores and eid not in semantic_scores
                  else "hybrid")
            results.append(SearchResult(entry=e, score=final[eid],
                                        match_type=mt, snippet=snippet))
        return results

    def _snippet(self, content: str, q_tokens: List[str],
                  length: int = 150) -> str:
        lower = content.lower()
        for term in q_tokens:
            pos = lower.find(term)
            if pos >= 0:
                s = max(0, pos - length // 2)
                return content[s:s + length].strip()
        return content[:length].strip()

    # ── RELATIONS ────────────────────────────────────────────────────

    def add_relation(self, from_id: str, to_id: str,
                      relation_type: RelationType = RelationType.RELATED_TO,
                      weight: float = 1.0,
                      description: str = "") -> KBRelation:
        if from_id not in self._entries or to_id not in self._entries:
            raise KeyError("Both entries must exist")
        rid = str(uuid.uuid4())[:8]
        rel = KBRelation(relation_id=rid, from_id=from_id, to_id=to_id,
                          relation_type=relation_type, weight=weight,
                          description=description)
        self._relations[rid] = rel
        self._db.execute(
            "INSERT INTO kb_relations VALUES (?,?,?,?,?,?,?)",
            (rid, from_id, to_id, relation_type.value,
             weight, description, rel.created_at))
        self._db.commit()
        return rel

    def remove_relation(self, relation_id: str) -> bool:
        rel = self._relations.pop(relation_id, None)
        if rel:
            self._db.execute(
                "DELETE FROM kb_relations WHERE relation_id=?",
                (relation_id,))
            self._db.commit()
        return rel is not None

    def get_relations(self, entry_id: str,
                       relation_type: Optional[RelationType] = None) -> List[Dict]:
        rels = [r for r in self._relations.values()
                if r.from_id == entry_id or r.to_id == entry_id]
        if relation_type:
            rels = [r for r in rels if r.relation_type == relation_type]
        return [r.to_dict() for r in rels]

    def traverse(self, start_id: str,
                  relation_type: Optional[RelationType] = None,
                  max_depth: int = 3,
                  mode: str = "bfs") -> List[str]:
        visited: List[str] = []
        queue   = [start_id]
        depths  = {start_id: 0}
        while queue:
            curr = queue.pop(0) if mode == "bfs" else queue.pop()
            if curr in visited: continue
            visited.append(curr)
            if depths[curr] >= max_depth: continue
            for rel in self._relations.values():
                nxt = None
                if rel.from_id == curr:
                    nxt = rel.to_id
                elif rel.to_id == curr:
                    nxt = rel.from_id
                if nxt and nxt not in visited:
                    if relation_type and rel.relation_type != relation_type:
                        continue
                    depths[nxt] = depths[curr] + 1
                    queue.append(nxt)
        return visited

    # ── UTILITIES ────────────────────────────────────────────────────

    def find_duplicates(self) -> List[Tuple[str, str]]:
        """Return pairs of entries with identical content hash."""
        seen: Dict[str, str] = {}
        dupes: List[Tuple[str, str]] = []
        for eid, e in self._entries.items():
            h = e._content_hash
            if h in seen:
                dupes.append((seen[h], eid))
            else:
                seen[h] = eid
        return dupes

    def find_conflicts(self) -> List[Dict]:
        """Return all CONTRADICTS relations."""
        return [r.to_dict() for r in self._relations.values()
                if r.relation_type == RelationType.CONTRADICTS]

    def get_history(self, entry_id: str) -> List[Dict]:
        return self._history.get(entry_id, [])

    def _persist_entry(self, e: KBEntry):
        self._db.execute(
            "INSERT OR REPLACE INTO kb_entries VALUES (?,?,?,?,?,?,?,?,?,?)",
            (e.entry_id, e.title, e.content[:2000],
             e.entry_type.value, json.dumps(e.tags), e.source,
             e.confidence, e.version, e.created_at, e.updated_at))
        self._db.commit()

    def stats(self) -> Dict[str, Any]:
        by_type: Dict[str, int] = {}
        for e in self._entries.values():
            k = e.entry_type.value
            by_type[k] = by_type.get(k, 0) + 1
        return {
            "entries": len(self._entries),
            "relations": len(self._relations),
            "index_terms": len(self._index),
            "by_type": by_type,
        }
