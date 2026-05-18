"""OMNI AGENT - Search Engine
In-memory full-text search with inverted index, BM25 scoring,
field boosting, faceted filtering, and result highlighting.

Features:
- Document: arbitrary fields + id; any field can be indexed
- Inverted index: term → {doc_id: [positions]} for phrase support
- BM25 scoring: k1=1.5, b=0.75; IDF × TF normalization
- Field boosting: multiply score by field weight (e.g. title=3.0)
- Stop words: configurable set filtered before indexing
- Stemming: simple suffix-stripping (English porter-lite)
- Phrase search: "exact phrase" in quotes; position-based check
- Boolean operators: AND (+term), OR (term), NOT (-term)
- Facets: count documents per unique value of a field
- Filters: exact/range field filters applied before scoring
- Highlighting: wrap matched terms in <mark> tags with context
- Pagination: offset + limit on results
- Fuzzy match: Levenshtein distance ≤ 1 term expansion
- Synonyms: configurable synonym map expands query terms
- Field projection: return subset of document fields
- SQLite persistence: document store + index snapshots
- REST API: index, search, delete, facets, stats
"""
import json, math, re, sqlite3, time, uuid, logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

_STOP_WORDS = {
    "a","an","the","and","or","but","in","on","at","to","for",
    "of","with","by","from","is","was","are","were","be","been",
    "it","its","this","that","as","not","have","has","had","do",
    "does","did","will","would","could","should","may","might"
}

