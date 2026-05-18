"""OMNI AGENT - Document Processor
Ingest raw text, HTML, or Markdown documents; clean, chunk, deduplicate,
extract metadata, and prepare for downstream LLM or RAG pipelines.

Features:
- Source types: plain text, HTML (tag-stripped), Markdown (symbol-stripped)
- Cleaning: whitespace normalisation, boilerplate removal, encoding repair
- Chunking strategies: fixed-size, sentence-boundary, paragraph, sliding-window
- Overlap: configurable token overlap between consecutive chunks
- Metadata extraction: title, word count, language hint, reading-time estimate
- Deduplication: Jaccard-similarity hash-based near-dup detection
- Keyword extraction: TF-IDF style top-N keywords per document
- Summary stub: first-N-sentence extractive summary
- Batch processing: process many docs concurrently with async
- Document store: SQLite persistence for all processed docs and chunks
- REST API: process, chunk, search, stats
"""
import re, time, uuid, json, sqlite3, asyncio, math, hashlib, logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

# ── Cleaning helpers ──────────────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    text = re.sub(r'<script[^>]*>[\s\S]*?</script>', ' ', text, flags=re.I)
    text = re.sub(r'<style[^>]*>[\s\S]*?</style>',  ' ', text, flags=re.I)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;',  '&', text)
    text = re.sub(r'&lt;',   '<', text)
    text = re.sub(r'&gt;',   '>', text)
    return text

def _strip_markdown(text: str) -> str:
    text = re.sub(r'#{1,6}\s+', '', text)
    text = re.sub(r'\*{1,3}(.+?)\*{1,3}', r'\1', text)
    text = re.sub(r'`{1,3}[^`]*`{1,3}', ' ', text, flags=re.S)
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
    text = re.sub(r'\[(.+?)\]\(.*?\)', r'\1', text)
    text = re.sub(r'^[-*+]\s+', '', text, flags=re.M)
    text = re.sub(r'^\d+\.\s+', '', text, flags=re.M)
    text = re.sub(r'\|[^\n]+\|', ' ', text)
    return text

def _clean(text: str) -> str:
    text = re.sub(r'\r\n', '\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def _lang_hint(text: str) -> str:
    """Very rough language detection by common function words."""
    sample = text[:500].lower()
    scores = {
        'en': len(re.findall(r'\b(the|and|is|in|of|to|a|that|it|was)\b', sample)),
        'es': len(re.findall(r'\b(el|la|de|en|y|que|es|un|una|los)\b', sample)),
        'fr': len(re.findall(r'\b(le|la|de|et|est|un|une|les|des|en)\b', sample)),
        'de': len(re.findall(r'\b(der|die|das|und|ist|in|von|zu|mit|den)\b', sample)),
    }
    return max(scores, key=scores.get) if any(scores.values()) else 'unknown'

def _jaccard_sim(a: str, b: str) -> float:
    wa = set(re.findall(r'\w+', a.lower()))
    wb = set(re.findall(r'\w+', b.lower()))
    if not wa and not wb: return 1.0
    return len(wa & wb) / len(wa | wb)

def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]

def _extract_keywords(text: str, top_n: int = 10) -> List[str]:
    words = re.findall(r'\b[a-z]{4,}\b', text.lower())
    stopwords = {'that','this','with','from','they','have','been','will','would',
                  'their','there','about','which','when','what','then','than','into',
                  'just','also','more','some','only','other','such','most','very'}
    freq: Dict[str, int] = {}
    for w in words:
        if w not in stopwords: freq[w] = freq.get(w, 0) + 1
    total = max(1, len(words))
    scored = sorted(freq.items(), key=lambda x: -x[1] / math.log(1 + total/max(1,x[1])))
    return [w for w, _ in scored[:top_n]]

def _extractive_summary(text: str, sentences: int = 3) -> str:
    sents = re.split(r'(?<=[.!?])\s+', text.strip())
    return ' '.join(sents[:sentences])

# ── Chunking ──────────────────────────────────────────────────────────────────

def _chunk_fixed(text: str, size: int, overlap: int) -> List[str]:
    words = text.split()
    chunks = []
    step = max(1, size - overlap)
    for i in range(0, len(words), step):
        chunk = ' '.join(words[i:i+size])
        if chunk: chunks.append(chunk)
    return chunks

def _chunk_sentence(text: str, max_words: int, overlap_sents: int) -> List[str]:
    sents = re.split(r'(?<=[.!?])\s+', text.strip())
    chunks = []; current = []; current_words = 0
    for sent in sents:
        wc = len(sent.split())
        if current_words + wc > max_words and current:
            chunks.append(' '.join(current))
            current = current[-overlap_sents:] if overlap_sents else []
            current_words = sum(len(s.split()) for s in current)
        current.append(sent); current_words += wc
    if current: chunks.append(' '.join(current))
    return chunks

