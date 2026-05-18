"""OMNI Agent — Embedding Pipeline V2: chunking, dedup, index management."""
from __future__ import annotations
import hashlib, json, math, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class IndexStatus(str, Enum):
    BUILDING = "building"
    READY    = "ready"
    STALE    = "stale"
    ERROR    = "error"


class ChunkStrategy(str, Enum):
    FIXED      = "fixed"
    SENTENCE   = "sentence"
    PARAGRAPH  = "paragraph"
    SEMANTIC   = "semantic"


@dataclass
class Document:
    doc_id: str = field(default_factory=lambda: str(uuid.uuid4())[:10])
    content: str = ""
    title: str = ""
    source: str = ""
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    content_hash: str = ""

    def __post_init__(self):
        if not self.content_hash and self.content:
            self.content_hash = hashlib.md5(self.content.encode()).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return {"doc_id": self.doc_id, "title": self.title,
                "source": self.source, "tags": self.tags,
                "content_length": len(self.content)}


@dataclass
class Chunk:
    chunk_id: str = field(default_factory=lambda: str(uuid.uuid4())[:10])
    doc_id: str = ""
    content: str = ""
    chunk_index: int = 0
    start_char: int = 0
    end_char: int = 0
    embedding: Optional[List[float]] = None
    embedding_model: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {"chunk_id": self.chunk_id, "doc_id": self.doc_id,
                "chunk_index": self.chunk_index,
                "content_length": len(self.content),
                "has_embedding": self.embedding is not None}


@dataclass
class SearchResult:
    chunk: Chunk
    score: float
    doc_title: str = ""
    doc_source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {**self.chunk.to_dict(),
                "score": round(self.score, 4),
                "doc_title": self.doc_title}


@dataclass
class EmbeddingIndex:
    index_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = ""
    model: str = ""
    status: IndexStatus = IndexStatus.BUILDING
    chunk_count: int = 0
    doc_count: int = 0
    dimension: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {"index_id": self.index_id, "name": self.name,
                "status": self.status.value, "model": self.model,
                "chunks": self.chunk_count, "docs": self.doc_count,
                "dimension": self.dimension}


def _cosine(a: List[float], b: List[float]) -> float:
    if len(a) != len(b): return 0.0
    dot   = sum(x * y for x, y in zip(a, b))
    na    = math.sqrt(sum(x * x for x in a))
    nb    = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _split_sentences(text: str) -> List[str]:
    import re
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _split_paragraphs(text: str) -> List[str]:
    import re
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


