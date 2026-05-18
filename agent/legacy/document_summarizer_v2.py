"""OMNI Agent — Document Summarizer V2: multi-strategy summarization with chunking."""
from __future__ import annotations
import json, re, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class SummaryStrategy(str, Enum):
    EXTRACTIVE  = "extractive"   # pick important sentences
    ABSTRACTIVE = "abstractive"  # rewrite via LLM
    HYBRID      = "hybrid"       # extract then compress
    HIERARCHICAL = "hierarchical" # chunk → summarize each → combine
    BULLET      = "bullet"       # key points list
    HEADLINE    = "headline"     # single sentence


class ChunkStrategy(str, Enum):
    SENTENCE    = "sentence"
    PARAGRAPH   = "paragraph"
    FIXED       = "fixed"        # fixed token count
    SEMANTIC    = "semantic"     # by topic boundary


@dataclass
class SummaryConfig:
    strategy: SummaryStrategy = SummaryStrategy.EXTRACTIVE
    max_sentences: int = 5
    max_tokens: int = 200
    chunk_strategy: ChunkStrategy = ChunkStrategy.PARAGRAPH
    chunk_size: int = 500         # tokens per chunk
    chunk_overlap: int = 50
    language: str = "en"
    preserve_order: bool = True
    include_metadata: bool = False


@dataclass
class SummaryResult:
    result_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    source_length: int = 0
    summary: str = ""
    bullet_points: List[str] = field(default_factory=list)
    key_sentences: List[str] = field(default_factory=list)
    compression_ratio: float = 0.0
    strategy: str = ""
    chunk_count: int = 0
    duration_ms: float = 0.0
    ts: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "result_id": self.result_id,
            "source_length": self.source_length,
            "summary_length": len(self.summary),
            "compression_ratio": round(self.compression_ratio, 3),
            "strategy": self.strategy,
            "chunk_count": self.chunk_count,
            "duration_ms": round(self.duration_ms, 2),
        }


def _sent_tokenize(text: str) -> List[str]:
    """Simple sentence tokenizer."""
    sents = re.split(r'(?<=[.!?])\s+', text.strip())
    return [s.strip() for s in sents if s.strip()]


def _word_count(text: str) -> int:
    return len(text.split())


def _score_sentence(sent: str, word_freq: Dict[str, int],
                     position: int, total: int) -> float:
    """Score sentence by word frequency + position bias."""
    words = re.findall(r'\b\w+\b', sent.lower())
    freq_score = sum(word_freq.get(w, 0) for w in words) / max(len(words), 1)
    pos_score  = 1.0 if position == 0 else (0.5 if position == total - 1 else 0.3)
    return freq_score + pos_score