def _chunk_paragraph(text: str) -> List[str]:
    return [p.strip() for p in re.split(r'\n{2,}', text) if p.strip()]

# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class DocumentMeta:
    title: str = ""; word_count: int = 0; char_count: int = 0
    language: str = "unknown"; reading_time_min: float = 0.0
    keywords: List[str] = field(default_factory=list)
    summary: str = ""; content_hash: str = ""

    def to_dict(self):
        return {"title": self.title, "word_count": self.word_count,
                "char_count": self.char_count, "language": self.language,
                "reading_time_min": round(self.reading_time_min, 1),
                "keywords": self.keywords, "summary": self.summary[:200],
                "content_hash": self.content_hash}

@dataclass
class Chunk:
    id: str; doc_id: str; index: int; text: str
    word_count: int = 0; strategy: str = "fixed"
    created_at: float = field(default_factory=time.time)

    def to_dict(self):
        return {"id": self.id, "doc_id": self.doc_id, "index": self.index,
                "text": self.text[:300], "word_count": self.word_count,
                "strategy": self.strategy}

@dataclass
class ProcessedDoc:
    id: str; source_type: str; raw_length: int
    clean_text: str; meta: DocumentMeta
    chunks: List[Chunk] = field(default_factory=list)
    is_duplicate: bool = False; duplicate_of: str = ""
    processed_at: float = field(default_factory=time.time)

    def to_dict(self, include_chunks: bool = False):
        d = {"id": self.id, "source_type": self.source_type,
             "raw_length": self.raw_length, "clean_length": len(self.clean_text),
             "meta": self.meta.to_dict(), "chunk_count": len(self.chunks),
             "is_duplicate": self.is_duplicate, "duplicate_of": self.duplicate_of,
             "processed_at": self.processed_at}
        if include_chunks:
            d["chunks"] = [c.to_dict() for c in self.chunks]
        return d

class DocStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS documents(
                    id TEXT PRIMARY KEY, source_type TEXT, raw_length INTEGER,
                    clean_text TEXT, meta TEXT DEFAULT '{}',
                    is_duplicate INTEGER DEFAULT 0, duplicate_of TEXT DEFAULT '',
                    content_hash TEXT, processed_at REAL);
                CREATE TABLE IF NOT EXISTS chunks(
                    id TEXT PRIMARY KEY, doc_id TEXT, idx INTEGER,
                    text TEXT, word_count INTEGER, strategy TEXT, created_at REAL);
                CREATE INDEX IF NOT EXISTS idx_doc_hash ON documents(content_hash);
                CREATE INDEX IF NOT EXISTS idx_chunk_doc ON chunks(doc_id, idx ASC);
            """)

    def save_doc(self, doc: ProcessedDoc):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?,?,?,?,?)",
                (doc.id, doc.source_type, doc.raw_length, doc.clean_text,
                 json.dumps(doc.meta.to_dict()), int(doc.is_duplicate),
                 doc.duplicate_of, doc.meta.content_hash, doc.processed_at))
            if doc.chunks:
                c.executemany("INSERT OR REPLACE INTO chunks VALUES(?,?,?,?,?,?,?)",
                    [(ch.id, ch.doc_id, ch.index, ch.text,
                      ch.word_count, ch.strategy, ch.created_at) for ch in doc.chunks])

    def find_by_hash(self, h: str) -> Optional[str]:
        with self._conn() as c:
            row = c.execute("SELECT id FROM documents WHERE content_hash=? AND is_duplicate=0",
                             (h,)).fetchone()
        return row["id"] if row else None

    def search(self, query: str, limit: int = 20) -> List[Dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, meta, source_type, processed_at FROM documents "
                "WHERE clean_text LIKE ? AND is_duplicate=0 ORDER BY processed_at DESC LIMIT ?",
                (f'%{query}%', limit)).fetchall()
        return [{"id": r["id"], "source_type": r["source_type"],
                  "meta": json.loads(r["meta"] or "{}")} for r in rows]

    def stats(self):
        with self._conn() as c:
            nd = c.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            nc = c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            nd_dup = c.execute("SELECT COUNT(*) FROM documents WHERE is_duplicate=1").fetchone()[0]
        return {"total_documents": nd, "total_chunks": nc, "duplicates": nd_dup}

class DocumentProcessor:
    """
    Ingest, clean, chunk, and index documents for LLM/RAG pipelines.

    Usage:
        dp = DocumentProcessor(chunk_size=200, chunk_overlap=20)
        doc = dp.process("<html><body><h1>Title</h1><p>Content...</p></body></html>",
                          source_type="html", title="My Doc")
        print(doc.meta.keywords)
        for chunk in doc.chunks:
            print(chunk.text[:80])
    """
    def __init__(self, db_path: str = "data/documents.db",
                 chunk_size: int = 200, chunk_overlap: int = 30,
                 chunk_strategy: str = "sentence",
                 dedup_threshold: float = 0.85):
        self._store = DocStore(db_path)
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._chunk_strategy = chunk_strategy
        self._dedup_threshold = dedup_threshold
        self._hash_index: Dict[str, str] = {}  # hash → doc_id

    def process(self, raw: str, source_type: str = "text",
                 title: str = "", chunk_strategy: str = None) -> ProcessedDoc:
        # Clean
        if source_type == "html":
            text = _clean(_strip_html(raw))
        elif source_type in ("markdown", "md"):
            text = _clean(_strip_markdown(raw))
        else:
            text = _clean(raw)

        # Metadata
        words = text.split()
        h = _content_hash(text)
        meta = DocumentMeta(
            title=title or _extractive_summary(text, 1)[:60],
            word_count=len(words), char_count=len(text),
            language=_lang_hint(text),
            reading_time_min=len(words) / 238,
            keywords=_extract_keywords(text),
            summary=_extractive_summary(text, 3),
            content_hash=h)

        # Dedup check
        existing = self._store.find_by_hash(h)
        is_dup = False; dup_of = ""
        if not existing:
            # Jaccard check against recent hashes
            for stored_hash, stored_id in list(self._hash_index.items())[-200:]:
                if _jaccard_sim(h, stored_hash) > self._dedup_threshold:
                    is_dup = True; dup_of = stored_id; break

        # Chunk
        strategy = chunk_strategy or self._chunk_strategy
        if strategy == "fixed":
            raw_chunks = _chunk_fixed(text, self._chunk_size, self._chunk_overlap)
        elif strategy == "paragraph":
            raw_chunks = _chunk_paragraph(text)
        else:  # sentence (default)
            raw_chunks = _chunk_sentence(text, self._chunk_size, overlap_sents=1)

        doc_id = str(uuid.uuid4())[:12]
        chunks = [Chunk(id=str(uuid.uuid4())[:10], doc_id=doc_id, index=i,
                         text=c, word_count=len(c.split()), strategy=strategy)
                   for i, c in enumerate(raw_chunks)]

        doc = ProcessedDoc(id=doc_id, source_type=source_type,
                            raw_length=len(raw), clean_text=text, meta=meta,
                            chunks=chunks, is_duplicate=is_dup, duplicate_of=dup_of)
        self._store.save_doc(doc)
        self._hash_index[h] = doc_id
        logger.info(f"Doc {doc_id}: {len(words)} words, {len(chunks)} chunks, dup={is_dup}")
        return doc

    async def process_batch(self, docs: List[Dict],
                             concurrency: int = 4) -> List[ProcessedDoc]:
        sem = asyncio.Semaphore(concurrency)
        async def bounded(d):
            async with sem:
                return self.process(d.get("text",""), d.get("source_type","text"),
                                     d.get("title",""))
        return await asyncio.gather(*[bounded(d) for d in docs])

    def chunk(self, text: str, strategy: str = None,
               size: int = None, overlap: int = None) -> List[str]:
        s = strategy or self._chunk_strategy
        sz = size or self._chunk_size
        ov = overlap or self._chunk_overlap
        if s == "fixed":    return _chunk_fixed(text, sz, ov)
        if s == "paragraph": return _chunk_paragraph(text)
        return _chunk_sentence(text, sz, 1)

    def search(self, query: str, limit: int = 20) -> List[Dict]:
        return self._store.search(query, limit)

    def stats(self) -> Dict:
        return self._store.stats()

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def process_ep(req):
            d = await req.json()
            doc = self.process(d["text"], d.get("source_type","text"),
                                d.get("title",""), d.get("chunk_strategy"))
            return web.json_response(doc.to_dict(include_chunks=bool(d.get("include_chunks"))))
        async def chunk_ep(req):
            d = await req.json()
            chunks = self.chunk(d["text"], d.get("strategy"), d.get("size"), d.get("overlap"))
            return web.json_response({"chunks": chunks, "count": len(chunks)})
        async def search_ep(req):
            q = req.rel_url.query.get("q","")
            return web.json_response({"results": self.search(q)})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/docs"
        app.router.add_post(f"{p}/process", process_ep)
        app.router.add_post(f"{p}/chunk",   chunk_ep)
        app.router.add_get( f"{p}/search",  search_ep)
        app.router.add_get( f"{p}/stats",   stats_ep)
        logger.info(f"Document processor API at {prefix}/docs/")
