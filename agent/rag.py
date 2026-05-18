"""
OMNI AGENT - RAG Pipeline
Retrieval-Augmented Generation: ingest documents, chunk, embed, store, retrieve.

Storage backend: SQLite with JSON-serialized embeddings (no external vector DB needed).
Upgrade path: swap _VectorStore for pgvector, Qdrant, or Chroma by replacing the
              _similarity_search() method.

Supported ingestion formats: .txt .md .py .json .csv  (+ raw strings)
"""
import re
import os
import csv
import json
import math
import time
import hashlib
import logging
import sqlite3
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# DATA MODELS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Chunk:
    """A single text chunk with metadata."""
    id: str
    doc_id: str
    text: str
    index: int            # chunk index within document
    metadata: Dict = field(default_factory=dict)
    embedding: List[float] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "doc_id": self.doc_id,
            "text": self.text, "index": self.index,
            "metadata": self.metadata,
        }


@dataclass
class Document:
    """An ingested document."""
    id: str
    title: str
    source: str           # file path or URL
    doc_type: str         # txt, md, py, json, csv, raw
    total_chunks: int
    metadata: Dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "title": self.title, "source": self.source,
            "doc_type": self.doc_type, "total_chunks": self.total_chunks,
            "metadata": self.metadata, "created_at": self.created_at,
        }


@dataclass
class RetrievalResult:
    """A retrieved chunk with relevance score."""
    chunk: Chunk
    score: float          # cosine similarity 0-1
    rank: int

    def to_dict(self) -> Dict:
        return {**self.chunk.to_dict(), "score": round(self.score, 4), "rank": self.rank}


# ══════════════════════════════════════════════════════════════════════════════
# CHUNKER
# ══════════════════════════════════════════════════════════════════════════════

class TextChunker:
    """Splits documents into overlapping chunks for embedding."""

    def __init__(self, chunk_size: int = 512, chunk_overlap: int = 64):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def chunk_text(self, text: str, doc_id: str,
                   metadata: Dict = None) -> List[Chunk]:
        """Split text into overlapping word-based chunks."""
        words = text.split()
        chunks = []
        start = 0
        idx = 0

        while start < len(words):
            end = min(start + self.chunk_size, len(words))
            chunk_text = " ".join(words[start:end])
            chunk_id = hashlib.sha256(
                f"{doc_id}:{idx}:{chunk_text[:64]}".encode()
            ).hexdigest()[:16]

            chunks.append(Chunk(
                id=chunk_id, doc_id=doc_id,
                text=chunk_text, index=idx,
                metadata=metadata or {},
            ))
            if end == len(words):
                break
            start = end - self.chunk_overlap
            idx += 1

        return chunks

    def chunk_by_paragraph(self, text: str, doc_id: str,
                            metadata: Dict = None) -> List[Chunk]:
        """Paragraph-aware chunking — better for structured docs."""
        paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
        chunks = []
        buffer = []
        buffer_words = 0
        idx = 0

        for para in paragraphs:
            para_words = len(para.split())
            if buffer_words + para_words > self.chunk_size and buffer:
                chunk_text = "\n\n".join(buffer)
                chunk_id = hashlib.sha256(
                    f"{doc_id}:{idx}:{chunk_text[:64]}".encode()
                ).hexdigest()[:16]
                chunks.append(Chunk(
                    id=chunk_id, doc_id=doc_id, text=chunk_text,
                    index=idx, metadata=metadata or {},
                ))
                # Keep last paragraph for overlap
                buffer = [buffer[-1]] if buffer else []
                buffer_words = len(buffer[0].split()) if buffer else 0
                idx += 1
            buffer.append(para)
            buffer_words += para_words

        if buffer:
            chunk_text = "\n\n".join(buffer)
            chunk_id = hashlib.sha256(
                f"{doc_id}:{idx}:{chunk_text[:64]}".encode()
            ).hexdigest()[:16]
            chunks.append(Chunk(
                id=chunk_id, doc_id=doc_id, text=chunk_text,
                index=idx, metadata=metadata or {},
            ))

        return chunks if chunks else self.chunk_text(text, doc_id, metadata)


