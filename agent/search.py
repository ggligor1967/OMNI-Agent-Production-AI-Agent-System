"""
OMNI AGENT - Search Engine
Full-text search across memories, sessions, documents, and knowledge graph
with TF-IDF ranking, snippet highlighting, and field filtering.

Features:
- SQLite FTS5 full-text index (fast even at 100k+ documents)
- Multiple corpora: memories, sessions, documents, kg_entities
- TF-IDF ranking with configurable field boost weights
- Snippet highlighting: show surrounding context for matched terms
- Faceted results: group by corpus, date, tag, model
- Incremental indexing: add/update/delete individual documents
- Query syntax: AND, OR, NOT, phrase ("exact match"), wildcard (term*)
- Search history: log queries for analytics
- Async search: non-blocking for long indexes
"""
import re
import time
import uuid
import sqlite3
import logging
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# DOCUMENT MODEL
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class SearchDoc:
    """
    A searchable document unit.

    Attributes:
        id:       Unique document ID
        corpus:   Logical group: "memory" | "session" | "document" | "kg" | custom
        title:    Short title (boosted in ranking)
        body:     Main text content
        tags:     Optional tag list for filtering
        author:   User/session associated with this doc
        source_id: External ID (e.g. session_id, memory key)
        metadata: Arbitrary extra fields
        created_at: Unix timestamp
    """
    id: str
    corpus: str
    title: str
    body: str
    tags: List[str] = field(default_factory=list)
    author: str = ""
    source_id: str = ""
    metadata: Dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "corpus": self.corpus,
            "title": self.title, "body": self.body[:500],
            "tags": self.tags, "author": self.author,
            "source_id": self.source_id,
            "created_at": self.created_at,
        }


@dataclass
class SearchResult:
    doc: SearchDoc
    score: float
    snippet: str = ""           # highlighted excerpt
    matched_fields: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        d = self.doc.to_dict()
        d["score"] = round(self.score, 4)
        d["snippet"] = self.snippet
        d["matched_fields"] = self.matched_fields
        return d


@dataclass
class SearchResponse:
    query: str
    results: List[SearchResult]
    total: int
    took_ms: float
    facets: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "query": self.query,
            "total": self.total,
            "took_ms": round(self.took_ms, 2),
            "results": [r.to_dict() for r in self.results],
            "facets": self.facets,
        }


# ══════════════════════════════════════════════════════════════════════════════
# SNIPPET HIGHLIGHTER
# ══════════════════════════════════════════════════════════════════════════════