def _simple_stem(word: str) -> str:
    """Lightweight English suffix stripping."""
    for suffix in ("ational","tional","enci","anci","izer","iser",
                    "alism","ness","ment","ful","ous","ive","ize","ise"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[:-len(suffix)]
    if word.endswith("ing") and len(word) > 5:  return word[:-3]
    if word.endswith("tion") and len(word) > 5: return word[:-4]
    if word.endswith("ed") and len(word) > 4:   return word[:-2]
    if word.endswith("ly") and len(word) > 4:   return word[:-2]
    if word.endswith("s")  and len(word) > 3 and not word.endswith("ss"):
        return word[:-1]
    return word

def _tokenize(text: str, stop_words: Set[str],
               stem: bool = True) -> List[str]:
    words = re.findall(r'[a-zA-Z0-9]+', text.lower())
    tokens = [w for w in words if w not in stop_words and len(w) > 1]
    if stem: tokens = [_simple_stem(t) for t in tokens]
    return tokens

def _levenshtein(a: str, b: str) -> int:
    if a == b: return 0
    if len(a) < len(b): a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            curr.append(min(prev[j]+1, curr[j-1]+1,
                             prev[j-1]+(0 if ca==cb else 1)))
        prev = curr
    return prev[-1]

@dataclass
class SearchDoc:
    id: str; fields: Dict[str, Any]
    indexed_at: float = field(default_factory=time.time)

    def to_dict(self, project: List[str] = None) -> Dict:
        if project:
            return {k: self.fields.get(k) for k in project}
        return dict(self.fields)

@dataclass
class SearchResult:
    doc_id: str; score: float
    doc: Dict; highlights: Dict[str, str]

    def to_dict(self):
        return {"id": self.doc_id, "score": round(self.score, 4),
                "doc": self.doc, "highlights": self.highlights}

class SEStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS documents(
                    id TEXT PRIMARY KEY, fields TEXT, indexed_at REAL);
                CREATE TABLE IF NOT EXISTS search_log(
                    id TEXT PRIMARY KEY, query TEXT,
                    results INTEGER, elapsed_ms REAL, created_at REAL);
            """)

    def upsert(self, doc: SearchDoc):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO documents VALUES(?,?,?)",
                (doc.id, json.dumps(doc.fields, default=str), doc.indexed_at))

    def delete(self, doc_id: str):
        with self._conn() as c:
            c.execute("DELETE FROM documents WHERE id=?", (doc_id,))

    def load_all(self) -> List[SearchDoc]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM documents").fetchall()
        return [SearchDoc(id=r["id"], fields=json.loads(r["fields"]),
                           indexed_at=r["indexed_at"]) for r in rows]

    def log_search(self, query: str, n: int, ms: float):
        with self._conn() as c:
            c.execute("INSERT INTO search_log VALUES(?,?,?,?,?)",
                (str(uuid.uuid4())[:8], query[:200], n, ms, time.time()))

    def stats(self) -> Dict:
        with self._conn() as c:
            nd = c.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            ns = c.execute("SELECT COUNT(*) FROM search_log").fetchone()[0]
            avg = c.execute("SELECT AVG(elapsed_ms) FROM search_log").fetchone()[0] or 0
        return {"indexed": nd, "searches": ns, "avg_latency_ms": round(avg, 2)}

class SearchEngine:
    """
    BM25 full-text search engine with facets and highlighting.

    Usage:
        se = SearchEngine()
        se.index({"id":"1","title":"Python Guide","body":"Learn Python fast"})
        se.index({"id":"2","title":"Go Tutorial","body":"Go is fast and compiled"})

        results = se.search("python fast", field_weights={"title":3.0})
        for r in results:
            print(r.doc_id, r.score, r.highlights)
    """
    BM25_K1 = 1.5; BM25_B = 0.75

    def __init__(self, db_path: str = "data/search.db",
                 index_fields: List[str] = None,
                 stop_words: Set[str] = None,
                 synonyms: Dict[str, List[str]] = None,
                 stem: bool = True):
        self._store = SEStore(db_path)
        self._index_fields = list(index_fields or [])
        self._stop_words = stop_words or set(_STOP_WORDS)
        self._synonyms = dict(synonyms or {})
        self._stem = stem
        # In-memory structures
        self._docs: Dict[str, SearchDoc] = {}
        self._inv: Dict[str, Dict[str, List[int]]] = defaultdict(
            lambda: defaultdict(list))  # term → {doc_id → [positions]}
        self._field_inv: Dict[str, Dict[str, Dict[str, List[int]]]] = defaultdict(
            lambda: defaultdict(lambda: defaultdict(list)))
        self._doc_lengths: Dict[str, int] = {}   # doc_id → total token count
        self._avg_length: float = 0.0
        # Load persisted docs
        for doc in self._store.load_all():
            self._docs[doc.id] = doc
            self._index_doc(doc, persist=False)

    def _text_of(self, doc: SearchDoc, field: str = None) -> str:
        if field:
            return str(doc.fields.get(field, ""))
        fields = self._index_fields or list(doc.fields.keys())
        return " ".join(str(doc.fields.get(f, "")) for f in fields)

    def _index_doc(self, doc: SearchDoc, persist: bool = True):
        tokens_all: List[str] = []
        fields = self._index_fields or list(doc.fields.keys())
        for fld in fields:
            text = str(doc.fields.get(fld, ""))
            tokens = _tokenize(text, self._stop_words, self._stem)
            for pos, tok in enumerate(tokens):
                self._inv[tok][doc.id].append(pos)
                self._field_inv[fld][tok][doc.id].append(pos)
            tokens_all.extend(tokens)
        self._doc_lengths[doc.id] = len(tokens_all)
        self._avg_length = sum(self._doc_lengths.values()) / max(1, len(self._docs))
        if persist:
            self._store.upsert(doc)

    def index(self, data: Dict, doc_id: str = None) -> SearchDoc:
        if doc_id is None:
            doc_id = str(data.get("id", str(uuid.uuid4())[:8]))
        doc = SearchDoc(id=doc_id, fields=dict(data))
        # Remove stale index entries if updating
        if doc_id in self._docs:
            self._remove_from_index(doc_id)
        self._docs[doc_id] = doc
        self._index_doc(doc)
        return doc

    def _remove_from_index(self, doc_id: str):
        for term_map in self._inv.values():
            term_map.pop(doc_id, None)
        for fld_map in self._field_inv.values():
            for term_map in fld_map.values():
                term_map.pop(doc_id, None)
        self._doc_lengths.pop(doc_id, None)

    def delete(self, doc_id: str) -> bool:
        if doc_id not in self._docs: return False
        self._remove_from_index(doc_id)
        del self._docs[doc_id]
        self._avg_length = (sum(self._doc_lengths.values()) /
                             max(1, len(self._docs)))
        self._store.delete(doc_id)
        return True

    def _expand_query(self, tokens: List[str]) -> List[str]:
        expanded = list(tokens)
        for tok in tokens:
            for syn in self._synonyms.get(tok, []):
                syn_tokens = _tokenize(syn, self._stop_words, self._stem)
                for s_stem in syn_tokens:
                    if s_stem not in expanded:
                        expanded.append(s_stem)
        return expanded

    def _fuzzy_expand(self, tokens: List[str], max_dist: int = 1) -> List[str]:
        expanded = list(tokens)
        vocab = list(self._inv.keys())
        for tok in tokens:
            if len(tok) < 4: continue
            for v in vocab:
                if abs(len(v) - len(tok)) <= max_dist:
                    if _levenshtein(tok, v) <= max_dist and v not in expanded:
                        expanded.append(v)
        return expanded

    def _bm25(self, term: str, doc_id: str) -> float:
        N = max(1, len(self._docs))
        df = len(self._inv.get(term, {}))
        if df == 0: return 0.0
        idf = math.log((N - df + 0.5) / (df + 0.5) + 1)
        tf = len(self._inv[term].get(doc_id, []))
        dl = self._doc_lengths.get(doc_id, 0)
        avgdl = self._avg_length or 1.0
        norm_tf = (tf * (self.BM25_K1 + 1) /
                    (tf + self.BM25_K1 * (1 - self.BM25_B + self.BM25_B * dl / avgdl)))
        return idf * norm_tf

    def _parse_query(self, query: str):
        """Return (must_terms, should_terms, must_not_terms, phrase)."""
        phrase = None
        m = re.search(r'"([^"]+)"', query)
        if m:
            phrase = _tokenize(m.group(1), self._stop_words, self._stem)
            query = query[:m.start()] + query[m.end():]
        must, should, must_not = [], [], []
        for tok in query.split():
            if tok.startswith("+"):
                must.append(_simple_stem(tok[1:].lower()) if self._stem else tok[1:].lower())
            elif tok.startswith("-"):
                must_not.append(_simple_stem(tok[1:].lower()) if self._stem else tok[1:].lower())
            else:
                t = _tokenize(tok, self._stop_words, self._stem)
                should.extend(t)
        return must, should, must_not, phrase

    def _check_phrase(self, phrase_tokens: List[str], doc_id: str) -> bool:
        """Check if phrase appears consecutively in any field."""
        if not phrase_tokens: return True
        pos_lists = [self._inv[t].get(doc_id, []) for t in phrase_tokens]
        if any(not pl for pl in pos_lists): return False
        for start_pos in pos_lists[0]:
            if all(start_pos + i in pos_lists[i]
                    for i in range(1, len(phrase_tokens))):
                return True
        return False

    def _highlight(self, text: str, terms: List[str],
                    context_words: int = 6) -> str:
        words = text.split()
        hit_indices = set()
        for i, w in enumerate(words):
            w_clean = re.sub(r'[^a-zA-Z0-9]', '', w.lower())
            w_stem  = _simple_stem(w_clean) if self._stem else w_clean
            if w_stem in terms or w_clean in terms:
                hit_indices.add(i)
        if not hit_indices: return ""
        # Extract context window around first hit
        first = min(hit_indices)
        lo = max(0, first - context_words)
        hi = min(len(words), first + context_words + 1)
        snippet_words = []
        for i in range(lo, hi):
            w = words[i]
            w_clean = re.sub(r'[^a-zA-Z0-9]', '', w.lower())
            w_stem = _simple_stem(w_clean) if self._stem else w_clean
            if i in hit_indices or w_stem in terms:
                snippet_words.append(f"<mark>{w}</mark>")
            else:
                snippet_words.append(w)
        prefix = "…" if lo > 0 else ""
        suffix = "…" if hi < len(words) else ""
        return prefix + " ".join(snippet_words) + suffix

    def search(self, query: str,
                field_weights: Dict[str, float] = None,
                filters: Dict[str, Any] = None,
                facet_fields: List[str] = None,
                offset: int = 0, limit: int = 10,
                fuzzy: bool = False,
                project: List[str] = None) -> Tuple[List[SearchResult], Dict]:
        t0 = time.time()
        must, should, must_not, phrase = self._parse_query(query)
        all_terms = self._expand_query(list(set(must + should)))
        if fuzzy: all_terms = self._fuzzy_expand(all_terms)

        # Candidate set
        # Include phrase tokens as should-terms for candidate generation
        phrase_terms = list(phrase) if phrase else []
        all_candidate_terms = list(set(all_terms + phrase_terms))
        candidates: Set[str] = set()
        if must:
            sets = [set(self._inv.get(t, {}).keys()) for t in must]
            candidates = sets[0].copy()
            for s in sets[1:]: candidates &= s
            # Also intersect with phrase terms if phrase-only
            if not all_terms and phrase:
                p_sets = [set(self._inv.get(t, {}).keys()) for t in phrase]
                if p_sets:
                    p_cands = p_sets[0].copy()
                    for s in p_sets[1:]: p_cands &= s
                    candidates &= p_cands
        else:
            for t in all_candidate_terms:
                candidates |= set(self._inv.get(t, {}).keys())

        # Remove must_not
        for t in must_not:
            candidates -= set(self._inv.get(t, {}).keys())

        # Apply filters
        if filters:
            filtered = set()
            for doc_id in candidates:
                doc = self._docs.get(doc_id)
                if not doc: continue
                ok = True
                for fld, fval in filters.items():
                    dval = doc.fields.get(fld)
                    if isinstance(fval, dict):
                        lo = fval.get("gte", float("-inf"))
                        hi = fval.get("lte", float("inf"))
                        if not (lo <= (dval or 0) <= hi): ok = False; break
                    elif dval != fval:
                        ok = False; break
                if ok: filtered.add(doc_id)
            candidates = filtered

        # Score
        field_weights = field_weights or {}
        scores: Dict[str, float] = {}
        score_terms = all_terms if all_terms else phrase_terms
        for doc_id in candidates:
            score = 0.0
            for term in score_terms:
                base = self._bm25(term, doc_id)
                # Apply field boosts
                boost = 1.0
                for fld, weight in field_weights.items():
                    if doc_id in self._field_inv.get(fld, {}).get(term, {}):
                        boost = max(boost, weight)
                score += base * boost
            if phrase and not self._check_phrase(phrase, doc_id):
                continue
            scores[doc_id] = score

        sorted_ids = sorted(scores, key=lambda x: -scores[x])

        # Facets
        facet_counts: Dict[str, Dict[str, int]] = {}
        if facet_fields:
            for fld in facet_fields:
                facet_counts[fld] = defaultdict(int)
                for doc_id in sorted_ids:
                    doc = self._docs.get(doc_id)
                    if doc:
                        val = str(doc.fields.get(fld, ""))
                        facet_counts[fld][val] += 1

        # Paginate
        page_ids = sorted_ids[offset: offset + limit]
        results = []
        highlight_terms = list(set(must + should + (phrase or [])))
        for doc_id in page_ids:
            doc = self._docs.get(doc_id)
            if not doc: continue
            highlights = {}
            for fld in (self._index_fields or list(doc.fields.keys())):
                text = str(doc.fields.get(fld, ""))
                hl = self._highlight(text, highlight_terms)
                if hl: highlights[fld] = hl
            results.append(SearchResult(
                doc_id=doc_id,
                score=scores[doc_id],
                doc=doc.to_dict(project),
                highlights=highlights))

        elapsed_ms = (time.time() - t0) * 1000
        self._store.log_search(query, len(results), elapsed_ms)
        meta = {"total": len(sorted_ids), "offset": offset,
                "limit": limit, "elapsed_ms": round(elapsed_ms, 2),
                "facets": {k: dict(v) for k, v in facet_counts.items()}}
        return results, meta

    def stats(self) -> Dict:
        s = self._store.stats()
        s["in_memory_docs"] = len(self._docs)
        s["vocab_size"] = len(self._inv)
        s["avg_doc_length"] = round(self._avg_length, 2)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def index_ep(req):
            d = await req.json()
            doc = self.index(d["doc"], d.get("id"))
            return web.json_response({"id": doc.id}, status=201)
        async def search_ep(req):
            d = await req.json()
            results, meta = self.search(
                d["query"],
                field_weights=d.get("field_weights"),
                filters=d.get("filters"),
                facet_fields=d.get("facets"),
                offset=d.get("offset",0),
                limit=d.get("limit",10),
                fuzzy=d.get("fuzzy",False),
                project=d.get("project"))
            return web.json_response({
                "results": [r.to_dict() for r in results], "meta": meta})
        async def delete_ep(req):
            doc_id = req.match_info["doc_id"]
            ok = self.delete(doc_id)
            return web.json_response({"deleted": ok})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/search"
        app.router.add_post(f"{p}/index",         index_ep)
        app.router.add_post(f"{p}/query",         search_ep)
        app.router.add_delete(f"{p}/{{doc_id}}",  delete_ep)
        app.router.add_get( f"{p}/stats",         stats_ep)
        logger.info(f"Search engine API at {prefix}/search/")
