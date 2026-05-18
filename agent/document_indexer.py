"""OMNI Agent — Document Indexer: full-text search with BM25 ranking and facets."""
from __future__ import annotations
import json, math, re, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class IndexStatus(str, Enum):
    INDEXED   = "indexed"
    PENDING   = "pending"
    DELETED   = "deleted"


@dataclass
class IndexedDoc:
    doc_id: str
    title: str
    content: str
    facets: Dict[str, Any] = field(default_factory=dict)   # filterable fields
    tags: List[str] = field(default_factory=list)
    source: str = ""
    url: str = ""
    status: IndexStatus = IndexStatus.INDEXED
    indexed_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)
    _tokens: List[str] = field(default_factory=list, repr=False)
    _token_freq: Dict[str, int] = field(default_factory=dict, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "content": self.content[:200],
            "facets": self.facets,
            "tags": self.tags,
            "source": self.source,
            "indexed_at": self.indexed_at,
        }


@dataclass
class SearchResult:
    doc_id: str
    title: str
    score: float
    snippet: str = ""
    highlights: List[str] = field(default_factory=list)
    facets: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    source: str = ""
    rank: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "score": round(self.score, 4),
            "snippet": self.snippet,
            "highlights": self.highlights,
            "rank": self.rank,
        }


def _tokenize(text: str) -> List[str]:
    """Simple whitespace + punctuation tokenizer, lowercased."""
    text = text.lower()
    tokens = re.findall(r"\b[a-z0-9][a-z0-9'_-]*\b", text)
    return tokens


def _freq(tokens: List[str]) -> Dict[str, int]:
    d: Dict[str, int] = {}
    for t in tokens:
        d[t] = d.get(t, 0) + 1
    return d


