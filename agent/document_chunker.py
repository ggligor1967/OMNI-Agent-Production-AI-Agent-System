"""OMNI Agent — Document Chunker: production chunking pipeline with metadata and structure."""
from __future__ import annotations
import hashlib, re, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class DocumentType(str, Enum):
    TEXT     = "text"
    MARKDOWN = "markdown"
    HTML     = "html"
    CODE     = "code"
    PDF_TEXT = "pdf_text"     # pre-extracted PDF text
    CSV      = "csv"
    JSON_DOC = "json_doc"
    CHAT     = "chat"         # conversation transcript


class OverlapStrategy(str, Enum):
    NONE     = "none"
    WORDS    = "words"
    SENTENCES = "sentences"


@dataclass
class ChunkMeta:
    chunk_id: str
    doc_id: str
    doc_type: DocumentType
    content: str
    index: int
    start_char: int
    end_char: int
    heading: str = ""           # nearest heading above chunk
    section: str = ""           # section identifier
    page: Optional[int] = None
    language: str = "en"
    token_count: int = 0
    word_count: int = 0
    char_count: int = 0
    content_hash: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "doc_type": self.doc_type.value,
            "index": self.index,
            "heading": self.heading,
            "section": self.section,
            "word_count": self.word_count,
            "token_count": self.token_count,
            "start_char": self.start_char,
            "end_char": self.end_char,
            "preview": self.content[:80] + "…" if len(self.content) > 80 else self.content,
        }


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return max(1, len(text) // 4)


def _extract_headings(text: str) -> List[Tuple[int, str]]:
    """Return (position, heading_text) for markdown headings."""
    headings = []
    for m in re.finditer(r'^#{1,6}\s+(.+)$', text, re.MULTILINE):
        headings.append((m.start(), m.group(1).strip()))
    return headings


def _nearest_heading(pos: int, headings: List[Tuple[int, str]]) -> str:
    best = ""
    for h_pos, h_text in headings:
        if h_pos <= pos:
            best = h_text
        else:
            break
    return best


class DocumentChunker:
    """
    Production document chunking pipeline supporting:
    - Multiple document types with type-aware splitting
    - Heading/section metadata extraction
    - Overlap strategies
    - Token budget enforcement
    - Post-processing hooks
    - Deduplication
    """

    def __init__(
        self,
        max_tokens: int = 512,
        overlap_strategy: OverlapStrategy = OverlapStrategy.SENTENCES,
        overlap_size: int = 1,        # sentences or words
        min_chunk_tokens: int = 10,
        dedup: bool = True,
    ):
        self.max_tokens       = max_tokens
        self.overlap_strategy = overlap_strategy
        self.overlap_size     = overlap_size
        self.min_chunk_tokens = min_chunk_tokens
        self.dedup            = dedup
        self._seen_hashes: set = set()
        self._post_hooks: List[Callable[[ChunkMeta], Optional[ChunkMeta]]] = []
        self._chunk_count = 0
        self._doc_count   = 0

    # ── PUBLIC API ────────────────────────────────────────────────────

    def chunk(self, text: str,
              doc_type: DocumentType = DocumentType.TEXT,
              doc_id: Optional[str] = None,
              metadata: Optional[Dict] = None,
              page: Optional[int] = None,
              language: str = "en") -> List[ChunkMeta]:
        doc_id = doc_id or str(uuid.uuid4())[:12]
        meta   = metadata or {}
        self._doc_count += 1

        if doc_type == DocumentType.MARKDOWN:
            raw_chunks = self._chunk_markdown(text)
        elif doc_type == DocumentType.HTML:
            raw_chunks = self._chunk_html(text)
        elif doc_type == DocumentType.CODE:
            raw_chunks = self._chunk_code(text)
        elif doc_type == DocumentType.CSV:
            raw_chunks = self._chunk_csv(text)
        elif doc_type == DocumentType.CHAT:
            raw_chunks = self._chunk_chat(text)
        else:
            raw_chunks = self._chunk_text(text)

        headings = _extract_headings(text) if doc_type == DocumentType.MARKDOWN else []
        result: List[ChunkMeta] = []

        for i, (content, start, end, section) in enumerate(raw_chunks):
            content = content.strip()
            if not content:
                continue
            tc = _estimate_tokens(content)
            if tc < self.min_chunk_tokens:
                continue
            ch = hashlib.md5(  # nosec B324 - content deduplication key only
                content.encode(), usedforsecurity=False
            ).hexdigest()
            if self.dedup and ch in self._seen_hashes:
                continue
            if self.dedup:
                self._seen_hashes.add(ch)
            heading = _nearest_heading(start, headings)
            chunk = ChunkMeta(
                chunk_id=str(uuid.uuid4()),
                doc_id=doc_id,
                doc_type=doc_type,
                content=content,
                index=i,
                start_char=start,
                end_char=end,
                heading=heading,
                section=section,
                page=page,
                language=language,
                token_count=tc,
                word_count=len(content.split()),
                char_count=len(content),
                content_hash=ch,
                metadata=dict(meta),
            )
            # Post-process hooks
            for hook in self._post_hooks:
                try:
                    chunk = hook(chunk) or chunk
                except Exception:
                    pass
            result.append(chunk)
            self._chunk_count += 1

        return result

    # ── CHUNKING STRATEGIES ───────────────────────────────────────────

    def _chunk_text(self, text: str) -> List[Tuple[str, int, int, str]]:
        """Generic text chunking by sentences with token budget."""
        sentences = re.split(r'(?<=[.!?])\s+', text)
        return self._pack_sentences(sentences, text)

    def _pack_sentences(self, sentences: List[str],
                         full_text: str) -> List[Tuple[str, int, int, str]]:
        chunks: List[Tuple[str, int, int, str]] = []
        buf: List[str] = []
        buf_tokens = 0
        pos = 0

        for sent in sentences:
            st = _estimate_tokens(sent)
            if buf_tokens + st > self.max_tokens and buf:
                content = " ".join(buf)
                start   = full_text.find(buf[0], pos)
                start   = start if start >= 0 else pos
                end     = start + len(content)
                chunks.append((content, start, end, ""))
                # Overlap
                if self.overlap_strategy == OverlapStrategy.SENTENCES:
                    buf = buf[-self.overlap_size:] if self.overlap_size else []
                    buf_tokens = sum(_estimate_tokens(s) for s in buf)
                else:
                    buf, buf_tokens = [], 0
                pos = end
            buf.append(sent)
            buf_tokens += st

        if buf:
            content = " ".join(buf)
            start   = full_text.find(buf[0], pos)
            start   = start if start >= 0 else pos
            chunks.append((content, start, start + len(content), ""))
        return chunks or [(full_text, 0, len(full_text), "")]

    def _chunk_markdown(self, text: str) -> List[Tuple[str, int, int, str]]:
        """Split at heading boundaries, then sub-chunk oversized sections."""
        sections = re.split(r'(?=^#{1,6}\s)', text, flags=re.MULTILINE)
        chunks: List[Tuple[str, int, int, str]] = []
        pos = 0
        for section in sections:
            if not section.strip():
                pos += len(section)
                continue
            heading_match = re.match(r'^(#{1,6})\s+(.+)', section)
            section_name = heading_match.group(2) if heading_match else ""
            tc = _estimate_tokens(section)
            if tc <= self.max_tokens:
                chunks.append((section, pos, pos + len(section), section_name))
            else:
                sub = self._chunk_text(section)
                for content, s, e, _ in sub:
                    chunks.append((content, pos + s, pos + e, section_name))
            pos += len(section)
        return chunks or self._chunk_text(text)

    def _chunk_html(self, text: str) -> List[Tuple[str, int, int, str]]:
        """Strip tags, then chunk as text."""
        clean = re.sub(r'<[^>]+>', ' ', text)
        clean = re.sub(r'\s+', ' ', clean).strip()
        return self._chunk_text(clean)

    def _chunk_code(self, text: str) -> List[Tuple[str, int, int, str]]:
        """Split at function/class boundaries or by line count."""
        boundaries = [0]
        for m in re.finditer(
                r'^(?:def |class |function |fn |public |private |async def )',
                text, re.MULTILINE):
            if m.start() > 0:
                boundaries.append(m.start())
        boundaries.append(len(text))
        chunks: List[Tuple[str, int, int, str]] = []
        for i in range(len(boundaries) - 1):
            s, e = boundaries[i], boundaries[i + 1]
            section = text[s:e]
            tc = _estimate_tokens(section)
            if tc <= self.max_tokens:
                chunks.append((section, s, e, ""))
            else:
                # Split by lines
                lines = section.splitlines(keepends=True)
                buf, buf_tc, buf_start = [], 0, s
                for line in lines:
                    lt = _estimate_tokens(line)
                    if buf_tc + lt > self.max_tokens and buf:
                        content = "".join(buf)
                        chunks.append((content, buf_start, buf_start + len(content), ""))
                        buf_start += len(content)
                        buf, buf_tc = [], 0
                    buf.append(line); buf_tc += lt
                if buf:
                    content = "".join(buf)
                    chunks.append((content, buf_start, buf_start + len(content), ""))
        return chunks or [(text, 0, len(text), "")]

    def _chunk_csv(self, text: str) -> List[Tuple[str, int, int, str]]:
        """Chunk by row groups, keeping header in each chunk."""
        lines = text.splitlines(keepends=True)
        if not lines:
            return [(text, 0, len(text), "")]
        header    = lines[0]
        data_rows = lines[1:]
        chunks: List[Tuple[str, int, int, str]] = []
        buf = [header]
        buf_tc = _estimate_tokens(header)
        pos = len(header)
        for row in data_rows:
            rt = _estimate_tokens(row)
            if buf_tc + rt > self.max_tokens and len(buf) > 1:
                content = "".join(buf)
                chunks.append((content, pos - len(content), pos, ""))
                buf    = [header]
                buf_tc = _estimate_tokens(header)
            buf.append(row)
            buf_tc += rt
            pos += len(row)
        if len(buf) > 1:
            content = "".join(buf)
            chunks.append((content, pos - len(content), pos, ""))
        return chunks or [(text, 0, len(text), "")]

    def _chunk_chat(self, text: str) -> List[Tuple[str, int, int, str]]:
        """Split at speaker turns."""
        turns = re.split(r'(?=^(?:User|Assistant|Human|AI|System)\s*:)',
                         text, flags=re.MULTILINE | re.IGNORECASE)
        return self._pack_sentences(turns, text)

    # ── HOOKS ─────────────────────────────────────────────────────────

    def add_post_hook(self, fn: Callable[[ChunkMeta], Optional[ChunkMeta]]):
        self._post_hooks.append(fn)

    def reset_dedup(self):
        self._seen_hashes.clear()

    # ── STATS ─────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        return {
            "docs_processed": self._doc_count,
            "chunks_produced": self._chunk_count,
            "dedup_enabled": self.dedup,
            "max_tokens": self.max_tokens,
            "overlap_strategy": self.overlap_strategy.value,
        }