class DocumentSummarizerV2:
    """
    Multi-strategy document summarization:
    - Extractive: TF-IDF-style sentence scoring + top-K selection
    - Abstractive: delegates to pluggable LLM fn
    - Hierarchical: chunk → summarize chunk → combine
    - Bullet points: extract key fact sentences
    - Headline: single most representative sentence
    - Configurable chunking (sentence/paragraph/fixed)
    - Multi-document summarization (merge + summarize)
    - Compression ratio tracking
    - Named summary templates
    - Summary cache (dedup by content hash)
    - SQLite persistence
    """

    def __init__(self, llm_fn: Optional[Callable[[str], str]] = None,
                 db_path: str = ":memory:"):
        self._llm_fn   = llm_fn
        self._cache:   Dict[str, SummaryResult] = {}
        self._history: List[SummaryResult] = []
        self._templates: Dict[str, SummaryConfig] = {}
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS ds_summaries (
                result_id TEXT PRIMARY KEY, source_length INTEGER,
                summary_length INTEGER, compression_ratio REAL,
                strategy TEXT, duration_ms REAL, ts REAL
            );
        """)
        self._db.commit()

    # ── CHUNKING ─────────────────────────────────────────────────────

    def _chunk_by_sentence(self, text: str, size: int,
                             overlap: int) -> List[str]:
        sents  = _sent_tokenize(text)
        chunks = []; i = 0
        while i < len(sents):
            chunk = sents[i:i + size]
            chunks.append(" ".join(chunk))
            i += max(1, size - overlap)
        return chunks

    def _chunk_by_paragraph(self, text: str) -> List[str]:
        paras = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
        return paras if paras else [text]

    def _chunk_fixed(self, text: str, size: int, overlap: int) -> List[str]:
        words  = text.split()
        chunks = []
        i      = 0
        while i < len(words):
            chunk = words[i:i + size]
            chunks.append(" ".join(chunk))
            i += max(1, size - overlap)
        return chunks

    def _make_chunks(self, text: str, cfg: SummaryConfig) -> List[str]:
        if cfg.chunk_strategy == ChunkStrategy.PARAGRAPH:
            return self._chunk_by_paragraph(text)
        elif cfg.chunk_strategy == ChunkStrategy.SENTENCE:
            return self._chunk_by_sentence(text, cfg.chunk_size, cfg.chunk_overlap)
        else:
            return self._chunk_fixed(text, cfg.chunk_size, cfg.chunk_overlap)

    # ── STRATEGIES ───────────────────────────────────────────────────

    def _extractive(self, text: str, cfg: SummaryConfig) -> SummaryResult:
        sents = _sent_tokenize(text)
        if not sents:
            return SummaryResult(summary=text, strategy="extractive")

        words = re.findall(r'\b\w+\b', text.lower())
        stop  = {"the", "a", "an", "is", "in", "of", "and", "to",
                  "that", "it", "was", "for", "on", "are", "as", "with"}
        freq: Dict[str, int] = {}
        for w in words:
            if w not in stop and len(w) > 2:
                freq[w] = freq.get(w, 0) + 1

        scored = [(s, _score_sentence(s, freq, i, len(sents)))
                  for i, s in enumerate(sents)]
        scored.sort(key=lambda x: -x[1])
        top = [s for s, _ in scored[:cfg.max_sentences]]
        if cfg.preserve_order:
            top = [s for s in sents if s in set(top)]

        summary = " ".join(top)
        return SummaryResult(
            source_length=_word_count(text),
            summary=summary,
            key_sentences=top,
            strategy="extractive",
            compression_ratio=len(summary) / max(len(text), 1))

    def _headline(self, text: str, cfg: SummaryConfig) -> SummaryResult:
        sents = _sent_tokenize(text)
        if not sents:
            return SummaryResult(summary="", strategy="headline")
        words = re.findall(r'\b\w+\b', text.lower())
        freq: Dict[str, int] = {}
        for w in words: freq[w] = freq.get(w, 0) + 1
        best = max(sents,
                   key=lambda s: _score_sentence(s, freq, 0, len(sents)))
        return SummaryResult(
            source_length=_word_count(text),
            summary=best, strategy="headline",
            compression_ratio=len(best) / max(len(text), 1))

    def _bullet(self, text: str, cfg: SummaryConfig) -> SummaryResult:
        r = self._extractive(text, cfg)
        bullets = [f"• {s}" for s in r.key_sentences]
        r.bullet_points = r.key_sentences
        r.summary       = "\n".join(bullets)
        r.strategy      = "bullet"
        return r

    def _abstractive(self, text: str, cfg: SummaryConfig) -> SummaryResult:
        if not self._llm_fn:
            return self._extractive(text, cfg)
        prompt = (f"Summarize the following text in at most "
                  f"{cfg.max_tokens} words:\n\n{text}")
        try:
            summary = self._llm_fn(prompt)
        except Exception as e:
            summary = f"[LLM error: {e}]"
        return SummaryResult(
            source_length=_word_count(text),
            summary=summary, strategy="abstractive",
            compression_ratio=len(summary) / max(len(text), 1))

    def _hierarchical(self, text: str, cfg: SummaryConfig) -> SummaryResult:
        chunks = self._make_chunks(text, cfg)
        chunk_summaries = []
        for chunk in chunks:
            r = self._extractive(chunk, cfg)
            chunk_summaries.append(r.summary)
        combined = " ".join(chunk_summaries)
        # Second pass on combined
        r2 = self._extractive(combined, cfg)
        r2.chunk_count      = len(chunks)
        r2.source_length    = _word_count(text)
        r2.strategy         = "hierarchical"
        r2.compression_ratio = len(r2.summary) / max(len(text), 1)
        return r2

    # ── PUBLIC API ───────────────────────────────────────────────────

    def summarize(self, text: str,
                   config: Optional[SummaryConfig] = None,
                   use_cache: bool = True) -> SummaryResult:
        import hashlib
        cfg = config or SummaryConfig()
        if use_cache:
            cache_key = hashlib.md5(
                (text + cfg.strategy.value).encode()).hexdigest()
            if cache_key in self._cache:
                return self._cache[cache_key]
        t0  = time.time()
        if cfg.strategy == SummaryStrategy.EXTRACTIVE:
            res = self._extractive(text, cfg)
        elif cfg.strategy == SummaryStrategy.HEADLINE:
            res = self._headline(text, cfg)
        elif cfg.strategy == SummaryStrategy.BULLET:
            res = self._bullet(text, cfg)
        elif cfg.strategy == SummaryStrategy.ABSTRACTIVE:
            res = self._abstractive(text, cfg)
        elif cfg.strategy == SummaryStrategy.HIERARCHICAL:
            res = self._hierarchical(text, cfg)
        else:
            res = self._extractive(text, cfg)

        res.duration_ms = (time.time() - t0) * 1000
        if not res.source_length:
            res.source_length = _word_count(text)

        self._history.append(res)
        if use_cache:
            self._cache[cache_key] = res  # noqa

        self._db.execute(
            "INSERT OR REPLACE INTO ds_summaries VALUES (?,?,?,?,?,?,?)",
            (res.result_id, res.source_length, len(res.summary),
             res.compression_ratio, res.strategy,
             res.duration_ms, res.ts))
        self._db.commit()
        return res

    def summarize_multi(self, texts: List[str],
                         config: Optional[SummaryConfig] = None) -> SummaryResult:
        """Summarize multiple documents into one summary."""
        combined = "\n\n".join(texts)
        return self.summarize(combined, config)

    def register_template(self, name: str, config: SummaryConfig):
        self._templates[name] = config

    def summarize_with_template(self, text: str,
                                  template_name: str) -> SummaryResult:
        cfg = self._templates.get(template_name)
        if not cfg:
            raise KeyError(f"Template '{template_name}' not found")
        return self.summarize(text, cfg)

    def history(self, limit: int = 20) -> List[Dict]:
        return [r.to_dict() for r in self._history[-limit:]]

    def stats(self) -> Dict[str, Any]:
        if not self._history: return {"runs": 0}
        avg_ratio = sum(r.compression_ratio for r in self._history) / len(self._history)
        return {
            "runs": len(self._history),
            "cache_size": len(self._cache),
            "avg_compression_ratio": round(avg_ratio, 3),
            "templates": list(self._templates.keys()),
        }
