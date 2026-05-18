"""OMNI Agent — Search Engine V2: hybrid search (keyword + vector) with re-ranking."""
from __future__ import annotations
import json, math, re, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class SearchMode(str, Enum):
    KEYWORD = "keyword"
    VECTOR  = "vector"
    HYBRID  = "hybrid"


class SortOrder(str, Enum):
    RELEVANCE = "relevance"
    DATE_ASC  = "date_asc"
    DATE_DESC = "date_desc"
    FIELD     = "field"


@dataclass
class SearchDoc:
    doc_id: str
    title: str
    content: str
    embedding: Optional[List[float]] = None
    fields: Dict[str, Any] = field(default_factory=dict)  # filterable/sortable
    tags: List[str] = field(default_factory=list)
    source: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    boost: float = 1.0           # manual boost factor
    _tokens: List[str] = field(default_factory=list, repr=False)
    _tf: Dict[str, int] = field(default_factory=dict, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "doc_id": self.doc_id, "title": self.title,
            "content": self.content[:200],
            "fields": self.fields, "tags": self.tags,
            "source": self.source, "boost": self.boost,
        }


@dataclass
class SearchHit:
    doc_id: str
    title: str
    score: float
    bm25_score: float = 0.0
    vector_score: float = 0.0
    snippet: str = ""
    highlights: List[str] = field(default_factory=list)
    fields: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    rank: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "doc_id": self.doc_id, "title": self.title,
            "score": round(self.score, 4),
            "bm25_score": round(self.bm25_score, 4),
            "vector_score": round(self.vector_score, 4),
            "snippet": self.snippet, "rank": self.rank,
        }


@dataclass
class SearchRequest:
    query: str
    mode: SearchMode = SearchMode.HYBRID
    top_k: int = 10
    filters: Dict[str, Any] = field(default_factory=dict)   # field → value
    tag_filter: List[str] = field(default_factory=list)
    source_filter: Optional[str] = None
    date_from: Optional[float] = None
    date_to: Optional[float] = None
    sort: SortOrder = SortOrder.RELEVANCE
    sort_field: str = ""
    vector_weight: float = 0.5   # 0=full keyword, 1=full vector
    rerank: bool = True
    query_embedding: Optional[List[float]] = None


