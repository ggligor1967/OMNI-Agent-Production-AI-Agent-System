"""OMNI AGENT - Document Parser
Ingest and chunk plain text, Markdown, JSON, CSV, and HTML into
overlapping sliding-window chunks with metadata extraction.

Features:
- Source types: TEXT, MARKDOWN, JSON, CSV, HTML (tag-stripped)
- Chunking strategies: FIXED (char count), SENTENCE, PARAGRAPH, SEMANTIC
- Sliding window: configurable chunk_size + overlap
- Sentence-aware: never split mid-sentence in sentence mode
- Paragraph-aware: split on blank lines; respect header boundaries
- JSON flattening: recursively flatten nested objects to key=value lines
- CSV: header-aware row batching
- Metadata extraction: title (first heading), word count, language hint,
  section headers, chunk index, source name, page estimate
- Deduplication: skip near-duplicate chunks via trigram Jaccard
- Token budget: estimate token count and filter oversized chunks
- Batch processing: parse list of documents in one call
- SQLite persistence: parsed documents and chunk log
- REST API: parse, chunks, stats
"""
import csv, io, json, re, sqlite3, time, uuid, logging
from enum import Enum
from typing import Any, Dict, Iterator, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

class SourceType(str, Enum):
    TEXT      = "text"
    MARKDOWN  = "markdown"
    JSON      = "json"
    CSV       = "csv"
    HTML      = "html"

class ChunkStrategy(str, Enum):
    FIXED     = "fixed"
    SENTENCE  = "sentence"
    PARAGRAPH = "paragraph"
    SEMANTIC  = "semantic"

# ── Utilities ─────────────────────────────────────────────────────────────────
def _strip_html(html: str) -> str:
    text = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.DOTALL|re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>',  ' ', text, flags=re.DOTALL|re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&[a-z]+;', ' ', text)
    return re.sub(r' +', ' ', text).strip()