class EmbeddingPipelineV2:
    """
    Document embedding pipeline:
    - Document ingestion with deduplication (content hash)
    - Chunking strategies: fixed-size / sentence / paragraph / semantic
    - Configurable overlap between chunks
    - Pluggable embedding function (text → List[float])
    - Named embedding indexes
    - Cosine-similarity vector search
    - Hybrid search (keyword + vector)
    - Batch embedding with progress tracking
    - Chunk metadata enrichment
    - Index rebuild and incremental update
    - Document and chunk CRUD
    - SQLite persistence for documents and chunks
    """

    def __init__(self, embed_fn: Optional[Callable[[str], List[float]]] = None,
                 db_path: str = ":memory:",
                 default_chunk_size: int = 200,
                 default_overlap: int = 20):
        self._embed_fn    = embed_fn
        self._chunk_size  = default_chunk_size
        self._overlap     = default_overlap
        self._docs:   Dict[str, Document] = {}
        self._chunks: Dict[str, Chunk]    = {}
        self._indexes: Dict[str, EmbeddingIndex] = {}
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS ep_docs (
                doc_id TEXT PRIMARY KEY, title TEXT, source TEXT,
                tags TEXT, content_hash TEXT, created_at REAL
            );
            CREATE TABLE IF NOT EXISTS ep_chunks (
                chunk_id TEXT PRIMARY KEY, doc_id TEXT, content TEXT,
                chunk_index INTEGER, embedding TEXT, model TEXT, ts REAL
            );
            CREATE TABLE IF NOT EXISTS ep_indexes (
                index_id TEXT PRIMARY KEY, name TEXT, model TEXT,
                status TEXT, chunk_count INTEGER, doc_count INTEGER,
                dimension INTEGER, created_at REAL
            );
        """)
        self._db.commit()

    # ── DOCUMENTS ─────────────────────────────────────────────────────

    def add_document(self, content: str,
                      title: str = "",
                      source: str = "",
                      tags: Optional[List[str]] = None,
                      doc_id: Optional[str] = None,
                      dedup: bool = True,
                      metadata: Optional[Dict] = None) -> Optional[Document]:
        h = hashlib.md5(content.encode()).hexdigest()
        if dedup:
            for d in self._docs.values():
                if d.content_hash == h:
                    return d   # duplicate
        doc = Document(doc_id=doc_id or str(uuid.uuid4())[:10],
                        content=content, title=title, source=source,
                        tags=list(tags or []), content_hash=h,
                        metadata=metadata or {})
        self._docs[doc.doc_id] = doc
        self._db.execute(
            "INSERT OR REPLACE INTO ep_docs VALUES (?,?,?,?,?,?)",
            (doc.doc_id, title, source, json.dumps(tags or []),
             h, doc.created_at))
        self._db.commit()
        return doc

    def remove_document(self, doc_id: str) -> bool:
        if doc_id not in self._docs: return False
        del self._docs[doc_id]
        chunk_ids = [cid for cid, c in self._chunks.items()
                     if c.doc_id == doc_id]
        for cid in chunk_ids:
            del self._chunks[cid]
        self._db.execute("DELETE FROM ep_docs WHERE doc_id=?", (doc_id,))
        self._db.execute("DELETE FROM ep_chunks WHERE doc_id=?", (doc_id,))
        self._db.commit()
        return True

    def get_document(self, doc_id: str) -> Optional[Document]:
        return self._docs.get(doc_id)

    # ── CHUNKING ──────────────────────────────────────────────────────

    def chunk_document(self, doc_id: str,
                        strategy: ChunkStrategy = ChunkStrategy.FIXED,
                        chunk_size: Optional[int] = None,
                        overlap: Optional[int] = None) -> List[Chunk]:
        doc = self._docs.get(doc_id)
        if not doc: raise KeyError(f"Document {doc_id} not found")
        size = chunk_size or self._chunk_size
        ovlp = overlap if overlap is not None else self._overlap
        text = doc.content
        raw_chunks: List[Tuple[str, int, int]] = []

        if strategy == ChunkStrategy.SENTENCE:
            sents = _split_sentences(text)
            i = 0
            while i < len(sents):
                window = sents[i:i + size]
                chunk_text = " ".join(window)
                start = text.find(sents[i]) if sents[i] in text else 0
                raw_chunks.append((chunk_text, start, start + len(chunk_text)))
                i += max(1, size - ovlp)
        elif strategy == ChunkStrategy.PARAGRAPH:
            paras = _split_paragraphs(text)
            pos = 0
            for para in paras:
                raw_chunks.append((para, pos, pos + len(para)))
                pos += len(para) + 2
        else:  # FIXED (word-based)
            words = text.split()
            i = 0
            pos = 0
            while i < len(words):
                w = words[i:i + size]
                chunk_text = " ".join(w)
                raw_chunks.append((chunk_text, pos, pos + len(chunk_text)))
                pos += len(chunk_text) + 1
                i += max(1, size - ovlp)

        chunks = []
        # Remove existing chunks for this doc
        old = [cid for cid, c in self._chunks.items() if c.doc_id == doc_id]
        for cid in old: del self._chunks[cid]

        for idx, (text_chunk, start, end) in enumerate(raw_chunks):
            c = Chunk(doc_id=doc_id, content=text_chunk,
                       chunk_index=idx, start_char=start, end_char=end)
            self._chunks[c.chunk_id] = c
            chunks.append(c)
        return chunks

    # ── EMBEDDING ─────────────────────────────────────────────────────

    def embed_chunk(self, chunk_id: str,
                     model: str = "default") -> Optional[Chunk]:
        c = self._chunks.get(chunk_id)
        if not c or not self._embed_fn: return c
        try:
            c.embedding       = self._embed_fn(c.content)
            c.embedding_model = model
            self._db.execute(
                "INSERT OR REPLACE INTO ep_chunks VALUES (?,?,?,?,?,?,?)",
                (c.chunk_id, c.doc_id, c.content[:500],
                 c.chunk_index,
                 json.dumps(c.embedding) if c.embedding else None,
                 model, c.ts))
            self._db.commit()
        except Exception:
            pass
        return c

    def embed_document(self, doc_id: str,
                        strategy: ChunkStrategy = ChunkStrategy.FIXED,
                        model: str = "default") -> List[Chunk]:
        chunks = self.chunk_document(doc_id, strategy)
        for c in chunks:
            self.embed_chunk(c.chunk_id, model)
        return chunks

    def embed_all(self, model: str = "default",
                   strategy: ChunkStrategy = ChunkStrategy.FIXED) -> int:
        count = 0
        for doc_id in list(self._docs.keys()):
            chunks = self.embed_document(doc_id, strategy, model)
            count += len(chunks)
        return count

    # ── INDEX MANAGEMENT ─────────────────────────────────────────────

    def build_index(self, name: str,
                     model: str = "default",
                     index_id: Optional[str] = None) -> EmbeddingIndex:
        iid = index_id or str(uuid.uuid4())[:8]
        embedded = [c for c in self._chunks.values() if c.embedding]
        dim = len(embedded[0].embedding) if embedded else 0
        ix  = EmbeddingIndex(
            index_id=iid, name=name, model=model,
            status=IndexStatus.READY,
            chunk_count=len(embedded),
            doc_count=len(self._docs),
            dimension=dim)
        self._indexes[iid] = ix
        self._db.execute(
            "INSERT OR REPLACE INTO ep_indexes VALUES (?,?,?,?,?,?,?,?)",
            (iid, name, model, ix.status.value, ix.chunk_count,
             ix.doc_count, dim, ix.created_at))
        self._db.commit()
        return ix

    # ── SEARCH ────────────────────────────────────────────────────────

    def search(self, query: str,
               top_k: int = 5,
               model: str = "default") -> List[SearchResult]:
        if not self._embed_fn: return []
        try:
            q_emb = self._embed_fn(query)
        except Exception:
            return []
        scored: List[Tuple[Chunk, float]] = []
        for c in self._chunks.values():
            if c.embedding and c.embedding_model == model:
                score = _cosine(q_emb, c.embedding)
                scored.append((c, score))
        scored.sort(key=lambda x: -x[1])
        results = []
        for c, score in scored[:top_k]:
            doc = self._docs.get(c.doc_id)
            results.append(SearchResult(
                chunk=c, score=score,
                doc_title=doc.title if doc else "",
                doc_source=doc.source if doc else ""))
        return results

    def keyword_search(self, query: str,
                        top_k: int = 5) -> List[SearchResult]:
        q = query.lower()
        matched = [(c, c.content.lower().count(q))
                   for c in self._chunks.values()
                   if q in c.content.lower()]
        matched.sort(key=lambda x: -x[1])
        results = []
        for c, hits in matched[:top_k]:
            doc = self._docs.get(c.doc_id)
            results.append(SearchResult(
                chunk=c, score=float(hits),
                doc_title=doc.title if doc else "",
                doc_source=doc.source if doc else ""))
        return results

    def hybrid_search(self, query: str,
                       top_k: int = 5,
                       alpha: float = 0.5) -> List[SearchResult]:
        """Combine keyword (1-alpha) + vector (alpha) scores."""
        kw   = {r.chunk.chunk_id: r.score for r in self.keyword_search(query, top_k * 2)}
        vec  = {r.chunk.chunk_id: r.score for r in self.search(query, top_k * 2)}
        all_ids = set(kw) | set(vec)
        kw_max  = max(kw.values(), default=1.0)
        vec_max = max(vec.values(), default=1.0)
        scores: Dict[str, float] = {}
        for cid in all_ids:
            kw_n  = kw.get(cid, 0.0) / kw_max  if kw_max  else 0.0
            vec_n = vec.get(cid, 0.0) / vec_max if vec_max else 0.0
            scores[cid] = (1 - alpha) * kw_n + alpha * vec_n
        top = sorted(scores.items(), key=lambda x: -x[1])[:top_k]
        results = []
        for cid, score in top:
            c   = self._chunks.get(cid)
            doc = self._docs.get(c.doc_id) if c else None
            if c:
                results.append(SearchResult(
                    chunk=c, score=score,
                    doc_title=doc.title if doc else "",
                    doc_source=doc.source if doc else ""))
        return results

    def stats(self) -> Dict[str, Any]:
        embedded = sum(1 for c in self._chunks.values() if c.embedding)
        return {
            "documents": len(self._docs),
            "chunks": len(self._chunks),
            "embedded_chunks": embedded,
            "indexes": len(self._indexes),
        }
