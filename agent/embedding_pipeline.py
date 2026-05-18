"""OMNI Agent — Embedding Pipeline: chunking, embedding, indexing, nearest-neighbor search."""
from __future__ import annotations
import hashlib, math, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


# ── MATH UTILS ────────────────────────────────────────────────────────────────

def _dot(a: List[float], b: List[float]) -> float:
    return sum(x * y for x, y in zip(a, b))

def _norm(v: List[float]) -> float:
    s = sum(x * x for x in v)
    return math.sqrt(s) if s > 0 else 0.0

def cosine_sim(a: List[float], b: List[float]) -> float:
    na, nb = _norm(a), _norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return _dot(a, b) / (na * nb)

def euclidean_dist(a: List[float], b: List[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


# ── CHUNKING ──────────────────────────────────────────────────────────────────

class ChunkStrategy(str, Enum):
    FIXED_CHARS  = "fixed_chars"
    FIXED_WORDS  = "fixed_words"
    SENTENCE     = "sentence"
    PARAGRAPH    = "paragraph"
    RECURSIVE    = "recursive"


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    text: str
    index: int          # position within document
    start_char: int
    end_char: int
    metadata: Dict[str, Any] = field(default_factory=dict)
    embedding: Optional[List[float]] = None

    @property
    def token_estimate(self) -> int:
        return len(self.text.split())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "text": self.text[:80] + "…" if len(self.text) > 80 else self.text,
            "index": self.index,
            "token_estimate": self.token_estimate,
            "has_embedding": self.embedding is not None,
        }


class TextChunker:
    """Splits documents into chunks using multiple strategies."""

    def __init__(self, strategy: ChunkStrategy = ChunkStrategy.SENTENCE,
                 chunk_size: int = 200, overlap: int = 20):
        self.strategy  = strategy
        self.chunk_size = chunk_size
        self.overlap    = overlap

    def chunk(self, text: str, doc_id: str = "",
              metadata: Optional[Dict] = None) -> List[Chunk]:
        doc_id = doc_id or str(uuid.uuid4())[:8]
        meta   = metadata or {}
        if self.strategy == ChunkStrategy.FIXED_CHARS:
            return self._fixed_chars(text, doc_id, meta)
        if self.strategy == ChunkStrategy.FIXED_WORDS:
            return self._fixed_words(text, doc_id, meta)
        if self.strategy == ChunkStrategy.SENTENCE:
            return self._sentence(text, doc_id, meta)
        if self.strategy == ChunkStrategy.PARAGRAPH:
            return self._paragraph(text, doc_id, meta)
        if self.strategy == ChunkStrategy.RECURSIVE:
            return self._recursive(text, doc_id, meta)
        return self._sentence(text, doc_id, meta)

    def _make(self, text: str, doc_id: str, idx: int,
              start: int, end: int, meta: Dict) -> Chunk:
        return Chunk(
            chunk_id=str(uuid.uuid4()),
            doc_id=doc_id, text=text,
            index=idx, start_char=start, end_char=end,
            metadata=dict(meta))

    def _fixed_chars(self, text: str, doc_id: str, meta: Dict) -> List[Chunk]:
        chunks, i, idx = [], 0, 0
        while i < len(text):
            end = min(i + self.chunk_size, len(text))
            chunks.append(self._make(text[i:end], doc_id, idx, i, end, meta))
            i += self.chunk_size - self.overlap
            idx += 1
        return chunks

    def _fixed_words(self, text: str, doc_id: str, meta: Dict) -> List[Chunk]:
        words  = text.split()
        chunks, i, idx = [], 0, 0
        while i < len(words):
            end   = min(i + self.chunk_size, len(words))
            chunk_text = " ".join(words[i:end])
            start_char = text.find(words[i]) if words[i:i+1] else 0
            chunks.append(self._make(chunk_text, doc_id, idx, start_char,
                                     start_char + len(chunk_text), meta))
            i  += self.chunk_size - self.overlap
            idx += 1
        return chunks

    def _sentence(self, text: str, doc_id: str, meta: Dict) -> List[Chunk]:
        import re
        sents = re.split(r'(?<=[.!?])\s+', text.strip())
        chunks, buf, buf_start, start, idx = [], [], 0, 0, 0
        pos = 0
        for sent in sents:
            buf.append(sent)
            joined = " ".join(buf)
            if len(joined.split()) >= self.chunk_size:
                chunks.append(self._make(joined, doc_id, idx, pos - len(joined),
                                         pos, meta))
                buf  = buf[-max(1, self.overlap // 10):]
                idx += 1
            pos += len(sent) + 1
        if buf:
            chunks.append(self._make(" ".join(buf), doc_id, idx,
                                     max(0, pos - len(" ".join(buf))), pos, meta))
        return chunks or [self._make(text, doc_id, 0, 0, len(text), meta)]

    def _paragraph(self, text: str, doc_id: str, meta: Dict) -> List[Chunk]:
        paras = [p.strip() for p in text.split("\n\n") if p.strip()]
        pos   = 0
        chunks = []
        for idx, para in enumerate(paras):
            chunks.append(self._make(para, doc_id, idx, pos, pos + len(para), meta))
            pos += len(para) + 2
        return chunks or [self._make(text, doc_id, 0, 0, len(text), meta)]

    def _recursive(self, text: str, doc_id: str, meta: Dict,
                   depth: int = 0) -> List[Chunk]:
        """Recursively split: paragraph → sentence → word."""
        if len(text.split()) <= self.chunk_size or depth > 2:
            return [self._make(text.strip(), doc_id, 0, 0, len(text), meta)]
        seps = ["\n\n", ". ", " "]
        for sep in seps:
            parts = text.split(sep)
            if len(parts) > 1:
                chunks, idx = [], 0
                for part in parts:
                    sub = self._recursive(part, doc_id, meta, depth + 1)
                    for c in sub:
                        c.index = idx; idx += 1
                    chunks.extend(sub)
                return chunks
        return [self._make(text, doc_id, 0, 0, len(text), meta)]


# ── EMBEDDING STORE ───────────────────────────────────────────────────────────

@dataclass
class SearchResult:
    chunk: Chunk
    score: float
    rank: int

    def to_dict(self) -> Dict[str, Any]:
        return {**self.chunk.to_dict(), "score": round(self.score, 4), "rank": self.rank}


class EmbeddingPipeline:
    """
    End-to-end pipeline: chunk → embed → index → search.
    Uses a pluggable embed_fn; falls back to a deterministic hash-based mock.
    Supports cosine and euclidean nearest-neighbor search.
    """

    def __init__(
        self,
        embed_fn: Optional[Callable[[str], List[float]]] = None,
        chunk_strategy: ChunkStrategy = ChunkStrategy.SENTENCE,
        chunk_size: int = 100,
        overlap: int = 10,
        dim: int = 16,
        db_path: str = ":memory:",
    ):
        self.embed_fn = embed_fn or self._hash_embed(dim)
        self.chunker  = TextChunker(chunk_strategy, chunk_size, overlap)
        self.dim      = dim
        self._chunks: Dict[str, Chunk] = {}          # chunk_id → Chunk
        self._by_doc: Dict[str, List[str]] = {}       # doc_id   → [chunk_id]
        self._docs:   Dict[str, Dict]      = {}       # doc_id   → metadata
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    @staticmethod
    def _hash_embed(dim: int) -> Callable[[str], List[float]]:
        """Deterministic mock embedder using MD5 → float vector."""
        def embed(text: str) -> List[float]:
            h = hashlib.md5(text.encode()).digest()
            # Extend to dim floats by repeating hash bytes
            raw = list(h) * (dim // 16 + 1)
            vec = [(b / 127.5) - 1.0 for b in raw[:dim]]
            n   = _norm(vec)
            return [x / n for x in vec] if n > 0 else vec
        return embed

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS ep_docs (
                doc_id TEXT PRIMARY KEY, title TEXT,
                added_at REAL, chunk_count INTEGER, metadata TEXT
            );
            CREATE TABLE IF NOT EXISTS ep_chunks (
                chunk_id TEXT PRIMARY KEY, doc_id TEXT,
                text TEXT, idx INTEGER, token_est INTEGER
            );
        """)
        self._db.commit()

    # ── INGEST ────────────────────────────────────────────────────────

    def ingest(self, text: str, doc_id: Optional[str] = None,
               title: str = "", metadata: Optional[Dict] = None) -> List[Chunk]:
        import json
        doc_id = doc_id or str(uuid.uuid4())[:12]
        meta   = metadata or {}
        chunks = self.chunker.chunk(text, doc_id, meta)
        for chunk in chunks:
            chunk.embedding = self.embed_fn(chunk.text)
            self._chunks[chunk.chunk_id] = chunk
        self._by_doc[doc_id] = [c.chunk_id for c in chunks]
        self._docs[doc_id]   = {"title": title, "metadata": meta}
        self._db.execute(
            "INSERT OR REPLACE INTO ep_docs VALUES (?,?,?,?,?)",
            (doc_id, title, time.time(), len(chunks), json.dumps(meta)))
        for c in chunks:
            self._db.execute(
                "INSERT OR REPLACE INTO ep_chunks VALUES (?,?,?,?,?)",
                (c.chunk_id, doc_id, c.text, c.index, c.token_estimate))
        self._db.commit()
        return chunks

    def delete_doc(self, doc_id: str) -> int:
        ids = self._by_doc.pop(doc_id, [])
        for cid in ids:
            self._chunks.pop(cid, None)
        self._docs.pop(doc_id, None)
        self._db.execute("DELETE FROM ep_docs WHERE doc_id=?", (doc_id,))
        self._db.execute("DELETE FROM ep_chunks WHERE doc_id=?", (doc_id,))
        self._db.commit()
        return len(ids)

    # ── EMBED ─────────────────────────────────────────────────────────

    def embed(self, text: str) -> List[float]:
        return self.embed_fn(text)

    def reembed_doc(self, doc_id: str) -> int:
        ids = self._by_doc.get(doc_id, [])
        for cid in ids:
            chunk = self._chunks.get(cid)
            if chunk:
                chunk.embedding = self.embed_fn(chunk.text)
        return len(ids)

    # ── SEARCH ────────────────────────────────────────────────────────

    def search(self, query: str, top_k: int = 5,
               metric: str = "cosine",
               doc_filter: Optional[List[str]] = None) -> List[SearchResult]:
        q_emb = self.embed_fn(query)
        candidates = []
        for cid, chunk in self._chunks.items():
            if chunk.embedding is None:
                continue
            if doc_filter and chunk.doc_id not in doc_filter:
                continue
            if len(chunk.embedding) != len(q_emb):
                continue
            if metric == "cosine":
                score = cosine_sim(q_emb, chunk.embedding)
            else:
                score = -euclidean_dist(q_emb, chunk.embedding)
            candidates.append((chunk, score))
        candidates.sort(key=lambda x: x[1], reverse=True)
        return [
            SearchResult(chunk=c, score=s, rank=i + 1)
            for i, (c, s) in enumerate(candidates[:top_k])
        ]

    def search_by_embedding(self, embedding: List[float],
                             top_k: int = 5) -> List[SearchResult]:
        candidates = []
        for chunk in self._chunks.values():
            if chunk.embedding and len(chunk.embedding) == len(embedding):
                score = cosine_sim(embedding, chunk.embedding)
                candidates.append((chunk, score))
        candidates.sort(key=lambda x: x[1], reverse=True)
        return [SearchResult(chunk=c, score=s, rank=i + 1)
                for i, (c, s) in enumerate(candidates[:top_k])]

    # ── INTROSPECTION ─────────────────────────────────────────────────

    def get_chunk(self, chunk_id: str) -> Optional[Chunk]:
        return self._chunks.get(chunk_id)

    def get_doc_chunks(self, doc_id: str) -> List[Chunk]:
        return [self._chunks[cid] for cid in self._by_doc.get(doc_id, [])
                if cid in self._chunks]

    def list_docs(self) -> List[Dict[str, Any]]:
        return [{"doc_id": did, **info,
                 "chunk_count": len(self._by_doc.get(did, []))}
                for did, info in self._docs.items()]

    def stats(self) -> Dict[str, Any]:
        embedded = sum(1 for c in self._chunks.values() if c.embedding)
        return {
            "docs": len(self._docs),
            "chunks": len(self._chunks),
            "embedded": embedded,
            "dim": self.dim,
            "strategy": self.chunker.strategy.value,
        }