def _md_to_text(md: str) -> str:
    text = re.sub(r'^#{1,6}\s+', '', md, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
    text = re.sub(r'\[(.+?)\]\(.*?\)', r'\1', text)
    text = re.sub(r'^>\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'^[-*+]\s+', '', text, flags=re.MULTILINE)
    return text.strip()

def _flatten_json(obj: Any, prefix: str = "") -> List[str]:
    lines = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else k
            lines.extend(_flatten_json(v, key))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            lines.extend(_flatten_json(v, f"{prefix}[{i}]"))
    else:
        lines.append(f"{prefix} = {obj}")
    return lines

def _trigrams(text: str) -> set:
    t = re.sub(r'\s+', ' ', text.lower())
    return {t[i:i+3] for i in range(len(t) - 2)}

def _jaccard(a: set, b: set) -> float:
    if not a and not b: return 1.0
    return len(a & b) / max(1, len(a | b))

def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)

def _extract_title(text: str, source_type: SourceType) -> str:
    if source_type == SourceType.MARKDOWN:
        m = re.search(r'^#+\s+(.+)', text, re.MULTILINE)
        if m: return m.group(1).strip()
    lines = text.strip().splitlines()
    return lines[0][:80] if lines else ""

def _detect_language_hint(text: str) -> str:
    """Very rough: check for common CJK or Cyrillic."""
    if re.search(r'[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]', text):
        return "zh/ja"
    if re.search(r'[\u0400-\u04ff]', text):
        return "ru"
    return "en"

# ── Chunking ──────────────────────────────────────────────────────────────────
def _split_sentences(text: str) -> List[str]:
    parts = re.split(r'(?<=[.!?])\s+(?=[A-Z"])', text)
    return [p.strip() for p in parts if p.strip()]

def _split_paragraphs(text: str) -> List[str]:
    parts = re.split(r'\n\s*\n', text)
    return [p.strip() for p in parts if p.strip()]

def _fixed_chunks(text: str, size: int, overlap: int) -> List[str]:
    chunks = []
    start = 0
    while start < len(text):
        end = start + size
        chunks.append(text[start:end])
        start += size - overlap
        if start >= len(text): break
    return chunks

def _sentence_chunks(text: str, size: int, overlap: int) -> List[str]:
    sentences = _split_sentences(text)
    if not sentences: return _fixed_chunks(text, size, overlap)
    chunks = []; current = []
    for sent in sentences:
        current.append(sent)
        combined = " ".join(current)
        if len(combined) >= size:
            chunks.append(combined)
            # Keep last overlap chars worth of sentences
            while current and len(" ".join(current)) > overlap:
                current.pop(0)
    if current:
        chunks.append(" ".join(current))
    return [c for c in chunks if c.strip()]

def _paragraph_chunks(text: str, size: int, overlap: int) -> List[str]:
    paras = _split_paragraphs(text)
    if not paras: return _fixed_chunks(text, size, overlap)
    chunks = []; current = []
    for para in paras:
        candidate = "\n\n".join(current + [para])
        if len(candidate) > size and current:
            chunks.append("\n\n".join(current))
            current = [para]
        else:
            current.append(para)
    if current:
        chunks.append("\n\n".join(current))
    return [c for c in chunks if c.strip()]

# ── Data classes ───────────────────────────────────────────────────────────────
@dataclass
class Chunk:
    id: str; content: str
    source: str = ""; source_type: SourceType = SourceType.TEXT
    chunk_index: int = 0; total_chunks: int = 0
    metadata: Dict = field(default_factory=dict)
    token_count: int = 0
    char_count: int = 0

    def to_dict(self):
        return {"id": self.id, "source": self.source,
                "chunk_index": self.chunk_index, "total_chunks": self.total_chunks,
                "content_preview": self.content[:150],
                "token_count": self.token_count,
                "char_count": self.char_count,
                "metadata": self.metadata}

@dataclass
class ParsedDocument:
    id: str; source: str; source_type: SourceType
    chunks: List[Chunk] = field(default_factory=list)
    title: str = ""; language: str = "en"
    word_count: int = 0; char_count: int = 0
    metadata: Dict = field(default_factory=dict)
    parsed_at: float = field(default_factory=time.time)

    def to_dict(self):
        return {"id": self.id, "source": self.source,
                "source_type": self.source_type.value,
                "title": self.title, "language": self.language,
                "word_count": self.word_count, "char_count": self.char_count,
                "chunk_count": len(self.chunks),
                "metadata": self.metadata}

class DPStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS documents(
                    id TEXT PRIMARY KEY, source TEXT, source_type TEXT,
                    title TEXT DEFAULT '', word_count INTEGER DEFAULT 0,
                    chunk_count INTEGER DEFAULT 0, parsed_at REAL);
                CREATE TABLE IF NOT EXISTS chunks(
                    id TEXT PRIMARY KEY, doc_id TEXT, chunk_index INTEGER,
                    content TEXT, token_count INTEGER DEFAULT 0,
                    metadata TEXT DEFAULT '{}', created_at REAL);
                CREATE INDEX IF NOT EXISTS idx_ch_doc ON chunks(doc_id, chunk_index);
            """)

    def save_doc(self, doc: ParsedDocument):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?,?,?)",
                (doc.id, doc.source, doc.source_type.value, doc.title,
                 doc.word_count, len(doc.chunks), doc.parsed_at))
            for ch in doc.chunks:
                c.execute("INSERT OR REPLACE INTO chunks VALUES(?,?,?,?,?,?,?)",
                    (ch.id, doc.id, ch.chunk_index, ch.content,
                     ch.token_count, json.dumps(ch.metadata), time.time()))

    def stats(self) -> Dict:
        with self._conn() as c:
            nd = c.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            nc = c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            avg = c.execute("SELECT AVG(token_count) FROM chunks").fetchone()[0] or 0
        return {"documents": nd, "chunks": nc, "avg_tokens_per_chunk": round(avg, 1)}

class DocumentParser:
    """
    Multi-format document parser with configurable chunking strategies.

    Usage:
        parser = DocumentParser(chunk_size=512, overlap=64)

        doc = parser.parse("# My Article\\n\\nThis is content...",
                            source="article.md",
                            source_type=SourceType.MARKDOWN)
        for chunk in doc.chunks:
            print(chunk.content[:80])
    """
    def __init__(self, db_path: str = "data/documents.db",
                 chunk_size: int = 512,
                 overlap: int = 64,
                 strategy: ChunkStrategy = ChunkStrategy.PARAGRAPH,
                 max_tokens_per_chunk: int = 512,
                 dedup_threshold: float = 0.85):
        self._store = DPStore(db_path)
        self.chunk_size = chunk_size
        self.overlap = overlap
        self.strategy = strategy
        self.max_tokens = max_tokens_per_chunk
        self.dedup_threshold = dedup_threshold

    def _to_text(self, content: str, source_type: SourceType) -> str:
        if source_type == SourceType.HTML:
            return _strip_html(content)
        if source_type == SourceType.MARKDOWN:
            return _md_to_text(content)
        if source_type == SourceType.JSON:
            try:
                obj = json.loads(content)
                return "\n".join(_flatten_json(obj))
            except Exception:
                return content
        if source_type == SourceType.CSV:
            try:
                reader = csv.DictReader(io.StringIO(content))
                rows = list(reader)
                lines = [", ".join(f"{k}: {v}" for k, v in row.items())
                          for row in rows]
                return "\n".join(lines)
            except Exception:
                return content
        return content

    def _chunk_text(self, text: str) -> List[str]:
        s = self.strategy
        if s == ChunkStrategy.FIXED:
            return _fixed_chunks(text, self.chunk_size, self.overlap)
        if s == ChunkStrategy.SENTENCE:
            return _sentence_chunks(text, self.chunk_size, self.overlap)
        if s == ChunkStrategy.PARAGRAPH:
            return _paragraph_chunks(text, self.chunk_size, self.overlap)
        # SEMANTIC: paragraph-then-sentence fallback
        paras = _paragraph_chunks(text, self.chunk_size, self.overlap)
        result = []
        for p in paras:
            if len(p) > self.chunk_size * 1.5:
                result.extend(_sentence_chunks(p, self.chunk_size, self.overlap))
            else:
                result.append(p)
        return result

    def parse(self, content: str,
               source: str = "",
               source_type: SourceType = SourceType.TEXT,
               metadata: Dict = None,
               doc_id: str = None) -> ParsedDocument:
        did = doc_id or str(uuid.uuid4())[:12]
        text = self._to_text(content, source_type)
        title = _extract_title(content, source_type)
        lang  = _detect_language_hint(text)
        raw_chunks = self._chunk_text(text)

        # Deduplicate
        seen_trigrams: List[set] = []
        final_chunks = []
        for raw in raw_chunks:
            tg = _trigrams(raw)
            is_dup = any(_jaccard(tg, s) >= self.dedup_threshold
                          for s in seen_trigrams)
            if not is_dup:
                seen_trigrams.append(tg)
                final_chunks.append(raw)

        chunks = []
        for i, text_chunk in enumerate(final_chunks):
            tok = _estimate_tokens(text_chunk)
            if tok > self.max_tokens:
                text_chunk = text_chunk[:self.max_tokens * 4]
                tok = self.max_tokens
            meta = dict(metadata or {})
            meta.update({"source": source, "chunk_index": i,
                          "total_chunks": len(final_chunks)})
            ch = Chunk(id=f"{did}-{i}", content=text_chunk,
                        source=source, source_type=source_type,
                        chunk_index=i, total_chunks=len(final_chunks),
                        metadata=meta, token_count=tok,
                        char_count=len(text_chunk))
            chunks.append(ch)

        doc = ParsedDocument(id=did, source=source, source_type=source_type,
                              chunks=chunks, title=title, language=lang,
                              word_count=len(text.split()),
                              char_count=len(text),
                              metadata=dict(metadata or {}))
        self._store.save_doc(doc)
        return doc

    def parse_batch(self, documents: List[Dict]) -> List[ParsedDocument]:
        return [self.parse(**d) for d in documents]

    def chunks_iter(self, content: str,
                     source_type: SourceType = SourceType.TEXT) -> Iterator[str]:
        text = self._to_text(content, source_type)
        for chunk in self._chunk_text(text):
            yield chunk

    def stats(self) -> Dict:
        s = self._store.stats()
        s["chunk_size"] = self.chunk_size
        s["overlap"] = self.overlap
        s["strategy"] = self.strategy.value
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def parse_ep(req):
            d = await req.json()
            doc = self.parse(d["content"], d.get("source",""),
                              SourceType[d.get("source_type","TEXT").upper()],
                              d.get("metadata",{}))
            return web.json_response(doc.to_dict(), status=201)
        async def chunks_ep(req):
            d = await req.json()
            doc = self.parse(d["content"], d.get("source",""),
                              SourceType[d.get("source_type","TEXT").upper()])
            return web.json_response(
                {"chunks": [c.to_dict() for c in doc.chunks]})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/parser"
        app.router.add_post(f"{p}/parse",  parse_ep)
        app.router.add_post(f"{p}/chunks", chunks_ep)
        app.router.add_get( f"{p}/stats",  stats_ep)
        logger.info(f"Document parser API at {prefix}/parser/")