def _tokenize(text: str) -> List[str]:
    return re.findall(r"\b[a-z0-9][a-z0-9'_-]*\b", text.lower())


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b): return 0.0
    dot  = sum(x * y for x, y in zip(a, b))
    na   = math.sqrt(sum(x * x for x in a))
    nb   = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class SearchEngineV2:
    """
    Hybrid search engine:
    - BM25 keyword search (inverted index)
    - Vector similarity search (cosine)
    - Hybrid fusion (weighted combination)
    - Reciprocal Rank Fusion (RRF) re-ranking
    - Field filters (exact, range, in-list)
    - Tag and source filters
    - Date range filtering
    - Manual boost per document
    - Sort by relevance / date / custom field
    - Snippet and highlight extraction
    - Pluggable embedding function
    - Query analytics
    - SQLite persistence
    """

    BM25_K1 = 1.5
    BM25_B  = 0.75
    RRF_K   = 60   # RRF constant

    def __init__(self, embed_fn: Optional[Callable[[str], List[float]]] = None,
                 db_path: str = ":memory:",
                 snippet_len: int = 160):
        self._docs:   Dict[str, SearchDoc] = {}
        self._index:  Dict[str, List[str]] = {}   # term → [doc_ids]
        self.embed_fn    = embed_fn
        self.snippet_len = snippet_len
        self._query_count = 0
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS se_docs (
                doc_id TEXT PRIMARY KEY, title TEXT, content TEXT,
                fields TEXT, tags TEXT, source TEXT,
                created_at REAL, boost REAL
            );
            CREATE TABLE IF NOT EXISTS se_queries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT, mode TEXT, hits INTEGER, ts REAL
            );
        """)
        self._db.commit()

    # ── INDEX ────────────────────────────────────────────────────────

    def index(self, title: str, content: str,
              doc_id: Optional[str] = None,
              embedding: Optional[List[float]] = None,
              fields: Optional[Dict] = None,
              tags: Optional[List[str]] = None,
              source: str = "", boost: float = 1.0,
              created_at: Optional[float] = None) -> SearchDoc:
        did   = doc_id or str(uuid.uuid4())[:10]
        toks  = _tokenize(f"{title} {content}")
        tf    = {}
        for t in toks: tf[t] = tf.get(t, 0) + 1

        # Auto-embed if function available and no embedding given
        if embedding is None and self.embed_fn:
            try: embedding = self.embed_fn(f"{title} {content}")
            except Exception: pass

        doc = SearchDoc(
            doc_id=did, title=title, content=content,
            embedding=embedding,
            fields=dict(fields or {}), tags=list(tags or []),
            source=source, boost=boost,
            created_at=created_at or time.time(),
            _tokens=toks, _tf=tf)

        # Remove old index entries if re-indexing
        if did in self._docs:
            self._remove_from_index(did)

        self._docs[did] = doc
        for term in set(toks):
            self._index.setdefault(term, []).append(did)

        self._persist_doc(doc)
        return doc

    def index_batch(self, records: List[Dict]) -> List[SearchDoc]:
        return [self.index(**r) for r in records]

    def delete(self, doc_id: str):
        doc = self._docs.pop(doc_id, None)
        if doc:
            self._remove_from_index(doc_id)
            self._db.execute("DELETE FROM se_docs WHERE doc_id=?", (doc_id,))
            self._db.commit()

    def _remove_from_index(self, doc_id: str):
        doc = self._docs.get(doc_id)
        if not doc: return
        for term in set(doc._tokens):
            lst = self._index.get(term, [])
            if doc_id in lst: lst.remove(doc_id)

    # ── SEARCH ───────────────────────────────────────────────────────

    def search(self, req: SearchRequest) -> List[SearchHit]:
        self._query_count += 1
        q_tokens = _tokenize(req.query)

        # Candidate set
        candidates: set = set(self._docs.keys())

        # Apply filters
        candidates = self._apply_filters(candidates, req)
        if not candidates:
            self._log_query(req.query, req.mode.value, 0)
            return []

        # Score
        bm25_scores:   Dict[str, float] = {}
        vector_scores: Dict[str, float] = {}

        if req.mode in (SearchMode.KEYWORD, SearchMode.HYBRID):
            bm25_scores = self._bm25(q_tokens, candidates)

        if req.mode in (SearchMode.VECTOR, SearchMode.HYBRID):
            q_emb = req.query_embedding
            if q_emb is None and self.embed_fn and req.query:
                try: q_emb = self.embed_fn(req.query)
                except Exception: pass
            if q_emb:
                vector_scores = self._vector_search(q_emb, candidates)

        # Combine scores
        all_ids = candidates & (set(bm25_scores) | set(vector_scores))
        if not all_ids and req.mode == SearchMode.KEYWORD:
            all_ids = set(bm25_scores.keys())

        if req.rerank and bm25_scores and vector_scores:
            final_scores = self._rrf(bm25_scores, vector_scores)
        else:
            w_kw = 1.0 - req.vector_weight
            w_v  = req.vector_weight
            final_scores = {}
            for did in all_ids:
                bm  = bm25_scores.get(did, 0.0)
                vec = vector_scores.get(did, 0.0)
                final_scores[did] = w_kw * bm + w_v * vec

        # Apply boost
        for did in final_scores:
            doc = self._docs.get(did)
            if doc: final_scores[did] *= doc.boost

        # Sort
        sorted_ids = self._sort(list(final_scores.items()), req)
        top = sorted_ids[:req.top_k]

        hits = []
        for rank, (did, score) in enumerate(top):
            doc  = self._docs.get(did)
            if not doc: continue
            hits.append(SearchHit(
                doc_id=did, title=doc.title, score=score,
                bm25_score=bm25_scores.get(did, 0.0),
                vector_score=vector_scores.get(did, 0.0),
                snippet=self._snippet(doc.content, q_tokens),
                highlights=self._highlights(doc.content, q_tokens),
                fields=doc.fields, tags=doc.tags,
                rank=rank + 1))

        self._log_query(req.query, req.mode.value, len(hits))
        return hits

    def quick_search(self, query: str, top_k: int = 5,
                     mode: SearchMode = SearchMode.HYBRID) -> List[SearchHit]:
        return self.search(SearchRequest(query=query, top_k=top_k, mode=mode))

    # ── BM25 ─────────────────────────────────────────────────────────

    def _bm25(self, q_tokens: List[str],
               candidates: set) -> Dict[str, float]:
        N     = len(self._docs)
        avgdl = (sum(len(d._tokens) for d in self._docs.values()) / N
                 if N else 1.0)
        scores: Dict[str, float] = {}
        for did in candidates:
            doc = self._docs[did]
            dl  = len(doc._tokens)
            sc  = 0.0
            for term in q_tokens:
                if term not in self._index: continue
                tf  = doc._tf.get(term, 0)
                df  = len(self._index.get(term, []))
                idf = math.log((N - df + 0.5) / (df + 0.5) + 1)
                tf_n = (tf * (self.BM25_K1 + 1) /
                        (tf + self.BM25_K1 * (1 - self.BM25_B +
                         self.BM25_B * dl / avgdl)))
                sc += idf * tf_n
            if sc > 0: scores[did] = sc
        return scores

    # ── VECTOR ───────────────────────────────────────────────────────

    def _vector_search(self, q_emb: List[float],
                        candidates: set) -> Dict[str, float]:
        scores: Dict[str, float] = {}
        for did in candidates:
            doc = self._docs.get(did)
            if doc and doc.embedding:
                sc = _cosine(q_emb, doc.embedding)
                if sc > 0: scores[did] = sc
        return scores

    # ── RRF ──────────────────────────────────────────────────────────

    def _rrf(self, kw: Dict[str, float],
              vec: Dict[str, float]) -> Dict[str, float]:
        kw_rank  = {did: i + 1 for i, (did, _)
                    in enumerate(sorted(kw.items(), key=lambda x: -x[1]))}
        vec_rank = {did: i + 1 for i, (did, _)
                    in enumerate(sorted(vec.items(), key=lambda x: -x[1]))}
        all_ids  = set(kw_rank) | set(vec_rank)
        return {did: (1.0 / (self.RRF_K + kw_rank.get(did, len(all_ids) + 1)) +
                      1.0 / (self.RRF_K + vec_rank.get(did, len(all_ids) + 1)))
                for did in all_ids}

    # ── FILTERS ──────────────────────────────────────────────────────

    def _apply_filters(self, candidates: set,
                        req: SearchRequest) -> set:
        result = set(candidates)
        for did in list(result):
            doc = self._docs.get(did)
            if not doc:
                result.discard(did); continue
            # Field filters
            for k, v in req.filters.items():
                fval = doc.fields.get(k)
                if isinstance(v, list):
                    if fval not in v:
                        result.discard(did); break
                elif isinstance(v, dict):
                    lo = v.get("gte"); hi = v.get("lte")
                    try:
                        fnum = float(fval)
                        if lo is not None and fnum < lo:
                            result.discard(did); break
                        if hi is not None and fnum > hi:
                            result.discard(did); break
                    except (TypeError, ValueError):
                        result.discard(did); break
                else:
                    if fval != v:
                        result.discard(did); break
            else:
                # Tag filter
                if req.tag_filter and not all(t in doc.tags for t in req.tag_filter):
                    result.discard(did)
                # Source filter
                elif req.source_filter and doc.source != req.source_filter:
                    result.discard(did)
                # Date filter
                elif req.date_from and doc.created_at < req.date_from:
                    result.discard(did)
                elif req.date_to and doc.created_at > req.date_to:
                    result.discard(did)
        return result

    def _sort(self, items: List[Tuple[str, float]],
               req: SearchRequest) -> List[Tuple[str, float]]:
        if req.sort == SortOrder.DATE_ASC:
            return sorted(items, key=lambda x: self._docs[x[0]].created_at)
        if req.sort == SortOrder.DATE_DESC:
            return sorted(items, key=lambda x: -self._docs[x[0]].created_at)
        if req.sort == SortOrder.FIELD and req.sort_field:
            return sorted(items,
                          key=lambda x: self._docs[x[0]].fields.get(req.sort_field, 0))
        return sorted(items, key=lambda x: -x[1])

    # ── SNIPPET / HIGHLIGHT ───────────────────────────────────────────

    def _snippet(self, content: str, q_tokens: List[str]) -> str:
        lower = content.lower()
        for term in q_tokens:
            pos = lower.find(term)
            if pos >= 0:
                s = max(0, pos - self.snippet_len // 2)
                return content[s:s + self.snippet_len].strip()
        return content[:self.snippet_len].strip()

    def _highlights(self, content: str, q_tokens: List[str]) -> List[str]:
        tokens = _tokenize(content)
        hits   = []
        w      = 5
        for i, tok in enumerate(tokens):
            if tok in q_tokens:
                window = " ".join(tokens[max(0, i - w):i + w + 1])
                if window not in hits: hits.append(window)
                if len(hits) >= 3: break
        return hits

    # ── PERSISTENCE ──────────────────────────────────────────────────

    def _persist_doc(self, doc: SearchDoc):
        self._db.execute(
            "INSERT OR REPLACE INTO se_docs VALUES (?,?,?,?,?,?,?,?)",
            (doc.doc_id, doc.title, doc.content[:2000],
             json.dumps(doc.fields), json.dumps(doc.tags),
             doc.source, doc.created_at, doc.boost))
        self._db.commit()

    def _log_query(self, query: str, mode: str, hits: int):
        self._db.execute(
            "INSERT INTO se_queries (query,mode,hits,ts) VALUES (?,?,?,?)",
            (query[:200], mode, hits, time.time()))
        self._db.commit()

    # ── STATS ────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        return {
            "docs": len(self._docs),
            "index_terms": len(self._index),
            "queries": self._query_count,
            "has_embed_fn": self.embed_fn is not None,
        }