class DocumentIndexer:
    """
    Full-text document indexer with:
    - BM25 ranking (k1=1.5, b=0.75)
    - Inverted index (term → [doc_ids])
    - Faceted filtering (exact match on facet fields)
    - Tag filtering
    - Highlight generation (surrounding context)
    - Snippet extraction
    - TF-IDF fallback
    - Incremental updates (re-index single doc)
    - Delete / soft-delete
    - SQLite persistence
    - Query analytics
    """

    BM25_K1 = 1.5
    BM25_B  = 0.75

    def __init__(self, db_path: str = ":memory:",
                 snippet_len: int = 150,
                 highlight_window: int = 5):
        self._docs:  Dict[str, IndexedDoc] = {}
        self._index: Dict[str, List[str]] = {}   # term → [doc_ids]
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self.snippet_len     = snippet_len
        self.highlight_window = highlight_window
        self._query_count    = 0
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS di_docs (
                doc_id TEXT PRIMARY KEY, title TEXT, content TEXT,
                facets TEXT, tags TEXT, source TEXT,
                status TEXT, indexed_at REAL
            );
            CREATE TABLE IF NOT EXISTS di_queries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT, result_count INTEGER, ts REAL
            );
        """)
        self._db.commit()

    # ── INDEXING ──────────────────────────────────────────────────────

    def index(self, title: str, content: str,
              facets: Optional[Dict[str, Any]] = None,
              tags: Optional[List[str]] = None,
              source: str = "", url: str = "",
              metadata: Optional[Dict] = None,
              doc_id: Optional[str] = None) -> IndexedDoc:
        did  = doc_id or str(uuid.uuid4())[:10]
        text = f"{title} {content}"
        toks = _tokenize(text)
        doc  = IndexedDoc(
            doc_id=did, title=title, content=content,
            facets=dict(facets or {}), tags=list(tags or []),
            source=source, url=url, metadata=metadata or {},
            _tokens=toks, _token_freq=_freq(toks))

        # Remove old entries from inverted index if re-indexing
        if did in self._docs:
            self._remove_from_index(did)

        self._docs[did] = doc
        for term in set(toks):
            self._index.setdefault(term, []).append(did)

        self._persist(doc)
        return doc

    def index_batch(self, records: List[Dict]) -> List[IndexedDoc]:
        return [self.index(**r) for r in records]

    def update(self, doc_id: str, **kwargs) -> Optional[IndexedDoc]:
        doc = self._docs.get(doc_id)
        if not doc: return None
        for k, v in kwargs.items():
            if hasattr(doc, k): setattr(doc, k, v)
        doc.updated_at = time.time()
        # Re-tokenize if content changed
        if "content" in kwargs or "title" in kwargs:
            self._remove_from_index(doc_id)
            text = f"{doc.title} {doc.content}"
            doc._tokens      = _tokenize(text)
            doc._token_freq  = _freq(doc._tokens)
            for term in set(doc._tokens):
                self._index.setdefault(term, []).append(doc_id)
        self._persist(doc)
        return doc

    def delete(self, doc_id: str, soft: bool = True) -> bool:
        doc = self._docs.get(doc_id)
        if not doc: return False
        if soft:
            doc.status = IndexStatus.DELETED
        else:
            self._remove_from_index(doc_id)
            self._docs.pop(doc_id)
            self._db.execute("DELETE FROM di_docs WHERE doc_id=?", (doc_id,))
            self._db.commit()
        return True

    def _remove_from_index(self, doc_id: str):
        doc = self._docs.get(doc_id)
        if not doc: return
        for term in set(doc._tokens):
            lst = self._index.get(term, [])
            if doc_id in lst:
                lst.remove(doc_id)

    def _persist(self, doc: IndexedDoc):
        self._db.execute(
            "INSERT OR REPLACE INTO di_docs VALUES (?,?,?,?,?,?,?,?)",
            (doc.doc_id, doc.title, doc.content[:2000],
             json.dumps(doc.facets), json.dumps(doc.tags),
             doc.source, doc.status.value, doc.indexed_at))
        self._db.commit()

    # ── SEARCH ────────────────────────────────────────────────────────

    def search(self, query: str,
               top_k: int = 10,
               facet_filter: Optional[Dict[str, Any]] = None,
               tag_filter: Optional[List[str]] = None,
               source_filter: Optional[str] = None,
               include_deleted: bool = False) -> List[SearchResult]:
        self._query_count += 1
        q_tokens = _tokenize(query)
        if not q_tokens:
            return []

        # Candidate docs from inverted index
        candidate_ids: Dict[str, int] = {}
        for term in q_tokens:
            for did in self._index.get(term, []):
                candidate_ids[did] = candidate_ids.get(did, 0) + 1

        if not candidate_ids:
            self._log_query(query, 0)
            return []

        # Filter
        active_docs = {did: self._docs[did]
                       for did in candidate_ids if did in self._docs}
        if not include_deleted:
            active_docs = {did: d for did, d in active_docs.items()
                           if d.status != IndexStatus.DELETED}
        if facet_filter:
            active_docs = {
                did: d for did, d in active_docs.items()
                if all(d.facets.get(k) == v
                       for k, v in facet_filter.items())}
        if tag_filter:
            active_docs = {
                did: d for did, d in active_docs.items()
                if all(t in d.tags for t in tag_filter)}
        if source_filter:
            active_docs = {did: d for did, d in active_docs.items()
                           if d.source == source_filter}

        if not active_docs:
            self._log_query(query, 0)
            return []

        # BM25 scoring
        N    = len(self._docs)
        avgdl = (sum(len(d._tokens) for d in self._docs.values()) / N
                 if N > 0 else 1.0)
        scores: List[Tuple[str, float]] = []
        for did, doc in active_docs.items():
            score = 0.0
            dl    = len(doc._tokens)
            for term in q_tokens:
                tf  = doc._token_freq.get(term, 0)
                df  = len(self._index.get(term, []))
                if df == 0: continue
                idf = math.log((N - df + 0.5) / (df + 0.5) + 1)
                tf_norm = (tf * (self.BM25_K1 + 1) /
                           (tf + self.BM25_K1 * (1 - self.BM25_B +
                            self.BM25_B * dl / avgdl)))
                score += idf * tf_norm
            scores.append((did, score))

        scores.sort(key=lambda x: -x[1])
        top = scores[:top_k]

        results = []
        for rank, (did, score) in enumerate(top):
            doc = active_docs[did]
            snippet = self._make_snippet(doc.content, q_tokens)
            highlights = self._make_highlights(doc.content, q_tokens)
            results.append(SearchResult(
                doc_id=did, title=doc.title, score=score,
                snippet=snippet, highlights=highlights,
                facets=doc.facets, tags=doc.tags,
                source=doc.source, rank=rank + 1))

        self._log_query(query, len(results))
        return results

    def _make_snippet(self, content: str, q_tokens: List[str]) -> str:
        lower = content.lower()
        for term in q_tokens:
            pos = lower.find(term)
            if pos >= 0:
                start = max(0, pos - self.snippet_len // 2)
                end   = min(len(content), start + self.snippet_len)
                return content[start:end].strip()
        return content[:self.snippet_len].strip()

    def _make_highlights(self, content: str,
                          q_tokens: List[str]) -> List[str]:
        tokens = _tokenize(content)
        hits   = []
        for i, tok in enumerate(tokens):
            if tok in q_tokens:
                start = max(0, i - self.highlight_window)
                end   = min(len(tokens), i + self.highlight_window + 1)
                window = " ".join(tokens[start:end])
                if window not in hits:
                    hits.append(window)
                if len(hits) >= 3:
                    break
        return hits

    def _log_query(self, query: str, count: int):
        self._db.execute(
            "INSERT INTO di_queries (query,result_count,ts) VALUES (?,?,?)",
            (query[:200], count, time.time()))
        self._db.commit()

    # ── QUERY ─────────────────────────────────────────────────────────

    def get_doc(self, doc_id: str) -> Optional[IndexedDoc]:
        return self._docs.get(doc_id)

    def list_docs(self, source: Optional[str] = None,
                  tag: Optional[str] = None,
                  limit: int = 50) -> List[Dict]:
        docs = [d for d in self._docs.values()
                if d.status != IndexStatus.DELETED]
        if source: docs = [d for d in docs if d.source == source]
        if tag:    docs = [d for d in docs if tag in d.tags]
        return [d.to_dict() for d in docs[:limit]]

    def query_log(self, limit: int = 20) -> List[Dict]:
        rows = self._db.execute(
            "SELECT query,result_count,ts FROM di_queries "
            "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [{"query": r[0], "results": r[1]} for r in rows]

    def stats(self) -> Dict[str, Any]:
        active = sum(1 for d in self._docs.values()
                     if d.status == IndexStatus.INDEXED)
        return {
            "total_docs": len(self._docs),
            "active_docs": active,
            "index_terms": len(self._index),
            "queries": self._query_count,
        }