# ══════════════════════════════════════════════════════════════════════════════
# DOCUMENT PARSERS
# ══════════════════════════════════════════════════════════════════════════════

class DocumentParser:
    """Parses different file formats into raw text."""

    def parse_file(self, file_path: str) -> Tuple[str, str, Dict]:
        """Returns (text, doc_type, metadata)."""
        path = Path(file_path)
        suffix = path.suffix.lower().lstrip(".")
        meta = {"filename": path.name, "size": path.stat().st_size}

        if suffix in ("txt", "md", "py", "js", "ts", "html", "css", "sh", "yaml", "yml", "toml"):
            return path.read_text(errors="replace"), suffix or "txt", meta

        elif suffix == "json":
            data = json.loads(path.read_text())
            text = json.dumps(data, indent=2) if isinstance(data, (dict, list)) else str(data)
            return text, "json", meta

        elif suffix == "csv":
            rows = []
            with open(file_path, newline="", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    rows.append(" | ".join(f"{k}: {v}" for k, v in row.items()))
            return "\n".join(rows), "csv", {**meta, "rows": len(rows)}

        else:
            # Try reading as text regardless
            try:
                return path.read_text(errors="replace"), "raw", meta
            except Exception:
                raise ValueError(f"Unsupported file format: {suffix}")

    def parse_raw(self, text: str, title: str = "untitled") -> Tuple[str, str, Dict]:
        return text, "raw", {"title": title}


# ══════════════════════════════════════════════════════════════════════════════
# VECTOR STORE (SQLite-backed)
# ══════════════════════════════════════════════════════════════════════════════

class VectorStore:
    """
    SQLite-backed vector store with cosine similarity search.
    Embeddings stored as JSON-serialized float arrays.

    For production: replace _similarity_search() with a call to
    pgvector, Qdrant, or Chroma while keeping the same public API.
    """

    def __init__(self, db_path: str = "data/rag.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS documents (
                    id          TEXT PRIMARY KEY,
                    title       TEXT NOT NULL,
                    source      TEXT,
                    doc_type    TEXT,
                    total_chunks INTEGER DEFAULT 0,
                    metadata    TEXT DEFAULT '{}',
                    created_at  REAL DEFAULT (unixepoch('now','subsec'))
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id          TEXT PRIMARY KEY,
                    doc_id      TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    text        TEXT NOT NULL,
                    idx         INTEGER,
                    metadata    TEXT DEFAULT '{}',
                    embedding   TEXT,          -- JSON float array
                    created_at  REAL DEFAULT (unixepoch('now','subsec'))
                );
                CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
            """)

    # ── Documents ─────────────────────────────────────────────────────────────

    def save_document(self, doc: Document):
        with self._conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO documents
                (id, title, source, doc_type, total_chunks, metadata, created_at)
                VALUES (?,?,?,?,?,?,?)
            """, (doc.id, doc.title, doc.source, doc.doc_type,
                  doc.total_chunks, json.dumps(doc.metadata), doc.created_at))

    def get_document(self, doc_id: str) -> Optional[Document]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE id=?", (doc_id,)
            ).fetchone()
        if not row:
            return None
        return Document(
            id=row["id"], title=row["title"], source=row["source"],
            doc_type=row["doc_type"], total_chunks=row["total_chunks"],
            metadata=json.loads(row["metadata"]), created_at=row["created_at"],
        )

    def list_documents(self) -> List[Document]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM documents ORDER BY created_at DESC"
            ).fetchall()
        return [Document(
            id=r["id"], title=r["title"], source=r["source"],
            doc_type=r["doc_type"], total_chunks=r["total_chunks"],
            metadata=json.loads(r["metadata"]), created_at=r["created_at"],
        ) for r in rows]

    def delete_document(self, doc_id: str) -> bool:
        with self._conn() as conn:
            conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
            conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        return True

    # ── Chunks ────────────────────────────────────────────────────────────────

    def save_chunks(self, chunks: List[Chunk]):
        with self._conn() as conn:
            conn.executemany("""
                INSERT OR REPLACE INTO chunks (id, doc_id, text, idx, metadata, embedding)
                VALUES (?,?,?,?,?,?)
            """, [
                (c.id, c.doc_id, c.text, c.index,
                 json.dumps(c.metadata),
                 json.dumps(c.embedding) if c.embedding else None)
                for c in chunks
            ])

    def get_chunks(self, doc_id: str) -> List[Chunk]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM chunks WHERE doc_id=? ORDER BY idx", (doc_id,)
            ).fetchall()
        return [self._row_to_chunk(r) for r in rows]

    def _row_to_chunk(self, row) -> Chunk:
        return Chunk(
            id=row["id"], doc_id=row["doc_id"], text=row["text"],
            index=row["idx"], metadata=json.loads(row["metadata"] or "{}"),
            embedding=json.loads(row["embedding"]) if row["embedding"] else [],
        )

    # ── Similarity Search ─────────────────────────────────────────────────────

    @staticmethod
    def _cosine(a: List[float], b: List[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def similarity_search(self, query_embedding: List[float],
                          top_k: int = 5,
                          doc_id: str = None,
                          min_score: float = 0.0) -> List[RetrievalResult]:
        """Brute-force cosine similarity over all embedded chunks."""
        with self._conn() as conn:
            if doc_id:
                rows = conn.execute(
                    "SELECT * FROM chunks WHERE doc_id=? AND embedding IS NOT NULL",
                    (doc_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM chunks WHERE embedding IS NOT NULL"
                ).fetchall()

        scored = []
        for row in rows:
            emb = json.loads(row["embedding"])
            score = self._cosine(query_embedding, emb)
            if score >= min_score:
                chunk = self._row_to_chunk(row)
                scored.append((score, chunk))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            RetrievalResult(chunk=chunk, score=score, rank=i + 1)
            for i, (score, chunk) in enumerate(scored[:top_k])
        ]

    def keyword_search(self, query: str, top_k: int = 5,
                       doc_id: str = None) -> List[RetrievalResult]:
        """Fallback keyword search when no embeddings are available."""
        terms = query.lower().split()
        sql = "SELECT * FROM chunks WHERE " + " AND ".join(
            ["LOWER(text) LIKE ?" for _ in terms]
        )
        params = [f"%{t}%" for t in terms]
        if doc_id:
            sql += " AND doc_id=?"
            params.append(doc_id)
        sql += f" LIMIT {top_k}"

        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()

        return [
            RetrievalResult(chunk=self._row_to_chunk(r), score=0.5, rank=i + 1)
            for i, r in enumerate(rows)
        ]

    def stats(self) -> Dict:
        with self._conn() as conn:
            docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            embedded = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL"
            ).fetchone()[0]
        return {"documents": docs, "chunks": chunks, "embedded_chunks": embedded}


# ══════════════════════════════════════════════════════════════════════════════
# RAG PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

class RAGPipeline:
    """
    End-to-end RAG pipeline:
      ingest() → chunk → embed → store
      retrieve() → top-k similar chunks
      generate_context() → formatted context string for LLM prompt
      augment_prompt() → original prompt + retrieved context
    """

    def __init__(self, vector_store: VectorStore = None,
                 embed_fn=None,
                 chunk_size: int = 512, chunk_overlap: int = 64,
                 use_paragraphs: bool = True):
        self.store = vector_store or VectorStore()
        self.embed_fn = embed_fn          # async fn(text) -> List[float]
        self.chunker = TextChunker(chunk_size, chunk_overlap)
        self.parser = DocumentParser()
        self.use_paragraphs = use_paragraphs

    # ── Ingestion ─────────────────────────────────────────────────────────────

    async def ingest_file(self, file_path: str,
                          title: str = None) -> Document:
        """Ingest a file: parse → chunk → embed → store."""
        text, doc_type, meta = self.parser.parse_file(file_path)
        title = title or Path(file_path).name
        return await self._ingest(text=text, title=title, source=file_path,
                                  doc_type=doc_type, metadata=meta)

    async def ingest_text(self, text: str, title: str = "inline",
                          source: str = "direct", metadata: Dict = None) -> Document:
        """Ingest a raw string."""
        return await self._ingest(text=text, title=title, source=source,
                                  doc_type="raw", metadata=metadata or {})

    async def ingest_directory(self, dir_path: str,
                               extensions: List[str] = None) -> List[Document]:
        """Recursively ingest all matching files in a directory."""
        allowed = set(extensions or ["txt", "md", "py", "json", "csv"])
        docs = []
        for path in Path(dir_path).rglob("*"):
            if path.is_file() and path.suffix.lstrip(".").lower() in allowed:
                try:
                    doc = await self.ingest_file(str(path))
                    docs.append(doc)
                    logger.info(f"RAG ingested: {path.name} ({doc.total_chunks} chunks)")
                except Exception as e:
                    logger.warning(f"RAG skip {path}: {e}")
        return docs

    async def _ingest(self, text: str, title: str, source: str,
                      doc_type: str, metadata: Dict) -> Document:
        doc_id = hashlib.sha256(
            f"{source}:{title}:{len(text)}".encode()
        ).hexdigest()[:16]

        # Chunk
        if self.use_paragraphs:
            chunks = self.chunker.chunk_by_paragraph(text, doc_id, metadata)
        else:
            chunks = self.chunker.chunk_text(text, doc_id, metadata)

        # Embed
        if self.embed_fn:
            for chunk in chunks:
                try:
                    chunk.embedding = await self.embed_fn(chunk.text)
                except Exception as e:
                    logger.warning(f"Embed failed for chunk {chunk.id}: {e}")

        # Store
        doc = Document(
            id=doc_id, title=title, source=source,
            doc_type=doc_type, total_chunks=len(chunks), metadata=metadata,
        )
        self.store.save_document(doc)
        self.store.save_chunks(chunks)
        logger.info(f"RAG: ingested '{title}' → {len(chunks)} chunks")
        return doc

    # ── Retrieval ─────────────────────────────────────────────────────────────

    async def retrieve(self, query: str, top_k: int = 5,
                       doc_id: str = None,
                       min_score: float = 0.1) -> List[RetrievalResult]:
        """Retrieve top-k relevant chunks for a query."""
        if self.embed_fn:
            try:
                q_emb = await self.embed_fn(query)
                results = self.store.similarity_search(
                    q_emb, top_k=top_k, doc_id=doc_id, min_score=min_score
                )
                if results:
                    return results
            except Exception as e:
                logger.warning(f"Embedding retrieval failed: {e}, falling back to keyword")

        # Fallback to keyword search
        return self.store.keyword_search(query, top_k=top_k, doc_id=doc_id)

    def generate_context(self, results: List[RetrievalResult],
                         max_chars: int = 3000) -> str:
        """Format retrieval results into a context block for the LLM."""
        if not results:
            return ""

        lines = ["## Retrieved Context\n"]
        total = 0
        for r in results:
            header = (f"[Source: {r.chunk.metadata.get('filename', r.chunk.doc_id)} "
                     f"| Chunk {r.chunk.index} | Score: {r.score:.2f}]")
            block = f"{header}\n{r.chunk.text}\n"
            if total + len(block) > max_chars:
                break
            lines.append(block)
            total += len(block)

        return "\n".join(lines)

    async def augment_prompt(self, user_query: str, top_k: int = 5,
                             doc_id: str = None) -> Tuple[str, List[RetrievalResult]]:
        """
        Retrieve relevant context and build an augmented prompt.
        Returns (augmented_prompt, results).
        """
        results = await self.retrieve(user_query, top_k=top_k, doc_id=doc_id)
        context = self.generate_context(results)

        if not context:
            return user_query, results

        augmented = (
            f"{context}\n\n"
            f"---\nUsing the context above, answer the following:\n\n"
            f"{user_query}"
        )
        return augmented, results

    # ── Management ────────────────────────────────────────────────────────────

    def list_documents(self) -> List[Dict]:
        return [d.to_dict() for d in self.store.list_documents()]

    def delete_document(self, doc_id: str) -> bool:
        return self.store.delete_document(doc_id)

    def stats(self) -> Dict:
        return self.store.stats()