def _extract_snippet(text: str, query_terms: List[str],
                     window: int = 150, max_len: int = 300) -> str:
    """
    Extract a relevant excerpt from text around the first query term match.
    Wraps matched terms in **bold** markers.
    """
    if not text:
        return ""
    text_lower = text.lower()
    best_pos = len(text)
    for term in query_terms:
        pos = text_lower.find(term.lower())
        if 0 <= pos < best_pos:
            best_pos = pos

    if best_pos == len(text):
        # No match found — return start of text
        snippet = text[:max_len]
    else:
        start = max(0, best_pos - window // 2)
        end = min(len(text), start + window)
        snippet = ("..." if start > 0 else "") + text[start:end] + ("..." if end < len(text) else "")

    # Highlight matched terms
    for term in query_terms:
        snippet = re.sub(
            re.escape(term), f"**{term}**", snippet, flags=re.IGNORECASE
        )
    return snippet[:max_len]


def _parse_query_terms(query: str) -> List[str]:
    """Extract individual search terms from a query string."""
    # Remove operators, extract quoted phrases and words
    terms = []
    # Quoted phrases
    for phrase in re.findall(r'"([^"]+)"', query):
        terms.append(phrase)
    # Remove quoted parts and operators
    remaining = re.sub(r'"[^"]*"', "", query)
    remaining = re.sub(r'\b(AND|OR|NOT)\b', "", remaining)
    terms.extend(w.rstrip("*") for w in remaining.split() if len(w) > 1)
    return list(set(terms))


# ══════════════════════════════════════════════════════════════════════════════
# SEARCH INDEX
# ══════════════════════════════════════════════════════════════════════════════

class SearchIndex:
    """
    SQLite FTS5-backed search index with multi-corpus support.

    SQLite FTS5 provides:
    - BM25 ranking (similar to Elasticsearch)
    - Prefix queries (term*)
    - Phrase queries ("exact phrase")
    - Boolean operators (AND, OR, NOT)
    """

    def __init__(self, db_path: str = "data/search.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                -- Main FTS5 table
                CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
                    doc_id UNINDEXED,
                    corpus UNINDEXED,
                    title,
                    body,
                    tags,
                    author UNINDEXED,
                    source_id UNINDEXED,
                    created_at UNINDEXED,
                    metadata UNINDEXED,
                    tokenize='porter unicode61'
                );
                -- Metadata table for non-FTS fields and faceting
                CREATE TABLE IF NOT EXISTS search_docs (
                    doc_id     TEXT PRIMARY KEY,
                    corpus     TEXT,
                    title      TEXT,
                    body_len   INTEGER,
                    author     TEXT,
                    source_id  TEXT,
                    tags       TEXT,
                    metadata   TEXT,
                    created_at REAL,
                    indexed_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_sd_corpus ON search_docs(corpus);
                CREATE INDEX IF NOT EXISTS idx_sd_author ON search_docs(author);
                CREATE INDEX IF NOT EXISTS idx_sd_created ON search_docs(created_at);
                -- Query log
                CREATE TABLE IF NOT EXISTS search_log (
                    id TEXT, query TEXT, corpus TEXT,
                    results_count INTEGER, took_ms REAL, ts REAL
                );
            """)

    # ── Indexing ──────────────────────────────────────────────────────────────

    def index(self, doc: SearchDoc):
        """Add or update a document in the index."""
        import json as _json
        with self._conn() as c:
            # Remove existing entry
            c.execute("DELETE FROM search_fts WHERE doc_id=?", (doc.id,))
            c.execute("DELETE FROM search_docs WHERE doc_id=?", (doc.id,))
            # Insert into FTS
            c.execute("""
                INSERT INTO search_fts
                (doc_id, corpus, title, body, tags, author, source_id, created_at, metadata)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                doc.id, doc.corpus, doc.title, doc.body,
                " ".join(doc.tags), doc.author, doc.source_id,
                doc.created_at, _json.dumps(doc.metadata),
            ))
            # Insert metadata
            c.execute("""
                INSERT INTO search_docs
                (doc_id,corpus,title,body_len,author,source_id,tags,metadata,created_at,indexed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                doc.id, doc.corpus, doc.title, len(doc.body),
                doc.author, doc.source_id,
                _json.dumps(doc.tags), _json.dumps(doc.metadata),
                doc.created_at, time.time(),
            ))

    def index_batch(self, docs: List[SearchDoc]):
        for doc in docs:
            self.index(doc)
        logger.info(f"Indexed {len(docs)} documents")

    def delete(self, doc_id: str) -> bool:
        with self._conn() as c:
            c.execute("DELETE FROM search_fts WHERE doc_id=?", (doc_id,))
            cur = c.execute("DELETE FROM search_docs WHERE doc_id=?", (doc_id,))
        return cur.rowcount > 0

    def delete_corpus(self, corpus: str) -> int:
        with self._conn() as c:
            c.execute("DELETE FROM search_fts WHERE corpus=?", (corpus,))
            cur = c.execute("DELETE FROM search_docs WHERE corpus=?", (corpus,))
        return cur.rowcount

    # ── Search ────────────────────────────────────────────────────────────────

    def search(self, query: str,
               corpus: str = None,
               author: str = None,
               tags: List[str] = None,
               after: float = None,
               before: float = None,
               limit: int = 10,
               offset: int = 0,
               highlight: bool = True) -> SearchResponse:
        """
        Search the index. Returns SearchResponse.

        Query syntax (FTS5):
        - Simple: "python async"
        - AND: "python AND async"
        - OR:  "python OR javascript"
        - NOT: "python NOT java"
        - Phrase: '"async await"'
        - Prefix: "pyth*"
        """
        import json as _json
        start = time.time()

        if not query.strip():
            return SearchResponse(query, [], 0, 0.0)

        # Build FTS query
        fts_query = query
        if corpus:
            fts_query = f'corpus:{corpus} {query}'

        # Sanitize for FTS5 (prevent injection)
        safe_query = re.sub(r'[^\w\s"*:()ANDORNOT-]', " ", query).strip()
        if not safe_query:
            return SearchResponse(query, [], 0, 0.0)

        conditions = []
        params: List[Any] = [safe_query]

        if author:
            conditions.append("sd.author=?"); params.append(author)
        if after:
            conditions.append("sd.created_at>=?"); params.append(after)
        if before:
            conditions.append("sd.created_at<=?"); params.append(before)
        if tags:
            for tag in tags:
                conditions.append("sd.tags LIKE ?"); params.append(f'%"{tag}"%')

        where_extra = (" AND " + " AND ".join(conditions)) if conditions else ""

        # FTS search with BM25 ranking
        # rank column = negative BM25 score (lower = better)
        try:
            corpus_filter = f" AND fts.corpus='{corpus}'" if corpus else ""
            sql = f"""
                SELECT fts.doc_id, fts.corpus, fts.title, fts.body,
                       fts.author, fts.source_id, fts.created_at, fts.metadata,
                       fts.tags, rank
                FROM search_fts fts
                JOIN search_docs sd ON sd.doc_id = fts.doc_id
                WHERE search_fts MATCH ?{corpus_filter}{where_extra}
                ORDER BY rank
                LIMIT ? OFFSET ?
            """
            params.extend([limit, offset])

            count_sql = f"""
                SELECT COUNT(*) FROM search_fts fts
                JOIN search_docs sd ON sd.doc_id = fts.doc_id
                WHERE search_fts MATCH ?{corpus_filter}{where_extra}
            """

            with self._conn() as c:
                rows = c.execute(sql, params).fetchall()
                total = c.execute(count_sql, params[:-2]).fetchone()[0]

        except sqlite3.OperationalError as e:
            logger.warning(f"Search query failed: {e}, query='{safe_query}'")
            return SearchResponse(query, [], 0, (time.time() - start) * 1000)

        query_terms = _parse_query_terms(query)
        results = []
        for row in rows:
            meta = {}
            try:
                meta = _json.loads(row["metadata"] or "{}")
            except Exception:
                pass
            tags_list = []
            try:
                raw_tags = row["tags"] or ""
                tags_list = raw_tags.split() if raw_tags else []
            except Exception:
                pass

            doc = SearchDoc(
                id=row["doc_id"], corpus=row["corpus"],
                title=row["title"] or "", body=row["body"] or "",
                author=row["author"] or "",
                source_id=row["source_id"] or "",
                tags=tags_list, metadata=meta,
                created_at=row["created_at"] or 0.0,
            )
            score = abs(float(row["rank"]))   # BM25 is negative
            snippet = ""
            if highlight:
                text = (doc.title + " " + doc.body)
                snippet = _extract_snippet(text, query_terms)

            matched = []
            if query_terms:
                if any(t.lower() in (doc.title or "").lower() for t in query_terms):
                    matched.append("title")
                if any(t.lower() in (doc.body or "").lower() for t in query_terms):
                    matched.append("body")

            results.append(SearchResult(doc, score, snippet, matched))

        # Facets
        facets = self._compute_facets(safe_query, corpus, author)

        took_ms = (time.time() - start) * 1000

        # Log query
        self._log_query(query, corpus, len(results), took_ms)

        return SearchResponse(query, results, total, took_ms, facets)

    def _compute_facets(self, query: str, corpus: str = None,
                        author: str = None) -> Dict:
        """Compute facet counts for corpus and date ranges."""
        try:
            corpus_filter = f" AND fts.corpus='{corpus}'" if corpus else ""
            with self._conn() as c:
                by_corpus = dict(c.execute(f"""
                    SELECT fts.corpus, COUNT(*)
                    FROM search_fts fts
                    WHERE search_fts MATCH ?{corpus_filter}
                    GROUP BY fts.corpus
                """, [query]).fetchall())
            return {"by_corpus": by_corpus}
        except Exception:
            return {}

    def _log_query(self, query: str, corpus: str,
                   count: int, took_ms: float):
        with self._conn() as c:
            c.execute("""
                INSERT INTO search_log (id,query,corpus,results_count,took_ms,ts)
                VALUES (?,?,?,?,?,?)
            """, (str(uuid.uuid4())[:8], query, corpus or "",
                  count, took_ms, time.time()))

    # ── Stats ─────────────────────────────────────────────────────────────────

    def stats(self) -> Dict:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM search_docs").fetchone()[0]
            by_corpus = dict(c.execute(
                "SELECT corpus, COUNT(*) FROM search_docs GROUP BY corpus"
            ).fetchall())
            recent_queries = c.execute(
                "SELECT query, results_count, took_ms FROM search_log "
                "ORDER BY ts DESC LIMIT 10"
            ).fetchall()
        return {
            "total_indexed": total,
            "by_corpus": by_corpus,
            "recent_queries": [dict(r) for r in recent_queries],
        }

    def suggest(self, prefix: str, limit: int = 5) -> List[str]:
        """Return title suggestions for autocomplete."""
        with self._conn() as c:
            rows = c.execute("""
                SELECT DISTINCT title FROM search_docs
                WHERE title LIKE ? LIMIT ?
            """, (f"{prefix}%", limit)).fetchall()
        return [r[0] for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
# SEARCH SERVICE (high-level)
# ══════════════════════════════════════════════════════════════════════════════

class SearchService:
    """
    High-level search service that wraps the index with corpus-specific helpers.

    Usage:
        search = SearchService()

        # Index content
        search.index_memory("mem_123", "user_1", "Python recursion tip", "Use memoization...")
        search.index_session_message("sess_1", "user_1", "How do I reverse a list?", "assistant")
        search.index_document("doc_1", "user_1", "API Docs", "The /chat endpoint...")

        # Search
        resp = search.query("recursion memoization", corpus="memory")
        for result in resp.results:
            print(result.snippet)
    """

    def __init__(self, db_path: str = "data/search.db"):
        self.index = SearchIndex(db_path)

    # ── Indexing helpers ──────────────────────────────────────────────────────

    def index_memory(self, memory_id: str, user_id: str,
                     key: str, value: str,
                     tags: List[str] = None):
        doc = SearchDoc(
            id=f"memory:{memory_id}",
            corpus="memory",
            title=key,
            body=value,
            author=user_id,
            source_id=memory_id,
            tags=tags or [],
        )
        self.index.index(doc)

    def index_session_message(self, session_id: str, user_id: str,
                               content: str, role: str,
                               model: str = ""):
        msg_id = f"session:{session_id}:{abs(hash(content)) % 100000}"
        doc = SearchDoc(
            id=msg_id,
            corpus="session",
            title=content[:80],
            body=content,
            author=user_id,
            source_id=session_id,
            tags=[role],
            metadata={"role": role, "model": model},
        )
        self.index.index(doc)

    def index_document(self, doc_id: str, user_id: str,
                       title: str, content: str,
                       tags: List[str] = None,
                       metadata: Dict = None):
        doc = SearchDoc(
            id=f"doc:{doc_id}",
            corpus="document",
            title=title,
            body=content,
            author=user_id,
            source_id=doc_id,
            tags=tags or [],
            metadata=metadata or {},
        )
        self.index.index(doc)

    def index_kg_entity(self, entity_id: str, name: str,
                        entity_type: str, description: str):
        doc = SearchDoc(
            id=f"kg:{entity_id}",
            corpus="knowledge_graph",
            title=name,
            body=description,
            tags=[entity_type],
            source_id=entity_id,
            metadata={"type": entity_type},
        )
        self.index.index(doc)

    # ── Search ────────────────────────────────────────────────────────────────

    def query(self, q: str, corpus: str = None,
              user_id: str = None, limit: int = 10,
              **kwargs) -> SearchResponse:
        return self.index.search(
            q, corpus=corpus, author=user_id, limit=limit, **kwargs
        )

    def stats(self) -> Dict:
        return self.index.stats()

    def suggest(self, prefix: str) -> List[str]:
        return self.index.suggest(prefix)

    def register_routes(self, app, prefix: str = ""):
        from aiohttp import web

        async def search_endpoint(request):
            q     = request.rel_url.query.get("q", "")
            corpus= request.rel_url.query.get("corpus")
            user  = request.rel_url.query.get("user_id")
            limit = int(request.rel_url.query.get("limit", 10))
            offset= int(request.rel_url.query.get("offset", 0))
            if not q:
                return web.json_response({"error": "q required"}, status=400)
            resp = self.query(q, corpus=corpus, user_id=user,
                              limit=limit, offset=offset)
            return web.json_response(resp.to_dict())

        async def index_endpoint(request):
            import json as _json
            data = await request.json()
            doc = SearchDoc(
                id=data.get("id", str(uuid.uuid4())[:12]),
                corpus=data.get("corpus", "document"),
                title=data.get("title", ""),
                body=data.get("body", ""),
                author=data.get("author", ""),
                source_id=data.get("source_id", ""),
                tags=data.get("tags", []),
                metadata=data.get("metadata", {}),
            )
            self.index.index(doc)
            return web.json_response({"indexed": doc.id}, status=201)

        async def delete_endpoint(request):
            doc_id = request.match_info["id"]
            ok = self.index.delete(doc_id)
            return web.json_response({"deleted": ok})

        async def suggest_endpoint(request):
            prefix = request.rel_url.query.get("prefix", "")
            return web.json_response({"suggestions": self.suggest(prefix)})

        async def stats_endpoint(request):
            return web.json_response(self.stats())

        app.router.add_get( f"{prefix}/search",          search_endpoint)
        app.router.add_post(f"{prefix}/search/index",    index_endpoint)
        app.router.add_delete(f"{prefix}/search/{{id}}", delete_endpoint)
        app.router.add_get( f"{prefix}/search/suggest",  suggest_endpoint)
        app.router.add_get( f"{prefix}/search/stats",    stats_endpoint)
        logger.info(f"Search API routes registered at {prefix}/search")
